"""시장+전략 지정 시 데이터 fetch부터 포트폴리오 백테스트까지 한번에 처리하는 래퍼."""

from src import markets as market_api
from src.backtest.portfolio import run_portfolio_backtest
from src.data.kr_data import fetch_universe_data
from src.screening import get_multi_market_universe
from src.strategies import STRATEGIES


def run_universe_portfolio_backtest(
    sub_markets: list,
    strategy_name: str,
    start: str,
    end: str,
    limit: int = 100,
    initial_cash: float = 10_000_000,
    position_size_pct: float = 0.2,
    max_positions: int = 5,
    stop_loss_pct: float = None,
    take_profit_pct: float = None,
    show_progress: bool = True,
    region: str = None,
):
    """여러 종목 유니버스에 대해 전략 하나로 포트폴리오 백테스트를 돌린다.

    반환: (PortfolioResult, {종목코드: 종목명})
    """
    universe = get_multi_market_universe(sub_markets, limit, region or market_api.KR)
    data_by_code = fetch_universe_data(universe, start, end, show_progress=show_progress)

    module = STRATEGIES[strategy_name]
    signals_by_code = {}
    code_to_name = {}
    for row in universe.itertuples():
        df = data_by_code.get(row.Code)
        if df is None or len(df) < 30:
            continue
        try:
            sig_df = module.generate_signals(df, **module.DEFAULT_PARAMS)
        except Exception:
            continue
        signals_by_code[row.Code] = sig_df
        code_to_name[row.Code] = row.Name

    result = run_portfolio_backtest(
        signals_by_code,
        initial_cash=initial_cash,
        position_size_pct=position_size_pct,
        max_positions=max_positions,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    return result, code_to_name
