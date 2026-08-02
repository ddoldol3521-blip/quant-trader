"""종사종팔 V5 설정 관리.

**매매 기록을 저장하지 않는다.** 시작일과 규칙이 정해지면 그날부터 오늘까지의
모든 매매가 자동으로 결정되기 때문이다. 앱은 열릴 때마다 시작일부터 다시 계산한다.
그래서 여기에 남는 건 '설정값'뿐이고, 그걸 jongsa_settings.json에 저장한다.
"""

import json
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "jongsa_settings.json"

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


def load_config() -> dict:
    """저장된 설정을 불러온다. 없거나 항목이 빠져 있으면 기본값으로 채운다."""
    if not SETTINGS_PATH.exists():
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


def save_config(cfg: dict) -> None:
    keep = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)


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


def business_days_between(start: str, end: str) -> int:
    """두 날짜 사이 영업일 수 (주말만 제외, 공휴일은 미반영).

    미국 공휴일까지 정확히 세려면 거래일 캘린더가 필요하다. 여기서는 근사치를 쓰고,
    화면에 '공휴일은 반영 안 됨'을 표시한다.
    """
    import numpy as np

    return int(np.busday_count(np.datetime64(start), np.datetime64(end)))
