"""종사종팔 V5 실전 운용 도우미.

매일 '얼마 사고, 뭘 팔지'를 계산해준다. 실제 주문은 사용자가 증권사 앱에서 직접 한다.

보유 내역은 로컬 JSON에 저장한다(jongsa_positions.json). 이 파일은 .gitignore에 있다.
"""

import json
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "jongsa_positions.json"

DEFAULT_CONFIG = {
    "ticker": "SOXL",
    "target_return": 0.0275,   # 목표수익률 2.75%
    "daily_buy_pct": 0.10,     # 하루 매수금 = 총자산의 10%
    "stop_days": 10,           # 10영업일째 강제청산
    "initial_cash": 10000.0,
    "fee_rate": 0.0,           # 편도 수수료 (증권사마다 다름). 0이면 무시된다
    "fee_in_target": True,     # 목표가에 왕복 수수료를 얹을지 (원본 스프레드시트 방식)
    "whole_shares": True,      # 정수주만 매수 (원본 스프레드시트 방식)
    "sell_day_buy_mode": "never",  # 매도일에도 매수할지 — never / all_loss / any_loss
}

# 매도가 있는 날 매수를 허용하는 방식들. 손절로 청산된 자리는 목표 미달이니
# 다시 진입한다는 발상이다. 수익률은 오르지만 낙폭도 깊어진다.
SELL_DAY_MODES = {
    "never": "매도일엔 매수 안 함 (원본 V5)",
    "all_loss": "판 게 전부 손실일 때만 매수",
    "any_loss": "판 것 중 손실이 하나라도 있으면 매수",
}


# 검증된 설정 묶음. CAGR/MDD 수치는 2010-04~2024-12 SOXL 백테스트 결과로,
# 원본 스프레드시트와 0.1%p 이내로 일치함을 확인했다.
PRESETS = {
    "안정형 (3%)": {
        "daily_buy_pct": 0.03,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "가장 안전. 낙폭이 얕은 대신 수익도 낮다.",
        "CAGR": 8.4,
        "MDD": -9.6,
        "효율": 0.87,
    },
    "안정형+ (6.5%)": {
        "daily_buy_pct": 0.065,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "표준보다 한 단계 보수적.",
        "CAGR": 19.5,
        "MDD": -20.3,
        "효율": 0.96,
    },
    "표준 (10%) ★추천": {
        "daily_buy_pct": 0.10,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "원본 기본값. 위험 대비 효율이 가장 좋은 구간이라 처음엔 여기서 시작하는 게 좋다.",
        "CAGR": 30.5,
        "MDD": -30.4,
        "효율": 1.01,
    },
    "적극형 (11.1%)": {
        "daily_buy_pct": 0.111,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "표준에 익숙해진 뒤 다음 단계. 효율 손해가 거의 없다.",
        "CAGR": 33.5,
        "MDD": -33.4,
        "효율": 1.00,
    },
    "공격형 (12.5%)": {
        "daily_buy_pct": 0.125,
        "target_return": 0.0275,
        "stop_days": 10,
        "sell_day_buy_mode": "never",
        "설명": "여기부터 효율이 떨어지기 시작한다. 원본 자료도 이 위로는 권하지 않는다.",
        "CAGR": 36.5,
        "MDD": -37.6,
        "효율": 0.97,
    },
    "표준 + 손절재진입": {
        "daily_buy_pct": 0.10,
        "target_return": 0.027,
        "stop_days": 10,
        "sell_day_buy_mode": "any_loss",
        "설명": "손절로 비워진 자리를 바로 다시 채운다. 효율은 오르지만 낙폭이 깊어지고, 매일 판단할 게 하나 늘어난다.",
        "CAGR": 35.4,
        "MDD": -44.0,
        "효율": 0.80,
    },
}


def apply_preset(state: dict, preset_name: str) -> dict:
    """프리셋을 현재 설정에 적용한다 (수수료·정수주 설정은 그대로 둔다)."""
    p = PRESETS.get(preset_name)
    if not p:
        return state
    for k in ("daily_buy_pct", "target_return", "stop_days", "sell_day_buy_mode"):
        state["config"][k] = p[k]
    state["config"]["preset_name"] = preset_name
    save_state(state)
    return state


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


def _today_str() -> str:
    return date.today().isoformat()


def load_state() -> dict:
    """저장된 상태를 불러온다. 없으면 기본값."""
    if not STATE_PATH.exists():
        return {
            "config": dict(DEFAULT_CONFIG),
            "cash": DEFAULT_CONFIG["initial_cash"],
            "lots": [],
            "closed": [],
            "created": _today_str(),
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    # 예전 파일에 없는 설정 키는 기본값으로 채운다
    cfg = {**DEFAULT_CONFIG, **state.get("config", {})}
    state["config"] = cfg
    state.setdefault("lots", [])
    state.setdefault("closed", [])
    return state


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_state(initial_cash: float, config: dict = None) -> dict:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cfg["initial_cash"] = initial_cash
    state = {
        "config": cfg,
        "cash": float(initial_cash),
        "lots": [],
        "closed": [],
        "created": _today_str(),
    }
    save_state(state)
    return state


def business_days_between(start: str, end: str) -> int:
    """두 날짜 사이 영업일 수 (주말만 제외, 공휴일은 미반영).

    미국 공휴일까지 정확히 세려면 거래일 캘린더가 필요하다. 여기서는 근사치를 쓰고,
    화면에 '공휴일은 반영 안 됨'을 표시한다.
    """
    import numpy as np

    return int(np.busday_count(np.datetime64(start), np.datetime64(end)))


def total_assets(state: dict, current_price: float) -> float:
    shares = sum(lot["qty"] for lot in state["lots"])
    return state["cash"] + shares * current_price


def daily_plan(state: dict, current_price: float, prev_total: float = None) -> dict:
    """오늘 할 일을 계산한다.

    prev_total: 어제 마감 기준 총자산. 안 주면 현재가로 계산한 총자산을 쓴다
                (장중에 미리 볼 때는 근사치가 된다).
    """
    cfg = state["config"]
    today = _today_str()

    sells = []
    for lot in state["lots"]:
        held = business_days_between(lot["buy_date"], today)
        target = lot["target_price"]
        if held >= cfg["stop_days"]:
            sells.append({**lot, "보유영업일": held, "사유": "강제청산(10일 경과)", "목표가": target})
        elif held >= 1 and current_price >= target:
            sells.append({**lot, "보유영업일": held, "사유": "목표 도달", "목표가": target})

    # 각 매도 건의 예상 손익 (매도일 매수 허용 판정에 쓴다)
    fee_now = cfg.get("fee_rate", 0.0)
    sold_pnls = [
        s["qty"] * current_price * (1 - fee_now) - s["qty"] * s["buy_price"] * (1 + fee_now)
        for s in sells
    ]
    for s, p in zip(sells, sold_pnls):
        s["예상손익"] = p

    mode = cfg.get("sell_day_buy_mode", "never")
    will_buy = should_buy_on_sell_day(sold_pnls, mode)

    base = prev_total if prev_total is not None else total_assets(state, current_price)
    desired = base * cfg["daily_buy_pct"]
    budget = min(desired, state["cash"]) if will_buy else 0.0

    fee = cfg.get("fee_rate", 0.0)
    buy_qty = 0.0
    actual_cost = 0.0
    if will_buy and current_price > 0 and budget > 0:
        # 스프레드시트 방식: 수량 = 예산*(1-수수료)/종가, 실제비용 = 종가*수량*(1+수수료)
        buy_qty = budget * (1 - fee) / current_price
        if cfg.get("whole_shares", True):
            buy_qty = float(int(buy_qty))
        actual_cost = buy_qty * current_price * (1 + fee)

    return {
        "날짜": today,
        "현재가": current_price,
        "총자산": total_assets(state, current_price),
        "예수금": state["cash"],
        "보유수량": sum(lot["qty"] for lot in state["lots"]),
        "보유건수": len(state["lots"]),
        "매도대상": sells,
        "매수여부": will_buy,
        "매수예산": budget,
        "매수금액": actual_cost,
        "매수수량": buy_qty,
        "매수후_목표가": target_price_for(current_price, cfg) if will_buy else None,
        "예수금부족": will_buy and desired > state["cash"] + 1e-9,
    }


def record_buy(state: dict, buy_date: str, price: float, qty: float) -> dict:
    """실제로 매수한 내역을 기록한다."""
    cfg = state["config"]
    fee = cfg.get("fee_rate", 0.0)
    cost = price * qty * (1 + fee)
    state["cash"] -= cost
    state["lots"].append(
        {
            "buy_date": buy_date,
            "buy_price": float(price),
            "qty": float(qty),
            "target_price": target_price_for(float(price), cfg),
        }
    )
    save_state(state)
    return state


def record_sell(state: dict, lot_index: int, sell_date: str, price: float, reason: str = "") -> dict:
    """실제로 매도한 내역을 기록한다."""
    cfg = state["config"]
    fee = cfg.get("fee_rate", 0.0)
    lot = state["lots"].pop(lot_index)
    proceeds = price * lot["qty"] * (1 - fee)
    state["cash"] += proceeds
    pnl = proceeds - lot["buy_price"] * lot["qty"]
    state["closed"].append(
        {
            **lot,
            "sell_date": sell_date,
            "sell_price": float(price),
            "pnl": float(pnl),
            "return_pct": (price / lot["buy_price"] - 1) * 100,
            "reason": reason,
        }
    )
    save_state(state)
    return state


def performance(state: dict, current_price: float) -> dict:
    """지금까지 성적."""
    closed = state["closed"]
    cfg = state["config"]
    realized = sum(c["pnl"] for c in closed)
    unrealized = sum((current_price - lot["buy_price"]) * lot["qty"] for lot in state["lots"])
    total = total_assets(state, current_price)
    initial = cfg["initial_cash"]
    wins = sum(1 for c in closed if c["pnl"] > 0)

    return {
        "총자산": total,
        "초기자금": initial,
        "총수익": total - initial,
        "총수익률(%)": (total / initial - 1) * 100 if initial else 0.0,
        "실현손익": realized,
        "평가손익": unrealized,
        "매매횟수": len(closed),
        "승률(%)": (wins / len(closed) * 100) if closed else 0.0,
        "보유비중(%)": (sum(l["qty"] for l in state["lots"]) * current_price / total * 100) if total else 0.0,
    }
