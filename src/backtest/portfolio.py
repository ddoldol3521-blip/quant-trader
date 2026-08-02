from dataclasses import dataclass

import pandas as pd


@dataclass
class PortfolioTrade:
    date: pd.Timestamp
    code: str
    action: str  # 'BUY', 'SELL', 'STOP'(손절), 'TP'(익절)
    price: float
    shares: int
    cash_after: float


@dataclass
class PortfolioResult:
    equity_curve: pd.Series
    trades: list
    total_return_pct: float
    mdd_pct: float
    win_rate_pct: float
    num_trades: int
    num_stocks: int


def run_portfolio_backtest(
    signals_by_code: dict,
    initial_cash: float = 10_000_000,
    position_size_pct: float = 0.2,
    max_positions: int = 5,
    stop_loss_pct: float = None,
    take_profit_pct: float = None,
    fee_rate: float = 0.00015,
) -> PortfolioResult:
    """여러 종목을 동시에 보유할 수 있는 포트폴리오 백테스트.

    signals_by_code: {종목코드: DataFrame(Close, position 컬럼 포함)}
    position_size_pct: 종목 하나에 그 시점 포트폴리오 평가금액의 최대 몇 %까지 넣을지 (0.2 = 20%)
    max_positions: 동시에 보유할 수 있는 최대 종목 수
    stop_loss_pct: 진입가 대비 이 비율만큼 빠지면 강제 매도 (None이면 손절 없음)
    take_profit_pct: 진입가 대비 이 비율만큼 오르면 미리 이익 실현 (None이면 익절 없음)

    같은 날 여러 종목이 동시에 매수 신호를 내면, signals_by_code에 전달된 순서대로(보통 시가총액 순)
    남은 자금과 빈 자리가 허용하는 한 채운다.
    """
    codes = list(signals_by_code.keys())
    close_df = pd.DataFrame({c: signals_by_code[c]["Close"] for c in codes}).sort_index()
    pos_df = pd.DataFrame({c: signals_by_code[c]["position"] for c in codes}).reindex(close_df.index)

    cash = initial_cash
    holdings = {}  # code -> {"shares": int, "entry_price": float}
    trades: list[PortfolioTrade] = []
    equity_values = []
    wins = 0
    closed_trades = 0

    for date in close_df.index:
        prices = close_df.loc[date]
        positions = pos_df.loc[date]
        just_exited = set()

        # 1) 청산 먼저
        for code in list(holdings.keys()):
            price = prices.get(code)
            if pd.isna(price):
                continue
            entry_price = holdings[code]["entry_price"]
            shares = holdings[code]["shares"]
            stop_hit = stop_loss_pct is not None and price <= entry_price * (1 - stop_loss_pct)
            tp_hit = take_profit_pct is not None and price >= entry_price * (1 + take_profit_pct)
            signal_exit = positions.get(code) == 0
            if stop_hit or tp_hit or signal_exit:
                cash += shares * price * (1 - fee_rate)
                action = "STOP" if stop_hit else ("TP" if tp_hit else "SELL")
                trades.append(PortfolioTrade(date, code, action, price, shares, cash))
                closed_trades += 1
                if price > entry_price:
                    wins += 1
                del holdings[code]
                just_exited.add(code)

        holdings_value = sum(
            holdings[c]["shares"] * prices.get(c, 0) for c in holdings if not pd.isna(prices.get(c))
        )
        portfolio_value = cash + holdings_value

        # 2) 신규 진입
        if len(holdings) < max_positions:
            for code in codes:
                if len(holdings) >= max_positions:
                    break
                if code in holdings or code in just_exited:
                    continue
                price = prices.get(code)
                if pd.isna(price) or positions.get(code) != 1:
                    continue
                budget = min(portfolio_value * position_size_pct, cash)
                shares = int(budget // (price * (1 + fee_rate)))
                if shares > 0:
                    cash -= shares * price * (1 + fee_rate)
                    holdings[code] = {"shares": shares, "entry_price": price}
                    trades.append(PortfolioTrade(date, code, "BUY", price, shares, cash))

        holdings_value = sum(
            holdings[c]["shares"] * prices.get(c, 0) for c in holdings if not pd.isna(prices.get(c))
        )
        equity_values.append(cash + holdings_value)

    equity_curve = pd.Series(equity_values, index=close_df.index, name="equity")
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    mdd_pct = drawdown.min() * 100

    total_return_pct = (equity_curve.iloc[-1] / initial_cash - 1) * 100
    win_rate_pct = (wins / closed_trades * 100) if closed_trades > 0 else 0.0

    return PortfolioResult(
        equity_curve=equity_curve,
        trades=trades,
        total_return_pct=total_return_pct,
        mdd_pct=mdd_pct,
        win_rate_pct=win_rate_pct,
        num_trades=closed_trades,
        num_stocks=len(codes),
    )
