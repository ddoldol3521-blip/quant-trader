"""종목 하나 x 전략 하나에 대해 파라미터 조합을 탐색하는 로직."""

import itertools

import pandas as pd

from src.backtest.engine import run_backtest
from src.data.kr_data import get_kr_ohlcv
from src.strategies import STRATEGIES


def _param_combinations(grid: dict) -> list:
    keys = list(grid.keys())
    combos = itertools.product(*[grid[k] for k in keys])
    return [dict(zip(keys, combo)) for combo in combos]


def optimize_strategy(
    strategy_name: str,
    code: str,
    full_start: str,
    split_date: str,
    full_end: str,
    initial_cash: float = 10_000_000,
    take_profit_pct: float = None,
) -> pd.DataFrame:
    """파라미터 조합을 선별기간 성과로 탐색하고, 검증기간 성과도 같이 계산한다.

    지표 계산은 전체 기간으로 하되(초반 구간 워밍업), 자금 시뮬레이션은 선별/검증 구간을 나눠서
    각각 독립적으로 돌린다. 결과는 선별기간 수익률 순으로 정렬한다.
    """
    module = STRATEGIES[strategy_name]
    grid = getattr(module, "PARAM_GRID", None)
    if not grid:
        raise ValueError(f"'{strategy_name}' 전략은 최적화용 파라미터 범위(PARAM_GRID)가 정의되어 있지 않습니다.")

    df = get_kr_ohlcv(code, full_start, full_end)
    combos = _param_combinations(grid)

    rows = []
    for params in combos:
        try:
            sig_df = module.generate_signals(df, **params)
        except Exception:
            continue

        in_sample = sig_df.loc[:split_date]
        out_sample = sig_df.loc[split_date:]

        row = {"params": str(params)}
        if len(in_sample) >= 30:
            r = run_backtest(in_sample, initial_cash=initial_cash, take_profit_pct=take_profit_pct)
            row["in_return_pct"] = round(r.total_return_pct, 2)
            row["in_mdd_pct"] = round(r.mdd_pct, 2)
        if len(out_sample) >= 30:
            r = run_backtest(out_sample, initial_cash=initial_cash, take_profit_pct=take_profit_pct)
            row["out_return_pct"] = round(r.total_return_pct, 2)
            row["out_mdd_pct"] = round(r.mdd_pct, 2)
        rows.append(row)

    result = pd.DataFrame(rows)
    if "in_return_pct" in result.columns:
        result = result.sort_values("in_return_pct", ascending=False).reset_index(drop=True)
    return result
