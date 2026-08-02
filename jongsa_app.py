"""종사종팔 V5 전용 앱 — 매일 '얼마 사고 뭘 팔지'만 알려주는 화면.

메인 앱(app.py)과 분리해서, 매일 실제로 쓰는 것만 남겼다.
보유 내역은 jongsa_positions.json에 저장된다.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src.data.kr_data import get_kr_ohlcv
from src.jongsa_backtest import run_jongsa
from src.jongsa_live import PRESETS, SELL_DAY_MODES
from src.jongsa_live import apply_preset
from src.jongsa_live import business_days_between as bdays
from src.jongsa_live import daily_plan, load_state, performance
from src.jongsa_live import record_buy, record_sell, reset_state, save_state

st.set_page_config(page_title="종사종팔 V5", page_icon="🔁", layout="wide")

state = load_state()
cfg = state["config"]


@st.cache_data(ttl=3600, show_spinner=False)
def load_price_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    return get_kr_ohlcv(ticker, start, end)


st.title("🔁 종사종팔 V5")
st.caption(f"{cfg['ticker']} 분할매매 — 오늘 뭘 할지만 알려줍니다. 주문은 증권사 앱에서 직접 하세요.")

tab_today, tab_bt, tab_set = st.tabs(["📅 오늘 할 일", "📊 백테스트", "⚙️ 설정"])

# ================================================================= 오늘 할 일
with tab_today:
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("현재가 불러오기", type="primary", width="stretch"):
            try:
                with st.spinner("조회 중..."):
                    end = datetime.today().strftime("%Y-%m-%d")
                    start = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
                    px = load_price_history(cfg["ticker"], start, end)
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

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("총자산", f"${plan['총자산']:,.0f}", f"{perf['총수익']:+,.0f}")
    m2.metric("예수금", f"${plan['예수금']:,.0f}")
    m3.metric("보유", f"{plan['보유수량']:,.0f}주 / {plan['보유건수']}건")
    m4.metric("총수익률", f"{perf['총수익률(%)']:+.2f}%")

    st.divider()

    # ---------- 팔 것 ----------
    st.markdown("## 🔴 팔 것")
    if plan["매도대상"]:
        for i, s in enumerate(plan["매도대상"], 1):
            pnl_pct = (price / s["buy_price"] - 1) * 100
            pnl_amt = s.get("예상손익", 0)
            is_stop = "강제" in s["사유"]
            box = st.error if is_stop else st.success
            box(
                f"### {i}. {s['qty']:,.0f}주 팔기\n"
                f"| | |\n|---|---|\n"
                f"| **왜** | {'🛑 **손절** — 10영업일이 지났습니다. 가격 상관없이 오늘 팝니다.' if is_stop else '🎯 **익절** — 목표가에 닿았습니다.'} |\n"
                f"| 산 날 | {s['buy_date']} (\\${s['buy_price']:.2f}) |\n"
                f"| 목표가 | \\${s['목표가']:.2f} |\n"
                f"| 지금 | \\${price:.2f} ({pnl_pct:+.2f}%) |\n"
                f"| 예상 손익 | **\\${pnl_amt:+,.2f}** |"
            )
        st.caption("**장 마감 무렵 시장가로 파세요.** 여러 건이면 전부 팝니다.")
    else:
        st.success("### 팔 것 없음")
        st.caption("목표가에 닿았거나 10영업일이 지난 건이 없습니다.")

    # ---------- 살 것 ----------
    st.markdown("## 🟢 살 것")
    if plan["매수여부"] and plan["매수금액"] > 0:
        st.info(
            f"### {plan['매수수량']:,.0f}주 사기\n"
            f"| | |\n|---|---|\n"
            f"| 금액 | 약 **\\${plan['매수금액']:,.2f}** |\n"
            f"| 사는 가격 | 오늘 종가 (\\${price:.2f} 근처) |\n"
            f"| **목표가** | **\\${plan['매수후_목표가']:.2f}** ← 여기 닿으면 팔기 |\n"
            f"| **손절 예정일** | **{(date.today() + timedelta(days=int(cfg['stop_days'] * 1.45))).isoformat()} 무렵** "
            f"({cfg['stop_days']}영업일 뒤) ← 그때까지 목표 미달이면 무조건 팔기 |"
        )
        st.caption("**장 마감 무렵 시장가로 사세요.** 지정가를 걸면 백테스트상 수익률이 오히려 낮아졌습니다.")
        if plan["예수금부족"]:
            st.warning("예수금이 부족해서 목표금액보다 적게 계산됐습니다.")
    elif not plan["매수여부"]:
        mode = cfg.get("sell_day_buy_mode", "never")
        st.error("### 오늘은 사지 않음")
        if mode == "never":
            st.caption("**매도가 있는 날은 매수하지 않습니다.** (원본 V5 규칙)")
        else:
            losses = [s for s in plan["매도대상"] if s.get("예상손익", 0) < 0]
            st.caption(
                f"설정: {SELL_DAY_MODES[mode]} — 오늘 매도 {len(plan['매도대상'])}건 중 "
                f"손실 {len(losses)}건이라 조건에 안 맞습니다."
            )
    else:
        st.error("### 예수금 부족으로 매수 불가")

    # ---------- 규칙 요약 ----------
    with st.expander("📖 이 전략 규칙 요약 (헷갈릴 때 펼쳐보세요)"):
        st.markdown(
            f"""
**매일 장 마감 무렵에 딱 두 가지만 확인합니다.**

**1단계 — 팔 것부터 확인**

보유 중인 각 건마다:
- 오늘 종가가 **목표가({cfg['target_return']*100:.2f}% 위) 이상**이면 → 🎯 **판다** (익절)
- 산 지 **{cfg['stop_days']}영업일**이 지났으면 → 🛑 **가격 상관없이 판다** (손절)
- 둘 다 아니면 → 그냥 들고 있는다

**2단계 — 살 것 확인**

- 오늘 **판 게 하나라도 있으면** → {'사지 않는다' if cfg.get('sell_day_buy_mode','never') == 'never' else SELL_DAY_MODES[cfg.get('sell_day_buy_mode')]}
- 판 게 없으면 → **어제 마감 총자산의 {cfg['daily_buy_pct']*100:.1f}%** 만큼 산다

**손절에 대해**

이 전략의 손절은 '얼마 떨어지면 판다'가 아니라 **며칠 지나면 판다**입니다.
-30%가 나도 {cfg['stop_days']}영업일 전에는 안 팔고, 그날이 오면 -30%든 +1%든 무조건 팝니다.
**이걸 어기면 백테스트 결과가 의미 없어집니다.**

**절대 하면 안 되는 것**
- 목표가 왔는데 "더 오를 것 같아서" 안 팔기
- 손절일인데 "곧 오를 것 같아서" 안 팔기
- 무서워서 중간에 다 팔아버리기
"""
        )

    # ---------- 기록 ----------
    st.divider()
    st.markdown("## ✍️ 주문했으면 기록하기")
    st.caption("**이걸 해야 다음날 계산이 맞습니다.** 증권사에서 실제 체결된 가격·수량을 넣으세요.")

    rec1, rec2 = st.columns(2)
    with rec1:
        st.markdown("#### 매수 기록")
        bd = st.date_input("매수일", value=date.today(), key="bd")
        b1, b2 = st.columns(2)
        with b1:
            bp = st.number_input("체결가($)", min_value=0.01, value=float(price), step=0.01, format="%.2f", key="bp")
        with b2:
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
            s1, s2 = st.columns(2)
            with s1:
                sd = st.date_input("매도일", value=date.today(), key="sd")
            with s2:
                sp = st.number_input("체결가($)", min_value=0.01, value=float(price), step=0.01, format="%.2f", key="sp")
            if st.button("매도 기록 추가", width="stretch"):
                record_sell(state, labels.index(pick), sd.isoformat(), sp, "수동기록")
                st.success("기록 완료")
                st.rerun()
        else:
            st.caption("보유 중인 건이 없습니다.")

    # ---------- 보유 목록 ----------
    if state["lots"]:
        st.divider()
        st.markdown("## 📦 보유 목록")
        rows = []
        for l in state["lots"]:
            held = bdays(l["buy_date"], date.today().isoformat())
            left = max(0, cfg["stop_days"] - held)
            rows.append(
                {
                    "매수일": l["buy_date"],
                    "매수가($)": round(l["buy_price"], 2),
                    "수량": round(l["qty"]),
                    "목표가($)": round(l["target_price"], 2),
                    "현재손익(%)": round((price / l["buy_price"] - 1) * 100, 2),
                    "목표까지(%)": round((l["target_price"] / price - 1) * 100, 2),
                    "보유일": held,
                    "손절까지": f"{left}일" if left > 0 else "오늘!",
                }
            )
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption("보유일은 주말만 제외한 근사치입니다 (미국 공휴일 미반영).")

# ================================================================= 백테스트
with tab_bt:
    st.markdown("## 📊 백테스트")
    st.caption(
        "현재 설정으로 과거에 어땠을지 직접 돌려봅니다. "
        "원본 스프레드시트와 대조해 검증된 엔진입니다 (CAGR·MDD·승률 모두 0.1%p 이내 일치)."
    )

    bc1, bc2, bc3, bc4 = st.columns(4)
    with bc1:
        bt_start = st.date_input("시작일", value=date(2010, 4, 1), key="bt_start")
    with bc2:
        bt_end = st.date_input("종료일", value=date.today(), key="bt_end")
    with bc3:
        bt_cash = st.number_input("초기자금($)", min_value=100.0, value=10000.0, step=1000.0, key="bt_cash")
    with bc4:
        bt_use_current = st.checkbox("현재 설정 사용", value=True, key="bt_use_cur")

    if bt_use_current:
        bt_pct, bt_tgt, bt_stop = cfg["daily_buy_pct"], cfg["target_return"], cfg["stop_days"]
        bt_mode = cfg.get("sell_day_buy_mode", "never")
        st.caption(
            f"현재 설정 → 매수비율 {bt_pct*100:.1f}% / 목표 {bt_tgt*100:.2f}% / "
            f"청산 {bt_stop}영업일 / {SELL_DAY_MODES[bt_mode]}"
        )
    else:
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            bt_pct = st.number_input("매수비율(%)", 1.0, 50.0, cfg["daily_buy_pct"] * 100, 0.1, key="btp") / 100
        with p2:
            bt_tgt = st.number_input("목표수익률(%)", 0.5, 20.0, cfg["target_return"] * 100, 0.05, key="btt") / 100
        with p3:
            bt_stop = st.number_input("청산 영업일", 2, 60, cfg["stop_days"], key="bts")
        with p4:
            keys = list(SELL_DAY_MODES.keys())
            bt_mode = st.selectbox(
                "매도일 매수", keys, index=keys.index(cfg.get("sell_day_buy_mode", "never")),
                format_func=lambda k: SELL_DAY_MODES[k], key="btm",
            )

    if st.button("백테스트 실행", type="primary", key="run_bt"):
        try:
            with st.spinner(f"{cfg['ticker']} 데이터 받아서 계산 중..."):
                hist = load_price_history(cfg["ticker"], bt_start.isoformat(), bt_end.isoformat())
                res = run_jongsa(
                    hist, "V5",
                    initial_cash=bt_cash, target_return=bt_tgt, daily_buy_pct=bt_pct,
                    stop_days=int(bt_stop), fee_rate=cfg.get("fee_rate", 0.0),
                    whole_shares=cfg.get("whole_shares", True),
                    fee_in_target=cfg.get("fee_in_target", True),
                    sell_day_buy_mode=bt_mode,
                )
                st.session_state["bt_res"] = res
                st.session_state["bt_hist"] = hist
        except Exception as e:
            st.error(f"실행 실패: {e}")

    res = st.session_state.get("bt_res")
    hist = st.session_state.get("bt_hist")

    if res is not None and hist is not None:
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("CAGR (연평균)", f"{res.cagr_pct:.2f}%")
        r2.metric("MDD (최대낙폭)", f"{res.mdd_pct:.2f}%")
        r3.metric("승률", f"{res.win_rate_pct:.1f}%")
        r4.metric("효율 (CAGR/MDD)", f"{res.cagr_pct/-res.mdd_pct:.2f}")
        r5.metric("최종자산", f"${res.final_value:,.0f}")

        t1, t2, t3 = st.columns(3)
        t1.metric("총 매매", f"{res.num_trades}회")
        t2.metric("목표달성 청산", f"{res.num_target_sells}회")
        t3.metric("손절 청산", f"{res.num_forced_sells}회")
        st.caption(f"평균 보유비중 {res.avg_exposure_pct:.1f}% (나머지는 현금) · 예수금 소진 {res.cash_exhausted_days}일")

        # ---- 연도별 (기본 표시) ----
        st.markdown("### 연도별 성과")
        eq = res.equity_curve
        close = hist["Close"]
        tr = res.trades.copy()
        if not tr.empty:
            tr["연도"] = pd.to_datetime(tr["매도일"]).dt.year

        yr_rows = []
        for yr, grp in eq.groupby(eq.index.year):
            if len(grp) < 20:
                continue
            s_ret = (grp.iloc[-1] / grp.iloc[0] - 1) * 100
            s_dd = ((grp - grp.cummax()) / grp.cummax()).min() * 100
            bh = close.loc[grp.index]
            b_ret = (bh.iloc[-1] / bh.iloc[0] - 1) * 100
            if not tr.empty:
                yt = tr[tr["연도"] == yr]
                wr = (yt["손익"] > 0).mean() * 100 if len(yt) else None
                n = len(yt)
            else:
                wr, n = None, 0
            yr_rows.append(
                {
                    "연도": yr,
                    "수익률(%)": round(s_ret, 1),
                    "연내 MDD(%)": round(s_dd, 1),
                    "승률(%)": round(wr, 1) if wr is not None else None,
                    "매매": n,
                    f"{cfg['ticker']} 수익률(%)": round(b_ret, 1),
                }
            )
        ydf = pd.DataFrame(yr_rows)
        st.dataframe(ydf, width="stretch", hide_index=True)

        if len(ydf) >= 3:
            full = ydf.iloc[1:-1] if len(ydf) > 2 else ydf
            plus = int((full["수익률(%)"] > 0).sum())
            st.info(
                f"**온전한 {len(full)}개년 중 {plus}년 플러스** · "
                f"평균 {full['수익률(%)'].mean():.1f}% · "
                f"최고 {full['수익률(%)'].max():.1f}% / 최저 {full['수익률(%)'].min():.1f}% · "
                f"연내 낙폭 평균 {full['연내 MDD(%)'].mean():.1f}% (최악 {full['연내 MDD(%)'].min():.1f}%)"
            )
            st.caption("첫 해와 마지막 해는 부분연도라 요약에서 제외했습니다.")

        # ---- 자산곡선 ----
        st.markdown("### 자산 곡선")
        chart_df = pd.DataFrame(
            {"전략": eq, f"{cfg['ticker']} 단순보유": close / close.iloc[0] * bt_cash}
        )
        st.line_chart(chart_df)
        bh_dd = ((close / close.cummax()) - 1).min() * 100
        bh_ret = (close.iloc[-1] / close.iloc[0] - 1) * 100
        d1, d2 = st.columns(2)
        d1.metric(f"{cfg['ticker']} 단순보유 최대낙폭", f"{bh_dd:.1f}%")
        d2.metric("이 전략 최대낙폭", f"{res.mdd_pct:.1f}%")
        st.caption(
            f"단순보유는 같은 기간 총 {bh_ret:,.0f}% 올랐지만 한때 {bh_dd:.1f}%까지 빠졌습니다. "
            "수익률만 보지 말고 이 낙폭 차이를 꼭 같이 보세요 — 대부분은 이걸 못 버티고 중간에 팝니다."
        )

        with st.expander(f"매매 기록 ({len(res.trades)}건)"):
            st.dataframe(res.trades.tail(300), width="stretch", hide_index=True)
    else:
        st.info("위에서 **백테스트 실행**을 누르면 결과가 나옵니다. 데이터 받는 데 10~20초 걸립니다.")

# ================================================================= 설정
with tab_set:
    st.markdown("## ⚙️ 설정")

    st.markdown("### 프리셋 — 검증된 설정 묶음")
    st.caption(
        "SOXL 2010~2024 백테스트 기준입니다. **효율(CAGR/MDD)이 높을수록 위험 대비 수익이 좋습니다.**"
    )

    preset_df = pd.DataFrame(
        [
            {
                "프리셋": name,
                "매수비율(%)": round(p["daily_buy_pct"] * 100, 1),
                "CAGR(%)": p["CAGR"],
                "MDD(%)": p["MDD"],
                "효율": p["효율"],
                "설명": p["설명"],
            }
            for name, p in PRESETS.items()
        ]
    )
    st.dataframe(preset_df, width="stretch", hide_index=True)

    cur_preset = cfg.get("preset_name", "(직접 설정)")
    st.caption(f"현재 적용된 프리셋: **{cur_preset}**")

    pc1, pc2 = st.columns([2, 1])
    with pc1:
        chosen = st.selectbox("프리셋 고르기", list(PRESETS.keys()), key="preset_pick")
    with pc2:
        st.write("")
        if st.button("이 프리셋 적용", type="primary", width="stretch"):
            apply_preset(state, chosen)
            st.success(f"'{chosen}' 적용 완료")
            st.rerun()

    p = PRESETS[chosen]
    st.info(
        f"**{chosen}** — {p['설명']}\n\n"
        f"매수비율 {p['daily_buy_pct']*100:.1f}% · 목표 {p['target_return']*100:.2f}% · "
        f"청산 {p['stop_days']}영업일 · {SELL_DAY_MODES[p['sell_day_buy_mode']]}\n\n"
        f"과거 성적: CAGR **{p['CAGR']}%** / MDD **{p['MDD']}%** / 효율 **{p['효율']}**"
    )

    st.divider()
    st.markdown("### 직접 조정")

    d1, d2, d3 = st.columns(3)
    with d1:
        t = st.number_input("목표수익률(%)", 0.5, 20.0, cfg["target_return"] * 100, 0.05)
    with d2:
        pp = st.number_input("하루 매수 비율(%)", 1.0, 50.0, cfg["daily_buy_pct"] * 100, 0.1)
    with d3:
        d = st.number_input("강제청산 영업일", 2, 60, cfg["stop_days"])

    keys = list(SELL_DAY_MODES.keys())
    cur = cfg.get("sell_day_buy_mode", "never")
    m = st.radio(
        "매도가 있는 날에도 매수할까",
        keys,
        index=keys.index(cur) if cur in keys else 0,
        format_func=lambda k: SELL_DAY_MODES[k],
        horizontal=True,
    )

    st.markdown("#### 수수료 (증권사마다 다릅니다)")
    f1, f2, f3 = st.columns(3)
    with f1:
        f = st.number_input("편도 수수료(%)", 0.0, 1.0, cfg.get("fee_rate", 0.0) * 100, 0.001, format="%.4f")
        st.caption("사고팔 때 각각. 무료면 0.")
    with f2:
        fit = st.checkbox("목표가에 수수료 반영", cfg.get("fee_in_target", True))
        st.caption("수수료 내고도 목표% 남기려면 켜기.")
    with f3:
        ws = st.checkbox("정수주만 매수", cfg.get("whole_shares", True))
        st.caption("소수점 주식 못 사면 켜기.")

    if f > 0:
        eff = (1 + t / 100) * (1 + 2 * f / 100) - 1 if fit else t / 100
        st.caption(f"→ 실제 목표 상승률 **{eff*100:.3f}%**")

    if st.button("직접 설정 저장", width="stretch"):
        cfg.update(
            {
                "target_return": t / 100,
                "daily_buy_pct": pp / 100,
                "stop_days": int(d),
                "fee_rate": f / 100,
                "fee_in_target": bool(fit),
                "whole_shares": bool(ws),
                "sell_day_buy_mode": m,
                "preset_name": "(직접 설정)",
            }
        )
        save_state(state)
        st.success("저장했습니다. (이미 보유 중인 건의 목표가는 그대로입니다)")
        st.rerun()

    st.divider()
    st.markdown("### 🔄 처음부터 다시")
    rr1, rr2 = st.columns([2, 1])
    with rr1:
        rc = st.number_input("초기 투자금($)", 100.0, value=float(cfg["initial_cash"]), step=100.0)
    with rr2:
        st.write("")
        ok = st.checkbox("정말 초기화")
    if st.button("초기화 실행", disabled=not ok):
        reset_state(rc, cfg)
        st.success("초기화했습니다")
        st.rerun()
    st.caption("⚠️ 보유·청산 기록이 전부 사라집니다.")

# ================================================================= 사이드바
with st.sidebar:
    st.markdown("### 📊 내 성과")
    perf_side = performance(state, st.session_state.get("price", 100.0))
    st.metric("총자산", f"${perf_side['총자산']:,.0f}", f"{perf_side['총수익']:+,.0f}")
    s1, s2 = st.columns(2)
    s1.metric("매매", f"{perf_side['매매횟수']}회")
    s2.metric("승률", f"{perf_side['승률(%)']:.0f}%")
    st.caption(f"실현 ${perf_side['실현손익']:+,.0f} / 평가 ${perf_side['평가손익']:+,.0f}")
    st.caption(f"보유비중 {perf_side['보유비중(%)']:.1f}%")

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
    st.caption(f"현재 프리셋: **{cfg.get('preset_name', '(직접 설정)')}**")
    st.caption(
        f"매수 {cfg['daily_buy_pct']*100:.1f}% · 목표 {cfg['target_return']*100:.2f}% · "
        f"청산 {cfg['stop_days']}일"
    )

    st.divider()
    st.caption(
        "**검증됨**: 원본 스프레드시트 대조로 CAGR·MDD·승률 재현 확인. "
        "다만 **SOXL은 3배 레버리지**라 원금의 상당 부분을 잃을 수 있습니다. "
        "이 도구는 계산기일 뿐 수익을 보장하지 않습니다."
    )
