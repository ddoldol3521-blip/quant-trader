"""전략별 선별기간/검증기간 성과 비교 로직.

선별기간에서 좋았던 전략이 검증기간에서도 상위권인지 봐야 과최적화를 걸러낼 수 있다.
"""

import pandas as pd

from src import markets as market_api
from src.backtest.engine import run_backtest
from src.data.kr_data import fetch_universe_data
from src.screening import get_multi_market_universe
from src.strategies import STRATEGIES


def compare_strategies(
    sub_markets: list,
    strategy_names: list,
    full_start: str,
    split_date: str,
    full_end: str,
    limit: int = 100,
    initial_cash: float = 10_000_000,
    show_progress: bool = True,
    region: str = None,
) -> pd.DataFrame:
    """여러 전략 x 여러 종목을 백테스트해서, split_date 기준으로 선별기간/검증기간 성과를 각각 계산한다.

    종목당 시세 데이터는 전체 기간을 한 번만 받아오고, split_date로 나눠서 각 구간을 독립적으로
    백테스트한다 (지표 계산은 전체 기간으로 하되, 자금 시뮬레이션은 구간별로 새로 시작).
    """
    region = region or market_api.KR
    universe = get_multi_market_universe(sub_markets, limit, region)
    data_by_code = fetch_universe_data(universe, full_start, full_end, show_progress=show_progress)
    records = []

    for row in universe.itertuples():
        code, name = row.Code, row.Name
        df = data_by_code.get(code)
        if df is None or len(df) < 60:
            continue

        for strat_name in strategy_names:
            module = STRATEGIES[strat_name]
            try:
                sig_df = module.generate_signals(df, **module.DEFAULT_PARAMS)
            except Exception:
                continue

            in_sample = sig_df.loc[:split_date]
            out_sample = sig_df.loc[split_date:]

            record = {"strategy": strat_name, "code": code, "name": name}
            if len(in_sample) >= 30:
                r = run_backtest(in_sample, initial_cash=initial_cash)
                record["in_return_pct"] = r.total_return_pct
                record["in_mdd_pct"] = r.mdd_pct
                record["in_win_rate_pct"] = r.win_rate_pct
                record["in_num_trades"] = r.num_trades
            if len(out_sample) >= 30:
                r = run_backtest(out_sample, initial_cash=initial_cash)
                record["out_return_pct"] = r.total_return_pct
                record["out_mdd_pct"] = r.mdd_pct
                record["out_win_rate_pct"] = r.win_rate_pct
                record["out_num_trades"] = r.num_trades

            records.append(record)

    return pd.DataFrame(records)


def summarize_comparison(records: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """prefix가 'in' 또는 'out'인 쪽 성과를 전략별로 집계해서 평균수익률 순으로 정렬한다."""
    col_return = f"{prefix}_return_pct"
    if records.empty or col_return not in records.columns:
        return pd.DataFrame()

    valid = records.dropna(subset=[col_return])
    if valid.empty:
        return pd.DataFrame()

    rows = []
    for strat, group in valid.groupby("strategy"):
        rows.append(
            {
                "전략": strat,
                "종목수": len(group),
                "평균수익률(%)": round(group[col_return].mean(), 2),
                "수익률중앙값(%)": round(group[col_return].median(), 2),
                "평균MDD(%)": round(group[f"{prefix}_mdd_pct"].mean(), 2),
                "평균승률(%)": round(group[f"{prefix}_win_rate_pct"].mean(), 1),
                "플러스종목비율(%)": round((group[col_return] > 0).mean() * 100, 1),
            }
        )
    return pd.DataFrame(rows).sort_values("평균수익률(%)", ascending=False).reset_index(drop=True)
