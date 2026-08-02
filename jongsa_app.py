"""종사종팔 V5 전용 앱 — 매일 '얼마 사고 뭘 팔지'만 알려주는 단순 화면.

메인 앱(app.py)과 분리해서, 매일 실제로 쓰는 것만 남겼다.
보유 내역은 jongsa_positions.json 하나를 공유한다.
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src.data.kr_data import get_kr_ohlcv
from src.jongsa_live import SELL_DAY_MODES
from src.jongsa_live import business_days_between as bdays
from src.jongsa_live import daily_plan, load_state, performance
from src.jongsa_live import record_buy, record_sell, reset_state, save_state

st.set_page_config(page_title="종사종팔 V5", page_icon="🔁", layout="wide")

state = load_state()
cfg = state["config"]

# ---------------------------------------------------------------- 헤더
st.title("🔁 종사종팔 V5")
st.caption(f"{cfg['ticker']} 분할매매 — 오늘 뭘 할지만 알려줍니다. 주문은 증권사 앱에서 직접 하세요.")

# ---------------------------------------------------------------- 현재가
c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    if st.button("현재가 불러오기", type="primary", width="stretch"):
        try:
            with st.spinner("조회 중..."):
                end = datetime.today().strftime("%Y-%m-%d")
                start = (datetime.today() - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
                px = get_kr_ohlcv(cfg["ticker"], start, end)
                st.session_state["price"] = float(px["Close"].iloc[-1])
                st.session_state["price_date"] = str(px.index[-1].date())
        except Exception as e:
            st.error(f"조회 실패: {e}")
with c2:
    price = st.number_input(
        "현재가($)",
        min_value=0.01,
        value=float(st.session_state.get("price", 100.0)),
        step=0.01,
        format="%.2f",
        label_visibility="collapsed",
    )
with c3:
    if st.session_state.get("price_date"):
        st.caption(f"불러온 종가 기준일: **{st.session_state['price_date']}**")
    else:
        st.caption("버튼을 누르거나 직접 입력하세요.")

plan = daily_plan(state, price)
perf = performance(state, price)

# ---------------------------------------------------------------- 요약
m1, m2, m3, m4 = st.columns(4)
m1.metric("총자산", f"${plan['총자산']:,.0f}", f"{perf['총수익']:+,.0f}")
m2.metric("예수금", f"${plan['예수금']:,.0f}")
m3.metric("보유", f"{plan['보유수량']:,.0f}주 / {plan['보유건수']}건")
m4.metric("총수익률", f"{perf['총수익률(%)']:+.2f}%")

st.divider()

# ---------------------------------------------------------------- 오늘 할 일
left, right = st.columns(2)

with left:
    st.markdown("## 🔴 팔 것")
    if plan["매도대상"]:
        for s in plan["매도대상"]:
            pnl_pct = (price / s["buy_price"] - 1) * 100
            st.warning(
                f"### {s['qty']:,.0f}주 매도\n"
                f"- 매수일: {s['buy_date']} (\\${s['buy_price']:.2f})\n"
                f"- 목표가: \\${s['목표가']:.2f}\n"
                f"- 사유: **{s['사유']}**\n"
                f"- 현재 수익률: **{pnl_pct:+.2f}%**"
            )
    else:
        st.success("### 없습니다")
        st.caption("목표가에 닿았거나 10영업일 지난 건이 없습니다.")

with right:
    st.markdown("## 🟢 살 것")
    if plan["매수여부"] and plan["매수금액"] > 0:
        st.info(
            f"### {plan['매수수량']:,.0f}주 매수\n"
            f"- 금액: 약 **\\${plan['매수금액']:,.2f}**\n"
            f"- 매수 후 목표가: **\\${plan['매수후_목표가']:.2f}**\n"
            f"- 목표 미달 시 청산: **{cfg['stop_days']}영업일 뒤**"
        )
        st.caption("장 마감 무렵 시장가로 사면 됩니다.")
        if plan["예수금부족"]:
            st.warning("예수금이 부족해 목표금액보다 적게 계산됐습니다.")
    elif not plan["매수여부"]:
        mode = cfg.get("sell_day_buy_mode", "never")
        if mode == "never":
            st.error("### 오늘은 매수 안 함")
            st.caption("매도가 있는 날이라 매수하지 않습니다. (원본 V5 규칙)")
        else:
            losses = [s for s in plan["매도대상"] if s.get("예상손익", 0) < 0]
            st.error("### 오늘은 매수 안 함")
            st.caption(
                f"설정: {SELL_DAY_MODES[mode]} — 오늘 매도 {len(plan['매도대상'])}건 중 "
                f"손실 {len(losses)}건이라 조건 미충족."
            )
    else:
        st.error("### 예수금 부족")

# ---------------------------------------------------------------- 기록
st.divider()
st.markdown("## ✍️ 주문했으면 기록하기")
st.caption("**이걸 해야 다음날 계산이 맞습니다.** 증권사에서 실제 체결된 가격·수량을 넣으세요.")

rec1, rec2 = st.columns(2)

with rec1:
    st.markdown("#### 매수 기록")
    bd = st.date_input("매수일", value=date.today(), key="bd")
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        bp = st.number_input("체결가($)", min_value=0.01, value=float(price), step=0.01, format="%.2f", key="bp")
    with bcol2:
        bq = st.number_input("수량(주)", min_value=0.0, value=float(plan["매수수량"]), step=1.0, format="%.0f", key="bq")
    if st.button("매수 기록 추가", width="stretch"):
        if bq > 0:
            record_buy(state, bd.isoformat(), bp, bq)
            st.success(f"{bq:,.0f}주 @ ${bp:.2f} 기록 완료")
            st.rerun()
        else:
            st.warning("수량을 입력하세요.")

with rec2:
    st.markdown("#### 매도 기록")
    if state["lots"]:
        labels = [
            f"{l['buy_date']} · {l['qty']:,.0f}주 @ ${l['buy_price']:.2f} (목표 ${l['target_price']:.2f})"
            for l in state["lots"]
        ]
        pick = st.selectbox("어느 건을 팔았나요", labels, key="sellpick")
        scol1, scol2 = st.columns(2)
        with scol1:
            sd = st.date_input("매도일", value=date.today(), key="sd")
        with scol2:
            sp = st.number_input("체결가($)", min_value=0.01, value=float(price), step=0.01, format="%.2f", key="sp")
        if st.button("매도 기록 추가", width="stretch"):
            record_sell(state, labels.index(pick), sd.isoformat(), sp, "수동기록")
            st.success("기록 완료")
            st.rerun()
    else:
        st.caption("보유 중인 건이 없습니다.")

# ---------------------------------------------------------------- 보유 목록
if state["lots"]:
    st.divider()
    st.markdown("## 📦 보유 목록")
    rows = []
    for l in state["lots"]:
        held = bdays(l["buy_date"], date.today().isoformat())
        rows.append(
            {
                "매수일": l["buy_date"],
                "매수가($)": round(l["buy_price"], 2),
                "수량": round(l["qty"]),
                "목표가($)": round(l["target_price"], 2),
                "현재손익(%)": round((price / l["buy_price"] - 1) * 100, 2),
                "보유일": held,
                "청산까지": max(0, cfg["stop_days"] - held),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("보유일은 주말만 제외한 근사치입니다 (미국 공휴일 미반영).")

# ---------------------------------------------------------------- 사이드바: 성과 + 설정
with st.sidebar:
    st.markdown("### 📊 성과")
    st.metric("총자산", f"${perf['총자산']:,.0f}", f"{perf['총수익']:+,.0f}")
    s1, s2 = st.columns(2)
    s1.metric("매매", f"{perf['매매횟수']}회")
    s2.metric("승률", f"{perf['승률(%)']:.0f}%")
    st.caption(f"실현 ${perf['실현손익']:+,.0f} / 평가 ${perf['평가손익']:+,.0f}")
    st.caption(f"보유비중 {perf['보유비중(%)']:.1f}%")

    if state["closed"]:
        with st.expander(f"청산 기록 {len(state['closed'])}건"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "매도일": c["sell_date"],
                            "수익률(%)": round(c["return_pct"], 2),
                            "손익($)": round(c["pnl"], 2),
                        }
                        for c in reversed(state["closed"][-30:])
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

    st.divider()
    with st.expander("⚙️ 설정"):
        t = st.number_input("목표수익률(%)", 0.5, 20.0, cfg["target_return"] * 100, 0.05)
        p = st.number_input("하루 매수 비율(%)", 1.0, 50.0, cfg["daily_buy_pct"] * 100, 0.1)
        d = st.number_input("강제청산 영업일", 2, 60, cfg["stop_days"])
        f = st.number_input("편도 수수료(%)", 0.0, 1.0, cfg.get("fee_rate", 0.0) * 100, 0.001, format="%.4f")
        fit = st.checkbox("목표가에 수수료 반영", cfg.get("fee_in_target", True))
        ws = st.checkbox("정수주만 매수", cfg.get("whole_shares", True))

        keys = list(SELL_DAY_MODES.keys())
        cur = cfg.get("sell_day_buy_mode", "never")
        m = st.radio(
            "매도일에도 매수?",
            keys,
            index=keys.index(cur) if cur in keys else 0,
            format_func=lambda k: SELL_DAY_MODES[k],
        )

        if st.button("설정 저장", width="stretch"):
            cfg.update(
                {
                    "target_return": t / 100,
                    "daily_buy_pct": p / 100,
                    "stop_days": int(d),
                    "fee_rate": f / 100,
                    "fee_in_target": bool(fit),
                    "whole_shares": bool(ws),
                    "sell_day_buy_mode": m,
                }
            )
            save_state(state)
            st.success("저장했습니다")
            st.rerun()

    with st.expander("🔄 처음부터 다시"):
        rc = st.number_input("초기 투자금($)", 100.0, value=float(cfg["initial_cash"]), step=100.0)
        ok = st.checkbox("정말 초기화")
        if st.button("초기화 실행", disabled=not ok, width="stretch"):
            reset_state(rc, cfg)
            st.success("초기화했습니다")
            st.rerun()
        st.caption("보유·청산 기록이 전부 사라집니다.")

    st.divider()
    st.caption(
        "**검증됨**: 원본 스프레드시트 대조 결과 CAGR 30.5%, MDD -30.4%, 승률 79.5% 재현 "
        "(2010~2024). 다만 **SOXL은 3배 레버리지**라 실제로 원금의 상당 부분을 잃을 수 있습니다. "
        "이 도구는 계산기일 뿐 수익을 보장하지 않습니다."
    )
