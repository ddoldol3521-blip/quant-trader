"""미국 증시 휴장일 판정이 맞는지 눈으로 확인하는 스크립트.

    .venv\\Scripts\\python.exe scripts\\check_holidays.py

주문은 미국 장이 열리는 날에만 넣을 수 있다. 휴장일을 열린 날로 착각하면
보유일이 하루 부풀어 **하루 일찍 팔라고** 안내한다.
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jongsa_live import is_us_market_open as is_open

CASES = [
    ("2026-11-26", "추수감사절", False),
    ("2026-11-27", "블프(반장)", True),
    ("2026-12-25", "크리스마스", False),
    ("2026-12-24", "크리스마스이브", True),
    ("2026-10-12", "콜럼버스데이", True),   # 연방휴일이지만 증시는 연다
    ("2026-11-11", "재향군인의날", True),   # 위와 같다
    ("2026-10-03", "토요일", False),
    ("2027-01-01", "신정", False),
    ("2027-01-18", "마틴루터킹데이", False),
    ("2027-03-26", "성금요일", False),      # 연방휴일 아니지만 증시는 닫는다
    ("2027-05-31", "메모리얼데이", False),
    ("2027-07-05", "독립기념일 대체", False),
    ("2027-09-06", "노동절", False),
]

bad = 0
print("미국 증시 휴장일 판정\n")
for day, name, want in CASES:
    got = is_open(day)
    ok = got == want
    if not ok:
        bad += 1
    print(f"  {'OK ' if ok else '!! '}{day}  {name:14} "
          f"{'열림' if got else '휴장'}   (기대 {'열림' if want else '휴장'})")

print()
print("전부 맞음" if bad == 0 else f"{bad}개 틀림")
sys.exit(0 if bad == 0 else 1)
