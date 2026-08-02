"""전략 일치 개수별로 실제 과거 수익률이 어땠는지 검증하는 로직.

'여러 전략이 동시에 매수하라고 하면 더 좋다'는 감을, 과거 데이터로 확인하기 위한 것.
비교 기준선(아무 날에나 샀을 때)도 같이 계산해서 착시를 막는다.
"""

import pandas as pd

from src import markets as market_api
from src.data.kr_data import fetch_universe_data
from src.screening import get_multi_market_universe
from src.strategies import STRATEGIES

DEFAULT_FORWARD_DAYS = (5, 10, 20)


def collect_agreement_events(
    sub_markets: list,
    strategy_names: list,
    start: str,
    end: str,
    forward_days=DEFAULT_FORWARD_DAYS,
    limit: int = 100,
    show_progress: bool = True,
    region: str = None,
) -> pd.DataFrame:
    """과거 전체 기간에서 '그날 막 매수 신호가 뜬' 날들을 모두 찾아서,
    그날 몇 개 전략이 동시에 일치했는지와 이후 N일 수익률을 계산한다.
    """
    universe = get_multi_market_universe(sub_markets, limit, region or market_api.KR)
    data_by_code = fetch_universe_data(universe, start, end, show_progress=show_progress)
    records = []

    for row in universe.itertuples():
        code, name = row.Code, row.Name
        df = data_by_code.get(code)
        if df is None or len(df) < max(forward_days) + 30:
            continue

        buy_flags = pd.DataFrame(index=df.index)
        for strat_name in strategy_names:
            module = STRATEGIES[strat_name]
            try:
                sig_df = module.generate_signals(df, **module.DEFAULT_PARAMS)
            except Exception:
                continue
            position = sig_df["position"]
            buy_flags[strat_name] = (position == 1) & (position.shift(1) == 0)

        if buy_flags.empty:
            continue

        agreement = buy_flags.sum(axis=1)
        close = df["Close"]

        for t in range(len(df)):
            count = int(agreement.iloc[t])
            if count == 0:
                continue
            if t + max(forward_days) >= len(df):
                continue
            record = {"code": code, "name": name, "date": df.index[t], "agreement": count}
            for fd in forward_days:
                record[f"fwd_{fd}d"] = close.iloc[t + fd] / close.iloc[t] - 1
            records.append(record)

    return pd.DataFrame(records)


def compute_baseline(
    sub_markets: list,
    start: str,
    end: str,
    forward_days=DEFAULT_FORWARD_DAYS,
    limit: int = 100,
    show_progress: bool = True,
    region: str = None,
) -> dict:
    """비교 기준선: 신호와 상관없이 '아무 날에나' 샀을 때의 평균수익률/승률.

    이게 없으면 '승률 52%'가 좋아 보이지만, 사실 아무 날에나 사도 52%였을 수 있다.
    """
    universe = get_multi_market_universe(sub_markets, limit, region or market_api.KR)
    data_by_code = fetch_universe_data(universe, start, end, show_progress=show_progress)

    rows = []
    for row in universe.itertuples():
        df = data_by_code.get(row.Code)
        if df is None or len(df) < max(forward_days) + 5:
            continue
        close = df["Close"]
        for t in range(len(df) - max(forward_days)):
            rec = {}
            for fd in forward_days:
                rec[f"fwd_{fd}d"] = close.iloc[t + fd] / close.iloc[t] - 1
            rows.append(rec)

    if not rows:
        return {}

    baseline = pd.DataFrame(rows)
    out = {"샘플수": len(baseline)}
    for fd in forward_days:
        col = f"fwd_{fd}d"
        out[f"{fd}일평균수익률(%)"] = round(baseline[col].mean() * 100, 2)
        out[f"{fd}일승률(%)"] = round((baseline[col] > 0).mean() * 100, 1)
    return out


def summarize_agreement(events: pd.DataFrame, forward_days=DEFAULT_FORWARD_DAYS) -> pd.DataFrame:
    """일치 개수별로 그룹화해서 샘플수, 평균 수익률, 승률을 계산한다."""
    if events.empty:
        return pd.DataFrame()

    rows = []
    for count, group in events.groupby("agreement"):
        row = {"일치개수": count, "샘플수": len(group)}
        for fd in forward_days:
            col = f"fwd_{fd}d"
            row[f"{fd}일평균수익률(%)"] = round(group[col].mean() * 100, 2)
            row[f"{fd}일승률(%)"] = round((group[col] > 0).mean() * 100, 1)
        rows.append(row)

    return pd.DataFrame(rows).sort_values("일치개수").reset_index(drop=True)
