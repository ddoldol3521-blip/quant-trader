"""전략 조합 리서치 재실행 스크립트.

src/playbook.py의 숫자는 이 스크립트를 2026-08-02에 돌린 결과다.
시간이 지나 다시 검증하고 싶으면 이걸 실행하고, 나온 결과로 playbook.py를 갱신하면 된다.

실행: .venv\\Scripts\\python.exe scripts/run_combo_research.py
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import markets as M
from src.combo_research import (
    add_edge,
    analyze_combos,
    compute_baseline,
    consistency_across_periods,
    screen_playbook,
)
from src.data.kr_data import fetch_universe_data

PERIODS = [
    ("P1_2014-2017", "2014-01-01", "2017-12-31"),
    ("P2_2017-2020", "2017-01-01", "2020-12-31"),
    ("P3_2020-2023", "2020-01-01", "2023-12-31"),
    ("P4_2023-현재", "2023-01-01", None),
]


def run_region(label, region, subs, limit, fetch_start, fetch_end):
    print(f"\n{'='*78}\n[{label}] {'+'.join(subs)}, 시장별 {limit}개\n{'='*78}", flush=True)
    uni = M.get_multi_universe(region, subs, limit)
    t0 = time.time()
    data = fetch_universe_data(uni, fetch_start, fetch_end, show_progress=True)
    print(f"  수집 완료: {len(data)}종목 ({time.time()-t0:.0f}초)", flush=True)

    period_passed = {}
    for plabel, pstart, pend in PERIODS:
        sliced = {}
        for code, df in data.items():
            if df is None or df.empty:
                continue
            s = df.loc[pstart : (pend or fetch_end)]
            if len(s) >= 120:
                sliced[code] = s
        if len(sliced) < 20:
            continue
        merged = add_edge(analyze_combos(sliced), compute_baseline(sliced))
        passed = screen_playbook(merged)
        period_passed[plabel] = passed
        print(f"  [{plabel}] 종목 {len(sliced)} / 통과 {len(passed)}", flush=True)

    consistent = consistency_across_periods(period_passed, min_periods=2)
    print(f"\n  >>> 2개 기간 이상 반복 통과: {len(consistent)}개")
    if not consistent.empty:
        print(consistent.head(20).to_string(index=False))
    return consistent


def main():
    parser = argparse.ArgumentParser(description="전략 조합 x 보유기간 대규모 리서치")
    parser.add_argument("--limit", type=int, default=150, help="시장별 종목 수 (기본 150)")
    parser.add_argument("--start", default="2014-01-01", help="데이터 시작일")
    parser.add_argument("--end", default=None, help="데이터 종료일 (기본 오늘)")
    args = parser.parse_args()

    from datetime import datetime

    end = args.end or datetime.today().strftime("%Y-%m-%d")

    run_region("한국", M.KR, ["KOSPI", "KOSDAQ"], args.limit, args.start, end)
    run_region("미국", M.US, ["NASDAQ", "NYSE"], args.limit, args.start, end)

    print("\n주의: 수백 개 조합을 테스트하면 우연히 좋아 보이는 게 반드시 나옵니다.")
    print("표본수·기준선 초과·기간 일관성 3관문을 모두 통과한 것만 신뢰하세요.")


if __name__ == "__main__":
    main()
