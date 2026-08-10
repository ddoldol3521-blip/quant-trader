"""종사종팔 V5 — '어젯밤 결과 + 오늘 넣을 주문' 알림 메시지를 만든다.

한 통에 다 담는다. 어젯밤 결과와 오늘 주문은 같은 종가에서 나오는 값이라
따로 보낼 이유가 없고, 나누면 하나를 놓칠 위험만 생긴다.

설정을 읽는 순서는 **환경변수 → 저장된 설정 파일** 이다.

- 내 PC에서는 앱에서 저장한 값(jongsa_settings.json)이 그대로 쓰인다.
- GitHub Actions처럼 설정 파일이 없는 곳에서는 환경변수로 넣는다.
  저장소가 공개라 시드처럼 보이면 안 되는 값은 Secrets에 둔다.

앱의 '알림' 탭과 scripts/jongsa_daily_notify.py가 이 모듈을 같이 쓴다.
"""

import os
from datetime import date

import pandas as pd

from src.data.kr_data import get_dividends, get_kr_ohlcv
from src.jongsa_backtest import run_jongsa
from src.jongsa_live import load_config, make_held_counter, order_plan
from src.scheduler import load_jongsa_notify_config
from src.telegram_notify import load_telegram_config, send_telegram_message

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 환경변수 이름 → 설정 항목. 표로 두면 문서와 코드가 따로 놀지 않는다.
ENV_KEYS = {
    "JONGSA_TICKER": "종목",
    "JONGSA_START": "시작일 (YYYY-MM-DD)",
    "JONGSA_SEED": "시드 달러",
    "JONGSA_SPLITS": "분할수",
    "JONGSA_TARGET": "목표수익률 %",
    "JONGSA_STOP": "청산 영업일",
    "JONGSA_FEE": "편도 수수료 %",
    "JONGSA_RANGE": "매수 범위 %",
    "JONGSA_APP_URL": "앱 주소",
}


def env(name, default=None, cast=str):
    """환경변수를 읽는다. 비어 있거나 형식이 틀리면 기본값을 쓴다."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        return default


def settings() -> dict:
    """저장된 설정을 기본으로 쓰고, 환경변수가 있으면 그쪽을 우선한다."""
    cfg = load_config()
    splits = env("JONGSA_SPLITS", None, int)

    return {
        "ticker": env("JONGSA_TICKER", cfg["ticker"]).upper(),
        "start": env("JONGSA_START", cfg["start_date"]),
        "seed": env("JONGSA_SEED", cfg["initial_cash"], float),
        "daily_pct": 1 / splits if splits else cfg["daily_buy_pct"],
        "tgt": env("JONGSA_TARGET", cfg["target_return"] * 100, float) / 100,
        "stop": env("JONGSA_STOP", cfg["stop_days"], int),
        "fee": env("JONGSA_FEE", cfg["fee_rate"] * 100, float) / 100,
        "rng": env("JONGSA_RANGE", cfg["buy_range_pct"] * 100, float) / 100,
        "app_url": env("JONGSA_APP_URL", load_jongsa_notify_config().get("app_url", "")),
        "fee_in_target": cfg["fee_in_target"],
        "whole_shares": cfg["whole_shares"],
        "sell_day_buy_mode": cfg["sell_day_buy_mode"],
        "reinvest": cfg["reinvest"],
        "moc_available": cfg["moc_available"],
        "ladder_rungs": env("JONGSA_LADDER_RUNGS", cfg.get("ladder_rungs", 0), int),
        "ladder_step": env("JONGSA_LADDER_STEP", cfg.get("ladder_step", 0.03) * 100, float) / 100,
    }


def build_message(today: date = None) -> str:
    """오늘 보낼 메시지 전체를 만든다."""
    s = settings()
    ticker, stop, rng = s["ticker"], s["stop"], s["rng"]
    if not s["start"]:
        raise ValueError("시작일이 비어 있습니다. 앱에서 시작일을 정하고 저장하세요.")

    today = today or date.today()
    hist = get_kr_ohlcv(ticker, s["start"], today.isoformat())
    res = run_jongsa(
        hist, "V5", initial_cash=s["seed"], target_return=s["tgt"],
        daily_buy_pct=s["daily_pct"], stop_days=stop, fee_rate=s["fee"],
        whole_shares=s["whole_shares"], fee_in_target=s["fee_in_target"],
        sell_day_buy_mode=s["sell_day_buy_mode"], reinvest=s["reinvest"],
        buy_range_pct=rng, dividends=get_dividends(ticker, s["start"], today.isoformat()),
        ladder_rungs=s["ladder_rungs"], ladder_step=s["ladder_step"],
    )

    last = res.daily_log.iloc[-1]
    price = float(last["종가"])
    close_date = pd.Timestamp(last["날짜"]).date()
    total = float(last["총자산"])
    cash = float(last["예수금"])

    L = [f"📅 {today:%m/%d}({WEEKDAY_KR[today.weekday()]}) {ticker} 주문", ""]

    # ---------- 어젯밤 결과 ----------
    L.append(f"【{close_date:%m/%d} 마감 결과】  종가 ${price:,.2f}")
    did = False
    if pd.notna(last.get("매도수량")):
        why = str(last["청산사유"])
        icon = "🛑" if "손절" in why else "🎯"
        L.append(
            f"{icon} {float(last['매도수량']):,.0f}주 매도 · "
            f"실현 ${float(last['실현손익']):+,.2f}  ({why})"
        )
        did = True
    if pd.notna(last.get("매수수량")):
        L.append(
            f"🟢 {float(last['매수수량']):,.0f}주 매수 @ ${price:,.2f}"
            f" · 목표가 ${float(last['목표가']):,.2f}"
        )
        did = True
    if not did:
        L.append("· 체결 없음")

    if pd.notna(last.get("배당")):
        L.append(f"🪙 배당 ${float(last['배당']):,.2f} 입금")

    L.append(f"💰 총자산 ${total:,.0f} ({res.net_profit:+,.0f} / {res.net_return_pct:+.2f}%)")
    L.append(f"   보유 {int(last['보유건수'])}건 {float(last['보유수량']):,.0f}주 · 현금 ${cash:,.0f}")
    if res.total_dividends > 0:
        L.append(f"   받은 배당 합계 ${res.total_dividends:,.2f} (재투자 안 함)")
    L.append("")

    # ---------- 오늘 넣을 주문 ----------
    plan_cfg = dict(
        stop_days=stop, daily_buy_pct=s["daily_pct"], target_return=s["tgt"],
        fee_rate=s["fee"], fee_in_target=s["fee_in_target"],
        whole_shares=s["whole_shares"], buy_range_pct=rng,
        moc_available=s["moc_available"], _last_close=price,
        ladder_rungs=s["ladder_rungs"], ladder_step=s["ladder_step"],
    )
    # 배당은 하루 매수금 기준액에서 뺀다 (재투자하지 않으므로)
    plan = order_plan(res.final_lots, res.final_cash, total - res.total_dividends, plan_cfg,
                      today.isoformat(), trading_dates=hist.index)
    forced, pending, buy = plan["강제매도"], plan["목표매도"], plan["매수"]

    L.append("【오늘 넣을 주문】")
    if not forced and not pending and not buy["type"]:
        L.append("· 넣을 주문 없음")
    for od in forced:
        L.append(f"🛑 MOC 매도 {od['qty']:,.0f}주  (손절일 도래 · {od['보유영업일']}영업일)")
    for od in pending:
        L.append(f"🎯 LOC 매도 {od['qty']:,.0f}주 @ ${od['target_price']:,.2f}  ({od['보유영업일']}일차)")
    if buy["type"] == "LOC":
        L.append(f"🟢 LOC 매수 {buy['qty']:,.0f}주 @ ${buy['limit']:,.2f}  (최저 목표가 −$0.01)")
    elif buy["type"] == "LOC_RANGE":
        L.append(
            f"🟢 LOC 매수 {buy['qty']:,.0f}주 @ ${buy['limit']:,.2f}"
            f"  (팔 물량 없음 · 매수범위 {rng * 100:.0f}%)"
        )
    elif buy["type"] == "MOC":
        L.append(f"🟢 MOC 매수 {buy['qty']:,.0f}주")
    else:
        L.append(f"· 매수 없음 — {buy['reason']}")

    # 사다리 주문 — 종가가 더 낮게 끝날 때 예산을 마저 쓰는 추가 주문
    cum = buy["qty"]
    for q, px in buy.get("사다리", []):
        cum += q
        L.append(f"   ➕ 추가 {q:,.0f}주 @ ${px:,.2f}  (여기까지면 총 {cum:,.0f}주)")

    if plan["부족"] and buy["type"]:
        L.append(f"⚠️ 예수금 부족 — 목표 ${plan['목표금액']:,.0f} 중 ${cash:,.0f}만 가능")

    # ---------- 손절 예고 ----------
    # 목표 매도는 주문만 걸어두면 알아서 체결되지만, 손절은 날짜를 직접
    # 세야 해서 제일 놓치기 쉽다. 그래서 3영업일 전부터 미리 알린다.
    held_of = make_held_counter(today.isoformat(), hist.index)
    soon = sorted(
        (stop - held_of(lot["buy_date"]), lot) for lot in res.final_lots
    )
    soon = [(left, lot) for left, lot in soon if 0 < left <= 3]
    if soon:
        L.append("")
        L.append("【손절 예정】")
        for left, lot in soon:
            L.append(f"⚠️ {lot['qty']:,.0f}주 ({lot['buy_date']} 매수) — {left}영업일 뒤 MOC 매도")

    L.append("")
    L.append("⏰ 미 동부 15:50 (한국 새벽 4:50 / 서머타임 해제 시 5:50) 까지")
    if s["app_url"]:
        L.append(f"📱 {s['app_url']}")
    L.append("※ 종가 기준 계산입니다. 실제 체결은 증권사 기록이 우선입니다.")

    return "\n".join(L)


def resolve_telegram() -> tuple:
    """봇 토큰과 chat_id를 찾는다. 환경변수가 먼저, 없으면 저장된 파일."""
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return load_telegram_config()


def send_now(today: date = None) -> str:
    """지금 바로 한 통 보낸다. 보낸 내용을 돌려준다."""
    msg = build_message(today)
    token, chat_id = resolve_telegram()
    send_telegram_message(msg, token=token, chat_id=chat_id)
    return msg
