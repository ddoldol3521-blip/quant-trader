"""손실 리셋 규칙 검사.

    .venv\\Scripts\\python.exe scripts\\verify_loss_reset.py

손절일이 찬 물량이 **전부 손실**이면 전량 팔지 않고 전일 총자산의 일정 비율만
남긴다. 남긴 것은 그날 종가를 새 기준으로 삼아 다시 센다.

여기서 지키는 것 두 가지.

  1. 껐을 때(loss_reset_pct=0) 예전 결과가 **한 자리도** 안 바뀐다
  2. 켰을 때 명세대로 움직인다

특히 1번이 중요하다. 새 규칙을 넣다가 기존 전략이 조용히 달라지면, 그동안
검증해 둔 수치가 전부 못 쓰게 된다. 그래서 지문을 박아 두고 대조한다.

지문은 SOXL 전체 구간(2011-01~2026-08, 3923거래일) 실측값이다.
시세를 다시 받으면 마지막 며칠이 늘어 값이 달라질 수 있다. 그때는 기간을
잘라서 비교한다(아래 REGRESSION_END).
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.jongsa_backtest import Lot, run_jongsa
from src.jongsa_live import order_plan, plan_loss_reset

fail = 0


def must(ok: bool, message: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {message}" + (f"  -- {got}" if got else ""))


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


print("손실 리셋 검사\n")

# ===========================================================================
# 1. 공용 계산 (백테스트와 실전이 같이 쓰는 함수)
# ===========================================================================
print("[전부 손실인지 판정]")

loss_lots = [
    {"quantity": 60, "strategy_basis_price": 80.0},
    {"quantity": 40, "strategy_basis_price": 75.0},
]
r = plan_loss_reset(loss_lots, prev_close=70.0, prev_total_assets=20_000, retain_pct=0.06, fee=0.001)
must(r["all_loss"], "전일 종가가 모든 기준가보다 낮으면 전부 손실")

mixed = [
    {"quantity": 60, "strategy_basis_price": 80.0},
    {"quantity": 40, "strategy_basis_price": 65.0},   # 70 > 65 이므로 이건 이익
]
r2 = plan_loss_reset(mixed, prev_close=70.0, prev_total_assets=20_000, retain_pct=0.06)
must(not r2["all_loss"], "하나라도 손실이 아니면 전부 손실이 아니다")
must(approx(r2["net_sell_qty"], 100), "그때는 전량 매도한다", f'{r2["net_sell_qty"]}')
must(approx(r2["retain_qty"], 0), "남기는 것이 없다")

print("\n[명세의 예시 그대로]")
# 강제청산 100주 / 전일 총자산 $20,000 / 전일 종가 $70 / 수수료 0.1%
#   목표 유지수량 = floor(20000 x 0.06 x 0.999 / 70) = floor(17.125...) = 17
#   순매도 = 100 - 17 = 83
r3 = plan_loss_reset(loss_lots, prev_close=70.0, prev_total_assets=20_000,
                     retain_pct=0.06, fee=0.001, whole_shares=True)
must(approx(r3["desired_qty"], 17), "목표 유지수량 17주", f'{r3["desired_qty"]}')
must(approx(r3["retain_qty"], 17), "실제 유지수량 17주", f'{r3["retain_qty"]}')
must(approx(r3["net_sell_qty"], 83), "순매도 83주", f'{r3["net_sell_qty"]}')

print("\n[-7.5% 손실 문턱]")
threshold_lots = [
    {"quantity": 60, "strategy_basis_price": 80.0},
    {"quantity": 40, "strategy_basis_price": 75.0},
]
near_loss = plan_loss_reset(
    threshold_lots, prev_close=70.0, prev_total_assets=20_000,
    retain_pct=0.095, fee=0.001, loss_threshold_pct=0.075,
)
must(not near_loss["all_loss"], "한 물량이라도 -7.5% 문턱을 못 넘으면 전량 청산")
deep_loss = plan_loss_reset(
    threshold_lots, prev_close=68.0, prev_total_assets=20_000,
    retain_pct=0.095, fee=0.001, loss_threshold_pct=0.075,
)
must(deep_loss["all_loss"], "모든 물량이 -7.5% 이하면 리셋")

print("\n[남기는 것이지 사는 것이 아니다]")
# 강제청산이 5주뿐인데 비율로는 17주를 남기고 싶은 경우
small = [{"quantity": 5, "strategy_basis_price": 80.0}]
r4 = plan_loss_reset(small, prev_close=70.0, prev_total_assets=20_000, retain_pct=0.06, fee=0.001)
must(approx(r4["desired_qty"], 17), "비율로는 17주를 원한다")
must(approx(r4["retain_qty"], 5), "그래도 있는 5주까지만 남긴다 (추가 매수 없음)", f'{r4["retain_qty"]}')
must(approx(r4["net_sell_qty"], 0), "팔 것이 없다")

print("\n[꺼져 있으면 예전 그대로]")
r5 = plan_loss_reset(loss_lots, prev_close=70.0, prev_total_assets=20_000, retain_pct=0.0)
must(not r5["all_loss"], "리셋 비율이 0이면 판정 자체를 안 한다")
must(approx(r5["net_sell_qty"], 100), "전량 매도")

print("\n[강제청산 대상이 없으면]")
r6 = plan_loss_reset([], prev_close=70.0, prev_total_assets=20_000, retain_pct=0.06)
must(approx(r6["net_sell_qty"], 0), "팔 것도 남길 것도 없다")
must(approx(r6["retain_qty"], 0), "리셋하지 않는다")

# ===========================================================================
# 2. 전일 값만 쓰는가
# ===========================================================================
print("\n[오늘 값을 안 본다]")
# 주문은 장 마감 전에 넣는다. 그 시점에 오늘 종가는 알 수 없다.
# 함수 인자에 오늘 값이 아예 없어야 한다.
import inspect

params = set(inspect.signature(plan_loss_reset).parameters)
must("prev_close" in params, "전일 종가를 받는다")
must("prev_total_assets" in params, "전일 총자산을 받는다")
must(
    not any("today" in p or "close_today" in p for p in params),
    "오늘 값은 아예 안 받는다 (받으면 실전에서 못 내는 주문이 된다)",
    ", ".join(sorted(params)),
)

# ===========================================================================
# 3. 실전 주문 생성기
# ===========================================================================
print("\n[실전 주문]")
cfg = {
    "target_return": 0.025, "stop_days": 16, "daily_buy_pct": 0.10,
    "loss_reset_pct": 0.06, "fee_rate": 0.001, "whole_shares": True,
    "fee_in_target": True, "buy_range_pct": 0.10, "_last_close": 70.0,
    "moc_available": True,
}
lots = [
    # 16영업일이 넘은 손실 물량 (아주 옛날 날짜로 둔다)
    {"buy_date": "2020-01-02", "buy_price": 80.0, "qty": 60, "target_price": 82.0,
     "strategy_basis_price": 80.0},
    {"buy_date": "2020-01-03", "buy_price": 75.0, "qty": 40, "target_price": 77.0,
     "strategy_basis_price": 75.0},
]
plan = order_plan(lots, cash=5_000, base_assets=20_000, cfg=cfg, today="2026-08-11")
reset = plan["손실리셋"]
must(reset is not None, "리셋 주문이 만들어졌다")
if reset:
    must(approx(reset["팔수량"], 83), "83주만 판다", f'{reset["팔수량"]}')
    must(approx(reset["남길수량"], 17), "17주를 남긴다")
must(plan["매수"]["qty"] == 0, "손절일에는 매수하지 않는다")

# 리셋을 끄면 예전처럼
cfg_off = dict(cfg, loss_reset_pct=0.0)
plan_off = order_plan(lots, cash=5_000, base_assets=20_000, cfg=cfg_off, today="2026-08-11")
must(plan_off["손실리셋"] is None, "꺼면 리셋 항목이 없다 (화면이 예전 그대로)")
must(len(plan_off["강제매도"]) == 2, "강제매도 목록은 그대로 나온다")

# ===========================================================================
# 4. Lot 기본값 — 켜지 않으면 기준가가 취득가와 같다
# ===========================================================================
print("\n[Lot 기본값]")
lot = Lot(buy_day_idx=0, buy_price=100.0, qty=10, target_price=102.5)
must(approx(lot.strategy_basis_price, 100.0), "기준가를 안 주면 취득가와 같다")
must(lot.origin == "normal_buy", "출처 기본값은 일반 매수")

reset_lot = Lot(buy_day_idx=5, buy_price=100.0, qty=10, target_price=72.0,
                strategy_basis_price=70.0, origin="loss_reset")
must(approx(reset_lot.buy_price, 100.0), "리셋해도 실제 취득단가는 안 바뀐다")
must(approx(reset_lot.strategy_basis_price, 70.0), "전략 기준가만 오늘 종가로 바뀐다")

# ===========================================================================
# 5. 엔진 — 켜고 끈 것이 실제로 다르고, 끈 것은 예전과 같은가
# ===========================================================================
print("\n[엔진]")
cache = Path(__file__).resolve().parent.parent / ".soxl_cache.pkl"
if not cache.exists():
    print("SKIP: 시세 캐시가 없습니다 (.soxl_cache.pkl)")
else:
    df = pd.read_pickle(cache)
    common = dict(version="V5", initial_cash=10_000, target_return=0.025, stop_days=16,
                  daily_buy_pct=0.10, fee_rate=0.001, whole_shares=True,
                  loc_buy_limit=True, fee_in_target=True, buy_range_pct=0.10)

    off = run_jongsa(df, loss_reset_pct=0.0, **common)
    on = run_jongsa(df, loss_reset_pct=0.06, **common)

    must(off.loss_reset_days == 0, "꺼면 리셋이 한 번도 안 일어난다")
    must(on.loss_reset_days > 0, "켜면 실제로 일어난다", f"{on.loss_reset_days}일")
    must(on.final_value != off.final_value, "결과가 달라진다")

    # 명세에 적힌 실측치. 구현이 흔들리면 여기서 걸린다.
    for label, start, end, want_cagr, want_mdd in [
        ("2011~2020", "2011-01-01", "2020-12-31", 31.60, -39.12),
        ("2021~2026", "2021-01-01", "2026-12-31", 38.16, -39.73),
    ]:
        r = run_jongsa(df.loc[start:end], loss_reset_pct=0.06, **common)
        must(abs(r.cagr_pct - want_cagr) < 0.10,
             f"{label} CAGR {want_cagr}% 근처", f"{r.cagr_pct:.2f}%")
        must(abs(r.mdd_pct - want_mdd) < 0.10,
             f"{label} MDD {want_mdd}% 근처", f"{r.mdd_pct:.2f}%")

    # 회귀: 리셋을 껐을 때 예전 지문 그대로인가
    print("\n[회귀 — 껐을 때 예전 결과 그대로]")
    # 손실 리셋을 넣기 **전에** 떠 둔 지문이다. 값을 눈대중으로 적으면 안 된다
    # (실제로 그렇게 적었다가 멀쩡한 코드를 고장 난 것으로 읽을 뻔했다).
    # 시세 캐시가 바뀌면 이 값도 다시 떠야 한다.
    BASELINE = {
        "v5_기본":    dict(cagr=23.768505, mdd=-37.910330, final=276506.1288, trades=2244),
        "v5_사다리3": dict(cagr=24.531731, mdd=-39.574745, final=304276.2295, trades=2244),
        "v5_16일":    dict(cagr=27.780558, mdd=-43.406202, final=454345.5103, trades=2305),
        "v4_기본":    dict(cagr=11.270209, mdd=-28.197899, final=52723.1976,  trades=2244),
    }
    CASES = {
        "v5_기본":    dict(version="V5", target_return=0.0275, stop_days=10, daily_buy_pct=0.10),
        "v5_사다리3": dict(version="V5", target_return=0.0275, stop_days=10, daily_buy_pct=0.10,
                           ladder_rungs=3),
        "v5_16일":    dict(version="V5", target_return=0.025, stop_days=16, daily_buy_pct=0.10),
        "v4_기본":    dict(version="V4", target_return=0.0275, stop_days=10),
    }
    for name, kw in CASES.items():
        r = run_jongsa(df, initial_cash=10_000, fee_rate=0.001, whole_shares=True,
                       loc_buy_limit=True, fee_in_target=True, **kw)
        want = BASELINE[name]
        same = (abs(r.cagr_pct - want["cagr"]) < 1e-4
                and abs(r.mdd_pct - want["mdd"]) < 1e-4
                and abs(r.final_value - want["final"]) < 0.01
                and r.num_trades == want["trades"])
        must(same, f"{name} 그대로",
             "" if same else
             f'CAGR {r.cagr_pct:.6f} MDD {r.mdd_pct:.6f} '
             f'최종 {r.final_value:.4f} 거래 {r.num_trades}')

    # 손절재진입 모드도 안 건드렸는지. 새 '손절일엔 매수 금지' 규칙이
    # 리셋 모드 밖으로 새면 여기가 깨진다.
    for mode, want_cagr in [("any_loss", 28.866), ("all_loss", 28.531)]:
        r = run_jongsa(df, version="V5", initial_cash=10_000, target_return=0.027,
                       stop_days=10, daily_buy_pct=0.10, fee_rate=0.001, whole_shares=True,
                       loc_buy_limit=True, fee_in_target=True, sell_day_buy_mode=mode)
        must(abs(r.cagr_pct - want_cagr) < 0.01, f"손절재진입({mode}) 그대로",
             f"{r.cagr_pct:.3f}%")

# ===========================================================================
# 6. 미국 휴장일 — 실시간 보유일이 부풀지 않는가
# ===========================================================================
print("\n[미국 휴장일]")
# 과거 백테스트는 시세 인덱스로 세므로 원래 정확했다. 어긋난 곳은 화면과
# 텔레그램이다. 오늘이 마지막 시세일보다 뒤이면 무조건 '다음 거래일'로 쳤는데,
# 그날이 휴장일이면 거래가 없으므로 보유일이 늘면 안 된다.
#
#   거래일 7/1, 7/2 / 7/3 은 독립기념일 휴장
#   7/3 에 물으면 -> 예전 2일(하루 일찍 손절 안내) / 지금 1일
from src.jongsa_live import is_us_market_open, make_held_counter

DATES = ["2026-07-01", "2026-07-02"]
for today, want in [("2026-07-02", 1), ("2026-07-03", 1), ("2026-07-04", 1), ("2026-07-06", 2)]:
    got = make_held_counter(today, DATES)("2026-07-01")
    label = "열림" if is_us_market_open(today) else "휴장"
    must(got == want, f"{today}({label}) 보유일 {want}일", f"{got}일")

# 증시만 여는 연방 휴일 둘과, 연방 휴일이 아닌 증시 휴장일
for day, want_open in [
    ("2026-10-12", True),   # 콜럼버스데이 — 연방 휴일이지만 증시는 연다
    ("2026-11-11", True),   # 재향군인의날 — 위와 같다
    ("2026-04-03", False),  # 성금요일 — 연방 휴일이 아니지만 증시는 닫는다
    ("2026-12-25", False),  # 크리스마스
    ("2026-07-04", False),  # 토요일
]:
    got = is_us_market_open(day)
    must(got == want_open, f"{day} {'열림' if want_open else '휴장'}",
         "열림" if got else "휴장")

# ===========================================================================
# 7. 텔레그램과 앱이 같은 수량을 말하는가
# ===========================================================================
print("\n[텔레그램 = 앱]")
# 알림 쪽 설정에 loss_reset_pct 가 빠져 있어서 **앱은 83주, 알림은 100주**를
# 안내했다. 오류가 안 나서 알아채기 어렵다. 두 경로가 같은 값을 받는지 본다.
import inspect as _inspect

from src import jongsa_notify

notify_src = _inspect.getsource(jongsa_notify)
for key in ["loss_reset_pct", "loss_reset_threshold_pct"]:
    # settings() 가 내주고, run_jongsa 와 plan_cfg 가 받아야 한다. 셋 다 필요하다.
    must(notify_src.count(key) >= 3, f"알림이 {key} 를 세 곳에 넘긴다",
         f"{notify_src.count(key)}곳")

# 실제로 같은 수량이 나오는지 — 같은 입력을 두 경로에 넣어 본다
notify_cfg = {
    "target_return": 0.027, "stop_days": 16, "daily_buy_pct": 0.10,
    "loss_reset_pct": 0.095, "loss_reset_threshold_pct": 0.075,
    "fee_rate": 0.001, "whole_shares": True, "fee_in_target": True,
    "buy_range_pct": 0.10, "_last_close": 68.0, "moc_available": True,
}
same_lots = [
    {"buy_date": "2020-01-02", "buy_price": 80.0, "qty": 60, "target_price": 82.0,
     "strategy_basis_price": 80.0},
    {"buy_date": "2020-01-03", "buy_price": 75.0, "qty": 40, "target_price": 77.0,
     "strategy_basis_price": 75.0},
]
p = order_plan(same_lots, cash=5_000, base_assets=20_000, cfg=notify_cfg, today="2026-08-11")
must(p["손실리셋"] is not None, "같은 설정이면 리셋이 걸린다")
if p["손실리셋"]:
    must(p["손실리셋"]["손실문턱"] == 0.075, "손실 기준이 메시지에 실린다",
         str(p["손실리셋"]["손실문턱"]))
    must(p["손실리셋"]["전량수량"] == 100, "전체 수량이 실린다")
    must(p["손실리셋"]["팔수량"] + p["손실리셋"]["남길수량"] == 100,
         "팔 수량 + 남길 수량 = 전체")

# 리셋을 끄면 알림도 예전 그대로
p_off = order_plan(same_lots, cash=5_000, base_assets=20_000,
                   cfg=dict(notify_cfg, loss_reset_pct=0.0), today="2026-08-11")
must(p_off["손실리셋"] is None, "꺼면 알림이 예전 형태로 나온다")

# ===========================================================================
# 8. 손실 기준 -7.5% 경계값
# ===========================================================================
print("\n[-7.5% 경계]")
# 기준가 100, 문턱 7.5% -> 92.50 미만이어야 리셋. 딱 92.50 은 안 된다.
edge = [{"quantity": 10, "strategy_basis_price": 100.0}]
for prev, want, label in [
    (92.49, True, "92.49 (문턱 아래) 리셋한다"),
    (92.50, False, "92.50 (문턱 정확히) 리셋 안 한다"),
    (92.51, False, "92.51 (문턱 위) 리셋 안 한다"),
]:
    got = plan_loss_reset(edge, prev_close=prev, prev_total_assets=20_000,
                          retain_pct=0.095, fee=0.001, loss_threshold_pct=0.075)["all_loss"]
    must(got == want, label, str(got))

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
