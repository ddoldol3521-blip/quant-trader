"""종사종팔 V4 / V5 매매법 백테스트.

네이버 카페 '송송 자동매매'에 개인(승찬아빠)이 공유한 SOXL 대상 규칙 기반 분할매매 전략.
공식 검증 자료가 아니라 커뮤니티 게시글 기반이며, 여기 구현은 사용자가 정리해준
스펙 문서를 그대로 옮긴 것이다.

V4와 V5는 '오늘 매수금액을 어떻게 정하는가' 하나만 다르다.
매도 조건(9영업일 내 목표수익률 도달 시 익절 / 10영업일째 강제청산)과
'매도한 날은 매수하지 않는다'는 규칙은 동일하다.

V4의 복리 로직은 원문이 모호해서 여러 해석을 옵션으로 뒀다(V4_INTERPRETATIONS 참고).
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Lot:
    """매수 1건(분할매수 단위)."""

    buy_day_idx: int
    buy_price: float
    qty: float
    target_price: float


@dataclass
class JongsaResult:
    equity_curve: pd.Series
    exposure_curve: pd.Series  # SOXL 보유 비중
    trades: pd.DataFrame
    cagr_pct: float
    mdd_pct: float
    win_rate_pct: float
    avg_exposure_pct: float
    num_trades: int
    num_target_sells: int
    num_forced_sells: int
    daily_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    days_below_dd: dict = field(default_factory=dict)
    # 시뮬레이션이 끝난 시점에 남아 있는 보유분과 예수금.
    # 과거부터 돌린 결과를 실제 기록으로 이어받을 때 쓴다.
    final_lots: list = field(default_factory=list)
    final_cash: float = 0.0
    max_open_lots: int = 0
    avg_open_lots: float = 0.0
    cash_exhausted_days: int = 0
    final_value: float = 0.0
    # ----- 중간 입출금 관련 -----
    # 입출금이 있으면 '총자산 / 시드 - 1'은 수익률이 아니게 된다.
    # (돈을 더 넣어서 자산이 는 것과 벌어서 는 것을 구분할 수 없다)
    # 그래서 넣은 돈 합계(contributed)를 따로 들고, 성적 지표는 입출금 효과를
    # 제거한 시간가중수익률(TWR) 곡선에서 계산한다.
    twr_curve: pd.Series = field(default_factory=pd.Series)
    contributed_curve: pd.Series = field(default_factory=pd.Series)
    total_contributed: float = 0.0
    net_profit: float = 0.0
    net_return_pct: float = 0.0
    flow_notes: list = field(default_factory=list)


# 이보다 짧은 기간을 연 단위로 환산하면 숫자가 터무니없어진다.
# (2주 수익률 3%를 연으로 늘리면 100%가 넘는다) 그 경우 CAGR은 내주지 않는다.
MIN_DAYS_FOR_CAGR = 60


def _metrics(equity: pd.Series, initial_cash: float, n_days: int) -> tuple:
    years = n_days / TRADING_DAYS_PER_YEAR
    final = float(equity.iloc[-1])
    if n_days >= MIN_DAYS_FOR_CAGR and years > 0 and final > 0:
        cagr = ((final / initial_cash) ** (1 / years) - 1) * 100
    else:
        cagr = float("nan")
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return cagr, float(dd.min()) * 100, dd


def _resolve_flows(cash_flows, dates) -> tuple:
    """입출금 (날짜, 금액) 목록을 거래일 인덱스별 금액 배열로 바꾼다.

    주말·공휴일에 넣은 돈은 다음 거래일에 반영한다.
    시작일 이전이면 첫 거래일로, 마지막 거래일 이후면 무시하고 안내를 남긴다.
    """
    n = len(dates)
    flows = np.zeros(n)
    notes = []
    if not cash_flows:
        return flows, notes

    for item in cash_flows:
        try:
            d, amt = item[0], float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if amt == 0:
            continue
        ts = pd.Timestamp(d)
        idx = int(dates.searchsorted(ts, side="left"))
        if idx >= n:
            notes.append(f"{ts.date()} {amt:+,.0f} — 마지막 거래일 이후라 반영하지 않았습니다.")
            continue
        if dates[idx] != ts:
            notes.append(f"{ts.date()}는 거래일이 아니라 {dates[idx].date()}에 반영했습니다.")
        flows[idx] += amt
    return flows, notes


def run_jongsa(
    df: pd.DataFrame,
    version: str = "V5",
    initial_cash: float = 10_000.0,
    target_return: float = 0.0275,
    daily_buy_pct: float = 0.10,
    compound_ratio: float = 0.70,
    n_splits: int = 10,
    stop_days: int = 10,
    fee_rate: float = 0.0,
    whole_shares: bool = False,
    v4_mode: str = "rolling_replace",
    loc_buy_limit: bool = False,
    season_reseed: bool = False,
    fee_in_target: bool = False,
    sell_day_buy_mode: str = "never",
    reinvest: bool = True,
    cash_flows=None,
    dd_thresholds=(-0.20, -0.25, -0.30),
) -> JongsaResult:
    """종사종팔 백테스트.

    df: 'Close' 컬럼을 가진 일별 데이터 (거래일만)
    version: 'V4' 또는 'V5'
    v4_mode: V4 복리 로직 해석 방식
        'rolling_replace' — 매일 [t-20, t-10] 손익을 다시 계산해서 증액분을 갱신(대체)
        'rolling_accum'   — 위와 같되 증액분을 기존에 누적
        'block_10d'       — 10일마다 한 번씩만 갱신
    whole_shares: True면 정수주만 매수
    cash_flows: 중간 입출금 [(날짜, 금액), ...]. 양수는 입금, 음수는 출금.
        출금액이 예수금보다 크면 규칙을 어기고 강제 매도하지 않고,
        가능한 만큼만 빼고 나머지는 예수금이 생길 때까지 이월한다.
    """
    close = df["Close"].astype(float).values
    dates = df.index
    n = len(close)
    if n < 1:
        raise ValueError("해당 기간에 거래일이 없습니다.")

    cash = float(initial_cash)
    shares = 0.0
    open_lots: list[Lot] = []
    trades = []

    equity = np.zeros(n)
    exposure = np.zeros(n)
    open_lot_counts = np.zeros(n, dtype=int)
    log_rows = []  # 스프레드시트처럼 하루 한 줄씩 남긴다

    base_daily_amount = initial_cash / n_splits
    v4_daily_target = base_daily_amount
    season_seed = base_daily_amount
    realized_pnl = np.zeros(n)  # 그날 실현손익 (V4 복리 계산용)
    cash_exhausted = 0

    prev_total_assets = float(initial_cash)

    flows, flow_notes = _resolve_flows(cash_flows, dates)
    contributed = float(initial_cash)     # 지금까지 내가 넣은 돈 (출금하면 줄어든다)
    applied_flows = np.zeros(n)           # 실제로 반영된 금액 (출금은 예수금 한도)
    contributed_arr = np.zeros(n)
    pending_withdrawal = 0.0              # 예수금이 모자라 아직 못 뺀 출금액

    for t in range(n):
        price = close[t]
        sell_qty_today = 0.0
        sell_amt_today = 0.0
        sell_reasons: list[str] = []

        # ---------- 0) 입출금 ----------
        # 그날 매매를 판단하기 전에 먼저 반영한다. 입금한 돈은 그날 바로 쓸 수 있다.
        flow_today = 0.0
        if flows[t] > 0:
            cash += flows[t]
            contributed += flows[t]
            flow_today += flows[t]
        if flows[t] < 0:
            # 가진 돈보다 많이 빼겠다는 건 입력 실수다. 있는 만큼으로 자른다.
            want = -flows[t]
            have = cash + shares * price
            if want > have:
                flow_notes.append(
                    f"{dates[t].date()} 출금 ${want:,.0f} 요청 — 그 시점 총자산이 "
                    f"${have:,.0f}뿐이라 전액 출금으로 처리했습니다."
                )
                want = have
            pending_withdrawal += want
        if pending_withdrawal > 0:
            # 규칙을 어기면서까지 팔지는 않는다. 예수금 범위에서만 뺀다.
            take = min(pending_withdrawal, cash)
            if take > 0:
                cash -= take
                contributed -= take
                flow_today -= take
                pending_withdrawal -= take
            if pending_withdrawal > 1e-6 and flows[t] < 0:
                flow_notes.append(
                    f"{dates[t].date()} 출금 요청분 중 ${pending_withdrawal:,.0f}는 "
                    f"예수금이 모자라 미뤘습니다 (보유분을 강제로 팔지 않습니다)."
                )
        applied_flows[t] = flow_today

        # ---------- 1) 매도 판정 ----------
        did_sell = False
        sold_pnls: list[float] = []
        remaining: list[Lot] = []
        for lot in open_lots:
            held = t - lot.buy_day_idx
            sell_reason = None
            if 1 <= held <= stop_days - 1 and price >= lot.target_price:
                sell_reason = "목표달성"
            elif held >= stop_days:
                sell_reason = "강제손절"

            if sell_reason:
                proceeds = lot.qty * price * (1 - fee_rate)
                cost = lot.qty * lot.buy_price
                cash += proceeds
                shares -= lot.qty
                pnl = proceeds - cost
                realized_pnl[t] += pnl
                trades.append(
                    {
                        "매수일": dates[lot.buy_day_idx],
                        "매도일": dates[t],
                        "보유일": held,
                        "매수가": lot.buy_price,
                        "매도가": price,
                        "수량": lot.qty,
                        "손익": pnl,
                        "수익률(%)": (price / lot.buy_price - 1) * 100,
                        "청산사유": sell_reason,
                    }
                )
                did_sell = True
                sold_pnls.append(pnl)
                sell_qty_today += lot.qty
                sell_amt_today += proceeds
                sell_reasons.append(sell_reason)
            else:
                remaining.append(lot)
        open_lots = remaining

        # ---------- 2) 매수 판정 ----------
        # 기본은 '매도가 있는 날은 매수하지 않는다'(V4부터의 규칙).
        # 변형: 손절로 청산된 건은 목표 미달이니 재진입을 허용한다는 발상.
        #   any_loss — 오늘 매도분 중 손실이 하나라도 있으면 매수
        #   all_loss — 오늘 매도분이 전부 손실일 때만 매수
        if not did_sell:
            buy_today = True
        elif sell_day_buy_mode == "any_loss":
            buy_today = any(p < 0 for p in sold_pnls)
        elif sell_day_buy_mode == "all_loss":
            buy_today = all(p < 0 for p in sold_pnls)
        else:
            buy_today = False

        # LOC 지정가 매수: 주문가 = 전일종가 x (1+목표수익률).
        # 오늘 종가가 그보다 높으면(= 어제보다 많이 올랐으면) 체결되지 않는다.
        # 구글시트 사례에서 확인된 규칙으로, 원 스펙 문서에는 없었다.
        if buy_today and loc_buy_limit and t > 0:
            order_limit = close[t - 1] * (1 + target_return)
            if price > order_limit:
                buy_today = False

        # 시즌 리시드: 보유 물량이 전부 청산되면 다음 매수부터 시드를 다시 계산한다
        # (구글시트에서 '1회 시드'가 시즌마다 갱신되는 것을 반영)
        if season_reseed and not open_lots and shares <= 1e-9:
            season_seed = (cash + shares * price) / n_splits
        elif t == 0:
            season_seed = initial_cash / n_splits

        if buy_today:
            if version.upper() == "V5":
                if season_reseed:
                    desired = season_seed
                elif reinvest:
                    # 번 돈까지 굴린다 — 자산이 늘면 하루 매수금도 같이 늘어난다
                    desired = prev_total_assets * daily_buy_pct
                else:
                    # 재투자 안 함 — 하루 매수금을 '넣은 돈' 기준으로 고정.
                    # 중간에 입금하면 그만큼은 늘어나야 한다. 벌어들인 이익만 제외한다.
                    desired = contributed * daily_buy_pct
            else:  # V4
                if t > 0:
                    should_update = True
                    if v4_mode == "block_10d":
                        should_update = (t % stop_days) == 0
                    if should_update:
                        lo = max(0, t - 20)
                        hi = max(0, t - stop_days)
                        window_pnl = float(realized_pnl[lo:hi].sum()) if hi > lo else 0.0
                        if window_pnl > 0:
                            bump = window_pnl * compound_ratio
                            if v4_mode == "rolling_accum":
                                v4_daily_target = v4_daily_target + bump
                            else:
                                v4_daily_target = base_daily_amount + bump
                        # 손실이거나 0이면 직전 목표를 그대로 유지 (줄이지 않음)
                desired = season_seed if season_reseed else v4_daily_target

            buy_amount = min(desired, cash)
            buy_qty_today = 0.0
            buy_amt_today = 0.0
            buy_target_today = None
            if buy_amount > 0 and cash > 0:
                if buy_amount >= cash - 1e-9:
                    cash_exhausted += 1
                # 원본 스프레드시트 방식: 수량 = 예산*(1-수수료)/종가, 매수비용 = 종가*수량*(1+수수료)
                qty = buy_amount * (1 - fee_rate) / price
                if whole_shares:
                    qty = float(int(qty))
                if qty > 0:
                    spend = qty * price * (1 + fee_rate)
                    if spend > cash:  # 반올림으로 예수금을 넘지 않게
                        qty = cash / (price * (1 + fee_rate))
                        if whole_shares:
                            qty = float(int(qty))
                        spend = qty * price * (1 + fee_rate)
                    if qty > 0:
                        cash -= spend
                        shares += qty
                        # 시트는 목표가에 왕복 수수료를 얹어둔다
                        tgt = price * (1 + target_return)
                        if fee_in_target:
                            tgt *= 1 + 2 * fee_rate
                        open_lots.append(
                            Lot(buy_day_idx=t, buy_price=price, qty=qty, target_price=tgt)
                        )
                        buy_qty_today = qty
                        buy_amt_today = spend
                        buy_target_today = tgt
        else:
            buy_qty_today = 0.0
            buy_amt_today = 0.0
            buy_target_today = None

        # ---------- 3) 기록 ----------
        total = cash + shares * price
        equity[t] = total
        exposure[t] = (shares * price / total) if total > 0 else 0.0
        open_lot_counts[t] = len(open_lots)
        contributed_arr[t] = contributed
        prev_total_assets = total

        log_rows.append(
            {
                "날짜": dates[t],
                "종가": round(price, 4),
                "입출금": round(applied_flows[t], 2) if abs(applied_flows[t]) > 1e-9 else None,
                "매수금액": round(buy_amt_today, 2) if buy_amt_today else None,
                "매수수량": round(buy_qty_today) if buy_qty_today else None,
                "목표가": round(buy_target_today, 2) if buy_target_today else None,
                "매도수량": round(sell_qty_today) if sell_qty_today else None,
                "매도금액": round(sell_amt_today, 2) if sell_amt_today else None,
                "실현손익": round(float(realized_pnl[t]), 2) if sell_qty_today else None,
                "청산사유": "+".join(sorted(set(sell_reasons))) if sell_reasons else None,
                "보유수량": round(shares),
                "보유건수": len(open_lots),
                "평가금": round(shares * price, 2),
                "예수금": round(cash, 2),
                "총자산": round(total, 2),
                "넣은돈": round(contributed, 2),
                "순손익": round(total - contributed, 2),
                "수익률(%)": round((total / contributed - 1) * 100, 2) if contributed > 0 else None,
            }
        )

    equity_s = pd.Series(equity, index=dates, name="equity")
    exposure_s = pd.Series(exposure, index=dates, name="exposure")
    contributed_s = pd.Series(contributed_arr, index=dates, name="contributed")
    trades_df = pd.DataFrame(trades)

    # ----- 시간가중수익률(TWR) 곡선 -----
    # 입금하면 총자산이 껑충 뛴다. 그 점프를 그대로 두면 CAGR은 부풀고
    # MDD는 왜곡된다(입금일이 새 최고점이 되어 버린다). 그래서 매일의 수익률을
    # '그날 입금분을 제외한 시작 자산' 대비로 계산한 뒤 이어 붙인다.
    # 결과는 '입출금 없이 시드만 굴렸다면 어땠을까' 곡선이다.
    twr = np.zeros(n)
    idx_val = float(initial_cash)
    for t in range(n):
        base = (equity[t - 1] if t > 0 else float(initial_cash)) + applied_flows[t]
        if base > 1e-9:
            idx_val *= equity[t] / base
        twr[t] = idx_val
    twr_s = pd.Series(twr, index=dates, name="twr")

    log_df = pd.DataFrame(log_rows)
    if not log_df.empty:
        # 낙폭도 TWR 기준이어야 '입금해서 최고점 경신'이 안 생긴다
        run_max = twr_s.cummax().values
        log_df["최고자산대비(%)"] = ((twr_s.values / run_max - 1) * 100).round(2)

    cagr, mdd, dd = _metrics(twr_s, initial_cash, n)

    if not trades_df.empty:
        wins = int((trades_df["손익"] > 0).sum())
        win_rate = wins / len(trades_df) * 100
        n_target = int((trades_df["청산사유"] == "목표달성").sum())
        n_forced = int((trades_df["청산사유"] == "강제손절").sum())
    else:
        win_rate, n_target, n_forced = 0.0, 0, 0

    days_below = {f"DD<{int(th*100)}%": int((dd < th).sum()) for th in dd_thresholds}

    return JongsaResult(
        equity_curve=equity_s,
        exposure_curve=exposure_s,
        trades=trades_df,
        daily_log=log_df,
        cagr_pct=cagr,
        mdd_pct=mdd,
        win_rate_pct=win_rate,
        avg_exposure_pct=float(exposure_s.mean()) * 100,
        num_trades=len(trades_df),
        num_target_sells=n_target,
        num_forced_sells=n_forced,
        days_below_dd=days_below,
        final_lots=[
            {
                "buy_date": dates[l.buy_day_idx].strftime("%Y-%m-%d"),
                "buy_price": float(l.buy_price),
                "qty": float(l.qty),
                "target_price": float(l.target_price),
            }
            for l in open_lots
        ],
        final_cash=float(cash),
        max_open_lots=int(open_lot_counts.max()),
        avg_open_lots=float(open_lot_counts.mean()),
        cash_exhausted_days=cash_exhausted,
        final_value=float(equity_s.iloc[-1]),
        twr_curve=twr_s,
        contributed_curve=contributed_s,
        total_contributed=float(contributed),
        net_profit=float(equity_s.iloc[-1] - contributed),
        net_return_pct=float((equity_s.iloc[-1] / contributed - 1) * 100) if contributed > 0 else float("nan"),
        flow_notes=flow_notes,
    )


def run_buy_and_hold(
    df: pd.DataFrame,
    initial_cash: float = 10_000.0,
    fee_rate: float = 0.0,
    whole_shares: bool = True,
    cash_flows=None,
) -> pd.Series:
    """'존버' 비교용 — 첫날 전액 매수하고 그냥 들고 있는다.

    같은 조건에서 비교해야 의미가 있으므로 입출금도 똑같이 반영한다.
    입금하면 그날 종가로 더 사고, 출금하면 그날 종가로 그만큼 판다.
    """
    close = df["Close"].astype(float).values
    dates = df.index
    n = len(close)
    flows, _ = _resolve_flows(cash_flows, dates)

    cash = float(initial_cash)
    shares = 0.0
    values = np.zeros(n)

    for t in range(n):
        price = close[t]
        cash += flows[t] if flows[t] > 0 else 0.0

        if flows[t] < 0:  # 출금 — 예수금 먼저 쓰고 모자라면 판다
            need = -flows[t]
            take = min(cash, need)
            cash -= take
            need -= take
            if need > 1e-9 and shares > 0:
                sell_qty = min(shares, need / (price * (1 - fee_rate)))
                shares -= sell_qty
                cash += sell_qty * price * (1 - fee_rate) - need
                cash = max(cash, 0.0)

        if cash > 0:  # 남은 현금은 전부 주식으로 (존버니까)
            qty = cash * (1 - fee_rate) / price
            if whole_shares:
                qty = float(int(qty))
            if qty > 0:
                cash -= qty * price * (1 + fee_rate)
                shares += qty

        values[t] = cash + shares * price

    return pd.Series(values, index=dates, name="buy_and_hold")


def summarize(result: JongsaResult, label: str = "") -> dict:
    """결과를 한 줄 딕셔너리로."""
    out = {
        "버전": label,
        "CAGR(%)": round(result.cagr_pct, 2),
        "MDD(%)": round(result.mdd_pct, 2),
        "승률(%)": round(result.win_rate_pct, 1),
        "평균보유비중(%)": round(result.avg_exposure_pct, 1),
        "매매횟수": result.num_trades,
        "목표달성": result.num_target_sells,
        "강제손절": result.num_forced_sells,
        "평균분할수": round(result.avg_open_lots, 1),
        "최대분할수": result.max_open_lots,
        "예수금소진일": result.cash_exhausted_days,
        "최종자산": round(result.final_value),
    }
    out.update(result.days_below_dd)
    return out
