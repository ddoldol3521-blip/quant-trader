import calendar
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src import markets as market_api
from src.backtest.engine import run_backtest
from src.charts import equity_curve_figure
from src.data.kr_data import get_kr_ohlcv, resample_ohlcv
from src.cot_data import COT_MARKETS, get_price_with_cot, summarize_latest
from src.jongsa_live import business_days_between as jongsa_business_days
from src.jongsa_live import daily_plan as jongsa_daily_plan
from src.jongsa_live import load_state as jongsa_load_state
from src.jongsa_live import performance as jongsa_performance
from src.jongsa_live import record_buy as jongsa_record_buy
from src.jongsa_live import record_sell as jongsa_record_sell
from src.jongsa_live import SELL_DAY_MODES as JONGSA_SELL_MODES
from src.jongsa_live import reset_state as jongsa_reset_state
from src.jongsa_live import save_state as jongsa_save_state
from src.interactive_chart import (
    AVAILABLE_OSCILLATORS,
    build_cot_price_chart,
    build_price_chart,
    strategy_chart_config,
)
from src.market_dashboard import get_dashboard, get_put_call_ratio
from src.optimization import optimize_strategy
from src.portfolio import run_universe_portfolio_backtest
from src.scheduler import (
    get_task_status,
    load_schedule_config,
    register_windows_task,
    remove_windows_task,
    save_schedule_config,
)
from src.playbook import (
    RESEARCH_DATE,
    RESEARCH_SCOPE,
    YEARLY_TOP_RULE_KR,
    combo_to_strategies,
    get_baseline,
    get_playbook,
)
from src.screening import scan as scan_stocks
from src.strategies import STRATEGIES
from src.strategy_comparison import compare_strategies, summarize_comparison
from src.telegram_notify import (
    find_chat_id,
    load_telegram_config,
    save_telegram_config,
    send_telegram_message,
)
from src.validation import collect_agreement_events, compute_baseline, summarize_agreement

st.set_page_config(page_title="퀀트 트레이더", layout="wide")

head_col1, head_col2 = st.columns([2, 3])
with head_col1:
    st.title("퀀트 트레이더")
with head_col2:
    REGION = st.radio(
        "어느 시장을 볼까요?",
        market_api.REGIONS,
        horizontal=True,
        key="region",
        format_func=lambda r: f"🇰🇷 {r} 주식" if r == market_api.KR else f"🇺🇸 {r} 주식",
    )

MI = market_api.info(REGION)
SUB_MARKETS = MI["sub_markets"]
DEFAULT_SUB = MI["default_sub_markets"]
CURRENCY = MI["currency"]
IS_KR = REGION == market_api.KR

st.caption(
    f"지금 **{REGION} 주식** 기준으로 보고 있습니다. "
    f"위에서 시장을 바꾸면 아래 모든 탭(종목 검색·차트·백테스트·스캔)이 그 시장으로 전환됩니다. "
    f"({MI['universe_note']})"
)

STRATEGY_NAMES = list(STRATEGIES.keys())
CHART_CONFIG = {"scrollZoom": True}
DEFAULT_MAS = [5, 20, 60, 120, 240]
TIMEFRAME_MAP = {"일봉": ("D", "일"), "주봉": ("W", "주"), "월봉": ("M", "월")}

QUICK_PERIODS = [
    ("1개월", 1), ("3개월", 3), ("6개월", 6), ("1년", 12),
    ("2년", 24), ("3년", 36), ("4년", 48), ("5년", 60), ("10년", 120),
]


def _subtract_months(d, months):
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _qb_set_today(key):
    st.session_state[key] = date.today()


def _qb_set_start(start_key, ref_key, months):
    ref_val = _as_date(st.session_state.get(ref_key)) if ref_key else None
    if ref_val is None:
        ref_val = date.today()
    st.session_state[start_key] = _subtract_months(ref_val, months)


def render_date_quick_buttons(start_key, ref_key=None):
    """시작일 빠른 설정 버튼 행. '오늘' 버튼은 ref_key(종료일)를 오늘로 바꾸고,
    기간 버튼들은 ref_key의 '현재 값' 기준으로 시작일을 역산한다."""
    buttons = ([("오늘", None)] if ref_key else []) + QUICK_PERIODS
    for i in range(0, len(buttons), 5):
        row = buttons[i : i + 5]
        cols = st.columns(5)
        for col, (label, months) in zip(cols, row):
            if months is None:
                col.button(label, key=f"{ref_key}_qb_today", on_click=_qb_set_today, args=(ref_key,))
            else:
                col.button(
                    label,
                    key=f"{start_key}_qb_{months}",
                    on_click=_qb_set_start,
                    args=(start_key, ref_key, months),
                )


def render_strategy_info(strategy_name):
    """선택한 전략의 매수/매도 조건을 바로 보여준다."""
    module = STRATEGIES[strategy_name]
    buy_cond = getattr(module, "BUY_CONDITION", None)
    sell_cond = getattr(module, "SELL_CONDITION", None)
    type_label = getattr(module, "TYPE_LABEL", "")
    if not (buy_cond and sell_cond):
        st.caption(module.DESCRIPTION)
        return
    st.caption(f"**{strategy_name}** ({type_label}) — 🟢 살 때: {buy_cond} / 🔴 팔 때: {sell_cond}")


def render_universe_note(sub_markets, limit):
    """선택한 시장이 어떤 순서로 잘리는지 알려준다.

    S&P500은 알파벳순이라 '상위 N개'가 규모 상위가 아니다 — 이걸 모르면 결과를 오해한다.
    """
    if not sub_markets:
        return
    st.caption(f"정렬 순서 — {market_api.ordering_note(sub_markets)}")
    if market_api.has_alpha_ordering(sub_markets) and limit < 503:
        st.warning(
            f"⚠️ S&P500은 **알파벳순**이라 '상위 {limit}개'는 규모 상위가 아니라 "
            f"**A로 시작하는 회사 {limit}개**에 가깝습니다. 500개 전부를 보려면 개수를 최대로 올리거나, "
            "규모 순으로 보고 싶으면 NASDAQ·NYSE를 선택하세요."
        )


def render_take_profit_controls(prefix: str):
    """익절 옵션 UI. (사용여부, 비율) 반환."""
    c1, c2 = st.columns(2)
    with c1:
        use_tp = st.checkbox("익절(먼저 이익 실현) 사용", key=f"{prefix}_use_tp")
    with c2:
        tp = st.number_input(
            "익절 비율(%)", min_value=1, max_value=200, value=20, key=f"{prefix}_take_profit", disabled=not use_tp
        ) / 100
    st.caption(
        "체크하면 전략의 매도 조건과 무관하게, 산 가격 대비 이 비율만큼 오르면 미리 팔아버립니다. "
        "승률은 올라가지만 큰 상승분을 놓쳐서 총수익률은 오히려 떨어지는 경우가 많아요 — 켜고 끄면서 비교해보세요."
    )
    return (tp if use_tp else None)


(
    tab_guide,
    tab_stocks,
    tab_chart,
    tab_list,
    tab_backtest,
    tab_compare,
    tab_scan,
    tab_full,
    tab_validate,
    tab_optimize,
    tab_portfolio,
    tab_market,
    tab_notify,
    tab_playbook,
    tab_jongsa,
    tab_research,
) = st.tabs(
    [
        "시작하기",
        "종목 목록",
        "차트",
        "전략 목록",
        "백테스트",
        "전략 비교",
        "오늘 신호 스캔",
        "전체 스캔",
        "신호 검증",
        "파라미터 최적화",
        "포트폴리오 백테스트",
        "시장 상황판",
        "텔레그램 알림",
        "📋 매매 플레이북",
        "🔁 종사종팔 V5",
        "전략 추천 (리서치)",
    ]
)

# ---------------------------------------------------------------- 시작하기
with tab_guide:
    st.subheader("처음 오셨다면 이 순서로 써보세요")
    st.caption("탭이 여러 개라 막막할 수 있어요. 아래 순서대로 하나씩 해보시면 됩니다.")

    st.info(
        "**제일 간단하게 쓰는 법 (3단계)**\n\n"
        "1. **전체 스캔** 탭 → 버튼 누르기 → '오늘 사도 괜찮아 보이는 종목' 목록이 나옵니다\n"
        "2. 목록에서 아는 회사나 마음에 드는 게 있는지 봅니다\n"
        "3. 사고 싶으면 **증권사 앱에서 직접** 삽니다\n\n"
        "이 앱은 '오늘 세일하는 물건 찾아주는 검색기'예요. 물건을 대신 사주지는 않습니다."
    )

    st.markdown("### 1. 종목 목록 — 종목코드 찾기")
    st.write("종목코드를 모르면 여기서 회사 이름으로 검색하세요. (예: 삼성전자, 카카오)")

    st.markdown("### 2. 전략 목록 — 어떤 방식들이 있는지 보기")
    st.write(
        "9개 전략이 각각 '언제 사고 언제 파는지' 나와 있어요. 크게 두 종류입니다 — "
        "**오르는거 따라사기**(요즘 계속 오르면 산다) vs **싸졌을때 줍기**(많이 빠지면 산다)."
    )

    st.markdown("### 3. 백테스트 — 과거에 이 방식을 썼으면 얼마 벌었을지")
    st.write("아는 종목 + 전략 하나를 골라 실행하면 수익률·승률·최대손실폭이 숫자로 나옵니다.")

    st.markdown("### 4. 전략 비교 — 믿을 만한 전략 찾기 (가장 중요)")
    st.info(
        "종목 하나로만 테스트한 결과는 우연일 수 있어요. 여기서 코스피+코스닥 상위 종목으로 9개 전략을 전부 비교합니다. "
        "**'선별기간'과 '검증기간' 둘 다에서** 순위가 괜찮은 전략이 상대적으로 신뢰할 만합니다. (몇 분 걸립니다)"
    )

    st.markdown("### 5. 오늘 신호 스캔 / 전체 스캔 — 오늘 후보 종목 찾기")
    st.write("고른 전략으로 오늘 매수 신호가 뜬 종목을 찾습니다. 종목을 골라 차트도 볼 수 있어요.")

    st.markdown("### 6. 직접 판단해서 증권사 앱(HTS/MTS)에서 주문")
    st.warning(
        "여기서 끝입니다. **자동으로 주문이 나가지 않습니다.** "
        "화면에 뜬 종목은 참고자료일 뿐, 최종 판단과 실제 매매는 본인이 직접 하셔야 합니다."
    )

    st.divider()
    st.markdown("**심화 도구 (익숙해지면)**")
    st.write("- **신호 검증**: '여러 전략이 동시에 사라고 하면 진짜 더 좋았나'를 과거 데이터로 확인")
    st.write("- **파라미터 최적화**: 전략의 숫자 설정을 종목별로 바꿔가며 탐색")
    st.write("- **포트폴리오 백테스트**: 한 종목 몰빵이 아니라 여러 종목에 나눠 담고 손절/익절까지 적용")
    st.write("- **시장 상황판**: VIX·금리·환율 등으로 지금 시장 분위기 보기")
    st.write("- **텔레그램 알림**: 매일 정해진 시간에 자동 스캔 결과 받기")
    st.write("- **전략 추천(리서치)**: 미리 대규모로 돌려본 백테스트 결론 정리")

    st.divider()
    st.markdown("**꼭 기억할 것 3가지**")
    st.markdown(
        "- 여기 나오는 신호·수익률은 전부 **과거 통계**일 뿐, 미래에 오른다는 보장이 아닙니다\n"
        "- 종목 하나·기간 하나짜리 결과는 믿지 말고 항상 여러 종목/기간으로 확인하세요\n"
        "- 실시간이 아니라 **일봉(하루 단위) 기준**입니다 — 장 마감 후 확인하는 게 가장 정확합니다"
    )

# ---------------------------------------------------------------- 종목 목록
with tab_stocks:
    st.subheader(f"{REGION} 종목 찾기")
    st.caption(f"코드를 몰라도 이름으로 검색할 수 있습니다. {MI['universe_note']}")

    stock_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"stock_markets_{REGION}")

    listing_key = f"full_listing_{REGION}"
    if listing_key not in st.session_state:
        st.session_state[listing_key] = None

    if st.button("종목 목록 불러오기", key=f"stocks_load_{REGION}") or st.session_state[listing_key] is not None:
        if st.session_state[listing_key] is None:
            with st.spinner("종목 목록 불러오는 중..."):
                st.session_state[listing_key] = market_api.get_full_listing(REGION, stock_markets)

        listing = st.session_state[listing_key]
        search = st.text_input(MI["search_hint"], key=f"stock_search_{REGION}")

        display_df = listing.copy()
        if search:
            mask = display_df["Name"].str.contains(search, case=False, na=False) | display_df["Code"].str.contains(
                search, case=False, na=False
            )
            display_df = display_df[mask]

        display_df = display_df.rename(
            columns={
                "Code": "종목코드" if IS_KR else "티커",
                "Name": "종목명",
                "Market": "시장",
                "Sector": "섹터",
                "Industry": "업종",
                "Close": "현재가",
                "ChagesRatio": "등락률(%)",
                "Marcap": "시가총액(억원)",
            }
        )
        if "시가총액(억원)" in display_df.columns:
            display_df["시가총액(억원)"] = (display_df["시가총액(억원)"] / 1e8).round(0).astype(int)

        st.write(f"{len(display_df)}개 종목")
        st.dataframe(display_df, width="stretch", hide_index=True)

        if st.button("새로고침", key=f"stocks_refresh_{REGION}"):
            st.session_state[listing_key] = None
            st.rerun()

# ---------------------------------------------------------------- 차트
with tab_chart:
    st.subheader("종목 차트 (이동평균 + 보조지표)")
    col1, col2 = st.columns(2)
    with col1:
        chart_ticker = st.text_input(MI["code_hint"], value=MI["sample_code"], key=f"chart_ticker_{REGION}")
        chart_timeframe_label = st.radio("봉 종류", ["일봉", "주봉", "월봉"], horizontal=True, key="chart_timeframe")
        chart_mas = st.multiselect(
            "이동평균 (봉 개수)", [5, 10, 20, 60, 120, 240], default=DEFAULT_MAS, key="chart_mas"
        )
    with col2:
        chart_start = st.date_input("시작일", value=datetime.today() - timedelta(days=365), key="chart_start")
        chart_end = st.date_input("종료일", value=datetime.today(), key="chart_end")

    render_date_quick_buttons("chart_start", "chart_end")

    col3, col4, col5 = st.columns(3)
    with col3:
        chart_bollinger = st.checkbox("볼린저밴드", key="chart_bollinger")
    with col4:
        chart_volume = st.checkbox("거래량", value=True, key="chart_volume")
    with col5:
        chart_oscillators = st.multiselect("보조지표", AVAILABLE_OSCILLATORS, key="chart_oscillators")

    if st.button("차트 그리기", key="chart_run"):
        with st.spinner("데이터 불러오는 중..."):
            chart_df = get_kr_ohlcv(chart_ticker, chart_start.strftime("%Y-%m-%d"), chart_end.strftime("%Y-%m-%d"))
        timeframe_code, unit_label = TIMEFRAME_MAP[chart_timeframe_label]
        chart_df = resample_ohlcv(chart_df, timeframe_code)
        fig = build_price_chart(
            chart_df,
            mas=chart_mas,
            show_bollinger=chart_bollinger,
            show_volume=chart_volume,
            oscillators=chart_oscillators,
            unit_label=unit_label,
        )
        st.plotly_chart(fig, width="stretch", config=CHART_CONFIG)

# ---------------------------------------------------------------- 전략 목록
with tab_list:
    st.subheader("사용 가능한 전략 (9개)")
    st.caption(
        "모든 전략은 크게 두 가지 방식 중 하나예요 — **오르는거 따라사기**(요즘 계속 오르면 산다) 또는 "
        "**싸졌을때 줍기**(많이 빠지면 산다). 카드를 펼치면 정확히 언제 사고 언제 파는지 나옵니다."
    )
    for name, module in STRATEGIES.items():
        type_label = getattr(module, "TYPE_LABEL", "")
        title = f"{name}  ·  {type_label}" if type_label else name
        with st.expander(title):
            buy_cond = getattr(module, "BUY_CONDITION", None)
            sell_cond = getattr(module, "SELL_CONDITION", None)
            if buy_cond and sell_cond:
                st.markdown(f"🟢 **매수 조건**: {buy_cond}")
                st.markdown(f"🔴 **매도 조건**: {sell_cond}")
            else:
                st.write(module.DESCRIPTION)
            with st.expander("자세한 설정값 보기 (숫자를 직접 바꾸고 싶을 때만)"):
                st.write("기본 파라미터:", module.DEFAULT_PARAMS)
                if hasattr(module, "PARAM_GRID"):
                    st.write("최적화 탐색 범위:", module.PARAM_GRID)

# ---------------------------------------------------------------- 백테스트
with tab_backtest:
    st.subheader("단일 종목 백테스트")
    col1, col2 = st.columns(2)
    with col1:
        ticker = st.text_input(MI["code_hint"], value=MI["sample_code"], key=f"bt_ticker_{REGION}")
        strategy = st.selectbox("전략", STRATEGY_NAMES, key="bt_strategy")
    with col2:
        start = st.date_input("시작일", value=datetime(2020, 1, 1), key="bt_start")
        end = st.date_input("종료일", value=datetime.today(), key="bt_end")

    render_strategy_info(strategy)
    render_date_quick_buttons("bt_start", "bt_end")
    bt_take_profit = render_take_profit_controls("bt")

    if st.button("백테스트 실행", key="bt_run"):
        module = STRATEGIES[strategy]
        with st.spinner("백테스트 중..."):
            df = get_kr_ohlcv(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            signals = module.generate_signals(df, **module.DEFAULT_PARAMS)
            result = run_backtest(signals, take_profit_pct=bt_take_profit)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총 거래 횟수", f"{result.num_trades}회")
        m2.metric("승률", f"{result.win_rate_pct:.1f}%")
        m3.metric("총 수익률", f"{result.total_return_pct:.2f}%")
        m4.metric("최대 낙폭(MDD)", f"{result.mdd_pct:.2f}%")

        st.caption(
            "**승률**은 '사고팔기 한 번'을 1회로 쳐서, 판 가격이 산 가격보다 높았던 비율이에요. "
            "추세추종 전략은 승률이 30%대여도 정상입니다 — 몇 번의 큰 수익으로 전체를 버는 구조라서요. "
            "**MDD**는 중간에 최악의 순간 자산이 얼마나 줄었었는지입니다."
        )

        if bt_take_profit:
            tp_trades = [t for t in result.trades if t.action == "TP"]
            st.write(f"익절로 미리 청산된 거래: {len(tp_trades)}건 (전체 {result.num_trades}건 중)")

        st.pyplot(equity_curve_figure(result.equity_curve, f"{ticker} 자산 곡선 ({strategy})", currency=CURRENCY))

        with st.expander("매매 기록 보기"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {"날짜": t.date.date(), "구분": t.action, "수량": t.shares, "가격": t.price, "잔고": round(t.cash_after)}
                        for t in result.trades
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

# ---------------------------------------------------------------- 전략 비교
with tab_compare:
    st.subheader("전략 비교 (선별기간 vs 검증기간)")
    st.caption(
        "선별기간에서 1등이던 전략이 검증기간에서도 상위권인지 봅니다. "
        "순위가 크게 떨어지면 '그 전략이 좋아서가 아니라 그 시기에 운이 좋았을 뿐'일 수 있어요."
    )
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        cmp_start = st.date_input("전체 시작일", value=datetime(2020, 1, 1), key="cmp_start")
    with col2:
        cmp_split = st.date_input("선별/검증 구분일", value=datetime(2024, 1, 1), key="cmp_split")
    with col3:
        cmp_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"cmp_markets_{REGION}")
    with col4:
        cmp_limit = st.number_input("시장별 상위 몇 개", min_value=10, max_value=300, value=100, step=10, key="cmp_limit")

    render_date_quick_buttons("cmp_start", "cmp_split")
    render_universe_note(cmp_markets, cmp_limit)

    if st.button("비교 실행 (몇 분 걸릴 수 있음)", key="cmp_run"):
        cmp_end = datetime.today().strftime("%Y-%m-%d")
        with st.spinner("비교 중..."):
            records = compare_strategies(
                cmp_markets,
                STRATEGY_NAMES,
                cmp_start.strftime("%Y-%m-%d"),
                cmp_split.strftime("%Y-%m-%d"),
                cmp_end,
                limit=cmp_limit,
                show_progress=False,
                region=REGION,
            )
        if records.empty:
            st.write("비교할 데이터가 없습니다.")
        else:
            in_summary = summarize_comparison(records, "in")
            out_summary = summarize_comparison(records, "out")
            c1, c2 = st.columns(2)
            with c1:
                st.write("**선별기간** (전략 고르는 데 쓰는 구간)")
                st.dataframe(in_summary, width="stretch", hide_index=True)
            with c2:
                st.write("**검증기간** (그 선택이 맞았는지 확인하는 구간)")
                st.dataframe(out_summary, width="stretch", hide_index=True)

            if not in_summary.empty and not out_summary.empty:
                top_in = in_summary.iloc[0]["전략"]
                out_ranked = out_summary.reset_index(drop=True)
                match = out_ranked.index[out_ranked["전략"] == top_in]
                if len(match) > 0:
                    rank = int(match[0]) + 1
                    if rank <= 3:
                        st.success(f"선별기간 1위 '{top_in}' → 검증기간 {rank}위 (상위권 유지, 상대적으로 신뢰할 만함)")
                    else:
                        st.warning(f"선별기간 1위 '{top_in}' → 검증기간 {rank}위 (순위 하락, 과최적화 의심)")
            st.caption("주의: 과거 데이터 기준이며 미래 수익을 보장하지 않습니다.")

# ---------------------------------------------------------------- 오늘 신호 스캔
with tab_scan:
    st.subheader("오늘 매수 신호 스캔")
    col1, col2, col3 = st.columns(3)
    with col1:
        markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"scan_markets_{REGION}")
    with col2:
        scan_strategy = st.selectbox("전략", STRATEGY_NAMES, key="scan_strategy")
    with col3:
        scan_limit = st.number_input("시장별 상위 몇 개", min_value=10, max_value=500, value=100, step=10, key="scan_limit")

    render_strategy_info(scan_strategy)
    render_universe_note(markets, scan_limit)

    if st.button("스캔 실행", key="scan_run"):
        if not markets:
            st.warning("시장을 하나 이상 선택하세요.")
        else:
            with st.spinner("스캔 중..."):
                results = scan_stocks(
                    markets, [scan_strategy], limit=scan_limit, show_progress=False, region=REGION
                )
            rows = [{"종목": f"{r['name']}({r['code']})", "신호": r["signals"][scan_strategy]} for r in results]
            df = pd.DataFrame(rows)
            buy_df = df[df["신호"] == "BUY"]
            st.write(f"**매수 신호(BUY): {len(buy_df)}개** / 전체 스캔 {len(df)}개")
            st.caption("BUY = 오늘 막 조건 충족 / HOLD = 며칠째 보유 상태 / SELL = 오늘 막 매도 조건 / NONE = 해당 없음")
            st.dataframe(buy_df, width="stretch", hide_index=True)
            with st.expander("전체 결과 보기"):
                st.dataframe(df, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 전체 스캔
with tab_full:
    st.subheader(f"전체 스캔 (9개 전략 x {REGION} 시장)")
    st.caption("9개 전략을 모두 돌려서, 오늘 매수 신호가 뜬 종목을 찾습니다.")

    col1, col2, col3 = st.columns(3)
    with col1:
        full_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"full_markets_{REGION}")
    with col2:
        full_limit = st.number_input("시장별 상위 몇 개", min_value=10, max_value=300, value=100, step=10, key="full_limit")
    with col3:
        full_min_match = st.slider("최소 몇 개 전략이 일치해야 볼지", 1, len(STRATEGY_NAMES), 2, key="full_min_match")

    render_universe_note(full_markets, full_limit)

    if st.button("전체 스캔 실행 (몇 분 걸릴 수 있음)", key="full_run"):
        if not full_markets:
            st.warning("시장을 하나 이상 선택하세요.")
        else:
            with st.spinner("스캔 중..."):
                results = scan_stocks(
                    full_markets, STRATEGY_NAMES, limit=full_limit, show_progress=False, region=REGION
                )
            matches = []
            for r in results:
                buys = [s for s, sig in r["signals"].items() if sig == "BUY"]
                if buys:
                    matches.append({"code": r["code"], "name": r["name"], "buys": buys})
            matches.sort(key=lambda m: len(m["buys"]), reverse=True)
            st.session_state[f"full_scan_matches_{REGION}"] = matches

    matches = st.session_state.get(f"full_scan_matches_{REGION}")
    if matches is not None:
        filtered = [m for m in matches if len(m["buys"]) >= full_min_match]
        st.write(f"**{full_min_match}개 이상 일치: {len(filtered)}개 종목** (전체 매칭 {len(matches)}개)")

        if filtered:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"종목": f"{m['name']}({m['code']})", "일치개수": len(m["buys"]), "일치 전략": ", ".join(m["buys"])}
                        for m in filtered
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

            st.divider()
            st.write("**종목 하나 골라서 차트 보기**")
            c1, c2, c3 = st.columns(3)
            with c1:
                pick = st.selectbox(
                    "종목 선택",
                    [f"{m['name']}({m['code']})" for m in filtered],
                    key=f"full_chart_select_{REGION}",
                )
            with c2:
                full_tf_label = st.radio("봉 종류", ["일봉", "주봉", "월봉"], horizontal=True, key="full_timeframe")
            with c3:
                full_mas = st.multiselect(
                    "이동평균", [5, 10, 20, 60, 120, 240], default=DEFAULT_MAS, key="full_mas"
                )

            if st.button("차트 보기", key="full_chart_run"):
                chosen = next(m for m in filtered if f"{m['name']}({m['code']})" == pick)
                cfg = strategy_chart_config(chosen["buys"])
                with st.spinner("차트 그리는 중..."):
                    end_d = datetime.today()
                    start_d = end_d - timedelta(days=365 * 2)
                    cdf = get_kr_ohlcv(chosen["code"], start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))
                    tf_code, unit = TIMEFRAME_MAP[full_tf_label]
                    cdf = resample_ohlcv(cdf, tf_code)
                    fig = build_price_chart(
                        cdf,
                        mas=full_mas,
                        show_bollinger=cfg["show_bollinger"],
                        show_volume=True,
                        oscillators=cfg["oscillators"],
                        show_breakout_target=cfg["show_breakout_target"],
                        unit_label=unit,
                        title=f"{chosen['name']} — 일치: {', '.join(chosen['buys'])}",
                    )
                st.plotly_chart(fig, width="stretch", config=CHART_CONFIG)
                st.caption(
                    "캔들·거래량·이동평균선은 모든 종목에서 동일하게 나오고, "
                    "볼린저밴드·보조지표·돌파목표가는 그 종목이 일치한 전략에 따라 자동으로 달라집니다."
                )

# ---------------------------------------------------------------- 신호 검증
with tab_validate:
    st.subheader("신호 검증 (전략 일치 개수별 실제 과거 수익률)")
    st.caption(
        "'여러 전략이 동시에 사라고 하면 더 좋다'가 사실인지 과거 데이터로 확인합니다. "
        "**비교 기준선(아무 날에나 샀을 때)도 같이 계산**해서, 그냥 시장이 좋아서 나온 착시인지 구분합니다."
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        val_years = st.slider("최근 몇 년", 1, 6, 3, key="val_years")
    with col2:
        val_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"val_markets_{REGION}")
    with col3:
        val_limit = st.number_input("시장별 상위 몇 개", min_value=10, max_value=300, value=100, step=10, key="val_limit")

    if st.button("검증 실행 (몇 분 걸릴 수 있음)", key="val_run"):
        val_end = datetime.today()
        val_start = val_end - timedelta(days=int(val_years * 365))
        with st.spinner("검증 중... (신호 사례 + 비교 기준선 둘 다 계산합니다)"):
            events = collect_agreement_events(
                val_markets,
                STRATEGY_NAMES,
                val_start.strftime("%Y-%m-%d"),
                val_end.strftime("%Y-%m-%d"),
                limit=val_limit,
                show_progress=False,
                region=REGION,
            )
            baseline = compute_baseline(
                val_markets,
                val_start.strftime("%Y-%m-%d"),
                val_end.strftime("%Y-%m-%d"),
                limit=val_limit,
                show_progress=False,
                region=REGION,
            )

        if events.empty:
            st.write("검증 기간 동안 매수 신호가 발생한 사례가 없습니다.")
        else:
            if baseline:
                st.write("**비교 기준선 — 신호 없이 그냥 아무 날에나 샀을 때**")
                st.dataframe(pd.DataFrame([baseline]), width="stretch", hide_index=True)
                st.caption("↑ 아래 표의 숫자가 이 기준선보다 확실히 높아야 '신호에 의미가 있다'고 말할 수 있습니다.")

            st.write(f"**전략 일치 개수별 결과** (총 사례 {len(events)}건)")
            st.dataframe(summarize_agreement(events), width="stretch", hide_index=True)
            st.warning(
                "표본 수가 적은 구간(특히 4개 이상 일치)은 우연일 가능성이 높습니다. "
                "샘플수를 꼭 같이 보세요 — 15건짜리 결과로 판단하면 안 됩니다."
            )

# ---------------------------------------------------------------- 파라미터 최적화
with tab_optimize:
    st.subheader("파라미터 최적화 (종목 하나 x 전략 하나)")
    col1, col2 = st.columns(2)
    with col1:
        opt_ticker = st.text_input(MI["code_hint"], value=MI["sample_code"], key=f"opt_ticker_{REGION}")
        opt_strategy = st.selectbox("전략", STRATEGY_NAMES, key="opt_strategy")
    with col2:
        opt_start = st.date_input("전체 시작일", value=datetime(2020, 1, 1), key="opt_start")
        opt_split = st.date_input("선별/검증 구분일", value=datetime(2024, 1, 1), key="opt_split")

    render_strategy_info(opt_strategy)
    render_date_quick_buttons("opt_start", "opt_split")

    if st.button("최적화 실행", key="opt_run"):
        opt_end = datetime.today().strftime("%Y-%m-%d")
        with st.spinner("탐색 중..."):
            try:
                result = optimize_strategy(
                    opt_strategy, opt_ticker, opt_start.strftime("%Y-%m-%d"), opt_split.strftime("%Y-%m-%d"), opt_end
                )
            except ValueError as e:
                st.error(str(e))
                result = None

        if result is not None:
            if result.empty:
                st.write("탐색 결과가 없습니다 (데이터 기간이 너무 짧을 수 있습니다).")
            else:
                st.dataframe(result, width="stretch", hide_index=True)
                st.caption(f"기본 파라미터: {STRATEGIES[opt_strategy].DEFAULT_PARAMS}")
                st.warning(
                    "**선별기간(in_) 1등 조합이 검증기간(out_)에서도 좋다는 보장이 없습니다.** "
                    "둘 다 준수한 조합을 고르는 게 안전해요. in_만 좋고 out_이 나쁘면 과최적화입니다."
                )

# ---------------------------------------------------------------- 포트폴리오 백테스트
with tab_portfolio:
    st.subheader("포트폴리오 백테스트 (여러 종목 동시 보유 + 손절/익절)")
    st.caption("한 종목 몰빵이 아니라 여러 종목에 자금을 나눠 담고, 손절매·익절 규칙까지 적용해서 실전에 가깝게 시뮬레이션합니다.")

    col1, col2 = st.columns(2)
    with col1:
        pf_strategy = st.selectbox("전략", STRATEGY_NAMES, key="pf_strategy")
        pf_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"pf_markets_{REGION}")
        pf_limit = st.number_input("시장별 상위 몇 개 중에서", min_value=10, max_value=300, value=100, step=10, key="pf_limit")
    with col2:
        pf_start = st.date_input("시작일", value=datetime(2020, 1, 1), key="pf_start")
        pf_cash = st.number_input(
            f"초기 투자금({CURRENCY})",
            min_value=MI["cash_step"],
            value=MI["default_cash"],
            step=MI["cash_step"],
            key=f"pf_cash_{REGION}",
        )

    render_strategy_info(pf_strategy)
    render_date_quick_buttons("pf_start")
    render_universe_note(pf_markets, pf_limit)

    col3, col4, col5 = st.columns(3)
    with col3:
        pf_position_size = st.slider("종목당 최대 비중(%)", 5, 100, 20, key="pf_position_size") / 100
    with col4:
        pf_max_positions = st.number_input("동시 보유 최대 종목 수", min_value=1, max_value=20, value=5, key="pf_max_positions")
    with col5:
        pf_use_stop = st.checkbox("손절매 사용", key="pf_use_stop")
        pf_stop_loss = st.number_input(
            "손절 비율(%)", min_value=1, max_value=50, value=10, key="pf_stop_loss", disabled=not pf_use_stop
        ) / 100

    pf_take_profit = render_take_profit_controls("pf")

    if st.button("포트폴리오 백테스트 실행 (몇 분 걸릴 수 있음)", key="pf_run"):
        if not pf_markets:
            st.warning("시장을 하나 이상 선택하세요.")
        else:
            pf_end = datetime.today().strftime("%Y-%m-%d")
            stop_loss_pct = pf_stop_loss if pf_use_stop else None
            with st.spinner("포트폴리오 백테스트 중..."):
                result, code_to_name = run_universe_portfolio_backtest(
                    pf_markets,
                    pf_strategy,
                    pf_start.strftime("%Y-%m-%d"),
                    pf_end,
                    limit=pf_limit,
                    initial_cash=pf_cash,
                    position_size_pct=pf_position_size,
                    max_positions=pf_max_positions,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=pf_take_profit,
                    show_progress=False,
                    region=REGION,
                )

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("대상 종목 수", f"{result.num_stocks}개")
            m2.metric("총 거래 횟수", f"{result.num_trades}회")
            m3.metric("총 수익률", f"{result.total_return_pct:.2f}%")
            m4.metric("최대 낙폭(MDD)", f"{result.mdd_pct:.2f}%")
            st.write(f"승률: {result.win_rate_pct:.1f}%")

            if stop_loss_pct:
                st.write(f"손절매로 청산된 거래: {len([t for t in result.trades if t.action == 'STOP'])}건")
            if pf_take_profit:
                st.write(f"익절로 청산된 거래: {len([t for t in result.trades if t.action == 'TP'])}건")

            st.pyplot(
                equity_curve_figure(result.equity_curve, f"포트폴리오 자산 곡선 ({pf_strategy})", currency=CURRENCY)
            )

            with st.expander("매매 기록 보기"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "날짜": t.date.date(),
                                "종목": f"{code_to_name.get(t.code, t.code)}({t.code})",
                                "구분": t.action,
                                "수량": t.shares,
                                "가격": t.price,
                                "잔고": round(t.cash_after),
                            }
                            for t in result.trades
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

# ---------------------------------------------------------------- 시장 상황판
with tab_market:
    st.subheader("시장 상황판")
    st.caption("지금 시장 분위기가 어떤지 한눈에 보는 화면입니다.")

    st.warning(
        "⚠️ **이건 '예측'이 아니라 '현재 상태 참고자료'입니다.**\n\n"
        "학계 연구는 일관되게 \"시장 타이밍을 맞추려는 시도는 대부분 실패한다\"고 말합니다 "
        "(뮤추얼펀드 57개 중 타이밍 능력이 인정된 건 1개뿐이었다는 연구도 있어요). "
        "유튜브에서 '이 지표로 폭락 예측했다'는 사람들은 대개 **맞춘 것만 보여주고 틀린 건 안 보여줍니다.** "
        "아래 숫자만 보고 전량 매수/매도 같은 결정은 하지 마세요."
    )

    if st.button("상황판 불러오기 (10~20초)", key="market_load"):
        with st.spinner("지표 수집 중..."):
            st.session_state["dashboard"] = get_dashboard()

    dash = st.session_state.get("dashboard")
    if dash:
        st.markdown("### 위험 신호 지표")
        c1, c2, c3 = st.columns(3)

        vix = dash["vix"]
        with c1:
            st.metric("VIX (공포지수)", vix["값"] if vix["값"] else "-", vix.get("전일대비"))
            st.caption(vix["해석"])

        yc = dash["yield_curve"]
        with c2:
            st.metric("미국 장단기 금리차", f"{yc['값']}%p" if yc["값"] is not None else "-")
            st.caption(yc["해석"])

        hy = dash["hy_spread"]
        with c3:
            st.metric("하이일드 스프레드", f"{hy['값']}%" if hy["값"] is not None else "-")
            st.caption(hy["해석"])

        st.markdown("### 한국 시장 관련")
        c4, c5, c6 = st.columns(3)

        fx = dash["usdkrw"]
        with c4:
            st.metric("원달러 환율", f"{fx['값']}원" if fx["값"] else "-", fx.get("전일대비"))
            st.caption(fx["해석"])

        kospi = dash["kospi"]
        with c5:
            st.metric("코스피", kospi["값"] if kospi["값"] else "-")
            st.caption(kospi["해석"])

        sox = dash["sox"]
        with c6:
            st.metric("반도체지수(SOX)", sox["값"] if sox["값"] else "-")
            st.caption(sox["해석"])

        nasdaq = dash["nasdaq"]
        st.metric("나스닥", nasdaq["값"] if nasdaq["값"] else "-")
        st.caption(nasdaq["해석"])

        st.divider()
        st.markdown("### 옵션 풋/콜 비율 (SPY = 미국 시장 전체)")
        pc = dash["put_call"]
        if "오류" in pc:
            st.error(pc["오류"])
        else:
            c7, c8, c9 = st.columns(3)
            c7.metric("풋콜비율 (거래량)", pc["풋콜비율(거래량)"])
            c8.metric("풋콜비율 (미결제약정)", pc["풋콜비율(미결제약정)"])
            c9.metric("기준 만기일", pc["만기일"])
            st.info(f"**해석**: {pc['해석']}")

    with st.expander("📖 풋/콜 비율 쉽게 이해하기 (꼭 읽어보세요)"):
        st.markdown(
            "**옵션이 뭐냐면**: '나중에 이 가격에 살 권리'(콜)와 '팔 권리'(풋)를 사고파는 시장이에요.\n\n"
            "- **콜을 많이 산다** = 사람들이 **오를 거라고** 베팅 중\n"
            "- **풋을 많이 산다** = 사람들이 **내릴 거라고** 베팅 중\n\n"
            "**풋/콜 비율 = 풋 거래량 ÷ 콜 거래량**\n\n"
            "| 값 | 뜻 |\n"
            "|---|---|\n"
            "| 1.2 이상 | 풋 쏠림 = 다들 공포 상태 |\n"
            "| 0.9 ~ 1.2 | 중립 |\n"
            "| 0.6 ~ 0.9 | 콜 우세 = 상승 기대 |\n"
            "| 0.6 이하 | 콜 쏠림 = 과열 주의 |\n\n"
            "**재미있는 점 — 역발상 지표로도 씁니다.** 다들 공포에 질려 풋을 사재낄 때가 오히려 바닥인 경우가 있어요 "
            "(팔 사람은 이미 다 팔았으니까). 반대로 다들 콜만 사면서 낙관에 빠져 있을 때가 꼭지인 경우도 있고요. "
            "그래서 '풋콜비율이 높다 = 무조건 나쁘다'가 아닙니다.\n\n"
            "**⚠️ 중요한 한계**: 이 값은 **오늘 현재 값만 볼 수 있고 과거 데이터를 구할 수 없어서 백테스트가 불가능합니다.** "
            "즉 '이 값이 얼마일 때 사면 좋았다'를 저희가 검증할 방법이 없어요. 그래서 순수 참고용입니다."
        )

    st.divider()
    st.markdown("### 기관·투기세력 선물 포지션 (미국 COT)")
    st.caption(
        "미국 정부(CFTC)가 매주 공개하는 공식 자료입니다. "
        "'큰손들이 지금 상승 쪽에 걸었나, 하락 쪽에 걸었나'를 볼 수 있어요."
    )

    with st.expander("📖 이게 뭔지 쉽게 보기"):
        st.markdown(
            "선물시장에서 **누가 얼마나 사고 팔았는지**를 투자자 종류별로 집계한 자료예요.\n\n"
            "- **투기세력**: 헤지펀드·자산운용사처럼 **방향에 베팅해서 돈 벌려는 쪽**. 흔히 '스마트머니'라 부릅니다.\n"
            "- **헤저**: 실제 사업이나 자산이 있어서 **위험을 줄이려는 쪽**(기업 등). 방향 베팅이 목적이 아니에요.\n\n"
            "**순포지션 = 롱(매수) - 숏(매도)**\n"
            "- **플러스(빨강)** = 상승 쪽에 더 걸어놓음\n"
            "- **마이너스(파랑)** = 하락 쪽에 더 걸어놓음\n\n"
            "**⚠️ 두 가지 주의**\n"
            "1. **실시간이 아닙니다.** 매주 화요일 기준으로 집계해서 금요일에 발표해요 — 항상 3일 지난 자료입니다.\n"
            "2. **'큰손이 샀으니 따라 사자'가 아닙니다.** 오히려 한쪽으로 극단적으로 쏠렸을 때가 "
            "반대로 꺾이는 신호였던 경우도 많아서, 역발상 지표로 보는 사람도 많아요. "
            "정답이 있는 지표가 아니니 '지금 분위기' 정도로만 참고하세요.\n\n"
            "**한국은 왜 없나요?** 한국거래소(KRX)가 기관·외국인 데이터를 무료로 안 줍니다 "
            "(로그인 계정을 요구해요). 그래서 미국 시장만 제공합니다."
        )

    cot_col1, cot_col2 = st.columns([1, 2])
    with cot_col1:
        cot_market = st.selectbox("시장", list(COT_MARKETS.keys()), key="cot_market")
    with cot_col2:
        cot_weeks = st.slider("몇 주치 볼지", 26, 260, 104, step=26, key="cot_weeks")

    if st.button("포지션 + 가격 불러오기", key="cot_load"):
        with st.spinner("CFTC 포지션 + 선물 가격 받는 중..."):
            try:
                st.session_state["cot_df"] = get_price_with_cot(cot_market, weeks=cot_weeks)
                st.session_state["cot_label"] = cot_market
            except Exception as e:
                st.error(f"조회 실패: {e}")
                st.session_state["cot_df"] = None

    cot_df = st.session_state.get("cot_df")
    if cot_df is not None and not cot_df.empty:
        s = summarize_latest(cot_df)
        label = st.session_state.get("cot_label", cot_market)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("투기세력 순포지션", f"{s['투기_순포지션']:+,}", f"{s['전주대비']:+,}")
        k2.metric("롱 비중", f"{s['롱비중(%)']}%")
        k3.metric("2년내 위치", f"{s['2년내_백분위']:.0f}%")
        k4.metric("기준일", s["기준일"])

        st.info(f"**방향**: {s['방향']}  \n**쏠림 정도**: {s['쏠림']}")
        st.caption(
            "'2년내 위치'는 최근 2년 중 지금이 몇 % 지점인지예요. "
            "90% 이상이면 2년래 가장 롱에 쏠린 상태, 10% 이하면 가장 숏에 쏠린 상태입니다."
        )

        st.plotly_chart(
            build_cot_price_chart(cot_df, title=f"{label} — 가격 + 투기세력 포지션", price_label=label),
            width="stretch",
            config=CHART_CONFIG,
        )

        if "가격" in cot_df.columns and cot_df["가격"].notna().any():
            corr = cot_df["투기_순"].corr(cot_df["가격"])
            st.caption(
                f"이 기간 **포지션과 가격의 상관계수: {corr:+.2f}** "
                "(+1에 가까우면 같이 움직임, -1에 가까우면 반대로 움직임, 0이면 관계 없음). "
                "상관관계가 있다고 해서 인과관계는 아닙니다 — 참고용 숫자예요."
            )

        st.info(
            "**차트 보는 법**: 맨 위가 실제 가격, 가운데가 투기세력 순포지션입니다. "
            "이 둘을 같이 봐야 의미가 생겨요 — 예를 들어 **가격은 오르는데 순포지션이 계속 줄고 있으면** "
            "'큰손들이 이 상승을 안 믿고 빠져나가는 중'으로 읽는 사람들이 있습니다. "
            "반대로 순포지션이 극단까지 한쪽으로 몰린 뒤 가격이 꺾인 사례도 많고요.\n\n"
            "다만 **이건 정답이 있는 지표가 아닙니다.** 같은 그림을 보고도 해석이 갈려요. "
            "'지금 큰손들 분위기가 이렇구나' 정도로만 보시고, 이것만으로 매매 결정하지 마세요."
        )

        with st.expander("원본 데이터 보기"):
            st.dataframe(cot_df.sort_values("날짜", ascending=False), width="stretch", hide_index=True)

    st.divider()
    st.markdown("### 미국 개별 종목 옵션 조회")
    st.caption("미국 상장 종목만 조회됩니다 (한국 종목은 개별 옵션 시장이 없어서 안 나와요).")
    c10, c11 = st.columns([1, 2])
    with c10:
        opt_symbol = st.text_input("티커 입력 (예: AAPL, TSLA, NVDA)", value="AAPL", key="opt_symbol")
    if st.button("옵션 조회", key="opt_symbol_run"):
        with st.spinner("조회 중..."):
            pcr = get_put_call_ratio(opt_symbol.strip().upper())
        if "오류" in pcr:
            st.error(pcr["오류"])
        else:
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("풋콜비율 (거래량)", pcr["풋콜비율(거래량)"])
            d2.metric("풋콜비율 (미결제)", pcr["풋콜비율(미결제약정)"])
            d3.metric("콜 거래량", f"{pcr['콜 거래량']:,}")
            d4.metric("풋 거래량", f"{pcr['풋 거래량']:,}")
            st.caption(f"기준 만기일: {pcr['만기일']} (가장 가까운 만기 = 위클리 옵션 포함)")
            st.info(f"**해석**: {pcr['해석']}")

# ---------------------------------------------------------------- 텔레그램 알림
with tab_notify:
    st.subheader("텔레그램 알림 설정")
    st.caption("전체 스캔 결과를 매일 정해진 시간에 텔레그램으로 받아볼 수 있습니다.")

    with st.expander("처음이신가요? 봇 만드는 법"):
        st.markdown(
            "1. 텔레그램에서 **@BotFather** 검색 → 대화 시작\n"
            "2. `/newbot` 입력 → 봇 이름 정하기 → 봇 아이디(꼭 `bot`으로 끝나야 함) 정하기\n"
            "3. 완료되면 나오는 **봇 토큰**을 복사해서 아래에 입력\n"
            "4. 만든 봇을 텔레그램에서 검색해서 대화 시작 → 아무 메시지나 하나 보내기\n"
            "5. 아래 '내 chat_id 찾기' 버튼을 눌러 chat_id 확인"
        )

    try:
        saved_token, saved_chat = load_telegram_config()
    except Exception:
        saved_token, saved_chat = "", ""

    st.markdown("#### 1. 봇 설정")
    c1, c2 = st.columns(2)
    with c1:
        bot_token = st.text_input("봇 토큰", value=saved_token, type="password", key="tg_token")
    with c2:
        chat_id = st.text_input("chat_id", value=saved_chat, key="tg_chat")

    c3, c4, c5 = st.columns(3)
    with c3:
        if st.button("설정 저장", key="tg_save"):
            if bot_token and chat_id:
                save_telegram_config(bot_token, chat_id)
                st.success("저장했습니다.")
            else:
                st.warning("봇 토큰과 chat_id를 모두 입력하세요.")
    with c4:
        if st.button("내 chat_id 찾기", key="tg_find"):
            if not bot_token:
                st.warning("먼저 봇 토큰을 입력하세요.")
            else:
                try:
                    found = find_chat_id(bot_token)
                    st.success(f"chat_id: {found} — 위 칸에 입력하고 저장하세요.")
                except Exception as e:
                    st.error(str(e))
    with c5:
        if st.button("테스트 메시지 보내기", key="tg_test"):
            try:
                send_telegram_message("[퀀트 트레이더] 테스트 메시지입니다. 정상 연결되었습니다.", bot_token, chat_id)
                st.success("보냈습니다. 텔레그램을 확인해보세요.")
            except Exception as e:
                st.error(f"전송 실패: {e}")

    st.markdown("#### 2. 매일 자동 스캔 예약")
    saved_schedule = load_schedule_config()
    st.caption(f"현재 위에서 고른 **{REGION} 주식** 기준으로 예약됩니다. 다른 시장으로 받고 싶으면 맨 위에서 시장을 바꾸세요.")

    saved_markets = saved_schedule.get("markets", DEFAULT_SUB)
    saved_markets = [m for m in saved_markets if m in SUB_MARKETS] or DEFAULT_SUB

    c6, c7, c8, c9 = st.columns(4)
    with c6:
        notify_markets = st.multiselect(
            "시장", SUB_MARKETS, default=saved_markets, key=f"notify_markets_{REGION}"
        )
    with c7:
        notify_limit = st.number_input(
            "시장별 상위 몇 개", min_value=10, max_value=300, value=saved_schedule.get("limit", 100), step=10, key="notify_limit"
        )
    with c8:
        default_time = saved_schedule.get("time", "16:00")
        hh, mm = [int(x) for x in default_time.split(":")]
        notify_time = st.time_input("알림 받을 시간", value=datetime.now().replace(hour=hh, minute=mm).time(), key="notify_time")
    with c9:
        notify_min_match = st.number_input(
            "최소 일치 개수", min_value=1, max_value=len(STRATEGY_NAMES), value=saved_schedule.get("min_match", 1), key="notify_min_match"
        )

    task_status = get_task_status()
    if task_status["exists"]:
        st.info("현재 예약이 활성화되어 있습니다.")
    else:
        st.caption("현재 예약이 꺼져 있습니다.")

    c10, c11 = st.columns(2)
    with c10:
        if st.button("예약 저장 및 켜기", key="notify_on"):
            if not notify_markets:
                st.warning("시장을 하나 이상 선택하세요.")
            else:
                try:
                    time_str = notify_time.strftime("%H:%M")
                    register_windows_task(
                        time_str, ",".join(notify_markets), notify_limit, notify_min_match, REGION
                    )
                    save_schedule_config(
                        {
                            "time": time_str,
                            "region": REGION,
                            "markets": notify_markets,
                            "limit": notify_limit,
                            "min_match": notify_min_match,
                        }
                    )
                    st.success(f"매일 {time_str}에 {REGION} 주식을 자동 스캔해서 텔레그램으로 보내드립니다.")
                except Exception as e:
                    st.error(f"예약 등록 실패: {e}")
    with c11:
        if st.button("예약 끄기", key="notify_off"):
            try:
                remove_windows_task()
                st.success("예약을 껐습니다.")
            except Exception as e:
                st.error(f"예약 삭제 실패: {e}")

    st.caption("이 컴퓨터가 켜져 있고 로그인된 상태여야 실행됩니다 (윈도우 작업 스케줄러 사용).")

# ---------------------------------------------------------------- 매매 플레이북
with tab_playbook:
    st.subheader("📋 매매 플레이북 — 검증을 통과한 규칙")
    st.caption(
        f"{RESEARCH_DATE} 실행한 대규모 리서치 결과입니다. 대상: {RESEARCH_SCOPE}. "
        "45개 전략조합 × 5개 보유기간 × 4개 시대구간을 전부 검증했습니다."
    )

    st.success(
        "**핵심 발견 한 줄**: `이격도(disparity) + 스토캐스틱(stochastic)`이 **두 신호가 같은 날 동시에** 뜨는 조합이, "
        "**한국과 미국 양쪽에서 각각 독립적으로** 검증을 통과한 유일한 규칙입니다."
    )

    pb = get_playbook(REGION)

    if not pb:
        st.warning("이 시장은 검증 통과 규칙이 없습니다.")
    else:
        st.markdown(f"### {REGION} 시장 — 통과 규칙 {len(pb)}개")
        st.caption(
            "**초과수익**은 '아무 날에나 샀을 때'보다 얼마나 더 벌었는지입니다. 이게 핵심 숫자예요 — "
            "그냥 시장이 올라서 번 건 빼고 계산한 값입니다."
        )

        pb_df = pd.DataFrame(pb)
        pb_df["매수 조건"] = pb_df["조합"]
        pb_df["매도 시점"] = pb_df["보유일"].apply(lambda d: f"{d}거래일 뒤 (약 {round(d/21) if d>=21 else round(d/5)}{'개월' if d>=21 else '주'})")
        show = pb_df[
            ["등급", "매수 조건", "매도 시점", "초과수익(%p)", "초과승률(%p)", "통과기간수", "표본수"]
        ]
        st.dataframe(show, width="stretch", hide_index=True)

        st.caption(
            "**등급**: A+ = 4개 시대구간 전부 통과 / A = 3개 통과 / B = 2개 통과. "
            "**통과기간수**가 높을수록 특정 시기 운이 아닐 가능성이 큽니다."
        )

        st.divider()
        st.markdown("### 이 규칙을 오늘 적용하면?")
        st.caption("위 규칙 중 하나를 골라서, 오늘 그 조건에 맞는 종목이 있는지 찾아봅니다.")

        rule_labels = [
            f"[{r['등급']}] {r['조합']} → {r['보유일']}일 보유 (초과수익 +{r['초과수익(%p)']}%p)" for r in pb
        ]
        c1, c2 = st.columns([2, 1])
        with c1:
            picked = st.selectbox("규칙 선택", rule_labels, key=f"pb_rule_{REGION}")
        with c2:
            pb_limit = st.number_input(
                "시장별 상위 몇 개", min_value=10, max_value=300, value=100, step=10, key="pb_limit"
            )

        pb_markets = st.multiselect("시장", SUB_MARKETS, default=DEFAULT_SUB, key=f"pb_markets_{REGION}")
        picked_rule = pb[rule_labels.index(picked)]
        picked_strats = combo_to_strategies(picked_rule["조합"])

        st.info(
            f"**선택한 규칙**\n\n"
            f"🟢 **살 때**: {' 그리고 '.join(picked_strats)} 신호가 **같은 날 동시에** 뜬 종목\n\n"
            f"🔴 **팔 때**: 산 날로부터 **{picked_rule['보유일']}거래일 뒤** (전략 신호와 무관하게 시간으로 청산)\n\n"
            f"📊 과거 성적: 기준선 대비 수익률 **+{picked_rule['초과수익(%p)']}%p**, "
            f"승률 **+{picked_rule['초과승률(%p)']}%p** (표본 {picked_rule['표본수']:,}건)"
        )

        if st.button("오늘 이 조건에 맞는 종목 찾기", key="pb_scan"):
            if not pb_markets:
                st.warning("시장을 하나 이상 선택하세요.")
            else:
                with st.spinner("스캔 중..."):
                    results = scan_stocks(
                        pb_markets, picked_strats, limit=pb_limit, show_progress=False, region=REGION
                    )
                hits = [
                    r for r in results if all(r["signals"].get(s) == "BUY" for s in picked_strats)
                ]
                st.write(f"**조건 충족 종목: {len(hits)}개** (전체 {len(results)}개 중)")
                if hits:
                    from datetime import date as _date

                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "종목": f"{h['name']}({h['code']})",
                                    "매수 신호": "오늘 발생",
                                    "권장 매도": f"{picked_rule['보유일']}거래일 뒤",
                                }
                                for h in hits
                            ]
                        ),
                        width="stretch",
                        hide_index=True,
                    )
                    st.caption(
                        "⚠️ 자동 주문되지 않습니다. 이 목록을 참고해서 본인이 증권사 앱에서 직접 매매하세요."
                    )
                else:
                    st.info(
                        "오늘은 조건에 맞는 종목이 없습니다. "
                        "두 신호가 같은 날 겹치는 건 흔치 않아요 — 며칠에 한 번씩 확인해보세요."
                    )

    st.divider()
    st.markdown("### 대표 규칙의 연도별 성적 (한국, 이격도+스토캐스틱, 60일 보유)")
    yearly = pd.DataFrame(YEARLY_TOP_RULE_KR)
    st.dataframe(yearly, width="stretch", hide_index=True)
    st.success(
        f"**13년 중 13년 전부 플러스**였습니다. 신호가 특정 해에 몰리지도 않았어요 "
        f"(가장 많은 해가 전체의 12%). 이건 '한 시기 운'이 아니라는 근거입니다."
    )

    baseline = get_baseline(REGION)
    if baseline:
        with st.expander("비교 기준선 — 아무 날에나 샀을 때 (이걸 넘어야 의미가 있음)"):
            st.dataframe(pd.DataFrame(baseline), width="stretch", hide_index=True)
            st.caption(
                "예를 들어 60일 보유 기준선이 +8.66%인데, 어떤 전략이 +9%를 냈다면 "
                "실질 초과수익은 0.34%p뿐입니다. 그냥 시장이 오른 거예요."
            )

    st.warning(
        "⚠️ **꼭 알아야 할 것**\n\n"
        "**1. 이건 '추세추종은 안 통한다'는 뜻이 아닙니다.** "
        "통과한 규칙이 전부 '싸졌을때 줍기'(평균회귀)인 이유는, 이 검증이 **'사고 N일 뒤에 판다'**는 "
        "방식이기 때문이에요. 골든크로스·모멘텀 같은 추세추종은 '신호가 꺼질 때까지 오래 들고 가는' 방식이라 "
        "이 틀에서는 불리합니다. 질문이 다르면 답도 다릅니다.\n\n"
        "**2. 생존편향은 여전히 남아 있습니다.** 종목을 '오늘 기준 큰 회사'로 뽑았기 때문에, "
        "그 시절 망한 회사가 빠져 있어요. 실제로는 이 표보다 성적이 낮았을 겁니다.\n\n"
        "**3. 초과수익 1~5%p는 큰 숫자가 아닙니다.** 수수료·세금·슬리피지를 빼면 더 줄어듭니다. "
        "'이걸로 부자 된다'가 아니라 '동전 던지기보다 아주 조금 낫다' 정도로 보세요.\n\n"
        "**4. 미국은 통과 규칙이 훨씬 적었고(4개), 2023년 이후 구간에서는 하나도 통과하지 못했습니다.** "
        "최근 미국 시장에서는 이 방식이 잘 안 통했다는 뜻입니다."
    )

# ---------------------------------------------------------------- 종사종팔 V5
with tab_jongsa:
    st.subheader("🔁 종사종팔 V5 — 매일 할 일")
    st.caption(
        "SOXL 대상 분할매매 전략. 커뮤니티(송송 자동매매)에 공유된 개인 전략을 검증한 것으로, "
        "공식 자료가 아닙니다."
    )

    st.error(
        "⚠️ **시작 전에 반드시 읽어주세요**\n\n"
        "**SOXL은 3배 레버리지 ETF입니다.** 반도체 지수가 하루 -10% 빠지면 SOXL은 -30% 빠집니다. "
        "백테스트에서도 2018년은 **-11.7% 손실**이었고, 2025년에는 연중 한때 **-40%까지** 밀렸습니다.\n\n"
        "**절대 여윳돈이 아닌 자금으로 하지 마세요.** 이 전략의 검증된 최대낙폭은 -30.7%지만, "
        "미래에 더 나빠질 수 있고 실제로 원금의 상당 부분을 잃을 수 있습니다.\n\n"
        "저는 투자 자문을 할 수 없고, 이 도구는 **계산기일 뿐** 수익을 보장하지 않습니다. "
        "**소액으로 최소 몇 달 굴려보고** 본인이 규칙을 지킬 수 있는지 확인한 뒤 늘리세요."
    )

    js_state = jongsa_load_state()
    js_cfg = js_state["config"]

    js_sub1, js_sub2 = st.tabs(["오늘 할 일", "설정 · 기록"])

    with js_sub1:
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("현재가 불러오기", key="js_fetch"):
                try:
                    with st.spinner("SOXL 현재가 조회 중..."):
                        px = get_kr_ohlcv(js_cfg["ticker"], "2026-01-01", datetime.today().strftime("%Y-%m-%d"))
                        st.session_state["js_price"] = float(px["Close"].iloc[-1])
                        st.session_state["js_price_date"] = str(px.index[-1].date())
                except Exception as e:
                    st.error(f"조회 실패: {e}")
        with c2:
            js_price = st.number_input(
                f"{js_cfg['ticker']} 현재가 (달러)",
                min_value=0.01,
                value=float(st.session_state.get("js_price", 100.0)),
                step=0.01,
                format="%.2f",
                key="js_price_input",
            )
        if st.session_state.get("js_price_date"):
            st.caption(f"불러온 종가 기준일: {st.session_state['js_price_date']}")

        plan = jongsa_daily_plan(js_state, js_price)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총자산", f"${plan['총자산']:,.0f}")
        m2.metric("예수금", f"${plan['예수금']:,.0f}")
        m3.metric("보유수량", f"{plan['보유수량']:,.2f}주")
        m4.metric("보유건수", f"{plan['보유건수']}건")

        st.divider()

        # --- 오늘 팔 것 ---
        if plan["매도대상"]:
            st.markdown("### 🔴 오늘 팔 것")
            for i, s in enumerate(plan["매도대상"]):
                pnl_pct = (js_price / s["buy_price"] - 1) * 100
                st.warning(
                    f"**{s['qty']:.2f}주 매도** — {s['buy_date']}에 \\${s['buy_price']:.2f}에 산 것 "
                    f"(목표가 \\${s['목표가']:.2f}, 보유 {s['보유영업일']}영업일)  \n"
                    f"사유: **{s['사유']}** / 현재 수익률 {pnl_pct:+.2f}%"
                )
            mode_now = js_cfg.get("sell_day_buy_mode", "never")
            if mode_now == "never":
                st.info("**매도가 있는 날은 매수하지 않습니다.** (원본 V5 규칙)")
            else:
                losses = [s for s in plan["매도대상"] if s.get("예상손익", 0) < 0]
                st.info(
                    f"현재 설정: **{JONGSA_SELL_MODES[mode_now]}**  \n"
                    f"오늘 매도 {len(plan['매도대상'])}건 중 손실 {len(losses)}건 → "
                    f"**{'매수함' if plan['매수여부'] else '매수 안 함'}**"
                )
        else:
            st.markdown("### 🔴 오늘 팔 것")
            st.write("없습니다.")

        # --- 오늘 살 것 ---
        st.markdown("### 🟢 오늘 살 것")
        if plan["매수여부"]:
            if plan["매수금액"] <= 0:
                st.error("예수금이 없어 매수할 수 없습니다.")
            else:
                # Streamlit 마크다운은 $...$ 를 수식으로 해석하므로 달러 기호를 이스케이프한다
                st.success(
                    f"**\\${plan['매수금액']:,.2f} 어치 매수** "
                    f"(약 **{plan['매수수량']:.2f}주** @ \\${js_price:.2f})  \n"
                    f"매수 후 목표가: **\\${plan['매수후_목표가']:.2f}** "
                    f"(+{js_cfg['target_return']*100:.2f}%)"
                )
                if plan["예수금부족"]:
                    st.warning("예수금이 부족해서 목표금액보다 적게 계산됐습니다.")
                fee_now = js_cfg.get("fee_rate", 0.0)
                if fee_now > 0:
                    st.caption(
                        f"예산 \\${plan['매수예산']:,.2f} 중 수수료({fee_now*100:.4f}%)를 감안한 실제 금액입니다."
                    )
                st.caption(
                    "종가 무렵에 시장가로 사면 됩니다. "
                    "(백테스트 결과 지정가를 걸면 오히려 수익률이 낮아졌습니다 — 그냥 종가에 사세요.)"
                )
        else:
            st.write("오늘은 매수하지 않습니다 (매도가 있는 날).")

        st.divider()
        st.markdown("### 실제로 주문했다면 여기에 기록하세요")
        st.caption("기록해야 다음날 계산이 맞습니다. 증권사에서 체결된 실제 가격·수량을 넣으세요.")

        rc1, rc2 = st.columns(2)
        with rc1:
            st.markdown("**매수 기록**")
            bd = st.date_input("매수일", value=date.today(), key="js_buy_date")
            bp = st.number_input("체결가($)", min_value=0.01, value=float(js_price), step=0.01, format="%.2f", key="js_buy_price")
            bq = st.number_input("수량(주)", min_value=0.0, value=float(round(plan["매수수량"], 2)), step=0.01, format="%.2f", key="js_buy_qty")
            if st.button("매수 기록 추가", key="js_add_buy"):
                if bq > 0:
                    jongsa_record_buy(js_state, bd.isoformat(), bp, bq)
                    st.success(f"기록했습니다: {bq:.2f}주 @ ${bp:.2f}")
                    st.rerun()
                else:
                    st.warning("수량을 입력하세요.")

        with rc2:
            st.markdown("**매도 기록**")
            if js_state["lots"]:
                lot_labels = [
                    f"{i}: {l['buy_date']} / {l['qty']:.2f}주 @ ${l['buy_price']:.2f} (목표 ${l['target_price']:.2f})"
                    for i, l in enumerate(js_state["lots"])
                ]
                pick_lot = st.selectbox("어느 건을 팔았나요", lot_labels, key="js_sell_pick")
                sd = st.date_input("매도일", value=date.today(), key="js_sell_date")
                sp = st.number_input("체결가($)", min_value=0.01, value=float(js_price), step=0.01, format="%.2f", key="js_sell_price")
                if st.button("매도 기록 추가", key="js_add_sell"):
                    idx = lot_labels.index(pick_lot)
                    jongsa_record_sell(js_state, idx, sd.isoformat(), sp, "수동기록")
                    st.success("기록했습니다.")
                    st.rerun()
            else:
                st.write("보유 중인 건이 없습니다.")

        # --- 현재 보유 목록 ---
        if js_state["lots"]:
            st.divider()
            st.markdown("### 현재 보유 목록")
            lot_rows = []
            for l in js_state["lots"]:
                held = jongsa_business_days(l["buy_date"], date.today().isoformat())
                lot_rows.append(
                    {
                        "매수일": l["buy_date"],
                        "매수가($)": round(l["buy_price"], 2),
                        "수량": round(l["qty"], 2),
                        "목표가($)": round(l["target_price"], 2),
                        "현재수익률(%)": round((js_price / l["buy_price"] - 1) * 100, 2),
                        "보유영업일": held,
                        "청산까지": max(0, js_cfg["stop_days"] - held),
                    }
                )
            st.dataframe(pd.DataFrame(lot_rows), width="stretch", hide_index=True)
            st.caption("※ 보유영업일은 주말만 제외한 근사치입니다. 미국 공휴일은 반영되지 않으니 참고만 하세요.")

    with js_sub2:
        st.markdown("### 성과")
        perf = jongsa_performance(js_state, js_price if "js_price_input" in st.session_state else 100.0)
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("총자산", f"${perf['총자산']:,.0f}", f"{perf['총수익']:+,.0f}")
        p2.metric("총수익률", f"{perf['총수익률(%)']:+.2f}%")
        p3.metric("매매횟수", f"{perf['매매횟수']}회")
        p4.metric("승률", f"{perf['승률(%)']:.1f}%")

        if js_state["closed"]:
            with st.expander(f"청산 기록 ({len(js_state['closed'])}건)"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "매수일": c["buy_date"],
                                "매도일": c["sell_date"],
                                "매수가": round(c["buy_price"], 2),
                                "매도가": round(c["sell_price"], 2),
                                "수량": round(c["qty"], 2),
                                "손익($)": round(c["pnl"], 2),
                                "수익률(%)": round(c["return_pct"], 2),
                            }
                            for c in reversed(js_state["closed"])
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

        st.divider()
        st.markdown("### 설정")
        s1, s2, s3 = st.columns(3)
        with s1:
            new_target = st.number_input(
                "목표수익률(%)", min_value=0.5, max_value=20.0, value=js_cfg["target_return"] * 100, step=0.05, key="js_cfg_target"
            )
        with s2:
            new_pct = st.number_input(
                "하루 매수 비율(%)", min_value=1.0, max_value=50.0, value=js_cfg["daily_buy_pct"] * 100, step=1.0, key="js_cfg_pct"
            )
        with s3:
            new_stop = st.number_input(
                "강제청산 영업일", min_value=2, max_value=60, value=js_cfg["stop_days"], key="js_cfg_stop"
            )

        st.markdown("#### 매도일에도 매수할까")
        mode_keys = list(JONGSA_SELL_MODES.keys())
        cur_mode = js_cfg.get("sell_day_buy_mode", "never")
        new_mode = st.radio(
            "방식 선택",
            mode_keys,
            index=mode_keys.index(cur_mode) if cur_mode in mode_keys else 0,
            format_func=lambda k: JONGSA_SELL_MODES[k],
            key="js_cfg_sellmode",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"방식": "매도일 매수 안 함 (원본)", "CAGR(%)": 29.96, "MDD(%)": -39.99, "CAGR/MDD": 0.749, "보유비중(%)": 27.2},
                    {"방식": "전부 손실일 때만", "CAGR(%)": 33.53, "MDD(%)": -43.12, "CAGR/MDD": 0.777, "보유비중(%)": 30.6},
                    {"방식": "하나라도 손실이면", "CAGR(%)": 35.36, "MDD(%)": -43.96, "CAGR/MDD": 0.804, "보유비중(%)": 32.3},
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "2010-03 ~ 2025-12, 목표 2.7%, 매수비율 10%, 수수료 0.03% 기준. 원본 스프레드시트로 검증한 값입니다 "
            "(MDD는 소수점까지 일치). **수익률도 오르지만 위험 대비 효율(CAGR/MDD)도 같이 오릅니다** — "
            "손절로 비워진 자리를 다시 채워서 자금이 노는 시간을 줄이는 효과예요.\n\n"
            "다만 **낙폭이 -40% → -44%로 깊어지고** 매일 '오늘 판 것 중 손해 본 게 있나'를 따져야 합니다. "
            "처음에는 원본(매수 안 함)으로 시작하시고, 익숙해진 뒤에 바꾸셔도 늦지 않습니다."
        )

        st.markdown("#### 수수료 (증권사마다 다릅니다)")
        f1, f2, f3 = st.columns(3)
        with f1:
            new_fee = st.number_input(
                "편도 수수료(%)",
                min_value=0.0,
                max_value=1.0,
                value=js_cfg.get("fee_rate", 0.0) * 100,
                step=0.001,
                format="%.4f",
                key="js_cfg_fee",
            )
            st.caption("사고팔 때 각각 떼는 비율. 무료면 0.")
        with f2:
            new_fee_in_target = st.checkbox(
                "목표가에 수수료 반영",
                value=js_cfg.get("fee_in_target", True),
                key="js_cfg_fee_target",
            )
            st.caption("켜면 목표가 = 매수가 × (1+목표%) × (1+2×수수료). 수수료 0이면 차이 없음.")
        with f3:
            new_whole = st.checkbox(
                "정수주만 매수",
                value=js_cfg.get("whole_shares", True),
                key="js_cfg_whole",
            )
            st.caption("소수점 주식을 못 사는 증권사면 켜두세요.")

        if new_fee > 0:
            eff = (1 + new_target / 100) * (1 + 2 * new_fee / 100) - 1 if new_fee_in_target else new_target / 100
            st.info(
                f"현재 설정 기준 **실제 목표 상승률은 {eff*100:.3f}%** 입니다. "
                + ("(수수료 왕복분이 목표가에 포함됨)" if new_fee_in_target else "(수수료 미반영 — 실수령은 이보다 낮아집니다)")
            )
        else:
            st.caption("수수료 0% 이므로 목표가는 정확히 매수가 × (1 + 목표수익률) 입니다.")

        if st.button("설정 저장", key="js_save_cfg"):
            js_state["config"]["target_return"] = new_target / 100
            js_state["config"]["daily_buy_pct"] = new_pct / 100
            js_state["config"]["stop_days"] = int(new_stop)
            js_state["config"]["fee_rate"] = new_fee / 100
            js_state["config"]["fee_in_target"] = bool(new_fee_in_target)
            js_state["config"]["whole_shares"] = bool(new_whole)
            js_state["config"]["sell_day_buy_mode"] = new_mode
            jongsa_save_state(js_state)
            st.success("저장했습니다. (이미 보유 중인 건의 목표가는 그대로입니다)")
            st.rerun()

        st.divider()
        st.markdown("### 처음부터 다시 시작")
        rs1, rs2 = st.columns([2, 1])
        with rs1:
            reset_cash = st.number_input(
                "초기 투자금($)", min_value=100.0, value=float(js_cfg["initial_cash"]), step=100.0, key="js_reset_cash"
            )
        with rs2:
            confirm_reset = st.checkbox("정말 초기화", key="js_confirm_reset")
        if st.button("초기화 실행", key="js_reset", disabled=not confirm_reset):
            jongsa_reset_state(reset_cash, js_state["config"])
            st.success("초기화했습니다.")
            st.rerun()
        st.caption("⚠️ 초기화하면 지금까지의 보유·청산 기록이 전부 사라집니다.")

    st.divider()
    with st.expander("⚖️ 하루 매수 비율(분할 수)을 얼마로 할까 — 검증된 비교표"):
        st.caption(
            "원본 자료의 비교표를 제 엔진으로 전부 재현한 결과입니다 (8개 설정 모두 0.1%p 이내 일치). "
            "기간 2010-04 ~ 2024-12, 목표수익률 2.75%, 수수료 0.03% 기준."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {"설정": "3% (안정형)", "CAGR(%)": 8.4, "MDD(%)": -9.6, "CAGR/MDD": 0.87, "평균보유비중(%)": 7.5},
                    {"설정": "6.5% (안정형)", "CAGR(%)": 19.5, "MDD(%)": -20.3, "CAGR/MDD": 0.96, "평균보유비중(%)": 17.5},
                    {"설정": "10% (10분할) ★기본", "CAGR(%)": 30.5, "MDD(%)": -30.4, "CAGR/MDD": 1.01, "평균보유비중(%)": 27.5},
                    {"설정": "11.1% (9분할)", "CAGR(%)": 33.5, "MDD(%)": -33.4, "CAGR/MDD": 1.00, "평균보유비중(%)": 30.5},
                    {"설정": "12.5% (8분할)", "CAGR(%)": 36.5, "MDD(%)": -37.6, "CAGR/MDD": 0.97, "평균보유비중(%)": 34.2},
                    {"설정": "14.3% (7분할)", "CAGR(%)": 40.3, "MDD(%)": -41.9, "CAGR/MDD": 0.96, "평균보유비중(%)": 38.6},
                    {"설정": "16.6% (6분할)", "CAGR(%)": 43.0, "MDD(%)": -47.2, "CAGR/MDD": 0.91, "평균보유비중(%)": 43.7},
                    {"설정": "20% (5분할)", "CAGR(%)": 44.5, "MDD(%)": -54.1, "CAGR/MDD": 0.82, "평균보유비중(%)": 49.4},
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.info(
            "**CAGR/MDD가 핵심 숫자입니다** — 위험 1만큼 지고 수익을 얼마나 얻었나입니다. "
            "**10~11.1%에서 정점(1.00~1.01)이고 그 뒤로는 계속 떨어집니다.**\n\n"
            "10% → 20%로 올리면 수익률은 +14%p 오르지만(30.5→44.5) 낙폭은 +24%p 커집니다(-30.4→-54.1). "
            "**얻는 것보다 잃는 게 큽니다.**\n\n"
            "그리고 -30%와 -54%는 체감이 완전히 다릅니다. 1천만원이 700만원 되는 것과 460만원 되는 것의 차이인데, "
            "후자는 대부분 중도에 포기합니다. **10%로 시작해서 익숙해지면 11.1%까지가 적당합니다.**"
        )

    with st.expander("📊 이 전략 백테스트 결과 (2010~2024, 원문 재현 검증됨)"):
        st.markdown(
            "**원문 주장과 제가 재현한 값이 거의 일치합니다** — 원문이 정직하게 검증했다는 근거입니다.\n\n"
            "| 항목 | 원문 주장 | 재현 결과 |\n"
            "|---|---|---|\n"
            "| CAGR | 30.5% | **31.60%** |\n"
            "| MDD | -30.4% | **-30.73%** |\n"
            "| 평균 보유비중 | 27.4% | **27.5%** |\n"
            "| 승률 | — | **79.5%** |\n\n"
            "**연도별로는 15년 중 14년 플러스**였고, 유일한 손실년은 2018년(-11.7%)입니다. "
            "SOXL이 -86.6% 폭락한 2022년에도 이 전략은 **+32.6%**를 냈습니다 "
            "(짧게 사고 팔아서 반등 구간마다 익절하는 구조라서요).\n\n"
            "다만 **그냥 SOXL을 사서 들고 있었으면 CAGR 37.06%로 더 높았습니다.** "
            "대신 최대낙폭이 **-90.5%**라 대부분의 사람은 못 버팁니다. "
            "위험 대비로는 이 전략이 2.5배 낫습니다."
        )

# ---------------------------------------------------------------- 전략 추천 (리서치)
with tab_research:
    st.subheader("전략 추천 (쉬운 설명)")
    st.caption(
        "미리 대규모로 돌려본 백테스트 결과를 정리해둔 화면이에요. 지금 다시 계산하는 게 아니라 "
        "그때 결과를 그대로 보여주는 것이니, 시간이 지났다면 '전략 비교' 탭에서 직접 다시 돌려보세요."
    )

    st.markdown("### 1. 이게 뭐하는 화면이냐면")
    st.write(
        "컴퓨터로 '만약 과거에 이 방식대로 사고팔았다면 얼마를 벌었을까'를 여러 번 계산해봤어요. "
        "시작 연도를 4번 다르게 바꿔가면서, 코스피·코스닥 큰 회사 200개, 9가지 매매 방식으로 전부 테스트했습니다."
    )

    st.markdown("### 2. 매매 방식은 크게 두 가지")
    st.markdown(
        "- **① 오르는 거 따라 사는 방식** (`momentum`, `golden_cross`, `macd`) — \"요즘 계속 오르네? 나도 산다\"\n"
        "- **② 싸졌을 때 줍는 방식** (`rsi`, `bollinger`, `disparity`, `stochastic`) — \"많이 빠졌네? 지금이 쌀 때다\"\n"
        "- **③ 가장 안전한 방식** (`high_breakout`) — 확실한 신고가에만 사는, 수익은 낮지만 마음 편한 방식"
    )

    st.markdown("### 3. 결과 (검증기간 기준, 4개 구간 평균)")
    st.dataframe(
        pd.DataFrame(
            [
                {"매매 방식": "momentum (오르는거 따라사기)", "순위": 1.25, "수익률(%)": 228.9, "최대 손실폭(%)": -52.4, "이긴 비율(%)": 33.0},
                {"매매 방식": "golden_cross (오르는거 따라사기)", "순위": 1.75, "수익률(%)": 220.8, "최대 손실폭(%)": -51.6, "이긴 비율(%)": 35.9},
                {"매매 방식": "macd (오르는거 따라사기)", "순위": 3.0, "수익률(%)": 187.1, "최대 손실폭(%)": -51.8, "이긴 비율(%)": 36.2},
                {"매매 방식": "stochastic (싸졌을때 줍기)", "순위": 4.5, "수익률(%)": 117.2, "최대 손실폭(%)": -49.2, "이긴 비율(%)": 67.9},
                {"매매 방식": "volatility_breakout (따라사기)", "순위": 5.0, "수익률(%)": 120.8, "최대 손실폭(%)": -52.0, "이긴 비율(%)": 42.5},
                {"매매 방식": "disparity (싸졌을때 줍기)", "순위": 5.5, "수익률(%)": 101.5, "최대 손실폭(%)": -44.8, "이긴 비율(%)": 70.6},
                {"매매 방식": "rsi (싸졌을때 줍기)", "순위": 7.0, "수익률(%)": 77.6, "최대 손실폭(%)": -48.5, "이긴 비율(%)": 66.8},
                {"매매 방식": "bollinger (싸졌을때 줍기)", "순위": 8.0, "수익률(%)": 66.6, "최대 손실폭(%)": -37.4, "이긴 비율(%)": 70.9},
                {"매매 방식": "high_breakout (가장 안전)", "순위": 9.0, "수익률(%)": 20.8, "최대 손실폭(%)": -26.1, "이긴 비율(%)": 31.5},
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "**표 읽는 법**: '이긴 비율'은 사고팔 때마다 10번 중 몇 번을 이익으로 끝냈는지예요. "
        "'최대 손실폭'은 중간 최악의 순간에 자산이 얼마나 줄었었는지입니다 (-52%면 한때 반토막 넘게 났다는 뜻)."
    )

    st.write(
        "**① 오르는 거 따라 사는 방식이 수익률은 훨씬 높았어요.** 4번 테스트 전부 1-3등. "
        "근데 함정이 있어요 — 이긴 비율이 33-36%밖에 안 됩니다. 10번 사면 6-7번은 손해예요. "
        "대신 몇 번이 크게 먹어서 전체적으론 이기는 구조라, **심리적으로 버티기 쉽지 않습니다.**\n\n"
        "**② 싸졌을 때 줍는 방식은 수익률은 낮지만 이긴 비율이 63-90%로 훨씬 마음 편합니다.**"
    )

    st.markdown("### 4. 익절(미리 이익 실현)은 도움이 됐나?")
    st.write(
        "9,936번의 백테스트(200종목 x 9전략 x 6가지 익절조건)를 돌려본 결과, "
        "**대부분 '익절 없음'이 가장 좋았습니다.** 5~30% 어떤 값을 넣어도 총수익률이 오히려 낮아졌어요.\n\n"
        "이유: 이런 전략들은 몇 번 안 되는 큰 상승장을 **끝까지 타야** 돈을 버는 구조인데, "
        "'20% 오르면 판다'는 규칙을 걸면 정작 크게 오를 기회를 20%에서 끊어버리게 됩니다. "
        "승률은 올라가지만(마음은 편해지지만) 돈은 덜 벌어요."
    )

    st.warning(
        "⚠️ **이 결과를 볼 때 꼭 알아야 할 것 (일종의 '치팅'이 섞여 있어요)**\n\n"
        "1. **지금 잘나가는 회사로만 테스트했어요.** 종목을 '지금 기준 큰 회사 200개'로 뽑아서 과거 데이터를 돌렸거든요. "
        "그때는 작았다가 지금 커진 회사는 포함되고, 그때는 컸는데 지금 망하거나 작아진 회사는 아예 빠져 있어요. "
        "**실제로 그 시절에 이 방식을 썼다면 이 표보다 훨씬 낮은 수익이 나왔을 가능성이 큽니다.**\n\n"
        "2. **4번 테스트한 게 사실 다 비슷한 시기예요.** 4번 다 '오늘'까지 이어져서 최근 상승장을 똑같이 포함합니다. "
        "서로 다른 4번의 검증이라기보다, 같은 상승장을 여러 각도로 잘라본 것에 가깝습니다.\n\n"
        "3. **종목마다 결과가 들쭉날쭉했어요.** 기간을 조금만 바꿔도 '제일 좋았던 설정'이 완전히 달라지고 "
        "손해로 뒤집히는 종목도 많았습니다."
    )

    st.info(
        "**정리하면**: 지금 데이터로는 골든크로스(짧은 쪽 10-15일, 긴 쪽 40-60일)나 모멘텀(20일)이 "
        "그나마 괜찮아 보이지만, '이걸 하면 반드시 번다'는 뜻이 아니라 **'참고용 방향'** 정도로만 받아들이세요. "
        "실전 전에는 '포트폴리오 백테스트'로 여러 종목에 나눠 담고 손절까지 넣어서 확인하고, **꼭 소액으로 시작하세요.**"
    )
