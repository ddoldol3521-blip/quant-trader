"""SOXL 퀀트믹스 확장 탐색.

넓은 무작위 조합을 빠른 선별기로 거른 뒤, 상위 후보를 정식 백테스트 엔진으로
재검증한다. 결과는 research_extended_soxl.csv에 저장한다.
"""

from __future__ import annotations

import math
import multiprocessing as mp
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.jongsa_backtest import run_jongsa


DATA = pd.read_pickle(ROOT / ".soxl_cache.pkl")
PRICES = DATA["Close"].astype(float).to_numpy()
DATES = DATA.index
TRAIN = DATA.loc[:"2020-12-31", "Close"].astype(float).to_numpy()
VALID = DATA.loc["2021-01-01":, "Close"].astype(float).to_numpy()
FEE = 0.001
RNG = random.Random(20260811)


def fast_sim(prices, target, stop, daily, retain, threshold, buy_range):
    cash = assets = peak = 10_000.0
    lots = []
    mdd = 0.0
    for t, price in enumerate(prices):
        prev = prices[t - 1] if t else price
        original = list(lots)
        had_sellable = any(t - lot[0] >= 1 for lot in original)
        targets = [lot[3] for lot in original if 1 <= t - lot[0] < stop]
        order_price = round(min(targets) - 0.01, 2) if targets else prev
        forced = [lot for lot in original if t - lot[0] >= stop]
        winners = [lot for lot in original if 1 <= t - lot[0] < stop and price >= lot[3]]
        removed = {id(lot) for lot in forced + winners}
        lots = [lot for lot in original if id(lot) not in removed]

        reset = bool(
            retain > 0 and forced and t
            and all(prev <= lot[1] * (1 - threshold) for lot in forced)
        )
        kept = 0
        if reset:
            forced_qty = sum(lot[2] for lot in forced)
            kept = min(forced_qty, max(0, math.floor(assets * retain * (1 - FEE) / prev)))
            cash += (forced_qty - kept) * price * (1 - FEE)
        else:
            cash += sum(lot[2] for lot in forced) * price * (1 - FEE)
        cash += sum(lot[2] for lot in winners) * price * (1 - FEE)
        if kept:
            lots.append([t, price, kept, price * (1 + target) * (1 + 2 * FEE)])

        buy = not (forced or winners)
        if buy and t and not had_sellable and price > prev * (1 + buy_range):
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
                    lots.append([t, price, qty, price * (1 + target) * (1 + 2 * FEE)])

        assets = cash + sum(lot[2] * price for lot in lots)
        peak = max(peak, assets)
        mdd = min(mdd, assets / peak - 1)
    cagr = (assets / 10_000) ** (252 / len(prices)) * 100 - 100
    return cagr, mdd * 100


def screen_one(cfg):
    target, stop, daily, retain, threshold, buy_range = cfg
    tr_c, tr_m = fast_sim(TRAIN, *cfg)
    va_c, va_m = fast_sim(VALID, *cfg)
    score = min(tr_c, va_c) - 0.35 * max(abs(tr_m), abs(va_m)) - 0.35 * abs(tr_c - va_c)
    return {
        "target": target, "stop": stop, "daily": daily, "retain": retain,
        "threshold": threshold, "buy_range": buy_range,
        "train_cagr": tr_c, "train_mdd": tr_m,
        "valid_cagr": va_c, "valid_mdd": va_m, "score": score,
    }


def main():
    candidates = set()
    while len(candidates) < 800:
        candidates.add((
            RNG.choice([x / 10_000 for x in range(220, 341, 5)]),
            RNG.choice(range(12, 25)),
            RNG.choice([x / 1000 for x in range(70, 121, 5)]),
            RNG.choice([x / 1000 for x in range(0, 111, 5)]),
            RNG.choice([x / 1000 for x in range(0, 151, 25)]),
            RNG.choice([0.05, 0.075, 0.10, 0.125, 0.15, 0.20]),
        ))

    rows = []
    workers = min(6, max(2, mp.cpu_count() - 1))
    with mp.Pool(workers) as pool:
        for i, row in enumerate(pool.imap_unordered(screen_one, candidates, chunksize=25), 1):
            rows.append(row)
            if i % 100 == 0:
                print(f"FAST {i}/800", flush=True)

    ranked = pd.DataFrame(rows).sort_values("score", ascending=False)
    # 빠른 선별기의 상위 40개를 실제 앱과 같은 정식 엔진으로 재검증한다.
    verified = []
    for rank, row in ranked.head(40).iterrows():
        kw = dict(
            version="V5", initial_cash=10_000, target_return=row.target,
            daily_buy_pct=row.daily, stop_days=int(row.stop), fee_rate=FEE,
            whole_shares=True, loc_buy_limit=True, fee_in_target=True,
            sell_day_buy_mode="never", reinvest=True, buy_range_pct=row.buy_range,
            ladder_rungs=0, loss_reset_pct=row.retain,
            loss_reset_threshold_pct=row.threshold,
        )
        tr = run_jongsa(DATA.loc[:"2020-12-31"], **kw)
        va = run_jongsa(DATA.loc["2021-01-01":], **kw)
        full = run_jongsa(DATA, **kw)
        verified.append({
            **{k: row[k] for k in ("target", "stop", "daily", "retain", "threshold", "buy_range")},
            "train_cagr": tr.cagr_pct, "train_mdd": tr.mdd_pct,
            "valid_cagr": va.cagr_pct, "valid_mdd": va.mdd_pct,
            "full_cagr": full.cagr_pct, "full_mdd": full.mdd_pct,
            "final_value": full.final_value,
            "robust_score": min(tr.cagr_pct, va.cagr_pct)
                            - 0.35 * max(abs(tr.mdd_pct), abs(va.mdd_pct))
                            - 0.35 * abs(tr.cagr_pct - va.cagr_pct),
        })
        print(f"VERIFY {len(verified)}/40", flush=True)

    out = pd.DataFrame(verified).sort_values("robust_score", ascending=False)
    out.to_csv(ROOT / "research_extended_soxl.csv", index=False, encoding="utf-8-sig")
    print(out.head(15).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
