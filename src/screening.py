"""여러 시장 x 여러 전략 스캔 공용 로직."""

from datetime import datetime, timedelta

import pandas as pd

from src import markets as market_api
from src.backtest.engine import today_signal
from src.data.kr_data import fetch_universe_data
from src.strategies import STRATEGIES

LOOKBACK_DAYS = 400  # 지표 계산(최대 252일 롤링)에 필요한 과거 데이터 확보용 여유분


def get_multi_market_universe(sub_markets: list, limit: int, region: str = market_api.KR) -> pd.DataFrame:
    """여러 하위 시장의 종목 목록을 합쳐서 가져온다 (중복 종목 제거)."""
    return market_api.get_multi_universe(region, sub_markets, limit)


def scan(
    sub_markets: list,
    strategy_names: list,
    limit: int = 100,
    params_override: dict = None,
    show_progress: bool = True,
    region: str = market_api.KR,
) -> list:
    """여러 시장 x 여러 전략을 스캔해서 종목별 오늘 신호를 계산한다.

    종목당 시세 데이터는 한 번만 받아오고, 그 데이터로 전략들을 모두 계산해 API 호출을 아낀다.
    반환: [{"code", "name", "signals": {전략이름: "BUY"/"HOLD"/"SELL"/"NONE"/"오류"}}, ...]
    """
    params_override = params_override or {}
    universe = get_multi_market_universe(sub_markets, limit, region)
    end = datetime.today()
    start = end - timedelta(days=LOOKBACK_DAYS)

    data_by_code = fetch_universe_data(
        universe, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), show_progress=show_progress
    )

    results = []
    for row in universe.itertuples():
        code, name = row.Code, row.Name
        df = data_by_code.get(code)
        if df is None or len(df) < 30:
            continue

        signals = {}
        for strat_name in strategy_names:
            module = STRATEGIES[strat_name]
            params = {**module.DEFAULT_PARAMS, **params_override.get(strat_name, {})}
            try:
                sig_df = module.generate_signals(df, **params)
                signals[strat_name] = today_signal(sig_df)
            except Exception:
                signals[strat_name] = "오류"

        results.append({"code": code, "name": name, "signals": signals})

    return results
