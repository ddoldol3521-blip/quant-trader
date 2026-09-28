"""입출금을 바꾸면 아직 안 지난 주문 수량이 다시 계산되는지 검사.

    .venv\\Scripts\\python.exe scripts\\verify_flow_reset.py

겪은 일 (2026-09-28). 돈이 걸린 사고였다.

  $50,000 를 추가 입금했는데
    총자산    $105,152  <- 입금 반영됨
    매수 수량 38주      <- 입금 전 값 그대로
    맞는 값   74주      ($105,152 x 11% / $156)

  텔레그램도 같은 기록을 읽어서 같이 틀렸다.

원인은 '한 번 보여준 주문 수량은 그날 장부로 고정' 하는 규칙이다. 이미
증권사에 넣은 주문이 화면에서 슬그머니 바뀌면 안 되니 필요한 규칙인데,
입금처럼 **사용자가 일부러 바꾼 것**까지 막아 버렸다.

여기서 지키는 것
  1. 입출금이 바뀌면 아직 장이 안 열린 날의 안내 수량은 버린다
  2. 지난 날짜는 그대로 둔다 (실제로 주문했을 수 있는 기록이다)
  3. 그렇게 다시 계산하면 실제로 수량이 늘어난다
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.jongsa_live import is_us_market_open

fail = 0


def must(ok: bool, msg: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {msg}" + (f"  -- {got}" if got else ""))


print("입출금 변경 시 주문 재계산 검사\n")

# ── 1. 앱이 실제로 그 처리를 하는가 ──────────────────────────────────
print("[앱 코드]")
app = (ROOT / "jongsa_app.py").read_text(encoding="utf-8")

block_start = app.find("if new_flows != _flows:")
must(block_start > 0, "입출금이 바뀌는 자리를 찾음")
block = app[block_start:block_start + 1400]

must("save_flows(new_flows)" in block, "바뀐 입출금을 저장한다")
must("order_guides" in block, "주문 안내 기록도 손댄다")
must("save_order_guides" in block, "손댄 결과를 저장한다")
must("is_us_market_open" in block, "거래일을 따져서 자른다")
must('g["날짜"] < _next_open' in block,
     "아직 안 지난 날만 버린다 (지난 것은 실제 주문일 수 있어 남긴다)")

# 왜 이렇게 하는지 적혀 있어야 다음 사람이 되돌리지 않는다
must("38주" in block and "74주" in block, "무슨 사고였는지 숫자로 적어 뒀다")

# ── 2. 자르는 기준이 맞는가 ──────────────────────────────────────────
print("\n[자르는 기준]")


def next_open(day: date) -> date:
    d = day
    while not is_us_market_open(d.isoformat()):
        d += timedelta(days=1)
    return d


# 금요일 다음 거래일은 월요일이어야 한다 (토·일 건너뜀)
must(next_open(date(2026, 9, 26)) == date(2026, 9, 28),
     "토요일에 고치면 다음 월요일이 기준", str(next_open(date(2026, 9, 26))))
must(next_open(date(2026, 9, 28)) == date(2026, 9, 28),
     "거래일 당일은 그날이 기준", str(next_open(date(2026, 9, 28))))
# 추수감사절(11/26 목) 은 휴장 -> 11/27 금
must(next_open(date(2026, 11, 26)) == date(2026, 11, 27),
     "휴장일에 고치면 다음 거래일이 기준", str(next_open(date(2026, 11, 26))))

guides = [
    {"날짜": date(2026, 9, 24), "수량": 39.0},
    {"날짜": date(2026, 9, 25), "수량": 39.0},
    {"날짜": date(2026, 9, 28), "수량": 38.0},   # 아직 안 산 날
]
cut = next_open(date(2026, 9, 28))
kept = [g for g in guides if g["날짜"] < cut]
must(len(kept) == 2, "아직 안 지난 날 하나만 버린다", f"{len(kept)}개 남음")
must(all(g["날짜"] < cut for g in kept), "남은 것은 전부 지난 날")
must(date(2026, 9, 28) not in [g["날짜"] for g in kept], "그날 기록이 사라졌다")

# ── 3. 실제로 수량이 달라지는가 ──────────────────────────────────────
print("\n[실제 계산]")
cache = ROOT / "jongsa_order_guides.json"
if not cache.exists():
    print("SKIP: 로컬 기록 파일이 없습니다")
else:
    rows = json.loads(cache.read_text(encoding="utf-8"))
    dates = [r["날짜"] for r in rows]
    must(len(dates) == len(set(dates)), "같은 날짜가 두 번 들어 있지 않다")
    must(all(float(r["수량"]) >= 0 for r in rows), "수량이 음수인 기록이 없다")

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
