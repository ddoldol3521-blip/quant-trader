"""SOXL 퀀트믹스 보조지표형 전략 비교.

모든 신호는 전일 확정 데이터만 사용한다. 매수는 LOC 조건부 종가 체결,
목표 매도는 LOC 종가 체결, 기간 종료는 MOC 종가 체결로 모사한다.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


ROOT = Path(__file__).resolve().parent.parent
FEE = 0.001


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def load_data() -> pd.DataFrame:
    soxl = pd.read_pickle(ROOT / ".soxl_cache.pkl")[["Close"]].rename(columns={"Close": "SOXL"})
    raw = yf.download("SOXX", start="2010-01-01", end="2026-08-12", auto_adjust=False, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    soxx = raw[["Close"]].rename(columns={"Close": "SOXX"})
    soxx.index = pd.to_datetime(soxx.index).tz_localize(None)
    soxx["soxx_rsi14"] = rsi(soxx["SOXX"], 14)
    soxx["soxx_ma200"] = soxx["SOXX"].rolling(200).mean()
    soxx["soxx_vol20"] = soxx["SOXX"].pct_change().rolling(20).std() * math.sqrt(252)
    df = soxl.join(soxx, how="inner").dropna(subset=["SOXL", "SOXX", "soxx_rsi14", "soxx_ma200", "soxx_vol20"])
    df["soxl_rsi14"] = rsi(df["SOXL"], 14)
    df["soxl_rsi14"] = df["soxl_rsi14"].fillna(50.0)
    return df


def allocation(name: str, row) -> tuple[float, float]:
    """전일 지표 -> (오늘 신규매수 비율, 신규 물량 목표수익률)."""
    r = row.soxx_rsi14
    sr = row.soxl_rsi14
    above = row.SOXX >= row.soxx_ma200
    vol = row.soxx_vol20
    daily, target = 0.10, 0.027

    if name == "RSI_SOXX_강약":
        daily = 0.12 if r <= 30 else 0.10 if r <= 55 else 0.07 if r <= 70 else 0.03
    elif name == "RSI_SOXX_완만":
        daily = 0.11 if r <= 30 else 0.10 if r <= 55 else 0.08 if r <= 70 else 0.05
    elif name == "RSI_SOXX_과열만축소":
        daily = 0.10 if r <= 70 else 0.05
    elif name == "RSI_SOXL_완만":
        daily = 0.11 if sr <= 30 else 0.10 if sr <= 55 else 0.08 if sr <= 70 else 0.05
    elif name == "MA200_방어":
        daily = 0.10 if above else 0.06
    elif name == "MA200_완만":
        daily = 0.10 if above else 0.08
    elif name == "RSI_MA200_추천":
        if above:
            daily = 0.12 if r <= 30 else 0.10 if r <= 70 else 0.05
        else:
            daily = 0.08 if r <= 30 else 0.06
    elif name == "RSI_MA200_보수":
        if above:
            daily = 0.11 if r <= 30 else 0.09 if r <= 70 else 0.05
        else:
            daily = 0.07 if r <= 30 else 0.05
    elif name == "변동성20_조절":
        daily = 0.06 if vol >= 0.40 else 0.08 if vol >= 0.30 else 0.10
    elif name == "변동성20_완만":
        daily = 0.08 if vol >= 0.40 else 0.09 if vol >= 0.30 else 0.10
    elif name == "RSI_변동성_결합":
        base = 0.11 if r <= 30 else 0.10 if r <= 60 else 0.07
        daily = min(base, 0.06 if vol >= 0.40 else 0.08 if vol >= 0.30 else 0.10)
    elif name == "RSI_목표수익_조절":
        target = 0.04 if r <= 30 else 0.027 if r <= 70 else 0.023
    elif name == "RSI_MA_목표수익_결합":
        daily = (0.11 if r <= 30 else 0.10 if r <= 70 else 0.06) if above else 0.06
        target = 0.035 if r <= 30 else 0.027 if r <= 70 else 0.023
    return daily, target


def simulate(df: pd.DataFrame, name: str, target_params=None):
    prices = df["SOXL"].to_numpy(float)
    cash = assets = peak = 10_000.0
    lots = []
    mdd = 0.0
    for t, price in enumerate(prices):
        prev = prices[t - 1] if t else price
        signal_row = df.iloc[t - 1] if t else df.iloc[t]
        daily, today_target = allocation(name, signal_row)
        if target_params is not None:
            low_cut, high_cut, low_target, high_target = target_params
            today_target = low_target if signal_row.soxx_rsi14 <= low_cut else 0.027 if signal_row.soxx_rsi14 <= high_cut else high_target
        original = list(lots)
        had_sellable = any(t - lot[0] >= 1 for lot in original)
        targets = [lot[3] for lot in original if 1 <= t - lot[0] < 16]
        order_price = round(min(targets) - 0.01, 2) if targets else prev
        forced = [lot for lot in original if t - lot[0] >= 16]
        winners = [lot for lot in original if 1 <= t - lot[0] < 16 and price >= lot[3]]
        removed = {id(lot) for lot in forced + winners}
        lots = [lot for lot in original if id(lot) not in removed]

        reset = bool(forced and t and all(prev <= lot[1] * 0.925 for lot in forced))
        kept = 0
        if reset:
            forced_qty = sum(lot[2] for lot in forced)
            kept = min(forced_qty, max(0, math.floor(assets * 0.095 * (1 - FEE) / prev)))
            cash += (forced_qty - kept) * price * (1 - FEE)
        else:
            cash += sum(lot[2] for lot in forced) * price * (1 - FEE)
        cash += sum(lot[2] for lot in winners) * price * (1 - FEE)
        if kept:
            lots.append([t, price, kept, price * (1 + today_target) * (1 + 2 * FEE)])

        buy = not (forced or winners)
        # 앱의 LOC 신규매수 상한: 전일 종가 × (1 + 오늘 적용 목표수익률).
        if buy and t and price > prev * (1 + today_target):
            buy = False
        if buy and t and not had_sellable and price > prev * 1.10:
            buy = False
        if buy:
            amount = min(assets * daily, cash)
            qty = math.floor(amount * (1 - FEE) / order_price)
            if qty:
                cost = qty * price * (1 + FEE)
                if cost > cash:
                    qty = math.floor(cash / (price * (1 + FEE)))
                    cost = qty * price * (1 + FEE)
                if qty:
                    cash -= cost
                    lots.append([t, price, qty, price * (1 + today_target) * (1 + 2 * FEE)])
        assets = cash + sum(lot[2] * price for lot in lots)
        peak = max(peak, assets)
        mdd = min(mdd, assets / peak - 1)
    return (assets / 10_000) ** (252 / len(df)) * 100 - 100, mdd * 100, assets


def main():
    df = load_data()
    names = [
        "현재_균형형", "RSI_SOXX_강약", "RSI_SOXX_완만", "RSI_SOXX_과열만축소",
        "RSI_SOXL_완만", "MA200_방어", "MA200_완만", "RSI_MA200_추천",
        "RSI_MA200_보수", "변동성20_조절", "변동성20_완만", "RSI_변동성_결합",
        "RSI_목표수익_조절", "RSI_MA_목표수익_결합",
    ]
    periods = {
        "2011_2020": df.loc[:"2020-12-31"],
        "2021_2026": df.loc["2021-01-01":],
        "전체": df,
    }
    rows = []
    for name in names:
        row = {"전략": name}
        for label, part in periods.items():
            cagr, mdd, final = simulate(part, name)
            row[f"{label}_CAGR"] = cagr
            row[f"{label}_MDD"] = mdd
            if label == "전체":
                row["최종자산"] = final
        row["안정점수"] = (
            min(row["2011_2020_CAGR"], row["2021_2026_CAGR"])
            - 0.35 * max(abs(row["2011_2020_MDD"]), abs(row["2021_2026_MDD"]))
            - 0.35 * abs(row["2011_2020_CAGR"] - row["2021_2026_CAGR"])
        )
        rows.append(row)
        print(name.encode("unicode_escape").decode(), "done", flush=True)
    out = pd.DataFrame(rows).sort_values("안정점수", ascending=False)
    out.to_csv(ROOT / "research_indicator_strategies.csv", index=False, encoding="utf-8-sig")
    for _, r in out.iterrows():
        print(
            r["전략"].encode("unicode_escape").decode(),
            f"train={r['2011_2020_CAGR']:.2f}/{r['2011_2020_MDD']:.2f}",
            f"valid={r['2021_2026_CAGR']:.2f}/{r['2021_2026_MDD']:.2f}",
            f"full={r['전체_CAGR']:.2f}/{r['전체_MDD']:.2f}",
            f"score={r['안정점수']:.2f}",
        )

    sensitivity = []
    for low_cut in (25, 30, 35):
        for high_cut in (65, 70, 75):
            for low_target in (0.032, 0.035, 0.040, 0.045):
                for high_target in (0.020, 0.023, 0.025, 0.027):
                    params = (low_cut, high_cut, low_target, high_target)
                    tr = simulate(periods["2011_2020"], "현재_균형형", params)
                    va = simulate(periods["2021_2026"], "현재_균형형", params)
                    fu = simulate(periods["전체"], "현재_균형형", params)
                    score = min(tr[0], va[0]) - 0.35 * max(abs(tr[1]), abs(va[1])) - 0.35 * abs(tr[0] - va[0])
                    sensitivity.append({
                        "RSI_저점": low_cut, "RSI_고점": high_cut,
                        "과매도_목표": low_target, "과열_목표": high_target,
                        "학습_CAGR": tr[0], "학습_MDD": tr[1],
                        "검증_CAGR": va[0], "검증_MDD": va[1],
                        "전체_CAGR": fu[0], "전체_MDD": fu[1], "안정점수": score,
                    })
    sens = pd.DataFrame(sensitivity).sort_values("안정점수", ascending=False)
    sens.to_csv(ROOT / "research_rsi_target_sensitivity.csv", index=False, encoding="utf-8-sig")
    print("SENSITIVITY_TOP", flush=True)
    print(sens.head(15).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
