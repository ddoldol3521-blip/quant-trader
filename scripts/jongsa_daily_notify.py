"""종사종팔 V5 — 매일 텔레그램으로 '어젯밤 결과 + 오늘 넣을 주문'을 보낸다.

한 통에 다 담는다. 어젯밤 결과와 오늘 주문은 같은 종가에서 나오는 값이라
따로 보낼 이유가 없고, 나누면 하나를 놓칠 위험만 생긴다.

설정은 환경변수로 받는다. GitHub Actions에서 돌릴 때 시드처럼 남에게
보이면 안 되는 값은 Secrets에 넣는다 (저장소가 공개라도 안 드러난다).

    TELEGRAM_BOT_TOKEN   봇 토큰            (필수)
    TELEGRAM_CHAT_ID     받을 사람 chat_id  (필수)
    JONGSA_START         시작일 YYYY-MM-DD  (필수)
    JONGSA_SEED          시드 달러           (기본 10000)
    JONGSA_TICKER        종목               (기본 SOXL)
    JONGSA_SPLITS        분할수             (기본 10)
    JONGSA_TARGET        목표수익률 %        (기본 2.75)
    JONGSA_STOP          청산 영업일         (기본 10)
    JONGSA_FEE           편도 수수료 %       (기본 0)
    JONGSA_RANGE         매수 범위 %         (기본 10)
    JONGSA_APP_URL       앱 주소 (있으면 링크로 붙인다)
    JONGSA_DRY_RUN       1이면 보내지 않고 화면에만 출력
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data.kr_data import get_kr_ohlcv
from src.jongsa_backtest import run_jongsa
from src.jongsa_live import business_days_between as bdays
from src.jongsa_live import order_plan
from src.telegram_notify import send_telegram_message

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def env(name, default=None, cast=str):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        return default


def build_message() -> str:
    ticker = env("JONGSA_TICKER", "SOXL").upper()
    start = env("JONGSA_START")
    if not start:
        raise SystemExit("JONGSA_START(시작일)가 없습니다.")

    seed = env("JONGSA_SEED", 10000.0, float)
    splits = env("JONGSA_SPLITS", 10, int)
    tgt = env("JONGSA_TARGET", 2.75, float) / 100
    stop = env("JONGSA_STOP", 10, int)
    fee = env("JONGSA_FEE", 0.0, float) / 100
    rng = env("JONGSA_RANGE", 10.0, float) / 100
    app_url = env("JONGSA_APP_URL")

    today = date.today()
    hist = get_kr_ohlcv(ticker, start, today.isoformat())
    res = run_jongsa(
        hist, "V5", initial_cash=seed, target_return=tgt, daily_buy_pct=1 / splits,
        stop_days=stop, fee_rate=fee, whole_shares=True, fee_in_target=True,
        sell_day_buy_mode="never", reinvest=True, buy_range_pct=rng,
    )

    log = res.daily_log
    last = log.iloc[-1]
    price = float(last["종가"])
    close_date = pd.Timestamp(last["날짜"]).date()
    total = float(last["총자산"])
    cash = float(last["예수금"])

    L = [f"📅 {today:%m/%d}({WEEKDAY_KR[today.weekday()]}) {ticker} 주문", ""]

    # ---------- 어젯밤 결과 ----------
    L.append(f"【{close_date:%m/%d} 마감 결과】  종가 ${price:,.2f}")
    did = False
    if pd.notna(last.get("매도수량")):
        pnl = float(last["실현손익"])
        why = str(last["청산사유"])
        icon = "🛑" if "손절" in why else "🎯"
        L.append(f"{icon} {float(last['매도수량']):,.0f}주 매도 · 실현 ${pnl:+,.2f}  ({why})")
        did = True
    if pd.notna(last.get("매수수량")):
        L.append(
            f"🟢 {float(last['매수수량']):,.0f}주 매수 @ ${price:,.2f}"
            f" · 목표가 ${float(last['목표가']):,.2f}"
        )
        did = True
    if not did:
        L.append("· 체결 없음")

    profit = res.net_profit
    L.append(
        f"💰 총자산 ${total:,.0f} ({profit:+,.0f} / {res.net_return_pct:+.2f}%)"
    )
    L.append(f"   보유 {int(last['보유건수'])}건 {float(last['보유수량']):,.0f}주 · 현금 ${cash:,.0f}")
    L.append("")

    # ---------- 오늘 넣을 주문 ----------
    cfg = dict(stop_days=stop, daily_buy_pct=1 / splits, target_return=tgt, fee_rate=fee,
               fee_in_target=True, whole_shares=True, buy_range_pct=rng,
               moc_available=True, _last_close=price)
    plan = order_plan(res.final_lots, res.final_cash, total, cfg, today.isoformat())
    forced, pending, buy = plan["강제매도"], plan["목표매도"], plan["매수"]

    L.append("【오늘 넣을 주문】")
    if not forced and not pending and not buy["type"]:
        L.append("· 넣을 주문 없음")
    for s in forced:
        L.append(f"🛑 MOC 매도 {s['qty']:,.0f}주  (손절일 도래 · {s['보유영업일']}영업일)")
    for s in pending:
        L.append(f"🎯 LOC 매도 {s['qty']:,.0f}주 @ ${s['target_price']:,.2f}  ({s['보유영업일']}일차)")
    if buy["type"] == "LOC":
        L.append(f"🟢 LOC 매수 {buy['qty']:,.0f}주 @ ${buy['limit']:,.2f}  (최저 목표가 −$0.01)")
    elif buy["type"] == "LOC_RANGE":
        L.append(
            f"🟢 LOC 매수 {buy['qty']:,.0f}주 @ ${buy['limit']:,.2f}"
            f"  (팔 물량 없음 · 매수범위 {rng*100:.0f}%)"
        )
    elif buy["type"] == "MOC":
        L.append(f"🟢 MOC 매수 {buy['qty']:,.0f}주")
    else:
        L.append(f"· 매수 없음 — {buy['reason']}")

    if plan["부족"] and buy["type"]:
        L.append(f"⚠️ 예수금 부족 — 목표 ${plan['목표금액']:,.0f} 중 ${cash:,.0f}만 가능")

    # ---------- 손절 예고 ----------
    # 목표 매도는 알아서 체결되지만 손절은 날짜를 직접 세야 해서 제일 놓치기 쉽다.
    soon = []
    for lot in res.final_lots:
        left = stop - bdays(lot["buy_date"], today.isoformat())
        if 0 < left <= 3:
            soon.append((left, lot))
    if soon:
        L.append("")
        L.append("【손절 예정】")
        for left, lot in sorted(soon):
            L.append(
                f"⚠️ {lot['qty']:,.0f}주 ({lot['buy_date']} 매수) — "
                f"{left}영업일 뒤 MOC 매도"
            )

    L.append("")
    L.append("⏰ 미 동부 15:50 (한국 새벽 4:50 / 서머타임 해제 시 5:50) 까지")
    if app_url:
        L.append(f"📱 {app_url}")
    L.append("※ 종가 기준 계산입니다. 실제 체결은 증권사 기록이 우선입니다.")

    return "\n".join(L)


def main():
    msg = build_message()
    if env("JONGSA_DRY_RUN") == "1":
        print(msg)
        return

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # 이게 없으면 send_telegram_message가 설정파일을 찾다가 엉뚱한 에러를 낸다.
        # GitHub Actions에서는 Secrets를 안 넣은 게 원인이므로 그렇게 알려준다.
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비어 있습니다.\n"
            "GitHub 저장소 → Settings → Secrets and variables → Actions 에서 등록하세요."
        )

    send_telegram_message(msg, token=token, chat_id=chat_id)
    print("보냈습니다.")
    print(msg)


if __name__ == "__main__":
    main()
