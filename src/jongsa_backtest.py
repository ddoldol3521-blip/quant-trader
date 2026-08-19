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

import math
import numpy as np
import pandas as pd

# 주문을 만드는 규칙은 실전 쪽(jongsa_live)에 있다. 백테스트가 그걸 그대로
# 가져다 써야 '앱이 알려준 주문'과 '엔진이 계산한 결과'가 어긋나지 않는다.
from src.jongsa_live import build_ladder, plan_loss_reset

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Lot:
    """매수 1건(분할매수 단위)."""

    buy_day_idx: int
    buy_price: float
    qty: float
    target_price: float
    # ----- 손실 리셋 모드에서만 쓰는 두 가지 -----
    # 리셋은 '판 뒤에 다시 사는' 것이 아니라 **일부만 팔고 남기는** 것이다.
    # 그래서 증권사 취득단가(buy_price)는 그대로 두고, 전략이 보는 기준가만
    # 그날 종가로 바꾼다. 둘을 한 값으로 쓰면 손익이 틀어지거나 목표가가
    # 안 움직이거나 둘 중 하나가 된다.
    #
    #   buy_price            실제로 낸 돈. 손익 계산은 늘 이걸로 한다.
    #   strategy_basis_price 손실 여부·목표가를 볼 때 쓰는 값.
    #
    # 리셋을 안 쓰면(loss_reset_pct=0) 둘은 언제나 같다.
    strategy_basis_price: float = None
    origin: str = "normal_buy"   # normal_buy | loss_reset
    expiry_day_idx: int = None
    residual_exit: bool = False

    def __post_init__(self):
        if self.strategy_basis_price is None:
            self.strategy_basis_price = self.buy_price


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
    buy_range_skips: int = 0   # 매수 범위를 넘겨 매수를 건너뛴 날 수
    # ----- 손실 리셋 -----
    # 몇 번이나 '전량 팔지 않고 남겼는지'. 0이면 리셋을 안 썼거나 조건이
    # 한 번도 안 맞은 것이다. 설정을 켰는데 0이면 뭔가 잘못된 것이다.
    loss_reset_days: int = 0
    loss_reset_kept_qty: float = 0.0
    overlay_entries: int = 0
    overlay_target_exits: int = 0
    overlay_expired_exits: int = 0
    overlay_stop_exits: int = 0
    overlay_events: list = field(default_factory=list)
    max_overlay_exposure_pct: float = 0.0
    avg_overlay_exposure_pct: float = 0.0
    max_overlay_lots: int = 0
    total_realized_tax: float = 0.0
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
    # ----- 배당 -----
    # 받은 배당은 따로 쌓아둔다. 총자산에는 들어가지만 하루 매수금을 정하는
    # 기준에서는 빠진다 (= 재투자하지 않는다).
    total_dividends: float = 0.0
    dividend_curve: pd.Series = field(default_factory=pd.Series)


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


def clean_prices(df: pd.DataFrame) -> pd.DataFrame:
    """계산에 넣기 전에 종가가 성한 줄만 남긴다.

    시세 제공처가 값이 비어 있는(NaN) 줄을 섞어 보내는 일이 있다. 그대로 두면
    수량 = 예산 / 종가 가 NaN이 되고, 정수주로 바꾸는 int()에서
    'cannot convert float NaN to integer'로 엉뚱하게 터진다.
    실제로 배포 환경에서 이 오류가 났다. 데이터를 어디서 받아왔든 여기서 막는다.
    """
    if df is None or len(df) == 0:
        raise ValueError("시세 데이터가 비어 있습니다.")
    if "Close" not in df.columns:
        raise ValueError("시세에 종가(Close) 항목이 없습니다.")
    close = pd.to_numeric(df["Close"], errors="coerce")
    good = np.isfinite(close.to_numpy(dtype="float64", na_value=np.nan)) & (close > 0)
    out = df[good]
    if len(out) == 0:
        raise ValueError("쓸 수 있는 종가가 하나도 없습니다.")
    return out


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


def _resolve_dividends(dividends, dates) -> np.ndarray:
    """배당락일별 주당 배당금을 거래일 인덱스 배열로 바꾼다.

    배당락일이 거래일 목록에 없으면(휴장일로 들어온 경우) 다음 거래일로 민다.
    기간 밖이면 버린다.
    """
    n = len(dates)
    out = np.zeros(n)
    if dividends is None or len(dividends) == 0:
        return out

    for d, amt in dividends.items():
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        idx = int(dates.searchsorted(pd.Timestamp(d).normalize(), side="left"))
        if idx < n:
            out[idx] += amt
    return out


def run_jongsa(
    df: pd.DataFrame,
    version: str = "V5",
    initial_cash: float = 10_000.0,
    target_return: float = 0.0275,
    dynamic_target_vol_days: int = 0,
    dynamic_target_vol_multiple: float = 0.0,
    dynamic_target_min: float = 0.0,
    dynamic_target_max: float = 0.0,
    base_target_signal=None,
    base_target_threshold: float = 0.0,
    base_target_below_threshold: float = 0.0,
    base_target_above_threshold: float = 0.0,
    base_buy_max_prior_rsi: float = 0.0,
    base_buy_allocation_signal=None,
    base_buy_allocation_threshold: float = 0.0,
    base_buy_pct_below_threshold: float = 0.0,
    base_buy_pct_above_threshold: float = 0.0,
    base_daily_buy_pct_signal=None,
    base_cash_low_threshold: float = 0.0,
    base_cash_low_buy_pct: float = 0.0,
    base_cash_high_threshold: float = 0.0,
    base_cash_high_buy_pct: float = 0.0,
    base_cash_low_target_return: float = 0.0,
    base_cash_high_target_return: float = 0.0,
    base_lot_count_low_threshold: int = 0,
    base_lot_count_low_buy_pct: float = 0.0,
    base_lot_count_high_threshold: int = 0,
    base_lot_count_high_buy_pct: float = 0.0,
    daily_buy_pct: float = 0.10,
    compound_ratio: float = 0.70,
    n_splits: int = 10,
    stop_days: int = 10,
    base_late_target_after_days: int = 0,
    base_late_target_return: float = 0.0,
    base_expiry_sell_fraction: float = 1.0,
    base_residual_hold_days: int = 0,
    base_residual_target_return: float = 0.0,
    base_residual_extension_signal=None,
    base_residual_extension_threshold: float = 0.0,
    base_residual_min_cash_pct: float = 0.0,
    base_residual_max_exposure_pct: float = 0.0,
    base_residual_min_lot_return=None,
    base_residual_max_lot_return=None,
    fee_rate: float = 0.0,
    whole_shares: bool = False,
    v4_mode: str = "rolling_replace",
    loc_buy_limit: bool = False,
    buy_range_pct: float = None,
    season_reseed: bool = False,
    fee_in_target: bool = False,
    sell_day_buy_mode: str = "never",
    reinvest: bool = True,
    cash_flows=None,
    order_sized_qty: bool = True,
    dividends=None,
    actual_buy_fills=None,
    ladder_rungs: int = 0,
    ladder_step: float = 0.03,
    loss_reset_pct: float = 0.0,
    loss_reset_threshold_pct: float = 0.0,
    overlay_dip_pct: float = 0.0,
    overlay_target_pct: float = 0.0,
    overlay_hold_days: int = 0,
    overlay_late_target_pct: float = 0.0,
    overlay_late_target_after_days: int = 0,
    overlay_partial_target_pct: float = 0.0,
    overlay_partial_exit_fraction: float = 0.0,
    overlay_trailing_activation_pct: float = 0.0,
    overlay_trailing_drawdown_pct: float = 0.0,
    overlay_stop_loss_pct: float = 0.0,
    overlay_entry_pct: float = 0.0,
    overlay_cap_pct: float = 0.0,
    overlay_entry_cooldown_days: int = 0,
    overlay_fee_rate: float | None = None,
    overlay_buy_execution_buffer_pct: float = 0.0,
    overlay_sell_execution_buffer_pct: float = 0.0,
    overlay_buy_fill_probability: float = 1.0,
    overlay_sell_fill_probability: float = 1.0,
    overlay_random_seed: int = 0,
    overlay_trend_ma_days: int = 0,
    overlay_trend_ma_slope_days: int = 0,
    overlay_trend_signal=None,
    overlay_regime_signal=None,
    overlay_regime_switch: bool = False,
    overlay_bear_dip_pct: float = 0.0,
    overlay_bear_target_pct: float = 0.0,
    overlay_bear_hold_days: int = 0,
    overlay_bear_entry_pct: float = 0.0,
    overlay_bear_cap_pct: float = 0.0,
    overlay_bear_max_prior_rsi: float = 0.0,
    overlay_max_prior_drawdown_pct: float = 0.0,
    overlay_rsi_days: int = 14,
    overlay_bear_rsi_days: int = 0,
    overlay_rsi_method: str = "wilder",
    overlay_min_prior_rsi: float = 0.0,
    overlay_max_prior_rsi: float = 0.0,
    overlay_deep_rsi_threshold: float = 0.0,
    overlay_deep_entry_pct: float = 0.0,
    overlay_entry_size_signal=None,
    overlay_entry_size_threshold: float = 0.0,
    overlay_entry_size_above_pct: float = 0.0,
    overlay_entry_size_below_pct: float = 0.0,
    overlay_volatility_days: int = 0,
    overlay_volatility_method: str = "rolling_std",
    overlay_volatility_multiple: float = 0.0,
    overlay_volatility_signal=None,
    overlay_min_dip_pct: float = 0.0,
    overlay_cumulative_lookback_days: int = 0,
    overlay_cumulative_dip_pct: float = 0.0,
    overlay_prior_day_signal_dip_pct: float = 0.0,
    overlay_next_day_discount_pct: float = 0.0,
    overlay_recovery_lookback_days: int = 0,
    overlay_recovery_crash_pct: float = 0.0,
    overlay_recovery_rebound_pct: float = 0.0,
    overlay_filter_signal=None,
    overlay_filter_signal_min: float = 0.0,
    overlay_filter_signal_max: float = 0.0,
    overlay_open_gap_min: float | None = None,
    overlay_open_gap_max: float | None = None,
    annual_realized_tax_rate: float = 0.0,
    annual_tax_exemption: float = 0.0,
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
    order_sized_qty: 매수 수량을 '주문 시점에 알 수 있는 가격'으로 정한다(기본).
        LOC 주문은 수량을 미리 적어내고 체결만 종가에 되므로 이쪽이 현실이다.
        False면 예전처럼 그날 종가로 수량을 정한다 — 종가를 미리 아는 셈이라
        결과가 실제보다 좋게 나온다. 예전 값과 비교할 때만 쓴다.
    dividends: 배당락일별 주당 배당금 Series. 받은 배당은 **따로 쌓아두고
        매매에 쓰지 않는다**. 총자산에는 더해지지만 하루 매수금을 정하는
        기준 금액에서는 빠진다 (= 배당은 재투자하지 않는다).
    ladder_rungs / ladder_step: 사다리 주문(정액매수). 기본 매수 아래로
        지정가를 낮춰가며 주문을 몇 칸 더 걸지. 0이면 안 쓴다.
        자세한 계산은 src/jongsa_live.py의 build_ladder() 참고.
    loss_reset_pct: **손실 리셋**. 0이면 안 쓴다(예전 그대로).
        0보다 크면, 손절일이 찬 물량이 전부 전일 기준 손실일 때 전량 팔지 않고
        전일 총자산의 이 비율만큼만 남긴다. 남긴 물량은 그날 종가를 새 기준으로
        삼아 보유일과 목표가를 다시 센다.

        전량 손절은 손실을 확정하고 자리를 비워 반등을 놓친다. 그 자리를
        조금 남겨 두자는 것이 이 규칙이다. 하루 매수금(daily_buy_pct)과는
        **다른 비율**이다 — 신규매수는 10%, 리셋 유지는 6% 식으로 쓴다.

        일부만 파는 것이지 팔았다가 되사는 것이 아니므로, 남긴 수량에는
        수수료가 붙지 않고 취득단가도 그대로다.
    loss_reset_threshold_pct: 리셋을 발동할 최소 손실률.
        0이면 전일 기준 단순 손실, 0.075면 모든 만기 물량이 각각 전일 기준
        -7.5% 이하일 때만 리셋한다.
    """
    df = clean_prices(df)
    close = df["Close"].astype(float).values
    open_prices = (
        df["Open"].astype(float).values if "Open" in df.columns else None
    )
    dates = df.index
    n = len(close)
    if n < 1:
        raise ValueError("해당 기간에 거래일이 없습니다.")

    close_series = pd.Series(close, index=dates)
    overlay_trend_series = close_series
    if overlay_trend_signal is not None:
        overlay_trend_series = pd.Series(overlay_trend_signal).reindex(
            dates
        ).ffill().astype(float)
    overlay_ma = (
        overlay_trend_series.rolling(overlay_trend_ma_days).mean().to_numpy()
        if overlay_trend_ma_days > 0 else None
    )
    overlay_trend_values = overlay_trend_series.to_numpy()
    overlay_regime_values = None
    if overlay_regime_signal is not None:
        overlay_regime_values = pd.Series(overlay_regime_signal).reindex(
            dates
        ).ffill().to_numpy(dtype=float)
    overlay_filter_values = None
    if overlay_filter_signal is not None:
        overlay_filter_values = pd.Series(overlay_filter_signal).reindex(
            dates
        ).ffill().to_numpy(dtype=float)
    overlay_entry_size_values = None
    if overlay_entry_size_signal is not None:
        overlay_entry_size_values = pd.Series(
            overlay_entry_size_signal
        ).reindex(dates).ffill().to_numpy(dtype=float)
    base_buy_allocation_values = None
    if base_buy_allocation_signal is not None:
        base_buy_allocation_values = pd.Series(
            base_buy_allocation_signal
        ).reindex(dates).ffill().to_numpy(dtype=float)
    base_daily_buy_pct_values = None
    if base_daily_buy_pct_signal is not None:
        base_daily_buy_pct_values = pd.Series(
            base_daily_buy_pct_signal
        ).reindex(dates).ffill().to_numpy(dtype=float)
    base_target_values = None
    if base_target_signal is not None:
        base_target_values = pd.Series(base_target_signal).reindex(
            dates
        ).ffill().to_numpy(dtype=float)
    base_residual_extension_values = None
    if base_residual_extension_signal is not None:
        base_residual_extension_values = pd.Series(
            base_residual_extension_signal
        ).reindex(dates).ffill().to_numpy(dtype=float)
    overlay_prior_peak = close_series.cummax().to_numpy()
    overlay_delta = close_series.diff()
    if overlay_rsi_method.lower() == "sma":
        overlay_avg_gain = overlay_delta.clip(lower=0).rolling(
            overlay_rsi_days, min_periods=overlay_rsi_days
        ).mean()
        overlay_avg_loss = (-overlay_delta.clip(upper=0)).rolling(
            overlay_rsi_days, min_periods=overlay_rsi_days
        ).mean()
    else:
        overlay_avg_gain = overlay_delta.clip(lower=0).ewm(
            alpha=1 / overlay_rsi_days, adjust=False,
            min_periods=overlay_rsi_days
        ).mean()
        overlay_avg_loss = (-overlay_delta.clip(upper=0)).ewm(
            alpha=1 / overlay_rsi_days, adjust=False,
            min_periods=overlay_rsi_days
        ).mean()
    overlay_rs = overlay_avg_gain / overlay_avg_loss.replace(0, np.nan)
    overlay_rsi_series = 100 - 100 / (1 + overlay_rs)
    overlay_rsi_series = overlay_rsi_series.mask(
        (overlay_avg_loss == 0) & (overlay_avg_gain > 0), 100.0
    ).mask((overlay_avg_gain == 0) & (overlay_avg_loss > 0), 0.0)
    overlay_rsi = overlay_rsi_series.to_numpy()
    overlay_bear_rsi = None
    if overlay_bear_rsi_days > 0 and overlay_bear_rsi_days != overlay_rsi_days:
        if overlay_rsi_method.lower() == "sma":
            bear_avg_gain = overlay_delta.clip(lower=0).rolling(
                overlay_bear_rsi_days, min_periods=overlay_bear_rsi_days
            ).mean()
            bear_avg_loss = (-overlay_delta.clip(upper=0)).rolling(
                overlay_bear_rsi_days, min_periods=overlay_bear_rsi_days
            ).mean()
        else:
            bear_avg_gain = overlay_delta.clip(lower=0).ewm(
                alpha=1 / overlay_bear_rsi_days, adjust=False,
                min_periods=overlay_bear_rsi_days
            ).mean()
            bear_avg_loss = (-overlay_delta.clip(upper=0)).ewm(
                alpha=1 / overlay_bear_rsi_days, adjust=False,
                min_periods=overlay_bear_rsi_days
            ).mean()
        bear_rs = bear_avg_gain / bear_avg_loss.replace(0, np.nan)
        bear_rsi_series = 100 - 100 / (1 + bear_rs)
        bear_rsi_series = bear_rsi_series.mask(
            (bear_avg_loss == 0) & (bear_avg_gain > 0), 100.0
        ).mask((bear_avg_gain == 0) & (bear_avg_loss > 0), 0.0)
        overlay_bear_rsi = bear_rsi_series.to_numpy()
    overlay_returns = close_series.pct_change()
    if overlay_volatility_signal is not None:
        overlay_volatility = pd.Series(overlay_volatility_signal).reindex(
            dates
        ).ffill().to_numpy(dtype=float)
    elif overlay_volatility_days > 1:
        if overlay_volatility_method.lower() == "ewma":
            overlay_volatility = overlay_returns.ewm(
                span=overlay_volatility_days,
                min_periods=overlay_volatility_days,
                adjust=False,
            ).std().to_numpy()
        elif overlay_volatility_method.lower() == "downside":
            overlay_volatility = overlay_returns.clip(upper=0).pow(2).rolling(
                overlay_volatility_days
            ).mean().pow(.5).to_numpy()
        else:
            overlay_volatility = overlay_returns.rolling(
                overlay_volatility_days
            ).std().to_numpy()
    else:
        overlay_volatility = None
    dynamic_target_volatility = (
        close_series.pct_change().rolling(dynamic_target_vol_days).std().to_numpy()
        if dynamic_target_vol_days > 1 else None
    )

    cash = float(initial_cash)
    shares = 0.0
    open_lots: list[Lot] = []
    overlay_lots: list[dict] = []
    overlay_entries = 0
    overlay_target_exits = 0
    overlay_expired_exits = 0
    overlay_stop_exits = 0
    overlay_events = []
    overlay_exposure = np.zeros(n)
    overlay_lot_counts = np.zeros(n, dtype=int)
    overlay_last_entry_idx = -10**9
    overlay_rng = np.random.default_rng(overlay_random_seed)
    taxable_realized_by_year: dict[int, float] = {}
    total_realized_tax = 0.0
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
    range_skips = 0   # 매수 범위를 넘겨 그냥 지나간 날
    reset_days = 0        # 손실 리셋이 일어난 날 수
    reset_kept_qty = 0.0  # 그때 남긴 수량 합계

    prev_total_assets = float(initial_cash)

    flows, flow_notes = _resolve_flows(cash_flows, dates)
    contributed = float(initial_cash)     # 지금까지 내가 넣은 돈 (출금하면 줄어든다)
    applied_flows = np.zeros(n)           # 실제로 반영된 금액 (출금은 예수금 한도)
    contributed_arr = np.zeros(n)
    pending_withdrawal = 0.0              # 예수금이 모자라 아직 못 뺀 출금액

    div_by_idx = _resolve_dividends(dividends, dates)
    # 실전 장부에서는 LOC가 거부되거나 수동으로 대체 매수한 경우가 있다.
    # 날짜별 실제 체결가/수량을 넘기면 이론 종가 체결 대신 그 값으로 계좌를
    # 이어간다. 백테스트 호출은 기본값(None)이므로 과거 성과에는 영향이 없다.
    actual_fills = {}
    for raw in actual_buy_fills or []:
        try:
            d, qty, fill_price = raw
            actual_fills[pd.Timestamp(d).normalize()] = (float(qty), float(fill_price))
        except (TypeError, ValueError):
            continue
    dividend_cash = 0.0        # 받은 배당. 따로 둔다 — 매매에 쓰지 않는다
    dividend_arr = np.zeros(n)

    for t in range(n):
        price = close[t]
        effective_daily_buy_pct = daily_buy_pct
        if (
            base_daily_buy_pct_values is not None
            and t > 0
            and np.isfinite(base_daily_buy_pct_values[t - 1])
            and base_daily_buy_pct_values[t - 1] > 0
        ):
            effective_daily_buy_pct = base_daily_buy_pct_values[t - 1]
        prior_cash_ratio = cash / max(prev_total_assets, 1e-9)
        prior_lot_count = len(open_lots)
        if (
            base_lot_count_low_threshold > 0
            and base_lot_count_low_buy_pct > 0
            and prior_lot_count <= base_lot_count_low_threshold
        ):
            effective_daily_buy_pct = base_lot_count_low_buy_pct
        elif (
            base_lot_count_high_threshold > 0
            and base_lot_count_high_buy_pct > 0
            and prior_lot_count >= base_lot_count_high_threshold
        ):
            effective_daily_buy_pct = base_lot_count_high_buy_pct
        if (
            base_cash_low_threshold > 0
            and base_cash_low_buy_pct > 0
            and prior_cash_ratio <= base_cash_low_threshold
        ):
            effective_daily_buy_pct = base_cash_low_buy_pct
        elif (
            base_cash_high_threshold > 0
            and base_cash_high_buy_pct > 0
            and prior_cash_ratio >= base_cash_high_threshold
        ):
            effective_daily_buy_pct = base_cash_high_buy_pct
        # External prior-day risk/allocation signals have final priority over
        # endogenous cash acceleration. A risky market must never be overridden
        # merely because the account currently holds a lot of cash.
        if (
            base_buy_allocation_values is not None
            and base_buy_allocation_threshold > 0
            and base_buy_pct_below_threshold > 0
            and base_buy_pct_above_threshold > 0
            and t > 0
            and np.isfinite(base_buy_allocation_values[t - 1])
        ):
            effective_daily_buy_pct = (
                base_buy_pct_above_threshold
                if base_buy_allocation_values[t - 1]
                >= base_buy_allocation_threshold
                else effective_daily_buy_pct
            )
        effective_target_return = target_return
        if (
            base_cash_low_threshold > 0
            and base_cash_low_target_return > 0
            and prior_cash_ratio <= base_cash_low_threshold
        ):
            effective_target_return = base_cash_low_target_return
        elif (
            base_cash_high_threshold > 0
            and base_cash_high_target_return > 0
            and prior_cash_ratio >= base_cash_high_threshold
        ):
            effective_target_return = base_cash_high_target_return
        if (
            base_target_values is not None
            and base_target_threshold > 0
            and base_target_below_threshold > 0
            and base_target_above_threshold > 0
            and t > 0
            and np.isfinite(base_target_values[t - 1])
        ):
            effective_target_return = (
                base_target_above_threshold
                if base_target_values[t - 1] >= base_target_threshold
                else base_target_below_threshold
            )
        if (
            dynamic_target_volatility is not None
            and dynamic_target_vol_multiple > 0
            and t > 0
            and np.isfinite(dynamic_target_volatility[t - 1])
        ):
            effective_target_return = (
                dynamic_target_volatility[t - 1] * dynamic_target_vol_multiple
            )
            if dynamic_target_min > 0:
                effective_target_return = max(
                    dynamic_target_min, effective_target_return
                )
            if dynamic_target_max > 0:
                effective_target_return = min(
                    dynamic_target_max, effective_target_return
                )
        sell_qty_today = 0.0
        sell_amt_today = 0.0
        sell_reasons: list[str] = []

        # ---------- 배당 ----------
        # 배당락일 전날 종가에 갖고 있어야 받는다. 그래서 그날 매매를 하기 전,
        # 어제부터 들고 온 수량으로 계산한다.
        div_today = div_by_idx[t] * shares if div_by_idx[t] > 0 else 0.0
        dividend_cash += div_today

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

        # Values known before today's closing auction.  Overlay order quantity
        # must not depend on today's close or on same-close sale proceeds.
        overlay_known_cash = cash
        overlay_known_value = sum(
            lot["qty"] * (close[t - 1] if t > 0 else price)
            for lot in overlay_lots
        )

        # Optional research overlay. It shares the real cash account with the
        # base strategy, preventing the same cash from being allocated twice.
        overlay_fee = fee_rate if overlay_fee_rate is None else overlay_fee_rate
        if overlay_dip_pct > 0 and overlay_lots:
            overlay_remaining = []
            for overlay_lot in overlay_lots:
                overlay_age = t - overlay_lot["buy_day_idx"]
                overlay_effective_target_pct = overlay_lot.get(
                    "target_pct", overlay_target_pct
                )
                if (
                    overlay_lot.get("late_target_after_days", 0) > 0
                    and overlay_age >= overlay_lot["late_target_after_days"]
                    and overlay_lot.get("late_target_pct", 0) > 0
                ):
                    overlay_effective_target_pct = overlay_lot["late_target_pct"]
                overlay_hit_signal = (
                    overlay_age >= 1
                    and price >= overlay_lot["buy_price"] * (
                        1 + overlay_effective_target_pct
                    ) * (
                        1 + overlay_sell_execution_buffer_pct
                    )
                )
                overlay_hit = (
                    overlay_hit_signal
                    and overlay_rng.random() <= overlay_sell_fill_probability
                )
                overlay_partial_hit_signal = (
                    not overlay_lot.get("partial_done", False)
                    and 0 < overlay_partial_target_pct
                    < overlay_lot.get("target_pct", overlay_target_pct)
                    and 0 < overlay_partial_exit_fraction < 1
                    and overlay_age >= 1
                    and price >= overlay_lot["buy_price"] * (
                        1 + overlay_partial_target_pct
                    ) * (1 + overlay_sell_execution_buffer_pct)
                )
                overlay_partial_hit = (
                    overlay_partial_hit_signal
                    and overlay_rng.random() <= overlay_sell_fill_probability
                )
                lot_hold_days = overlay_lot.get("hold_days", overlay_hold_days)
                overlay_expired = lot_hold_days > 0 and overlay_age >= lot_hold_days
                overlay_stopped = (
                    overlay_stop_loss_pct > 0
                    and overlay_age >= 2
                    and close[t - 1] <= overlay_lot["buy_price"] * (
                        1 - overlay_stop_loss_pct
                    )
                )
                overlay_trailing_stopped = False
                if (
                    overlay_trailing_activation_pct > 0
                    and overlay_trailing_drawdown_pct > 0
                    and overlay_age >= 2
                ):
                    prior_close = close[t - 1]
                    if prior_close >= overlay_lot["buy_price"] * (
                        1 + overlay_trailing_activation_pct
                    ):
                        overlay_lot["trailing_active"] = True
                    if overlay_lot.get("trailing_active", False):
                        prior_peak = overlay_lot.get(
                            "trailing_peak", overlay_lot["buy_price"]
                        )
                        overlay_lot["trailing_peak"] = max(prior_peak, prior_close)
                        overlay_trailing_stopped = prior_close <= prior_peak * (
                            1 - overlay_trailing_drawdown_pct
                        )
                if overlay_hit or overlay_expired or overlay_stopped or overlay_trailing_stopped:
                    overlay_proceeds = overlay_lot["qty"] * price * (1 - overlay_fee)
                    cash += overlay_proceeds
                    shares -= overlay_lot["qty"]
                    taxable_realized_by_year[dates[t].year] = (
                        taxable_realized_by_year.get(dates[t].year, 0.0)
                        + overlay_proceeds
                        - overlay_lot["qty"] * overlay_lot["tax_basis_price"]
                    )
                    overlay_target_exits += int(overlay_hit)
                    overlay_stop_exits += int(
                        not overlay_hit and (overlay_stopped or overlay_trailing_stopped)
                    )
                    overlay_expired_exits += int(
                        not overlay_hit and not overlay_stopped
                        and not overlay_trailing_stopped and overlay_expired
                    )
                    exit_reason = (
                        "target" if overlay_hit
                        else "trailing_stop" if overlay_trailing_stopped
                        else "stop" if overlay_stopped
                        else "expiry"
                    )
                    overlay_events.append({
                        "date": dates[t], "event": "sell",
                        "reason": exit_reason,
                        "qty": overlay_lot["qty"], "price": price,
                        "buy_date": dates[overlay_lot["buy_day_idx"]],
                        "buy_price": overlay_lot["buy_price"],
                    })
                elif overlay_partial_hit:
                    partial_qty = float(np.floor(
                        overlay_lot["qty"] * overlay_partial_exit_fraction
                    ))
                    if partial_qty >= 1 and partial_qty < overlay_lot["qty"]:
                        overlay_proceeds = partial_qty * price * (1 - overlay_fee)
                        cash += overlay_proceeds
                        shares -= partial_qty
                        taxable_realized_by_year[dates[t].year] = (
                            taxable_realized_by_year.get(dates[t].year, 0.0)
                            + overlay_proceeds
                            - partial_qty * overlay_lot["tax_basis_price"]
                        )
                        overlay_lot["qty"] -= partial_qty
                        overlay_lot["partial_done"] = True
                        overlay_target_exits += 1
                        overlay_events.append({
                            "date": dates[t], "event": "sell",
                            "reason": "partial_target",
                            "qty": partial_qty, "price": price,
                            "buy_date": dates[overlay_lot["buy_day_idx"]],
                            "buy_price": overlay_lot["buy_price"],
                        })
                    overlay_remaining.append(overlay_lot)
                else:
                    overlay_remaining.append(overlay_lot)
            overlay_lots = overlay_remaining

        # 오늘 '매도 주문을 걸 수 있는' 물량이 있었는지.
        # 있으면 매수 지정가를 (최저 목표가 - 0.01)로 걸게 되고, 그건 곧
        # '매도가 체결되면 매수는 미체결'이라 아래 did_sell 판정과 같아진다.
        # 없으면 그 장치를 못 쓰니 '매수 범위'라는 상한을 대신 건다.
        had_sell_candidates = any(
            (t - lot.buy_day_idx) >= 1 for lot in open_lots
        )

        # 어제 저녁 주문을 넣을 때 쓸 수 있었던 가격.
        #
        # 주문 수량은 '오늘 종가 ÷ 예산'으로 정할 수 없다. 주문을 넣는 시점에는
        # 오늘 종가를 모르기 때문이다. 실제로는 아래 가격으로 나눠서 수량을 정하고,
        # 체결은 종가에 된다. 그래서 종가가 예상보다 내리면 예산보다 적게 사고,
        # 오르면 예산을 조금 넘겨 산다. 이 차이를 반영하지 않으면 백테스트가
        # 현실보다 유리하게 나오고, 앱이 알려준 수량과 실제 보유량도 어긋난다.
        #
        # src/jongsa_live.py의 order_plan()이 실제로 쓰는 가격과 같아야 한다.
        def effective_lot_target(lot, held_days):
            if (
                base_late_target_after_days > 0
                and held_days >= base_late_target_after_days
                and base_late_target_return > 0
            ):
                target = lot.buy_price * (1 + base_late_target_return)
                if fee_in_target:
                    target *= 1 + 2 * fee_rate
                return min(lot.target_price, target)
            return lot.target_price

        pending_targets = [
            effective_lot_target(lot, t - lot.buy_day_idx) for lot in open_lots
            if 1 <= (t - lot.buy_day_idx) and t < (
                lot.expiry_day_idx if lot.expiry_day_idx is not None
                else lot.buy_day_idx + stop_days
            )
        ]
        if pending_targets:
            order_px = round(min(pending_targets) - 0.01, 2)   # 팔 물량이 있는 날
        elif t > 0:
            order_px = close[t - 1]                            # 매수 범위를 쓰는 날
        else:
            order_px = price   # 첫날은 이전 종가가 없다 — 하루뿐이라 그대로 둔다

        # ---------- 1) 매도 판정 ----------
        did_sell = False
        sold_pnls: list[float] = []
        remaining: list[Lot] = []

        # 손실 리셋: 16일이 찬 물량이 **전부 전일 기준 손실**이면 전량 팔지 않고
        # 전일 총자산의 일정 비율만 남긴다. 남긴 것은 오늘 종가를 새 기준으로
        # 삼아 다시 센다. loss_reset_pct=0이면 이 블록은 통째로 건너뛴다.
        forced_lots = [
            l for l in open_lots if t >= (
                l.expiry_day_idx if l.expiry_day_idx is not None
                else l.buy_day_idx + stop_days
            )
        ]
        reset_lots = [l for l in forced_lots if not l.residual_exit]
        reset = None
        if loss_reset_pct > 0 and reset_lots and t > 0:
            reset = plan_loss_reset(
                [{"quantity": l.qty, "strategy_basis_price": l.strategy_basis_price}
                 for l in reset_lots],
                prev_close=close[t - 1],
                prev_total_assets=prev_total_assets,
                retain_pct=loss_reset_pct,
                fee=fee_rate,
                whole_shares=whole_shares,
                loss_threshold_pct=loss_reset_threshold_pct,
            )
            if not reset["all_loss"]:
                reset = None   # 하나라도 손실이 아니면 예전처럼 전량 매도

        # 남길 수량은 오래된 것부터 채운다(선입선출). 어느 것을 남기든 현금은
        # 같지만, 취득단가가 섞이므로 순서를 정해두지 않으면 결과가 흔들린다.
        retain_left = reset["retain_qty"] if reset else 0.0
        retain_parts = []   # (수량, 취득단가) — 남긴 것들의 평균단가를 내는 데 쓴다

        for lot in open_lots:
            held = t - lot.buy_day_idx
            lot_expiry = (
                lot.expiry_day_idx if lot.expiry_day_idx is not None
                else lot.buy_day_idx + stop_days
            )
            sell_reason = None
            lot_sell_target = effective_lot_target(lot, held)
            if 1 <= held and t < lot_expiry and price >= lot_sell_target:
                sell_reason = "목표달성"
            elif t >= lot_expiry:
                sell_reason = "강제손절"

            # 리셋하는 날의 강제청산 건은 일부(또는 전부)를 남긴다.
            if sell_reason == "강제손절" and reset and retain_left > 0:
                keep = min(lot.qty, retain_left)
                retain_left -= keep
                retain_parts.append((keep, lot.buy_price))
                if keep >= lot.qty - 1e-9:
                    continue          # 이 건은 통째로 남았다 — 팔 것이 없다
                lot = Lot(buy_day_idx=lot.buy_day_idx, buy_price=lot.buy_price,
                          qty=lot.qty - keep, target_price=lot.target_price,
                          strategy_basis_price=lot.strategy_basis_price,
                          origin=lot.origin)   # 남은 만큼만 판다

            if (
                sell_reason and t >= lot_expiry and not reset
                and not lot.residual_exit
                and 0 < base_expiry_sell_fraction < 1
                and base_residual_hold_days > 0
                and (
                    base_residual_extension_values is None
                    or (
                        t > 0
                        and np.isfinite(base_residual_extension_values[t - 1])
                        and base_residual_extension_values[t - 1]
                        >= base_residual_extension_threshold
                    )
                )
                and (
                    base_residual_min_cash_pct <= 0
                    or cash / max(prev_total_assets, 1e-9)
                    >= base_residual_min_cash_pct
                )
                and (
                    base_residual_max_exposure_pct <= 0
                    or (shares * (close[t - 1] if t > 0 else price))
                    / max(prev_total_assets, 1e-9)
                    <= base_residual_max_exposure_pct
                )
                and (
                    base_residual_min_lot_return is None
                    or (
                        (close[t - 1] if t > 0 else price) / lot.buy_price - 1
                        >= base_residual_min_lot_return
                    )
                )
                and (
                    base_residual_max_lot_return is None
                    or (
                        (close[t - 1] if t > 0 else price) / lot.buy_price - 1
                        <= base_residual_max_lot_return
                    )
                )
            ):
                sell_qty = (
                    math.floor(lot.qty * base_expiry_sell_fraction)
                    if whole_shares else lot.qty * base_expiry_sell_fraction
                )
                sell_qty = max(1.0 if whole_shares else 1e-9, sell_qty)
                sell_qty = min(lot.qty, sell_qty)
                keep_qty = lot.qty - sell_qty
                if keep_qty > 1e-9:
                    residual_target = (
                        base_residual_target_return
                        if base_residual_target_return > 0 else target_return
                    )
                    residual_tgt = lot.buy_price * (1 + residual_target)
                    if fee_in_target:
                        residual_tgt *= 1 + 2 * fee_rate
                    remaining.append(Lot(
                        buy_day_idx=lot.buy_day_idx,
                        buy_price=lot.buy_price,
                        qty=keep_qty,
                        target_price=residual_tgt,
                        strategy_basis_price=lot.strategy_basis_price,
                        origin="expiry_residual",
                        expiry_day_idx=t + base_residual_hold_days,
                        residual_exit=True,
                    ))
                    lot = Lot(
                        buy_day_idx=lot.buy_day_idx,
                        buy_price=lot.buy_price,
                        qty=sell_qty,
                        target_price=lot.target_price,
                        strategy_basis_price=lot.strategy_basis_price,
                        origin=lot.origin,
                        expiry_day_idx=lot.expiry_day_idx,
                        residual_exit=lot.residual_exit,
                    )

            if sell_reason:
                proceeds = lot.qty * price * (1 - fee_rate)
                cost = lot.qty * lot.buy_price
                cash += proceeds
                shares -= lot.qty
                pnl = proceeds - cost
                taxable_realized_by_year[dates[t].year] = (
                    taxable_realized_by_year.get(dates[t].year, 0.0)
                    + proceeds - lot.qty * lot.buy_price * (1 + fee_rate)
                )
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

        # ---------- 1-2) 남긴 물량을 새 lot 으로 ----------
        # 장이 끝나야 오늘 종가가 확정된다. 그때 남은 수량을 하나로 묶어
        # 오늘 종가를 새 기준으로 삼는다.
        #
        # 취득단가는 **남긴 것들의 가중평균**을 쓴다. 실제로 낸 돈이 바뀌지
        # 않았으므로 손익은 이 값으로 계산해야 맞다. 전략이 보는 기준가만
        # 오늘 종가로 바꾼다.
        if reset and retain_parts:
            kept_qty = sum(q for q, _ in retain_parts)
            kept_cost = sum(q * p for q, p in retain_parts)
            avg_cost = kept_cost / kept_qty if kept_qty > 0 else price
            tgt = price * (1 + effective_target_return)
            if fee_in_target:
                tgt *= 1 + 2 * fee_rate
            open_lots.append(
                Lot(buy_day_idx=t, buy_price=avg_cost, qty=kept_qty,
                    target_price=tgt, strategy_basis_price=price,
                    origin="loss_reset")
            )
            reset_days += 1
            reset_kept_qty += kept_qty
            # 현금도 보유수량도 건드리지 않는다 — 판 것이 아니기 때문이다.
            # (판 몫은 위 반복문에서 이미 처리됐다)

        # ---------- 2) 매수 판정 ----------
        # 기본은 '매도가 있는 날은 매수하지 않는다'(V4부터의 규칙).
        # 변형: 손절로 청산된 건은 목표 미달이니 재진입을 허용한다는 발상.
        #   any_loss — 오늘 매도분 중 손실이 하나라도 있으면 매수
        #   all_loss — 오늘 매도분이 전부 손실일 때만 매수
        # 손절일이 찬 물량이 있던 날은 **판 것이 하나도 없어도** 매수하지 않는다.
        #
        # 리셋으로 전부 남기면(순매도 0) did_sell 이 False 가 되어 매수가 열린다.
        # 그러면 같은 날 '남기기 + 새로 사기'가 겹쳐 노출이 뛴다. 실제로 그
        # 상태로 재보니 2011~2020 MDD 가 명세보다 4%p 깊었다.
        # 규칙은 '손절일이 온 날은 정리하는 날' 이지 '판 날' 이 아니다.
        # (리셋 모드에서만. 예전 '손절재진입' 설정은 손절일에도 사도록 되어
        #  있으므로 그쪽 동작을 바꾸면 안 된다.)
        if loss_reset_pct > 0 and forced_lots:
            buy_today = False
        elif not did_sell:
            buy_today = True
        elif sell_day_buy_mode == "any_loss":
            buy_today = any(p < 0 for p in sold_pnls)
        elif sell_day_buy_mode == "all_loss":
            buy_today = all(p < 0 for p in sold_pnls)
        else:
            buy_today = False

        if buy_today and base_buy_max_prior_rsi > 0 and t > 0:
            if (
                not np.isfinite(overlay_rsi[t - 1])
                or overlay_rsi[t - 1] > base_buy_max_prior_rsi
            ):
                buy_today = False

        # LOC 지정가 매수: 주문가 = 전일종가 x (1+목표수익률).
        # 오늘 종가가 그보다 높으면(= 어제보다 많이 올랐으면) 체결되지 않는다.
        # 구글시트 사례에서 확인된 규칙으로, 원 스펙 문서에는 없었다.
        if buy_today and loc_buy_limit and t > 0:
            order_limit = close[t - 1] * (1 + effective_target_return)
            if price > order_limit:
                buy_today = False

        # 매수 범위: 팔 물량이 아예 없던 날은 '(최저 목표가 - 0.01)' 장치를 쓸 수
        # 없다. 대신 '어제 종가 x (1 + 매수 범위)'를 상한으로 걸고, 그보다 높게
        # 마감하면 사지 않는다. 어제보다 크게 튄 날은 비싸니 건너뛰겠다는 뜻이다.
        # (팔 물량이 있던 날은 위 did_sell 판정이 이미 같은 역할을 한다)
        buy_range_skipped = False
        if (buy_today and buy_range_pct is not None and t > 0
                and not had_sell_candidates):
            if price > close[t - 1] * (1 + buy_range_pct):
                buy_today = False
                buy_range_skipped = True
                range_skips += 1

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
                    desired = prev_total_assets * effective_daily_buy_pct
                else:
                    # 재투자 안 함 — 하루 매수금을 '넣은 돈' 기준으로 고정.
                    # 중간에 입금하면 그만큼은 늘어나야 한다. 벌어들인 이익만 제외한다.
                    desired = contributed * effective_daily_buy_pct
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
            buy_budget_today = buy_amount
            buy_qty_today = 0.0
            buy_amt_today = 0.0
            buy_target_today = None
            buy_fill_price_today = None
            actual_fill_used_today = False
            if buy_amount > 0 and cash > 0:
                if buy_amount >= cash - 1e-9:
                    cash_exhausted += 1
                # 수량은 주문 시점에 알 수 있는 가격으로 정하고, 체결은 종가에 된다.
                # (order_sized_qty=False로 두면 예전처럼 종가로 수량을 정한다 —
                #  현실에서는 불가능하지만 예전 결과와 비교할 때 쓴다)
                size_px = order_px if (order_sized_qty and order_px > 0) else price
                qty = buy_amount * (1 - fee_rate) / size_px
                if not np.isfinite(qty) or qty < 0:
                    qty = 0.0
                if whole_shares:
                    qty = float(int(qty))
                    # 사다리 주문: 기본 주문 아래에 걸어둔 칸들 중 종가가 지정가
                    # 이하인 것까지 체결된다. 지정가가 내림차순이라 위에서부터
                    # 하나라도 안 되면 그 아래도 안 된다.
                    for rung_qty, rung_px in build_ladder(
                        buy_amount, size_px, qty,
                        rungs=ladder_rungs, step=ladder_step, fee=fee_rate,
                    ):
                        if price > rung_px:
                            break
                        qty += rung_qty
                fill_price = price
                actual_fill = actual_fills.get(pd.Timestamp(dates[t]).normalize())
                if actual_fill is not None:
                    qty, fill_price = actual_fill
                    actual_fill_used_today = True
                    if qty <= 0 or fill_price <= 0:
                        qty = 0.0
                if qty > 0:
                    spend = qty * fill_price * (1 + fee_rate)
                    if spend > cash:  # 반올림으로 예수금을 넘지 않게
                        if actual_fill is not None:
                            raise ValueError(
                                f"{pd.Timestamp(dates[t]).date()} 실제 매수금액이 예수금을 초과합니다. "
                                "체결 수량·가격 또는 입금 기록을 확인해주세요."
                            )
                        qty = cash / (fill_price * (1 + fee_rate))
                        if whole_shares:
                            qty = float(int(qty))
                        spend = qty * fill_price * (1 + fee_rate)
                    if qty > 0:
                        cash -= spend
                        shares += qty
                        # 시트는 목표가에 왕복 수수료를 얹어둔다
                        tgt = fill_price * (1 + effective_target_return)
                        if fee_in_target:
                            tgt *= 1 + 2 * fee_rate
                        open_lots.append(
                            Lot(buy_day_idx=t, buy_price=fill_price, qty=qty, target_price=tgt)
                        )
                        buy_qty_today = qty
                        buy_amt_today = spend
                        buy_target_today = tgt
                        buy_fill_price_today = fill_price
        else:
            buy_budget_today = 0.0
            buy_qty_today = 0.0
            buy_amt_today = 0.0
            buy_target_today = None
            buy_fill_price_today = None
            actual_fill_used_today = False

        # Base orders have priority. The overlay LOC quantity is sized from
        # yesterday's known limit, then filled at today's closing price.
        if overlay_dip_pct > 0 and t > 0:
            overlay_bear_regime = bool(
                overlay_regime_switch
                and (
                    (
                        overlay_regime_values is not None
                        and np.isfinite(overlay_regime_values[t - 1])
                        and overlay_regime_values[t - 1] < 0.5
                    )
                    or (
                        overlay_regime_values is None
                        and overlay_ma is not None
                        and np.isfinite(overlay_ma[t - 1])
                        and overlay_trend_values[t - 1] < overlay_ma[t - 1]
                    )
                )
            )
            effective_dip_pct = overlay_dip_pct
            if (
                overlay_volatility is not None
                and overlay_volatility_multiple > 0
                and np.isfinite(overlay_volatility[t - 1])
            ):
                effective_dip_pct = max(
                    overlay_min_dip_pct,
                    overlay_volatility[t - 1] * overlay_volatility_multiple,
                )
            if overlay_bear_regime and overlay_bear_dip_pct > 0:
                effective_dip_pct = overlay_bear_dip_pct
            overlay_limit = close[t - 1] * (1 - effective_dip_pct)
            if (
                overlay_cumulative_lookback_days > 0
                and overlay_cumulative_dip_pct > 0
            ):
                if t >= overlay_cumulative_lookback_days:
                    reference_t = t - overlay_cumulative_lookback_days
                    overlay_limit = close[reference_t] * (
                        1 - overlay_cumulative_dip_pct
                    )
                else:
                    overlay_limit = float("-inf")
            if overlay_prior_day_signal_dip_pct > 0:
                prior_day_return = (
                    close[t - 1] / close[t - 2] - 1 if t >= 2 else 0.0
                )
                if prior_day_return <= -overlay_prior_day_signal_dip_pct:
                    overlay_limit = close[t - 1] * (
                        1 - overlay_next_day_discount_pct
                    )
                else:
                    overlay_limit = float("-inf")
            overlay_filter_ok = True
            if overlay_ma is not None:
                overlay_filter_ok = np.isfinite(overlay_ma[t - 1])
                if not overlay_regime_switch:
                    overlay_filter_ok = (
                        overlay_filter_ok
                        and overlay_trend_values[t - 1] >= overlay_ma[t - 1]
                    )
                if overlay_filter_ok and overlay_trend_ma_slope_days > 0:
                    slope_ref = t - 1 - overlay_trend_ma_slope_days
                    overlay_filter_ok = (
                        slope_ref >= 0
                        and np.isfinite(overlay_ma[slope_ref])
                        and overlay_ma[t - 1] >= overlay_ma[slope_ref]
                    )
            if overlay_filter_ok and overlay_max_prior_drawdown_pct > 0:
                prior_dd = close[t - 1] / overlay_prior_peak[t - 1] - 1
                overlay_filter_ok = prior_dd >= -overlay_max_prior_drawdown_pct
            if (
                overlay_filter_ok
                and overlay_recovery_lookback_days > 1
                and overlay_recovery_crash_pct > 0
                and overlay_recovery_rebound_pct > 0
            ):
                window_start = t - overlay_recovery_lookback_days
                if window_start < 0:
                    overlay_filter_ok = False
                else:
                    # Everything in this window ends at t-1, so the filter is
                    # known before today's close/LOC execution.  The trough
                    # must occur after the peak to represent crash -> recovery.
                    recovery_window = close[window_start:t]
                    peak_offset = int(np.argmax(recovery_window))
                    after_peak = recovery_window[peak_offset:]
                    trough_offset = int(np.argmin(after_peak))
                    recovery_peak = float(recovery_window[peak_offset])
                    recovery_trough = float(after_peak[trough_offset])
                    crash_return = recovery_trough / recovery_peak - 1
                    rebound_return = close[t - 1] / recovery_trough - 1
                    overlay_filter_ok = (
                        crash_return <= -overlay_recovery_crash_pct
                        and rebound_return >= overlay_recovery_rebound_pct
                    )
            effective_overlay_rsi = (
                overlay_bear_rsi
                if overlay_bear_regime and overlay_bear_rsi is not None
                else overlay_rsi
            )
            if overlay_filter_ok and overlay_min_prior_rsi > 0:
                overlay_filter_ok = (
                    np.isfinite(effective_overlay_rsi[t - 1])
                    and effective_overlay_rsi[t - 1] >= overlay_min_prior_rsi
                )
            effective_max_prior_rsi = (
                overlay_bear_max_prior_rsi
                if overlay_bear_regime and overlay_bear_max_prior_rsi > 0
                else overlay_max_prior_rsi
            )
            if overlay_filter_ok and effective_max_prior_rsi > 0:
                overlay_filter_ok = (
                    np.isfinite(effective_overlay_rsi[t - 1])
                    and effective_overlay_rsi[t - 1] <= effective_max_prior_rsi
                )
            if overlay_filter_ok and overlay_filter_signal_min > 0:
                overlay_filter_ok = (
                    overlay_filter_values is not None
                    and np.isfinite(overlay_filter_values[t - 1])
                    and overlay_filter_values[t - 1] >= overlay_filter_signal_min
                )
            if overlay_filter_ok and overlay_filter_signal_max > 0:
                overlay_filter_ok = (
                    overlay_filter_values is not None
                    and np.isfinite(overlay_filter_values[t - 1])
                    and overlay_filter_values[t - 1] <= overlay_filter_signal_max
                )
            if overlay_filter_ok and overlay_entry_cooldown_days > 0:
                overlay_filter_ok = (
                    t - overlay_last_entry_idx > overlay_entry_cooldown_days
                )
            if overlay_filter_ok and overlay_open_gap_min is not None:
                overlay_filter_ok = (
                    open_prices is not None
                    and open_prices[t] / close[t - 1] - 1 >= overlay_open_gap_min
                )
            if overlay_filter_ok and overlay_open_gap_max is not None:
                overlay_filter_ok = (
                    open_prices is not None
                    and open_prices[t] / close[t - 1] - 1 <= overlay_open_gap_max
                )
            if (
                price <= overlay_limit * (1 - overlay_buy_execution_buffer_pct)
                and overlay_filter_ok
                and overlay_rng.random() <= overlay_buy_fill_probability
            ):
                overlay_value = overlay_known_value
                effective_overlay_cap_pct = (
                    overlay_bear_cap_pct
                    if overlay_bear_regime and overlay_bear_cap_pct > 0
                    else overlay_cap_pct
                )
                overlay_room = max(
                    0.0, prev_total_assets * effective_overlay_cap_pct - overlay_value
                )
                overlay_known_available = max(
                    0.0,
                    overlay_known_cash
                    - prev_total_assets * effective_daily_buy_pct,
                )
                effective_overlay_entry_pct = overlay_entry_pct
                if overlay_bear_regime and overlay_bear_entry_pct > 0:
                    effective_overlay_entry_pct = overlay_bear_entry_pct
                if (
                    overlay_deep_rsi_threshold > 0
                    and overlay_deep_entry_pct > 0
                    and np.isfinite(effective_overlay_rsi[t - 1])
                    and effective_overlay_rsi[t - 1] <= overlay_deep_rsi_threshold
                ):
                    effective_overlay_entry_pct = overlay_deep_entry_pct
                if (
                    overlay_entry_size_values is not None
                    and np.isfinite(overlay_entry_size_values[t - 1])
                ):
                    if (
                        overlay_entry_size_values[t - 1]
                        >= overlay_entry_size_threshold
                        and overlay_entry_size_above_pct > 0
                    ):
                        effective_overlay_entry_pct = overlay_entry_size_above_pct
                    elif (
                        overlay_entry_size_values[t - 1]
                        < overlay_entry_size_threshold
                        and overlay_entry_size_below_pct > 0
                    ):
                        effective_overlay_entry_pct = overlay_entry_size_below_pct
                overlay_budget = min(
                    prev_total_assets * effective_overlay_entry_pct,
                    overlay_known_available,
                    cash,
                    overlay_room,
                )
                overlay_size_cost = overlay_limit * (1 + overlay_fee)
                overlay_qty = (
                    math.floor(overlay_budget / overlay_size_cost)
                    if overlay_size_cost > 0 else 0
                )
                if overlay_qty > 0:
                    overlay_spend = overlay_qty * price * (1 + overlay_fee)
                    if overlay_spend <= cash + 1e-9:
                        cash -= overlay_spend
                        shares += overlay_qty
                        overlay_lots.append({
                            "buy_day_idx": t,
                            "buy_price": price,
                            "tax_basis_price": price * (1 + overlay_fee),
                            "qty": float(overlay_qty),
                            "target_pct": (
                                overlay_bear_target_pct
                                if overlay_bear_regime and overlay_bear_target_pct > 0
                                else overlay_target_pct
                            ),
                            "hold_days": (
                                overlay_bear_hold_days
                                if overlay_bear_regime and overlay_bear_hold_days > 0
                                else overlay_hold_days
                            ),
                            "late_target_pct": overlay_late_target_pct,
                            "late_target_after_days": overlay_late_target_after_days,
                            "partial_done": False,
                            "trailing_active": False,
                            "trailing_peak": float(price),
                        })
                        overlay_events.append({
                            "date": dates[t], "event": "buy",
                            "reason": "bear" if overlay_bear_regime else "bull",
                            "qty": float(overlay_qty), "price": price,
                            "limit": overlay_limit,
                        })
                        overlay_last_entry_idx = t
                        overlay_entries += 1

        # Conservative annual tax accrual: deduct on the last trading day of
        # each calendar year instead of waiting until the following May.
        is_year_end = t == n - 1 or dates[t + 1].year != dates[t].year
        if annual_realized_tax_rate > 0 and is_year_end:
            taxable = max(
                0.0,
                taxable_realized_by_year.get(dates[t].year, 0.0)
                - annual_tax_exemption,
            )
            tax_due = taxable * annual_realized_tax_rate
            cash -= tax_due
            total_realized_tax += tax_due

        # ---------- 3) 기록 ----------
        # 하루 매수금은 '굴리는 돈'만 기준으로 삼는다. 배당까지 넣으면
        # 그게 곧 배당 재투자가 된다.
        trading_assets = cash + shares * price
        total = trading_assets + dividend_cash
        equity[t] = total
        exposure[t] = (shares * price / total) if total > 0 else 0.0
        current_overlay_value = sum(lot["qty"] * price for lot in overlay_lots)
        overlay_exposure[t] = current_overlay_value / total if total > 0 else 0.0
        overlay_lot_counts[t] = len(overlay_lots)
        open_lot_counts[t] = len(open_lots)
        contributed_arr[t] = contributed
        dividend_arr[t] = dividend_cash
        prev_total_assets = trading_assets

        log_rows.append(
            {
                "날짜": dates[t],
                "종가": round(price, 4),
                "입출금": round(applied_flows[t], 2) if abs(applied_flows[t]) > 1e-9 else None,
                "매수금액": round(buy_amt_today, 2) if buy_amt_today else None,
                "매수수량": round(buy_qty_today) if buy_qty_today else None,
                # 그날 걸었던 LOC 매수 지정가와 쓸 수 있었던 하루 매수금.
                # 둘이 있어야 '예산을 다 못 썼는지'를 나중에 따져볼 수 있다.
                # 수동 체결 정정일에는 원래 전략의 주문 기준가를 비워
                # 실제 체결가와 전략 계산값이 섞여 보이지 않게 한다.
                "주문기준가": (
                    round(order_px, 2)
                    if buy_qty_today and not actual_fill_used_today else None
                ),
                "매수체결가": (
                    round(buy_fill_price_today, 2)
                    if buy_fill_price_today is not None else None
                ),
                "매수예산": round(buy_budget_today, 2) if buy_qty_today else None,
                "목표가": round(buy_target_today, 2) if buy_target_today else None,
                "매도수량": round(sell_qty_today) if sell_qty_today else None,
                "매도금액": round(sell_amt_today, 2) if sell_amt_today else None,
                "실현손익": round(float(realized_pnl[t]), 2) if sell_qty_today else None,
                "청산사유": "+".join(sorted(set(sell_reasons))) if sell_reasons else None,
                "보유수량": round(shares),
                "보유건수": len(open_lots),
                "평가금": round(shares * price, 2),
                "예수금": round(cash, 2),
                "배당": round(div_today, 2) if div_today else None,
                "누적배당": round(dividend_cash, 2),
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
                # 손실 여부는 **전략 기준가**로 판정한다. 리셋된 물량은 취득단가와
                # 다르므로 이걸 안 실어 보내면 앱이 취득단가로 판정하게 되고,
                # 백테스트와 앱이 서로 다른 답을 낸다.
                "strategy_basis_price": float(l.strategy_basis_price),
                "origin": l.origin,
            }
            for l in open_lots
        ],
        final_cash=float(cash),
        max_open_lots=int(open_lot_counts.max()),
        avg_open_lots=float(open_lot_counts.mean()),
        cash_exhausted_days=cash_exhausted,
        buy_range_skips=range_skips,
        loss_reset_days=reset_days,
        loss_reset_kept_qty=reset_kept_qty,
        overlay_entries=overlay_entries,
        overlay_target_exits=overlay_target_exits,
        overlay_expired_exits=overlay_expired_exits,
        overlay_stop_exits=overlay_stop_exits,
        overlay_events=overlay_events,
        max_overlay_exposure_pct=float(overlay_exposure.max()) * 100,
        avg_overlay_exposure_pct=float(overlay_exposure.mean()) * 100,
        max_overlay_lots=int(overlay_lot_counts.max()),
        total_realized_tax=total_realized_tax,
        final_value=float(equity_s.iloc[-1]),
        twr_curve=twr_s,
        contributed_curve=contributed_s,
        total_dividends=float(dividend_cash),
        dividend_curve=pd.Series(dividend_arr, index=dates, name="dividend"),
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
    dividends=None,
) -> pd.Series:
    """'존버' 비교용 — 첫날 전액 매수하고 그냥 들고 있는다.

    같은 조건에서 비교해야 의미가 있으므로 입출금도 똑같이 반영한다.
    입금하면 그날 종가로 더 사고, 출금하면 그날 종가로 그만큼 판다.
    배당도 전략 쪽과 같은 규칙으로 받는다 — 따로 쌓아두고 재투자하지 않는다.
    (존버는 계속 들고 있으니 배당을 다 받는다. 안 넣으면 비교가 불공정해진다)
    """
    df = clean_prices(df)
    close = df["Close"].astype(float).values
    dates = df.index
    n = len(close)
    flows, _ = _resolve_flows(cash_flows, dates)
    div_by_idx = _resolve_dividends(dividends, dates)

    cash = float(initial_cash)
    shares = 0.0
    dividend_cash = 0.0
    values = np.zeros(n)

    for t in range(n):
        price = close[t]
        dividend_cash += div_by_idx[t] * shares if div_by_idx[t] > 0 else 0.0
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
            if not np.isfinite(qty) or qty < 0:
                qty = 0.0
            if whole_shares:
                qty = float(int(qty))
            if qty > 0:
                cash -= qty * price * (1 + fee_rate)
                shares += qty

        values[t] = cash + shares * price + dividend_cash

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
