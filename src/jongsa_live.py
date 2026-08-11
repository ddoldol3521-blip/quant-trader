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
    # '손실 리셋' — 손절일이 찬 물량이 **전부 손실**이면 전량 팔지 않고
    # 전일 총자산의 이 비율만큼만 남긴다. 남긴 것은 그날 종가를 새 기준으로
    # 삼아 보유일과 목표가를 다시 센다. 0이면 예전처럼 전량 매도한다.
    #
    # 하루 매수금(daily_buy_pct)과 **다른 값**이다.
    #   신규 매수  전일 총자산의 10%
    #   리셋 유지  전일 총자산의 6%
    #
    # 전량 손절은 손실을 확정하고 자리를 비워 반등을 놓친다. 조금 남겨 두면
    # 그 자리를 유지한 채로 다음 판을 기다린다.
    "loss_reset_pct": 0.0,
    # 손실 리셋을 발동할 최소 손실률. 0.075면 모든 만기 물량이 전일 기준
    # 각각 -7.5% 이하일 때만 리셋하고, 아니면 예전처럼 전량 청산한다.
    "loss_reset_threshold_pct": 0.0,
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
    "never": "매도하는 날에는 새로 사지 않음 (추천 프리셋 기본값)",
    "all_loss": "판 게 전부 손실일 때만 매수",
    "any_loss": "판 것 중 손실이 하나라도 있으면 매수",
}


# 검증된 설정 묶음. 성과 수치는 2011~2026 SOXL, 편도 비용 0.1% 기준이다.
#
# 예전에는 원본 스프레드시트와 0.1%p 이내로 일치했다. 매수 수량을 '주문 시점에
# 알 수 있는 가격'으로 정하도록 고친 뒤 다시 쟀는데, 이 구간에서는 거의 그대로였다
# (표준 30.5 -> 30.3). 원본 시트는 그날 종가로 수량을 정하는데, 그건 주문을 넣는
# 시점에 알 수 없는 값이라 실제로는 그렇게 살 수 없다.
# 예외는 '표준 + 손절재진입'으로, MDD가 -44.0에서 -35.2로 크게 줄었다.
# 손절 자리를 바로 채우는 방식이라 수량 차이가 노출로 그대로 이어지기 때문이다.
# ── CAGR/MDD 와 CAGR_범위/MDD_범위 ────────────────────────────────────────
#
# CAGR·MDD 는 **그 설정 하나의 실측치**다. 소수점까지 정확하다.
# 그런데 그 숫자만 보여주면 실제보다 정밀해 보인다. 설정값을 조금만 옮겨도
# 결과가 꽤 움직이기 때문이다.
#
# 그래서 이웃값을 같이 재서 범위를 함께 적어 둔다. 잰 이웃은 이렇다.
#   손절일 ±1일 / 목표수익률 ±0.1%p / 손실문턱 ±2.5%p / 리셋비율 ±0.5%p
#
# 재보니 **다섯 프리셋 모두 실측치가 범위의 가장 좋은 끝**이었다. 최적화가
# 최고점을 집었다는 뜻이다. 예를 들어 균형형은 실측 -40.40% 인데 이웃을
# 넣으면 -40~-46% 다. 실전에서 -45% 가 나와도 설정이 틀린 게 아니라
# **원래 그 범위**다. 그걸 모르면 정상인데도 고장난 줄 알고 그만두게 된다.
#
# 화면에는 범위를 먼저 보여주고, 정확한 값은 상세표에 남긴다.
PRESETS = {
    "안정형": {
        "daily_buy_pct": 0.09,
        "target_return": 0.027,
        "stop_days": 16,
        "sell_day_buy_mode": "never",
        "loss_reset_pct": 0.09,
        "loss_reset_threshold_pct": 0.075,
        "ladder_rungs": 0,
        "ladder_step": 0.03,
        "buy_range_pct": 0.10,
        "설명": "낙폭을 가장 줄인 선택입니다. 하루에 자산의 9%씩 사고, 크게 물린 만기 물량도 9%만 남깁니다. 수익보다 마음 편한 운용을 우선한다면 적합합니다.",
        "CAGR": 33.70,
        "MDD": -36.55,
        "CAGR_범위": "31~34%",
        "MDD_범위": "-37~-44%",
        "효율": 0.92,
    },
    "안정성장형": {
        "daily_buy_pct": 0.095,
        "target_return": 0.027,
        "stop_days": 16,
        "sell_day_buy_mode": "never",
        "loss_reset_pct": 0.095,
        "loss_reset_threshold_pct": 0.075,
        "ladder_rungs": 0,
        "ladder_step": 0.03,
        "buy_range_pct": 0.10,
        "설명": "안정형보다 하루 매수량을 0.5% 늘린 중간 단계입니다. 낙폭을 조금 더 감수하고 수익을 높이고 싶은 사람에게 적합합니다.",
        "CAGR": 35.46,
        "MDD": -38.53,
        "CAGR_범위": "32~35%",
        "MDD_범위": "-38~-45%",
        "효율": 0.92,
    },
    "균형형 ⭐ 추천": {
        "daily_buy_pct": 0.10,
        "target_return": 0.027,
        "stop_days": 16,
        "sell_day_buy_mode": "never",
        "loss_reset_pct": 0.095,
        "loss_reset_threshold_pct": 0.075,
        "ladder_rungs": 0,
        "ladder_step": 0.03,
        "buy_range_pct": 0.10,
        "설명": "수익과 낙폭의 균형을 고려한 기본 추천입니다. 하루 10%씩 사고, 16일 된 물량이 모두 크게 손실일 때만 9.5%를 남깁니다. 처음 선택한다면 이것을 권합니다.",
        "CAGR": 37.16,
        "MDD": -40.40,
        "CAGR_범위": "34~37%",
        "MDD_범위": "-40~-46%",
        "효율": 0.92,
    },
    "간편형": {
        "daily_buy_pct": 0.10,
        "target_return": 0.026,
        "stop_days": 16,
        "sell_day_buy_mode": "never",
        "loss_reset_pct": 0.065,
        "loss_reset_threshold_pct": 0.0,
        "ladder_rungs": 0,
        "ladder_step": 0.03,
        "buy_range_pct": 0.10,
        "설명": "판단 조건이 가장 단순합니다. 16일 된 물량이 손실이면 6.5%만 남깁니다. 이해하고 실행하기 쉽지만 안정형보다 낙폭은 클 수 있습니다.",
        "CAGR": 36.12,
        "MDD": -40.03,
        "CAGR_범위": "33~36%",
        "MDD_범위": "-40~-42%",
        "효율": 0.90,
    },
    "공격형": {
        "daily_buy_pct": 0.11,
        "target_return": 0.027,
        "stop_days": 16,
        "sell_day_buy_mode": "never",
        "loss_reset_pct": 0.10,
        "loss_reset_threshold_pct": 0.075,
        "ladder_rungs": 0,
        "ladder_step": 0.03,
        "buy_range_pct": 0.10,
        "설명": "하루 매수량을 11%로 높인 고수익·고위험 선택입니다. 과거에도 자산이 약 44% 줄어든 구간이 있었으므로 큰 하락을 견딜 수 있을 때만 적합합니다.",
        "CAGR": 40.16,
        "MDD": -43.80,
        "CAGR_범위": "37~40%",
        "MDD_범위": "-44~-48%",
        "효율": 0.92,
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
    for k in (
        "daily_buy_pct", "target_return", "stop_days", "sell_day_buy_mode",
        "loss_reset_pct", "loss_reset_threshold_pct", "ladder_rungs",
        "ladder_step", "buy_range_pct",
    ):
        if k in p:
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


def plan_loss_reset(forced_lots: list, prev_close: float, prev_total_assets: float,
                    retain_pct: float, fee: float = 0.0,
                    whole_shares: bool = True,
                    loss_threshold_pct: float = 0.0) -> dict:
    """16영업일이 찬 물량이 **전부 손실**일 때 얼마를 남길지 정한다.

    원래는 16일이 차면 전량 팔았다. 그러면 손실을 확정하고 그 자리가 비어,
    반등을 그대로 놓친다. 대신 전일 총자산의 일정 비율(기본 6%)만큼만 남기고
    나머지를 판다. 남긴 물량은 그날 종가를 새 기준으로 삼아 다시 16일을 센다.

    **일부만 파는 것이지 팔았다가 되사는 것이 아니다.** 그래서
      - 남긴 수량에는 매도·매수 수수료가 붙지 않는다
      - 증권사 취득단가는 그대로다 (전략 기준가만 바뀐다)

    ── 왜 전일 종가로만 판단하는가 ──────────────────────────────
    주문은 장 마감 전에 넣는다. 그 시점에 오늘 종가는 **알 수 없다.**
    오늘 종가로 손실 여부를 따지면 백테스트만 맞고 실전에서는 그 주문을
    낼 수가 없다. 그래서 손실 판정도, 유지수량 계산도 전일 값만 쓴다.
    (이 함수에 오늘 값을 아예 안 넘기는 이유다)

    forced_lots: 16영업일 이상 된 물량. 각각 quantity 와 strategy_basis_price 를
        가진 dict 또는 그 이름의 속성을 가진 객체.
    반환:
        all_loss        전부 손실인가
        forced_qty      강제청산 대상 총수량
        retain_qty      실제로 남길 수량
        net_sell_qty    실제로 팔 수량 (이것만 주문한다)
        desired_qty     비율만 보고 계산한 수량 (남길 수량의 상한 전)
    """
    def _get(lot, name):
        return lot[name] if isinstance(lot, dict) else getattr(lot, name)

    forced_qty = float(sum(_get(l, "quantity") for l in forced_lots))
    out = {"all_loss": False, "forced_qty": forced_qty,
           "retain_qty": 0.0, "net_sell_qty": forced_qty, "desired_qty": 0.0}

    if not forced_lots or forced_qty <= 0:
        out["net_sell_qty"] = 0.0
        return out
    if retain_pct <= 0 or prev_close <= 0 or prev_total_assets <= 0:
        return out   # 리셋을 안 쓰는 설정 — 예전처럼 전량 매도

    # 하나라도 최소 손실 문턱을 충족하지 않으면 '전부 손실'이 아니다.
    # threshold=0이면 기존 동작(단순 손실), 0.075면 각각 -7.5% 이하여야 한다.
    threshold = max(float(loss_threshold_pct or 0.0), 0.0)
    out["all_loss"] = all(
        prev_close < float(_get(l, "strategy_basis_price")) * (1 - threshold)
        for l in forced_lots
    )
    if not out["all_loss"]:
        return out   # 예전처럼 전량 매도

    desired = prev_total_assets * retain_pct * (1 - fee) / prev_close
    desired = float(int(desired)) if whole_shares else float(desired)
    out["desired_qty"] = desired

    # 강제청산 대상보다 많이 남기지 않는다. 남기는 것이지 사는 것이 아니다.
    retain = min(forced_qty, max(desired, 0.0))
    out["retain_qty"] = retain
    out["net_sell_qty"] = forced_qty - retain
    return out


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

    # ---- 손실 리셋 ----
    # 손절일이 찬 물량이 전부 전일 기준 손실이면 전량 팔지 않고 일부만 남긴다.
    # 백테스트(run_jongsa)와 **같은 함수**를 쓴다. 여기와 저기가 갈라지면
    # 앱이 알려준 주문과 검증된 결과가 어긋난다.
    reset_pct = float(cfg.get("loss_reset_pct", 0.0) or 0.0)
    reset = None
    if reset_pct > 0 and forced and last_close > 0:
        reset = plan_loss_reset(
            [{"quantity": r["qty"], "strategy_basis_price": r.get("strategy_basis_price",
                                                                  r.get("buy_price", 0.0))}
             for r in forced],
            prev_close=last_close,
            prev_total_assets=base_assets,
            retain_pct=reset_pct,
            fee=cfg.get("fee_rate", 0.0),
            whole_shares=cfg.get("whole_shares", True),
            loss_threshold_pct=cfg.get("loss_reset_threshold_pct", 0.0),
        )
        if not reset["all_loss"]:
            reset = None   # 하나라도 손실이 아니면 예전처럼 전량 매도

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
        # 리셋을 안 쓰면 None 이다. 화면은 이때만 '순매도'로 바꿔 보여주면 된다.
        "손실리셋": None if not reset else {
            "전량수량": reset["forced_qty"],
            "남길수량": reset["retain_qty"],
            "팔수량": reset["net_sell_qty"],
            "유지비율": reset_pct,
            "손실문턱": float(cfg.get("loss_reset_threshold_pct", 0.0) or 0.0),
            "설명": (
                f"손절일이 찬 {reset['forced_qty']:.0f}주가 전부 어제 종가 기준 "
                f"-{float(cfg.get('loss_reset_threshold_pct', 0.0) or 0.0)*100:.1f}% 이하입니다. "
                f"전량 팔지 않고 {reset['retain_qty']:.0f}주를 남깁니다 "
                f"(전일 총자산의 {reset_pct*100:.0f}%). "
                + (f"**{reset['net_sell_qty']:.0f}주만 MOC 매도**하세요."
                   if reset["net_sell_qty"] > 0
                   else "**팔 것이 없습니다** — 전량 그대로 두세요.")
            ),
        },
    }


def business_days_between(start: str, end: str) -> int:
    """두 날짜 사이 영업일 수 (주말만 제외, 공휴일은 미반영).

    거래일 목록을 못 넘겨줄 때 쓰는 대비책이다. 공휴일이 끼면 하루 빨라지므로
    가능하면 make_held_counter()를 쓴다.
    """
    import numpy as np

    return int(np.busday_count(np.datetime64(start), np.datetime64(end)))


def is_us_market_open(day: str) -> bool:
    """그날 미국 증시가 열리는가. (대략)

    주말과 휴장일이면 False. 정확한 NYSE 달력을 쓰려면 pandas_market_calendars가
    필요한데, 그것 하나 때문에 라이브러리를 늘리지 않는다. pandas에 들어 있는
    연방공휴일에서 **증시만 여는 이틀을 빼고 성금요일을 더하면** NYSE와 같아진다.

      콜럼버스데이(10월)   연방 휴일이지만 증시는 연다
      재향군인의날(11/11)  연방 휴일이지만 증시는 연다
      성금요일             연방 휴일이 아니지만 증시는 닫는다

    반장(오후 1시 마감)인 날은 열리는 날로 본다 — 종가가 나오므로 주문도 된다.
    대통령 서거 같은 임시 휴장은 알 수 없다. 그런 날은 시세가 안 들어오므로
    다음 날 계산에서 저절로 맞춰진다.
    """
    try:
        import pandas as _pd
        from pandas.tseries.holiday import GoodFriday, USFederalHolidayCalendar
    except Exception:
        return True   # 못 판단하면 열린 것으로 본다 (예전 동작)

    ts = _pd.Timestamp(day[:10])
    if ts.weekday() >= 5:
        return False

    year = ts.year
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    try:
        federal = set(USFederalHolidayCalendar().holidays(lo, hi))
        # 증시가 여는 연방 휴일 둘을 뺀다
        federal -= {d for d in federal if d.month == 10 and d.weekday() == 0}   # 콜럼버스데이
        federal -= {d for d in federal if d.month == 11 and 10 <= d.day <= 12}  # 재향군인의날
        closed = federal | set(GoodFriday.dates(lo, hi))
    except Exception:
        return True

    return ts.normalize() not in closed


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
        #
        # 예전에는 무조건 '다음 거래일'로 쳤다(t = len(days)). 그런데 오늘이
        # **휴장일**이면 거래 자체가 없으므로 보유일이 늘면 안 된다. 그대로
        # 늘리면 하루 일찍 손절하라고 안내한다.
        #
        #   실제 거래일 7/1, 7/2 / 7/3 은 독립기념일 휴장
        #   7/3 에 물으면 -> 예전: 2일 (틀림) / 지금: 1일 (7/2 와 같음)
        #
        # 과거 백테스트는 시세 인덱스로 세므로 원래 정확했다. 어긋나는 곳은
        # 화면과 텔레그램뿐이었다.
        t = len(days) if is_us_market_open(today) else len(days) - 1

    def held(buy: str) -> int:
        i = idx.get(buy[:10])
        return t - i if i is not None else business_days_between(buy, today)

    return held
