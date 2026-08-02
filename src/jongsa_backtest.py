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


def _metrics(equity: pd.Series, initial_cash: float, n_days: int) -> tuple:
    years = n_days / TRADING_DAYS_PER_YEAR
    final = float(equity.iloc[-1])
    cagr = ((final / initial_cash) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else float("nan")
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return cagr, float(dd.min()) * 100, dd


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
    """
    close = df["Close"].astype(float).values
    dates = df.index
    n = len(close)
    if n < stop_days + 2:
        raise ValueError("데이터가 너무 짧습니다.")

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

    for t in range(n):
        price = close[t]
        sell_qty_today = 0.0
        sell_amt_today = 0.0
        sell_reasons: list[str] = []

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
                desired = season_seed if season_reseed else prev_total_assets * daily_buy_pct
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
        prev_total_assets = total

        log_rows.append(
            {
                "날짜": dates[t],
                "종가": round(price, 4),
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
                "수익률(%)": round((total / initial_cash - 1) * 100, 2),
            }
        )

    equity_s = pd.Series(equity, index=dates, name="equity")
    exposure_s = pd.Series(exposure, index=dates, name="exposure")
    trades_df = pd.DataFrame(trades)

    log_df = pd.DataFrame(log_rows)
    if not log_df.empty:
        run_max = log_df["총자산"].cummax()
        log_df["최고자산대비(%)"] = ((log_df["총자산"] / run_max - 1) * 100).round(2)

    cagr, mdd, dd = _metrics(equity_s, initial_cash, n)

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
    )


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
