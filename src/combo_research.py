"""전략 조합 리서치 — '어떤 조합으로 사서 며칠 뒤에 팔면 좋았나'를 데이터로 찾는다.

접근 방식(이벤트 스터디):
전체 백테스트를 돌리는 대신, '매수 신호가 뜬 날'을 전부 찾아서 그 이후 N일 수익률을
측정한다. 이러면 '며칠 들고 있는 게 최적인가'를 직접 비교할 수 있다.

가장 중요한 장치는 기준선(baseline)이다. 신호 없이 아무 날에나 샀을 때의 수익률을
같이 계산해서, 신호가 진짜 우위가 있는지 아니면 그냥 시장이 올라서 그런 건지 구분한다.
이게 없으면 상승장에서는 어떤 신호든 좋아 보인다.
"""

import itertools

import numpy as np
import pandas as pd

from src.strategies import STRATEGIES

# 보유기간 후보 (거래일 기준) — 약 1주 / 2주 / 1개월 / 2개월 / 3개월
HOLD_DAYS = (5, 10, 20, 40, 60)

MIN_SAMPLES = 100  # 이보다 표본이 적으면 우연일 가능성이 커서 채택하지 않는다


def _buy_events(df: pd.DataFrame, strategy_names: list) -> pd.Series:
    """각 전략의 '오늘 막 매수 신호가 뜬 날'을 불리언 시리즈로 모아 AND 결합한다.

    조합이란 '두 전략이 같은 날 동시에 매수 신호'를 뜻한다.
    """
    flags = None
    for name in strategy_names:
        module = STRATEGIES[name]
        try:
            sig = module.generate_signals(df, **module.DEFAULT_PARAMS)
        except Exception:
            return pd.Series(False, index=df.index)
        pos = sig["position"]
        entry = (pos == 1) & (pos.shift(1) == 0)
        flags = entry if flags is None else (flags & entry)
    return flags if flags is not None else pd.Series(False, index=df.index)


def _forward_returns(close: pd.Series, idx_positions: np.ndarray, hold: int) -> np.ndarray:
    """진입 지점들에서 hold일 뒤 수익률을 계산한다."""
    valid = idx_positions[idx_positions + hold < len(close)]
    if len(valid) == 0:
        return np.array([])
    entry = close.values[valid]
    exit_ = close.values[valid + hold]
    return exit_ / entry - 1


def analyze_combos(
    data_by_code: dict,
    strategy_names: list = None,
    max_combo_size: int = 2,
    hold_days=HOLD_DAYS,
) -> pd.DataFrame:
    """모든 단일 전략 + 조합에 대해, 보유기간별 성과를 집계한다.

    data_by_code: {종목코드: OHLCV DataFrame}
    반환: 각 행이 (조합, 보유기간, 표본수, 평균수익률, 중앙값, 승률)인 DataFrame
    """
    strategy_names = strategy_names or list(STRATEGIES.keys())

    combos = [(s,) for s in strategy_names]
    if max_combo_size >= 2:
        combos += list(itertools.combinations(strategy_names, 2))

    # 조합 -> 보유기간 -> 수익률 리스트
    buckets = {c: {h: [] for h in hold_days} for c in combos}

    for code, df in data_by_code.items():
        if df is None or len(df) < max(hold_days) + 60:
            continue
        close = df["Close"]

        # 전략별 진입 시점을 한 번만 계산해두고 조합에서 재사용한다
        entries = {}
        for name in strategy_names:
            module = STRATEGIES[name]
            try:
                sig = module.generate_signals(df, **module.DEFAULT_PARAMS)
            except Exception:
                entries[name] = None
                continue
            pos = sig["position"]
            entries[name] = ((pos == 1) & (pos.shift(1) == 0)).values

        for combo in combos:
            flags = None
            ok = True
            for name in combo:
                e = entries.get(name)
                if e is None:
                    ok = False
                    break
                flags = e if flags is None else (flags & e)
            if not ok or flags is None or not flags.any():
                continue

            positions = np.flatnonzero(flags)
            for h in hold_days:
                rets = _forward_returns(close, positions, h)
                if len(rets):
                    buckets[combo][h].append(rets)

    rows = []
    for combo, by_hold in buckets.items():
        for h, chunks in by_hold.items():
            if not chunks:
                continue
            rets = np.concatenate(chunks)
            if len(rets) == 0:
                continue
            rows.append(
                {
                    "조합": " + ".join(combo),
                    "전략수": len(combo),
                    "보유일": h,
                    "표본수": len(rets),
                    "평균수익률(%)": round(float(rets.mean()) * 100, 3),
                    "중앙값(%)": round(float(np.median(rets)) * 100, 3),
                    "승률(%)": round(float((rets > 0).mean()) * 100, 2),
                }
            )

    return pd.DataFrame(rows)


def compute_baseline(data_by_code: dict, hold_days=HOLD_DAYS) -> pd.DataFrame:
    """신호와 무관하게 '아무 날에나' 샀을 때의 성과. 비교 기준선.

    이게 없으면 상승장에서는 어떤 신호든 좋아 보인다.
    """
    buckets = {h: [] for h in hold_days}

    for code, df in data_by_code.items():
        if df is None or len(df) < max(hold_days) + 60:
            continue
        close = df["Close"]
        n = len(close)
        for h in hold_days:
            if n <= h:
                continue
            entry = close.values[: n - h]
            exit_ = close.values[h:]
            buckets[h].append(exit_ / entry - 1)

    rows = []
    for h, chunks in buckets.items():
        if not chunks:
            continue
        rets = np.concatenate(chunks)
        rows.append(
            {
                "보유일": h,
                "기준_표본수": len(rets),
                "기준_평균수익률(%)": round(float(rets.mean()) * 100, 3),
                "기준_중앙값(%)": round(float(np.median(rets)) * 100, 3),
                "기준_승률(%)": round(float((rets > 0).mean()) * 100, 2),
            }
        )
    return pd.DataFrame(rows)


def add_edge(result: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    """기준선 대비 초과성과(edge)를 붙인다. 이 값이 양수여야 신호에 의미가 있다."""
    if result.empty or baseline.empty:
        return result
    merged = result.merge(baseline, on="보유일", how="left")
    merged["초과수익(%p)"] = (merged["평균수익률(%)"] - merged["기준_평균수익률(%)"]).round(3)
    merged["초과승률(%p)"] = (merged["승률(%)"] - merged["기준_승률(%)"]).round(2)
    return merged


def screen_playbook(
    merged: pd.DataFrame,
    min_samples: int = MIN_SAMPLES,
    min_edge_pct: float = 0.5,
    min_edge_winrate: float = 2.0,
) -> pd.DataFrame:
    """세 관문을 통과한 조합만 남긴다.

    1. 표본수가 충분한가
    2. 기준선보다 수익률이 나은가
    3. 기준선보다 승률이 나은가

    이걸 통과해도 '미래에 통한다'는 보장은 없다. 여러 기간에서 반복되는지는
    별도로(compare_across_periods) 확인해야 한다.
    """
    if merged.empty:
        return merged
    passed = merged[
        (merged["표본수"] >= min_samples)
        & (merged["초과수익(%p)"] >= min_edge_pct)
        & (merged["초과승률(%p)"] >= min_edge_winrate)
    ].copy()
    return passed.sort_values("초과수익(%p)", ascending=False).reset_index(drop=True)


def consistency_across_periods(period_results: dict, min_periods: int = 2) -> pd.DataFrame:
    """여러 기간 구간에서 반복적으로 통과한 조합만 골라낸다.

    period_results: {기간라벨: screen_playbook을 통과한 DataFrame}
    한 시기에만 좋았던 건 그 시기 운이었을 가능성이 크므로 걸러낸다.
    """
    counts = {}
    for label, df in period_results.items():
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            key = (row["조합"], int(row["보유일"]))
            rec = counts.setdefault(
                key, {"통과기간": [], "초과수익들": [], "초과승률들": [], "표본들": []}
            )
            rec["통과기간"].append(label)
            rec["초과수익들"].append(row["초과수익(%p)"])
            rec["초과승률들"].append(row["초과승률(%p)"])
            rec["표본들"].append(row["표본수"])

    rows = []
    for (combo, hold), rec in counts.items():
        if len(rec["통과기간"]) < min_periods:
            continue
        rows.append(
            {
                "조합": combo,
                "보유일": hold,
                "통과기간수": len(rec["통과기간"]),
                "통과기간": ", ".join(rec["통과기간"]),
                "평균초과수익(%p)": round(float(np.mean(rec["초과수익들"])), 3),
                "최소초과수익(%p)": round(float(np.min(rec["초과수익들"])), 3),
                "평균초과승률(%p)": round(float(np.mean(rec["초과승률들"])), 2),
                "총표본수": int(np.sum(rec["표본들"])),
            }
        )

    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values(["통과기간수", "평균초과수익(%p)"], ascending=[False, False])
        .reset_index(drop=True)
    )
