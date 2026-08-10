"""종사종팔 V5 설정 관리.

**매매 기록을 저장하지 않는다.** 시작일과 규칙이 정해지면 그날부터 오늘까지의
모든 매매가 자동으로 결정되기 때문이다. 앱은 열릴 때마다 시작일부터 다시 계산한다.
그래서 여기에 남는 건 '설정값'뿐이고, 그걸 jongsa_settings.json에 저장한다.

단, 웹에 배포된 상태에서는 **저장하지 않는다**. 서버 파일 하나를 모든 접속자가
공유하게 되어 서로의 설정을 덮어쓰기 때문이다. 그 경우 설정은 브라우저 세션
안에서만 유지된다 (앱 쪽에서 st.session_state로 들고 있는다).
"""

import json
import math
import os
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "jongsa_settings.json"


def is_shared_server() -> bool:
    """여러 사람이 함께 쓰는 서버에서 돌고 있는지.

    Streamlit Community Cloud는 저장소를 /mount/src 아래에 마운트한다.
    직접 서버에 올려 쓰는 경우를 위해 환경변수로도 켤 수 있게 해뒀다.
    """
    if os.environ.get("QUANT_SHARED_SERVER", "").strip().lower() in ("1", "true", "yes"):
        return True
    return Path("/mount/src").exists()

DEFAULT_CONFIG = {
    "ticker": "SOXL",
    "start_date": "2025-01-02",  # 이 날부터 규칙대로 했다고 가정하고 계산한다
    "target_return": 0.0275,   # 목표수익률 2.75%
    "daily_buy_pct": 0.10,     # 하루 매수금 = 총자산의 10% (=10분할)
    "stop_days": 10,           # 10영업일째 강제청산
    "initial_cash": 10000.0,   # 시드
    "fee_rate": 0.0,           # 편도 수수료 (증권사마다 다름). 0이면 무시된다
    "fee_in_target": True,     # 목표가에 왕복 수수료를 얹을지 (원본 스프레드시트 방식)
    "whole_shares": True,      # 정수주만 매수 (원본 스프레드시트 방식)
    "sell_day_buy_mode": "never",  # 매도일에도 매수할지 — never / all_loss / any_loss
    "reinvest": True,          # 번 돈까지 굴릴지(복리) / 하루 매수금을 시드 기준으로 고정할지
    "moc_available": True,     # 증권사에 MOC(종가 시장가)가 있는지. 손절 매도에만 쓴다
    # '매수 범위' — 팔 물량이 없는 날 LOC 매수를 어디에 걸지.
    # 전날 종가보다 이 비율까지 올라도 사겠다는 뜻. 원본 시트의 설정값이다.
    # (원작자 답변: "매도 물량이 있을 때는 매수가를 최저매도가-0.01로 해서
    #  매수나 매도만 생기게 되어있어요 / 시즌 첫 주문은 매수 범위 적용 되구요")
    "buy_range_pct": 0.10,
    # '사다리 주문' — 기본 매수 아래로 지정가를 낮춰가며 주문을 더 건다.
    # LOC는 수량을 미리 적어내는데 종가가 지정가보다 낮게 끝나면 예산이 남는다.
    # 아래쪽에 주문을 더 걸어두면 그만큼 더 사서 예산을 채운다.
    # 0이면 안 쓴다.
    "ladder_rungs": 3,
    "ladder_step": 0.03,   # 칸 간격. 기준가에서 -3%, -6%, -9% 지점을 덮는다
}


def build_ladder(budget: float, size_px: float, base_qty: float,
                 rungs: int = 3, step: float = 0.03, fee: float = 0.0) -> list:
    """기본 매수 아래로 걸 추가 LOC 주문 목록. [(수량, 지정가), ...]

    k번째 칸의 목표 누적수량  Nk = floor(예산 / (기준가 x (1 - k*간격)))
    k번째 칸의 수량          = Nk - N(k-1)   (0이면 건너뛴다)
    k번째 칸의 지정가        = 예산 / Nk 를 센트 단위로 **내림**

    내림이 중요하다. 올리면 그 가격에 체결됐을 때 예산을 넘긴다.
    내리면 (Nk x 지정가) <= 예산 이 항상 성립한다.

    카페에서 도는 방식은 '1주씩' 늘려가는 것인데, 계좌가 커지면 한 번에
    수백 주를 사게 되어 1주짜리 주문을 수십 개 걸어야 한다. 여기서는 칸 수를
    고정하고 칸마다 필요한 수량을 담는다 — 계좌가 커져도 주문 개수는 그대로다.

    추가 주문은 모두 기본 지정가보다 낮다. 그래서 목표가에 닿아 매도가
    일어나는 날에는 하나도 체결되지 않는다 ('판 날은 안 산다'가 유지된다).
    """
    out = []
    if not rungs or rungs < 1 or step <= 0 or size_px <= 0 or base_qty <= 0:
        return out

    usable = budget * (1 - fee)
    prev = int(base_qty)
    for k in range(1, int(rungs) + 1):
        px = size_px * (1 - k * step)
        if px <= 0:
            break
        n = int(usable / px)
        if n > prev:
            out.append((n - prev, math.floor(usable / n * 100) / 100))
            prev = n
    return out

# 매수 범위별 실제 영향. 백테스트를 돌려서 얻은 값이다.
#
# 처음엔 '전체 거래일 중 +10% 넘긴 날의 비율(3.95%)'을 썼는데 그건 과장이었다.
# 매수 범위는 '팔 물량이 하나도 없는 날'에만 적용된다. 그런 날 자체가 드물어서
# 실제로 건너뛰는 날은 훨씬 적다. SOXL 2011~2026(3,920거래일) 기준:
#
#   범위    거른 날   최종자산      제한없음 대비
#   +3%      157일   $589,762        -14.9%
#   +5%       72일   $652,455         -5.8%
#   +7%       32일   $691,910         -0.1%
#   +10%      11일   $694,298         +0.2%   <- 기본값
#   +15%       4일   $694,399         +0.3%
#   +20%       0일   $692,640          0.0%
#
# 즉 +10%는 15년 통틀어 11일 건너뛰고 손해도 사실상 없다.
# 반대로 +5% 이하로 조이면 급등 직후 재진입을 자주 놓쳐 눈에 띄게 불리해진다.
#
# 위 수치는 매수 수량을 '주문 시점에 알 수 있는 가격'으로 정하도록 엔진을
# 고친 뒤(order_sized_qty) 다시 잰 값이다. 거른 날 수는 그대로였다 —
# 건너뛸지 말지는 가격만 보고 정하므로 수량과 무관하기 때문이다.
BUY_RANGE_SKIPS_15Y = {0.03: 157, 0.05: 72, 0.07: 32, 0.10: 11, 0.15: 4, 0.20: 0, 0.30: 0}
BUY_RANGE_VS_NOLIMIT = {0.03: -14.9, 0.05: -5.8, 0.07: -0.1, 0.10: 0.2, 0.15: 0.3, 0.20: 0.0}

# 예전 이름 (호환용)
BUY_RANGE_MISS_RATE = {k: v / 39.19 for k, v in BUY_RANGE_SKIPS_15Y.items()}

# 손절 매도에 MOC가 없을 때 LOC로 대신할 여유폭 (아래로)
LOC_FALLBACK_BUFFER = 0.30

# 매도가 있는 날 매수를 허용하는 방식들. 손절로 청산된 자리는 목표 미달이니
# 다시 진입한다는 발상이다. 수익률은 오르지만 낙폭도 깊어진다.
SELL_DAY_MODES = {
    "never": "매도일엔 매수 안 함 (원본 V5)",
    "all_loss": "판 게 전부 손실일 때만 매수",
    "any_loss": "판 것 중 손실이 하나라도 있으면 매수",
}


# 검증된 설정 묶음. CAGR/MDD 수치는 2010-04~2024-12 SOXL 백테스트 결과다.
#
# 예전에는 원본 스프레드시트와 0.1%p 이내로 일치했다. 매수 수량을 '주문 시점에
# 알 수 있는 가격'으로 정하도록 고친 뒤 다시 쟀는데, 이 구간에서는 거의 그대로였다
# (표준 30.5 -> 30.3). 원본 시트는 그날 종가로 수량을 정하는데, 그건 주문을 넣는
# 시점에 알 수 없는 값이라 실제로는 그렇게 살 수 없다.
# 예외는 '표준 + 손절재진입'으로, MDD가 -44.0에서 -35.2로 크게 줄었다.
# 손절 자리를 바로 채우는 방식이라 수량 차이가 노출로 그대로 이어지기 때문이다.
PRESETS = {
    "안정형 (3%)": {
        "daily_buy_pct": 0.03,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "가장 안전. 낙폭이 얕은 대신 수익도 낮다.",
        "CAGR": 8.8,
        "MDD": -9.4,
        "효율": 0.94,
    },
    "안정형+ (6.5%)": {
        "daily_buy_pct": 0.065,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "표준보다 한 단계 보수적.",
        "CAGR": 19.5,
        "MDD": -19.8,
        "효율": 0.98,
    },
    "표준 (10%) ★추천": {
        "daily_buy_pct": 0.10,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "원본 기본값. 위험 대비 효율이 가장 좋은 구간이라 처음엔 여기서 시작하는 게 좋다.",
        "CAGR": 30.3,
        "MDD": -29.7,
        "효율": 1.02,
    },
    "적극형 (11.1%)": {
        "daily_buy_pct": 0.111,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "표준에 익숙해진 뒤 다음 단계. 효율 손해가 거의 없다.",
        "CAGR": 33.5,
        "MDD": -32.7,
        "효율": 1.02,
    },
    "공격형 (12.5%)": {
        "daily_buy_pct": 0.125,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "여기부터 효율이 떨어지기 시작한다. 원본 자료도 이 위로는 권하지 않는다.",
        "CAGR": 36.7,
        "MDD": -36.6,
        "효율": 1.0,
    },
    "표준 + 손절재진입": {
        "daily_buy_pct": 0.10,
        "target_return": 0.027,
        "stop_days": 10,
        "sell_day_buy_mode": "any_loss",
        "설명": "손절로 비워진 자리를 바로 다시 채운다. 효율은 오르지만 낙폭이 깊어지고, 매일 판단할 게 하나 늘어난다.",
        "CAGR": 34.1,
        "MDD": -35.2,
        "효율": 0.97,
    },
}


def load_config() -> dict:
    """저장된 설정을 불러온다. 없거나 항목이 빠져 있으면 기본값으로 채운다."""
    if is_shared_server() or not SETTINGS_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)
    # 예전 형식(설정이 config 키 안에 들어 있던 파일)도 읽어준다
    if "config" in saved and isinstance(saved["config"], dict):
        saved = saved["config"]
    return {**DEFAULT_CONFIG, **saved}


def save_config(cfg: dict) -> bool:
    """설정을 파일에 저장한다. 공용 서버에서는 저장하지 않고 False를 반환한다."""
    if is_shared_server():
        return False
    keep = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def apply_preset(cfg: dict, preset_name: str) -> dict:
    """프리셋을 설정에 적용한다 (수수료·정수주 설정은 그대로 둔다)."""
    p = PRESETS.get(preset_name)
    if not p:
        return cfg
    for k in ("daily_buy_pct", "target_return", "stop_days", "sell_day_buy_mode"):
        cfg[k] = p[k]
    save_config(cfg)
    return cfg


def should_buy_on_sell_day(sold_pnls: list, mode: str) -> bool:
    """오늘 매도가 있었을 때 매수해도 되는지 판정."""
    if not sold_pnls:
        return True
    if mode == "any_loss":
        return any(p < 0 for p in sold_pnls)
    if mode == "all_loss":
        return all(p < 0 for p in sold_pnls)
    return False


def target_price_for(price: float, cfg: dict) -> float:
    """매수가로부터 목표 매도가를 계산한다.

    원본 스프레드시트는 목표가에 왕복 수수료를 얹어둔다
    (= 수수료를 내고도 목표수익률이 남도록). 수수료가 0이면 결과는 동일하다.
    """
    tgt = price * (1 + cfg["target_return"])
    if cfg.get("fee_in_target", True):
        tgt *= 1 + 2 * cfg.get("fee_rate", 0.0)
    return tgt


def order_plan(lots: list, cash: float, base_assets: float, cfg: dict,
               today: str = None, trading_dates=None) -> dict:
    """오늘 장 마감에 넣을 주문을 만든다.

    핵심: **주문을 넣는 시점에는 오늘 종가를 모른다.** 그래서 '매도가 있는 날은
    매수하지 않는다'는 규칙을 지키려면 가격 조건으로 바꿔 걸어야 한다.

        매수 LOC 지정가 = (오늘 매도될 수 있는 목표가 중 가장 낮은 값) - 0.01

    - 종가가 그 목표가에 닿으면 -> 매도 체결 + 매수는 미체결 (지정가보다 높으니까)
    - 안 닿으면 -> 매도 없음 + 매수만 체결
    한 번의 주문으로 규칙이 저절로 지켜진다. (원 작성자가 공유한 요령)

    10영업일이 찬 건은 가격과 무관하게 팔아야 하므로 날짜만 보면 미리 알 수 있다.
    그런 날은 아예 매수 주문을 넣지 않는다.

    trading_dates: 시세의 거래일 목록. 넘기면 보유일을 실제 거래일로 세서
    미국 공휴일이 낀 주에도 손절일이 엔진과 어긋나지 않는다.
    """
    today = today or _today_str()
    stop_days = int(cfg["stop_days"])
    last_close = cfg.get("_last_close", 0.0)
    has_moc = cfg.get("moc_available", True)
    held_of = make_held_counter(today, trading_dates)

    forced, pending = [], []
    for lot in lots:
        held = held_of(lot["buy_date"])
        row = {**lot, "보유영업일": held}
        if held >= stop_days:
            # MOC가 없는 증권사면 '아주 낮은 지정가 LOC 매도'로 대신한다.
            # LOC 매도는 종가 >= 지정가일 때 체결되므로 지정가를 낮추면 사실상 무조건 체결.
            # 체결가는 지정가가 아니라 종가라서 싸게 팔리는 게 아니다.
            row["대체지정가"] = (
                round(last_close * (1 - LOC_FALLBACK_BUFFER), 2)
                if (not has_moc and last_close) else None
            )
            forced.append(row)
        elif held >= 1:
            pending.append(row)   # 종가가 목표가 이상이면 팔린다
        # held == 0 (어제 산 것)은 오늘 매도 대상이 아니다

    desired = base_assets * cfg["daily_buy_pct"]
    budget = min(desired, max(cash, 0.0))
    fee = cfg.get("fee_rate", 0.0)

    buy = {"type": None, "limit": None, "budget": budget, "qty": 0.0, "cost": 0.0,
           "reason": "", "기준가": None, "사다리": []}

    if forced:
        buy["reason"] = f"{stop_days}영업일이 찬 건이 있어 오늘은 무조건 매도일입니다 (매수 안 함)"
    elif budget <= 0:
        buy["reason"] = "예수금이 없습니다"
    else:
        # 매수는 언제나 LOC다 (원본 시트 방식). 지정가를 무엇으로 잡느냐만 다르다.
        if pending:
            # 팔 물량이 있는 날: 최저 목표가 - 0.01.
            # 종가가 목표가에 닿으면 매도만, 안 닿으면 매수만 일어난다.
            buy["type"] = "LOC"
            buy["limit"] = round(min(p["target_price"] for p in pending) - 0.01, 2)
            buy["reason"] = "팔 물량이 있는 날 — 매도와 매수 중 하나만 일어나게 겁니다"
        else:
            # 팔 물량이 없는 날: '매수 범위'를 씌운다.
            # 전날 종가보다 이 비율까지 올라도 사겠다는 뜻. 그보다 더 급등하면 안 산다.
            rng = cfg.get("buy_range_pct", 0.10)
            buy["type"] = "LOC_RANGE"
            buy["limit"] = round(last_close * (1 + rng), 2) if last_close else None
            buy["reason"] = f"팔 물량이 없는 날 — 매수 범위 +{rng*100:.0f}%까지 허용"

        # 수량은 예산을 넘지 않도록 계산한다.
        # 매수 범위 지정가는 현실적인 체결가가 아니므로(실제 체결은 종가) 마지막 종가로 잡는다.
        px = buy["limit"] if buy["type"] == "LOC" else last_close
        if px and px > 0:
            qty = budget * (1 - fee) / px
            if cfg.get("whole_shares", True):
                qty = float(int(qty))
            buy["qty"] = qty
            buy["cost"] = qty * px * (1 + fee)
            buy["기준가"] = px
            # 정수주로 살 때만 잔돈이 남는다. 소수점 매수가 되면 기본 주문이
            # 이미 예산을 딱 맞게 쓰므로 사다리가 필요 없다.
            if cfg.get("whole_shares", True):
                buy["사다리"] = build_ladder(
                    budget, px, qty,
                    rungs=cfg.get("ladder_rungs", 0),
                    step=cfg.get("ladder_step", 0.03),
                    fee=fee,
                )

    return {
        "강제매도": forced,
        "목표매도": pending,
        "매수": buy,
        "부족": desired > cash + 1e-9,
        "목표금액": desired,
    }


def business_days_between(start: str, end: str) -> int:
    """두 날짜 사이 영업일 수 (주말만 제외, 공휴일은 미반영).

    거래일 목록을 못 넘겨줄 때 쓰는 대비책이다. 공휴일이 끼면 하루 빨라지므로
    가능하면 make_held_counter()를 쓴다.
    """
    import numpy as np

    return int(np.busday_count(np.datetime64(start), np.datetime64(end)))


def make_held_counter(today: str, trading_dates=None):
    """'매수일 -> 보유 거래일 수'를 세는 함수를 만든다.

    엔진(run_jongsa)은 시세에 들어 있는 거래일 순번으로 보유일을 센다.
    화면과 알림도 같은 방식으로 세야 손절일이 어긋나지 않는다.

    예전에는 달력 평일(business_days_between)로 셌는데, 미국 공휴일을
    거래일로 착각해서 **하루 일찍 팔라고** 했다. 2026년 1~8월 148거래일 중
    5일이 그랬다 (독립기념일·메모리얼데이 등이 낀 주).

    trading_dates: 시세 데이터의 인덱스(실제 거래일). 없으면 예전 방식으로 돌아간다.
    """
    fallback = lambda buy: business_days_between(buy, today)   # noqa: E731
    if trading_dates is None or len(trading_dates) == 0:
        return fallback

    days = [str(d)[:10] for d in trading_dates]
    idx = {d: i for i, d in enumerate(days)}

    t = idx.get(today[:10])
    if t is None:
        if today[:10] <= days[-1]:
            return fallback     # 데이터 중간인데 없는 날 — 뭔가 어긋났다
        # 오늘이 데이터 마지막 날보다 뒤 = 아직 종가가 안 나온 오늘.
        # 마지막 거래일 바로 다음 거래일로 친다.
        t = len(days)

    def held(buy: str) -> int:
        i = idx.get(buy[:10])
        return t - i if i is not None else business_days_between(buy, today)

    return held
