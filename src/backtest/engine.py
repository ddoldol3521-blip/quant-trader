from dataclasses import dataclass

import pandas as pd


@dataclass
class Trade:
    date: pd.Timestamp
    action: str  # 'BUY', 'SELL', 'STOP'(손절), 'TP'(익절)
    price: float
    shares: int
    cash_after: float


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list
    total_return_pct: float
    mdd_pct: float
    win_rate_pct: float
    num_trades: int


def run_backtest(
    df: pd.DataFrame,
    initial_cash: float = 10_000_000,
    fee_rate: float = 0.00015,
    stop_loss_pct: float = None,
    take_profit_pct: float = None,
) -> BacktestResult:
    """position 컬럼(0/1)을 보고 하루 단위로 매수/매도를 시뮬레이션한다.

    - position이 0에서 1로 바뀌는 날 종가로 전액 매수
    - position이 1에서 0으로 바뀌는 날 종가로 전량 매도
    - stop_loss_pct가 주어지면, 진입가 대비 그 비율만큼 빠지는 날 전략 신호와 무관하게 강제 매도
      (거래 기록에 'STOP'으로 표시). None이면 손절 없음.
    - take_profit_pct가 주어지면, 진입가 대비 그 비율만큼 오르는 날 미리 이익 실현
      (거래 기록에 'TP'로 표시). None이면 익절 없음.
    - 매매마다 fee_rate만큼 수수료를 뗀다
    - 청산한 그날 바로 다시 사지 않는다(just_exited) — 손절하자마자 재매수하는 걸 막기 위함
    """
    cash = initial_cash
    shares = 0
    holding = 0
    entry_price = 0.0
    trades: list[Trade] = []
    wins = 0
    closed_trades = 0
    equity_values = []

    for date, row in df.iterrows():
        price = row["Close"]
        target = row["position"]
        just_exited = False

        if holding == 1:
            stop_hit = stop_loss_pct is not None and price <= entry_price * (1 - stop_loss_pct)
            tp_hit = take_profit_pct is not None and price >= entry_price * (1 + take_profit_pct)
            if stop_hit or tp_hit or target == 0:
                cash += shares * price * (1 - fee_rate)
                action = "STOP" if stop_hit else ("TP" if tp_hit else "SELL")
                trades.append(Trade(date, action, price, shares, cash))
                closed_trades += 1
                if price > entry_price:
                    wins += 1
                shares = 0
                holding = 0
                just_exited = True

        if target == 1 and holding == 0 and not just_exited:
            shares = int(cash // (price * (1 + fee_rate)))
            if shares > 0:
                cash -= shares * price * (1 + fee_rate)
                holding = 1
                entry_price = price
                trades.append(Trade(date, "BUY", price, shares, cash))

        equity_values.append(cash + shares * price)

    equity_curve = pd.Series(equity_values, index=df.index, name="equity")

    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    mdd_pct = drawdown.min() * 100

    total_return_pct = (equity_curve.iloc[-1] / initial_cash - 1) * 100
    win_rate_pct = (wins / closed_trades * 100) if closed_trades > 0 else 0.0

    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        total_return_pct=total_return_pct,
        mdd_pct=mdd_pct,
        win_rate_pct=win_rate_pct,
        num_trades=closed_trades,
    )


def today_signal(signals: pd.DataFrame) -> str:
    """가장 최근 두 날의 position을 비교해 오늘 상태를 판정한다.

    BUY: 오늘 막 매수 조건 충족 / HOLD: 이미 며칠째 보유 상태
    SELL: 오늘 막 매도 조건 충족 / NONE: 매수 조건 아님
    """
    if len(signals) < 2:
        return "데이터부족"
    today = signals["position"].iloc[-1]
    yesterday = signals["position"].iloc[-2]
    if today == 1 and yesterday == 0:
        return "BUY"
    if today == 0 and yesterday == 1:
        return "SELL"
    if today == 1:
        return "HOLD"
    return "NONE"
