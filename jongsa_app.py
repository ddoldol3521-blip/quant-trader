"""종사종팔 V5 전용 앱.

핵심 발상: **시작일만 정하면 규칙이 나머지를 전부 결정한다.**
그래서 매매를 하나하나 기록할 필요가 없다. 설정값만 넣으면
시작일부터 오늘까지를 매번 다시 계산해서 현재 상태와 오늘 할 일을 보여준다.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src.data.kr_data import get_kr_ohlcv
from src.jongsa_backtest import run_buy_and_hold, run_jongsa
from src.jongsa_live import PRESETS, SELL_DAY_MODES
from src.jongsa_live import business_days_between as bdays
from src.jongsa_live import apply_preset, is_shared_server, load_config, save_config
from src.jongsa_live import target_price_for

st.set_page_config(page_title="종사종팔 V5", page_icon="🔁", layout="wide")

# 여백을 줄여 화면을 꽉 채운다. 스트림릿 기본값은 위아래 패딩이 크다.
st.markdown(
    """
<style>
  .block-container {padding: 0.8rem 1.2rem 1rem 1.2rem; max-width: 100%;}
  [data-testid="stMetric"] {
      background: rgba(128,128,128,0.07);
      border: 1px solid rgba(128,128,128,0.22);
      border-radius: 8px; padding: 8px 12px;
  }
  [data-testid="stMetricLabel"] p {font-size: 0.78rem; opacity: 0.8;}
  [data-testid="stMetricValue"] {font-size: 1.45rem;}
  [data-testid="stVerticalBlock"] {gap: 0.55rem;}
  [data-testid="stHorizontalBlock"] {gap: 0.6rem;}
  hr {margin: 0.5rem 0 !important;}
  h1 {font-size: 1.6rem; margin-bottom: 0.1rem;}
  h3 {font-size: 1.05rem; margin: 0.3rem 0 0.2rem 0;}
  .stDataFrame {border-radius: 6px;}
  div[data-testid="stExpander"] {border-radius: 8px;}
  .stCaption, [data-testid="stCaptionContainer"] p {margin-bottom: 0.15rem;}
</style>
""",
    unsafe_allow_html=True,
)

# 설정을 주소(URL)에 담는다.
#
# 여러 사람이 같은 앱을 쓸 때 파일 하나에 저장하면 서로의 설정을 덮어쓴다.
# 주소에 담아두면 각자 자기 주소를 갖게 되고, 즐겨찾기 해두면 다음에 열 때도
# 그대로 뜬다. 계정도 로그인도 필요 없다.
_URL_KEYS = {
    "t": ("ticker", str),
    "s": ("initial_cash", float),
    "n": ("daily_buy_pct", None),   # 분할수로 넣고 비율로 바꾼다
    "d": ("start_date", str),
    "r": ("target_return", None),   # %로 넣고 소수로 바꾼다
    "p": ("stop_days", int),
    "f": ("fee_rate", None),        # %로 넣고 소수로 바꾼다
    "ri": ("reinvest", None),       # 1 / 0
    "m": ("sell_day_buy_mode", str),
}


def flows_from_url() -> list:
    """주소에 담긴 입출금 목록을 읽는다. 형식: c=2025-07-01:5000,2026-01-05:-3000"""
    raw = st.query_params.get("c", "")
    out = []
    for part in raw.split(","):
        if ":" not in part:
            continue
        d, _, amt = part.partition(":")
        try:
            out.append({"날짜": pd.Timestamp(d.strip()).date(), "금액": float(amt)})
        except (ValueError, TypeError):
            continue
    return out


def flows_to_param(flows: list) -> str:
    return ",".join(f"{f['날짜']}:{f['금액']:.0f}" for f in flows)


def cfg_from_url(base: dict) -> dict:
    """주소에 붙은 설정을 읽어 기본 설정 위에 덮어쓴다. 이상한 값은 무시한다."""
    cfg = dict(base)
    qp = st.query_params
    for key, (name, caster) in _URL_KEYS.items():
        if key not in qp:
            continue
        raw = qp[key]
        try:
            if key == "n":
                cfg["daily_buy_pct"] = 1 / int(raw)
            elif key in ("r", "f"):
                cfg[name] = float(raw) / 100
            elif key == "ri":
                cfg[name] = raw not in ("0", "false", "False")
            else:
                cfg[name] = caster(raw)
        except (ValueError, TypeError, ZeroDivisionError):
            continue  # 주소를 손으로 고치다 깨진 경우 — 기본값을 쓴다
    return cfg


def cfg_to_url(cfg: dict, flows: list) -> None:
    """현재 설정을 주소에 반영한다. 이 주소를 즐겨찾기하면 설정이 유지된다."""
    params = {
        "t": cfg["ticker"],
        "s": f"{cfg['initial_cash']:.0f}",
        "n": f"{round(1 / cfg['daily_buy_pct'])}",
        "d": cfg["start_date"],
        "r": f"{cfg['target_return'] * 100:g}",
        "p": f"{int(cfg['stop_days'])}",
        "f": f"{cfg['fee_rate'] * 100:g}",
        "ri": "1" if cfg["reinvest"] else "0",
        "m": cfg["sell_day_buy_mode"],
    }
    if flows:
        params["c"] = flows_to_param(flows)
    st.query_params.from_dict(params)


if "cfg" not in st.session_state:
    st.session_state.cfg = cfg_from_url(load_config())
cfg = st.session_state.cfg

if "flows" not in st.session_state:
    st.session_state.flows = flows_from_url()


@st.cache_data(ttl=1800, show_spinner=False)
def load_price_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    return get_kr_ohlcv(ticker, start, end)


@st.cache_data(ttl=1800, show_spinner=False)
def simulate(ticker, start, today, cash, pct, tgt, stop, fee, fee_in_tgt, whole, mode, reinvest, flows):
    """시작일부터 오늘까지 규칙대로 돌린다. 설정이 같으면 캐시에서 바로 나온다.

    today를 인자로 받는 이유: 캐시 키에 날짜가 들어가야 날이 바뀌는 순간
    무조건 다시 계산된다. 함수 안에서 date.today()를 부르면 캐시가 그대로
    남아 어제 결과를 보여줄 수 있다.
    flows는 캐시 키가 되어야 하므로 튜플로 받는다.
    존버(그냥 사서 놔두기)도 같은 입출금 조건으로 같이 돌려서 비교한다.
    """
    hist = load_price_history(ticker, start, today)
    res = run_jongsa(
        hist, "V5",
        initial_cash=cash, target_return=tgt, daily_buy_pct=pct, stop_days=int(stop),
        fee_rate=fee, whole_shares=whole, fee_in_target=fee_in_tgt,
        sell_day_buy_mode=mode, reinvest=reinvest, cash_flows=list(flows),
    )
    bh = run_buy_and_hold(
        hist, initial_cash=cash, fee_rate=fee, whole_shares=whole, cash_flows=list(flows)
    )
    return res, hist, bh


# ============================================================ 설정 (맨 위)
st.markdown("# 🔁 종사종팔 V5")
if is_shared_server():
    st.caption(
        "SOXL 분할매매 계산기입니다. **투자 자문이 아니고 수익을 보장하지 않습니다.** "
        "설정은 사람마다 따로 유지됩니다 — **지금 주소를 즐겨찾기 해두면** 다음에 열 때도 "
        "이 설정 그대로 뜹니다 (주소창을 보면 설정값이 붙어 있습니다)."
    )

r1c1, r1c2, r1c3, r1c4 = st.columns([1, 1.1, 1, 1.2])
with r1c1:
    ticker = st.text_input("종목", value=cfg.get("ticker", "SOXL")).strip().upper()
with r1c2:
    seed = st.number_input("시드 ($)", 100.0, value=float(cfg.get("initial_cash", 10000.0)), step=1000.0)
with r1c3:
    splits = st.number_input(
        "분할수", 2, 60, int(round(1 / cfg.get("daily_buy_pct", 0.10))),
        help="하루에 총자산의 1/분할수 만큼 산다. 10분할 = 10%. 추천은 10분할.",
    )
with r1c4:
    # 상한을 넉넉히 잡아 막지 않는다. 기간이 너무 짧으면 아래 계산에서
    # 며칠 이전으로 잡아야 하는지 알려준다 (무조건 막으면 왜 안 되는지 알 수 없다).
    _lo, _hi = date(2010, 3, 11), date.today()
    _saved = pd.Timestamp(cfg.get("start_date") or "2025-01-02").date()
    start_d = st.date_input("시작일", value=min(max(_saved, _lo), _hi), min_value=_lo, max_value=_hi)

r2c1, r2c2, r2c3, r2c4 = st.columns([1, 1, 1, 1.2])
with r2c1:
    tgt_pct = st.number_input("목표수익률 (%)", 0.5, 20.0, cfg.get("target_return", 0.0275) * 100, 0.05)
with r2c2:
    stop_days = st.number_input("청산 영업일", 2, 60, int(cfg.get("stop_days", 10)))
with r2c3:
    fee_pct = st.number_input("수수료 (%)", 0.0, 1.0, cfg.get("fee_rate", 0.0) * 100, 0.001, format="%.4f")
with r2c4:
    reinvest = st.radio(
        "수익 재투자", [True, False],
        index=0 if cfg.get("reinvest", True) else 1,
        format_func=lambda v: "⭕ 함 (복리)" if v else "❌ 안 함 (고정)",
        horizontal=True,
    )

daily_pct = 1 / splits
rec = "  ✅ **추천 설정**" if splits == 10 and abs(tgt_pct - 2.75) < 0.01 and stop_days == 10 else ""
st.caption(
    f"하루 매수금 = {'어제 총자산' if reinvest else '넣은 돈'}의 **{daily_pct*100:.1f}%** "
    f"({splits}분할) · 목표 **+{tgt_pct:.2f}%** 도달 시 매도 · **{stop_days}영업일** 지나면 무조건 매도{rec}"
)

# ---------- 중간 입출금 ----------
_flows = st.session_state.flows
with st.expander(
    f"💰 중간에 돈 넣고 뺀 기록 ({len(_flows)}건)" if _flows else "💰 중간에 돈 넣고 뺀 기록 — 없으면 안 열어도 됩니다",
    expanded=bool(_flows),
):
    st.caption(
        "투자 도중에 돈을 더 넣거나 뺐다면 여기에 적으세요. **매매 기록은 여전히 필요 없습니다.** "
        "맨 아래 빈 줄에 입력하면 자동으로 한 줄이 늘어나고, 줄 왼쪽을 선택하고 Delete를 누르면 지워집니다."
    )
    _edit_df = pd.DataFrame(
        [{"날짜": f["날짜"], "구분": "입금" if f["금액"] >= 0 else "출금", "금액($)": abs(f["금액"])}
         for f in _flows]
        or [{"날짜": None, "구분": "입금", "금액($)": None}]
    )
    edited = st.data_editor(
        _edit_df, width="stretch", hide_index=True, num_rows="dynamic", key="flow_editor",
        column_config={
            "날짜": st.column_config.DateColumn(format="YYYY-MM-DD", width="medium"),
            "구분": st.column_config.SelectboxColumn(options=["입금", "출금"], width="small"),
            "금액($)": st.column_config.NumberColumn(min_value=0.0, step=100.0, format="%.0f"),
        },
    )

    new_flows = []
    for _, row in edited.iterrows():
        if pd.isna(row["날짜"]) or pd.isna(row["금액($)"]) or float(row["금액($)"]) <= 0:
            continue
        amt = float(row["금액($)"])
        new_flows.append({
            "날짜": pd.Timestamp(row["날짜"]).date(),
            "금액": -amt if row["구분"] == "출금" else amt,
        })
    new_flows.sort(key=lambda f: f["날짜"])
    if new_flows != _flows:
        st.session_state.flows = new_flows
        st.rerun()

    if _flows:
        _in = sum(f["금액"] for f in _flows if f["금액"] > 0)
        _out = -sum(f["금액"] for f in _flows if f["금액"] < 0)
        # 스트림릿 마크다운은 $...$ 를 수식으로 읽어서 글자가 깨진다. 달러 기호를 escape.
        st.caption(f"입금 합계 **\\${_in:,.0f}** · 출금 합계 **\\${_out:,.0f}**")

# 설정이 바뀌면 조용히 저장해둔다 (다음에 열 때 그대로 뜨도록)
new_cfg = {
    "ticker": ticker, "initial_cash": float(seed), "daily_buy_pct": daily_pct,
    "target_return": tgt_pct / 100, "stop_days": int(stop_days), "fee_rate": fee_pct / 100,
    "reinvest": bool(reinvest), "start_date": start_d.isoformat(),
}
if any(cfg.get(k) != v for k, v in new_cfg.items()):
    cfg.update(new_cfg)
    save_config(cfg)   # 내 PC에서 켰을 때만 저장된다 (공용 서버에서는 무시)
cfg_to_url(cfg, st.session_state.flows)  # 즐겨찾기하면 이 설정으로 다시 열린다

# ============================================================ 계산
flow_tuples = tuple((str(f["날짜"]), float(f["금액"])) for f in st.session_state.flows)
try:
    with st.spinner("계산 중..."):
        res, hist, bh_curve = simulate(
            ticker, start_d.isoformat(), date.today().isoformat(),
            float(seed), daily_pct, tgt_pct / 100, int(stop_days),
            fee_pct / 100, cfg.get("fee_in_target", True), cfg.get("whole_shares", True),
            cfg.get("sell_day_buy_mode", "never"), bool(reinvest), flow_tuples,
        )
except Exception as e:
    if "데이터가 너무 짧습니다" in str(e):
        # 10영업일 청산 규칙을 한 번은 돌려봐야 결과가 나온다
        need_days = int((int(stop_days) + 2) * 1.5) + 4
        st.error(
            f"**기간이 너무 짧습니다.** 청산 규칙이 {stop_days}영업일이라 최소 그만큼은 지나야 "
            f"결과가 나옵니다.\n\n"
            f"시작일을 **{(date.today() - timedelta(days=need_days)).isoformat()}** 이전으로 잡아보세요. "
            f"(청산 영업일을 줄이면 더 최근 날짜도 됩니다)"
        )
    else:
        st.error(f"계산 실패: {e}  — 종목코드와 시작일을 확인하세요.")
    st.stop()

for _note in res.flow_notes:
    st.warning(f"입출금 안내 — {_note}")

log = res.daily_log
last = log.iloc[-1]
price = float(last["종가"])
price_date = pd.Timestamp(last["날짜"]).date()
shares = float(last["보유수량"])
cash = float(last["예수금"])
equity = float(last["평가금"])
total = float(last["총자산"])

# ============================================================ 현황
# 입출금이 있으면 '총자산/시드'는 수익률이 아니다. 넣은 돈 기준으로 계산한다.
put_in = res.total_contributed
profit = res.net_profit
has_flows = bool(st.session_state.flows)

k = st.columns(7 if has_flows else 6)
i = 0
k[i].metric("총자산", f"${total:,.0f}"); i += 1
if has_flows:
    k[i].metric("넣은 돈", f"${put_in:,.0f}", f"시드 ${seed:,.0f}"); i += 1
k[i].metric("누적 손익금", f"${profit:+,.0f}"); i += 1
k[i].metric("누적 수익률", f"{res.net_return_pct:+.2f}%"); i += 1
k[i].metric("남은 현금", f"${cash:,.0f}", f"{cash/total*100:.0f}%" if total else None); i += 1
k[i].metric(
    f"{ticker} 현재가", f"${price:,.2f}",
    f"{(price/log.iloc[-2]['종가']-1)*100:+.2f}%" if len(log) > 1 else None,
); i += 1
k[i].metric("현재 보유", f"{shares:,.0f}주", f"${equity:,.0f} · {int(last['보유건수'])}건")

st.caption(
    f"**{start_d} → {price_date}** 기준 · "
    f"매매 {res.num_trades}회 (익절 {res.num_target_sells} / 손절 {res.num_forced_sells}) · "
    f"승률 {res.win_rate_pct:.1f}% · **연평균 {res.cagr_pct:.1f}% · 최대낙폭 {res.mdd_pct:.1f}%**"
    + ("  (연평균·최대낙폭은 입출금 효과를 뺀 전략 자체의 성적입니다)" if has_flows else "")
)

tab_home, tab_grid, tab_year, tab_help = st.tabs(
    ["📅 오늘 할 일", "📋 일별 기록", "📊 연도별 성과", "📖 규칙 · 설정"]
)

# ============================================================ 오늘 할 일
with tab_home:
    hc1, hc2 = st.columns([1, 3])
    with hc1:
        use_price = st.number_input(
            "오늘 예상 종가 ($)", 0.01, value=price, step=0.01, format="%.2f",
            help="비워두면 마지막 종가로 계산합니다. 장중이면 현재가를 넣어보세요.",
        )
    with hc2:
        st.caption(
            f"마지막 반영 종가는 **{price_date}의 \\${price:,.2f}** 입니다. "
            "아래는 그 다음 거래일(=오늘) 마감에 넣을 주문입니다."
        )

    # 마지막 시뮬레이션 상태에서 이어서 '오늘' 계산
    sells, orders = [], []
    for lot in res.final_lots:
        held = bdays(lot["buy_date"], date.today().isoformat())
        if held >= int(stop_days):
            sells.append({**lot, "held": held, "stop": True})
        elif held >= 1 and use_price >= lot["target_price"]:
            sells.append({**lot, "held": held, "stop": False})

    sold_pnls = [(use_price - s["buy_price"]) * s["qty"] for s in sells]
    mode = cfg.get("sell_day_buy_mode", "never")
    will_buy = (not sells) or (
        mode == "any_loss" and any(p < 0 for p in sold_pnls)
    ) or (mode == "all_loss" and all(p < 0 for p in sold_pnls))

    base = total if reinvest else seed
    budget = min(base * daily_pct, cash) if will_buy else 0.0
    fee = fee_pct / 100
    buy_qty = int(budget * (1 - fee) / use_price) if (will_buy and budget > 0) else 0
    buy_cost = buy_qty * use_price * (1 + fee)

    a1, a2 = st.columns(2)
    with a1:
        st.markdown("### 🔴 팔 것")
        if sells:
            rows = []
            for s in sells:
                rows.append({
                    "매수일": s["buy_date"],
                    "수량": f"{s['qty']:,.0f}주",
                    "매수가": f"${s['buy_price']:.2f}",
                    "목표가": f"${s['target_price']:.2f}",
                    "손익": f"{(use_price/s['buy_price']-1)*100:+.2f}%",
                    "주문": "🛑 MOC 매도" if s["stop"] else f"🎯 LOC 매도 ${s['target_price']:.2f}",
                    "사유": f"{s['held']}영업일 경과 (강제)" if s["stop"] else "목표 도달",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            tot_q = sum(s["qty"] for s in sells)
            st.error(f"**총 {tot_q:,.0f}주 매도** · 예상 손익 ${sum(sold_pnls):+,.2f}")
        else:
            st.success("**없음** — 목표가에 닿았거나 기한이 찬 건이 없습니다.")

    with a2:
        st.markdown("### 🟢 살 것")
        if will_buy and buy_qty > 0:
            st.info(
                f"**{buy_qty:,.0f}주 매수** (MOC 시장가) · 약 **\\${buy_cost:,.2f}**\n\n"
                f"체결되면 → 목표가 **\\${target_price_for(use_price, cfg):.2f}** 로 다음날부터 LOC 매도 걸기 · "
                f"**{(date.today() + timedelta(days=int(stop_days*1.45))).isoformat()} 무렵** 까지 미달이면 강제 매도"
            )
            if base * daily_pct > cash + 1e-9:
                st.warning(f"예수금 부족 — 목표 \\${base*daily_pct:,.0f} 중 \\${cash:,.0f}만 가능")
        elif not will_buy:
            st.error(f"**오늘은 사지 않음** — {SELL_DAY_MODES[mode]}")
        else:
            st.error("**매수 불가** — 예수금이 부족합니다.")

    b1, b2 = st.columns([1.15, 1])
    with b1:
        st.markdown(f"### 📦 현재 보유 ({len(res.final_lots)}건)")
        if res.final_lots:
            hold = []
            for lot in sorted(res.final_lots, key=lambda x: x["buy_date"]):
                held = bdays(lot["buy_date"], date.today().isoformat())
                left = int(stop_days) - held
                hold.append({
                    "매수일": lot["buy_date"],
                    "수량": round(lot["qty"]),
                    "매수가($)": round(lot["buy_price"], 2),
                    "목표가($)": round(lot["target_price"], 2),
                    "현재손익(%)": round((use_price / lot["buy_price"] - 1) * 100, 2),
                    "목표까지(%)": round((lot["target_price"] / use_price - 1) * 100, 2),
                    "보유일": held,
                    "청산까지": "오늘!" if left <= 0 else f"{left}일",
                })
            st.dataframe(
                pd.DataFrame(hold), width="stretch", hide_index=True,
                height=min(330, 45 + 35 * len(hold)),
                column_config={
                    "현재손익(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                    "목표까지(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )
        else:
            st.caption("보유 중인 건이 없습니다.")

        st.markdown("### 🧾 최근 매매")
        rt = res.trades.tail(8).iloc[::-1].copy()
        if not rt.empty:
            rt["매도일"] = pd.to_datetime(rt["매도일"]).dt.strftime("%m-%d")
            rt["매수일"] = pd.to_datetime(rt["매수일"]).dt.strftime("%m-%d")
            st.dataframe(
                rt[["매수일", "매도일", "수량", "매수가", "매도가", "손익", "수익률(%)", "청산사유"]],
                width="stretch", hide_index=True, height=320,
                column_config={
                    "매수가": st.column_config.NumberColumn(format="$%.2f"),
                    "매도가": st.column_config.NumberColumn(format="$%.2f"),
                    "손익": st.column_config.NumberColumn(format="$%+.2f"),
                    "수익률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )

    with b2:
        st.markdown("### 📈 자산 추이")
        st.line_chart(pd.DataFrame({"총자산": res.equity_curve}), height=310)
        st.markdown("### 💵 현금 vs 주식")
        st.area_chart(
            pd.DataFrame({
                "주식": log.set_index(pd.to_datetime(log["날짜"]))["평가금"],
                "현금": log.set_index(pd.to_datetime(log["날짜"]))["예수금"],
            }),
            height=310,
        )

    # ---------- 존버 vs 전략 ----------
    st.markdown("### 🐢 존버 vs 전략")
    st.caption(
        f"**존버** = 첫날 시드 전액으로 {ticker}를 사서 그냥 놔둔 경우입니다. "
        "입출금도 똑같이 반영해서 공정하게 비교합니다. **이 전략을 할 이유가 있는지 여기서 판단하세요.**"
    )

    bh_final = float(bh_curve.iloc[-1])
    bh_dd = float(((bh_curve / bh_curve.cummax()) - 1).min() * 100)
    bh_ret = (bh_final / put_in - 1) * 100 if put_in > 0 else float("nan")

    v1, v2 = st.columns([1.35, 1])
    with v1:
        st.line_chart(
            pd.DataFrame({"전략": res.equity_curve, f"존버 ({ticker} 그냥 보유)": bh_curve}),
            height=330,
        )
    with v2:
        st.dataframe(
            pd.DataFrame([
                {"항목": "지금 총자산", "전략": f"${total:,.0f}", "존버": f"${bh_final:,.0f}"},
                {"항목": "순손익", "전략": f"${profit:+,.0f}", "존버": f"${bh_final - put_in:+,.0f}"},
                {"항목": "수익률", "전략": f"{res.net_return_pct:+.1f}%", "존버": f"{bh_ret:+.1f}%"},
                {"항목": "최대 낙폭", "전략": f"{res.mdd_pct:.1f}%", "존버": f"{bh_dd:.1f}%"},
                {"항목": "주식 보유 비중", "전략": f"{res.avg_exposure_pct:.0f}%", "존버": "100%"},
            ]),
            width="stretch", hide_index=True, height=220,
        )
        if bh_final > total:
            st.warning(
                f"**이 기간에는 존버가 ${bh_final - total:,.0f} 더 벌었습니다.** "
                f"대신 존버는 한때 {bh_dd:.0f}%까지 빠졌고 이 전략은 {res.mdd_pct:.0f}%였습니다. "
                f"돈이 반토막 나는 걸 버틸 수 있냐가 갈림길입니다."
            )
        else:
            st.success(
                f"**이 기간에는 전략이 ${total - bh_final:,.0f} 더 벌었습니다.** "
                f"낙폭도 존버 {bh_dd:.0f}% 대비 {res.mdd_pct:.0f}%로 얕았습니다."
            )

    st.caption(
        f"평균적으로 자산의 **{res.avg_exposure_pct:.0f}%만 주식**이고 나머지는 현금입니다. "
        "존버보다 수익이 낮게 나와도 이상한 게 아니라, 돈의 일부만 넣고 얻은 결과라는 뜻입니다. "
        "**낙폭과 같이 보세요.**"
    )

    st.caption(
        "⏰ LOC/MOC는 **미 동부 15:50(한국시간 새벽 4:50, 서머타임 해제 시 5:50)** 까지 넣어야 합니다. "
        "저녁에 미리 걸어두면 됩니다. · 보유일은 주말만 제외한 근사치라 미국 공휴일은 반영되지 않습니다."
    )

# ============================================================ 일별 기록
with tab_grid:
    g1, g2, g3 = st.columns([1, 1, 3])
    with g1:
        only_act = st.checkbox("매매한 날만", value=False)
    with g2:
        newest = st.checkbox("최신순", value=True)
    with g3:
        st.caption(
            f"전체 {len(log)}거래일 · 매수 {int(log['매수수량'].notna().sum())}일 · "
            f"매도 {int(log['매도수량'].notna().sum())}일 · 표 우측 상단에서 CSV 저장 가능"
        )

    show = log.copy()
    if only_act:
        show = show[show["매수수량"].notna() | show["매도수량"].notna()]
    show = show.sort_values("날짜", ascending=not newest)
    show["날짜"] = pd.to_datetime(show["날짜"]).dt.strftime("%Y-%m-%d")

    st.dataframe(
        show, width="stretch", hide_index=True, height=620,
        column_config={
            "종가": st.column_config.NumberColumn(format="$%.2f"),
            "입출금": st.column_config.NumberColumn(format="$%+.0f", help="양수는 입금, 음수는 출금"),
            "매수금액": st.column_config.NumberColumn(format="$%.2f"),
            "목표가": st.column_config.NumberColumn(format="$%.2f"),
            "매도금액": st.column_config.NumberColumn(format="$%.2f"),
            "실현손익": st.column_config.NumberColumn(format="$%.2f"),
            "평가금": st.column_config.NumberColumn(format="$%.0f"),
            "예수금": st.column_config.NumberColumn(format="$%.0f"),
            "총자산": st.column_config.NumberColumn(format="$%.0f"),
            "넣은돈": st.column_config.NumberColumn(format="$%.0f"),
            "순손익": st.column_config.NumberColumn(format="$%+.0f"),
            "수익률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
            "최고자산대비(%)": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    st.caption(
        "⚠️ 체결가를 모두 종가로 가정했고 미국 공휴일·배당은 반영하지 않았습니다. "
        "실제 거래하셨다면 증권사 기록이 우선입니다."
    )

# ============================================================ 연도별
with tab_year:
    # 연도별 수익률은 TWR 곡선으로 계산한다. 실제 자산 곡선을 쓰면
    # 그 해에 입금한 금액까지 '수익'으로 잡혀 버린다.
    eq, close = res.twr_curve, hist["Close"]
    tr = res.trades.copy()
    if not tr.empty:
        tr["연도"] = pd.to_datetime(tr["매도일"]).dt.year

    rows = []
    _years = sorted(set(eq.index.year))
    for yr, grp in eq.groupby(eq.index.year):
        if len(grp) < 5:  # 며칠뿐이면 의미가 없다. 그 이상이면 바로 줄을 만든다
            continue
        bh = close.loc[grp.index]
        yt = tr[tr["연도"] == yr] if not tr.empty else pd.DataFrame()
        # 아직 안 끝난 해와 중간부터 시작한 해는 표시해준다
        partial = "  (진행중)" if yr == _years[-1] else ("  (일부)" if yr == _years[0] and len(grp) < 200 else "")
        rows.append({
            "연도": f"{yr}{partial}",
            "수익률(%)": round((grp.iloc[-1] / grp.iloc[0] - 1) * 100, 1),
            "연내 MDD(%)": round(((grp - grp.cummax()) / grp.cummax()).min() * 100, 1),
            "승률(%)": round((yt["손익"] > 0).mean() * 100, 1) if len(yt) else None,
            "매매": len(yt),
            f"존버({ticker}) 수익률(%)": round((bh.iloc[-1] / bh.iloc[0] - 1) * 100, 1),
        })
    ydf = pd.DataFrame(rows)

    yc1, yc2 = st.columns([1.1, 1])
    with yc1:
        st.dataframe(ydf, width="stretch", hide_index=True, height=min(430, 60 + 36 * len(ydf)))
    with yc2:
        m = st.columns(3)
        m[0].metric("CAGR", f"{res.cagr_pct:.2f}%")
        m[1].metric("MDD", f"{res.mdd_pct:.2f}%")
        m[2].metric("효율", f"{res.cagr_pct / -res.mdd_pct:.2f}" if res.mdd_pct else "—")
        st.line_chart(
            pd.DataFrame({"이 전략": res.equity_curve, f"존버 ({ticker})": bh_curve}),
            height=330,
        )
        if has_flows:
            st.caption("표의 연도별 수치는 입출금 효과를 뺀 값이고, 위 그래프는 실제 자산 금액입니다.")

    # 요약은 온전히 채워진 해만 쓴다 ('일부'·'진행중' 표시된 해 제외)
    full = ydf[~ydf["연도"].astype(str).str.contains(r"\(")] if not ydf.empty else ydf
    if len(full) >= 2:
        st.info(
            f"**온전한 {len(full)}개년 중 {int((full['수익률(%)'] > 0).sum())}년 플러스** · "
            f"평균 {full['수익률(%)'].mean():.1f}% · 최고 {full['수익률(%)'].max():.1f}% / "
            f"최저 {full['수익률(%)'].min():.1f}% · 연내 낙폭 평균 {full['연내 MDD(%)'].mean():.1f}% "
            f"(최악 {full['연내 MDD(%)'].min():.1f}%) — **(일부)·(진행중)** 표시된 해는 제외했습니다"
        )
    _bhdd = ((bh_curve / bh_curve.cummax()) - 1).min() * 100
    st.caption(
        f"같은 기간 존버({ticker})는 {(close.iloc[-1]/close.iloc[0]-1)*100:,.0f}% 올랐지만 "
        f"한때 {_bhdd:.1f}%까지 빠졌습니다. 이 전략은 {res.mdd_pct:.1f}%. "
        "수익률만 보지 말고 낙폭 차이를 꼭 같이 보세요."
    )

# ============================================================ 규칙 · 설정
with tab_help:
    h1, h2 = st.columns([1, 1])

    with h1:
        st.markdown("### 📖 규칙 (이게 전부입니다)")
        st.markdown(
            f"""
**매일 장 마감 무렵, 두 가지만 확인합니다.**

**1) 팔 것** — 보유 중인 건마다
- 오늘 종가가 **목표가(+{tgt_pct:.2f}%) 이상** → 🎯 판다
- 산 지 **{stop_days}영업일** 경과 → 🛑 가격 상관없이 판다
- 둘 다 아니면 그냥 들고 있는다

**2) 살 것**
- 오늘 판 게 있으면 → {SELL_DAY_MODES[cfg.get('sell_day_buy_mode','never')]}
- 판 게 없으면 → **{'어제 마감 총자산' if reinvest else '시드'}의 {daily_pct*100:.1f}%** 만큼 산다

**손절은 "얼마 떨어지면"이 아니라 "며칠 지나면"입니다.**
-30%가 나도 {stop_days}영업일 전엔 안 팔고, 그날이 오면 무조건 팝니다.

**절대 하면 안 되는 것**
- 목표가 왔는데 더 오를 것 같아서 안 팔기
- 청산일인데 곧 오를 것 같아서 안 팔기
- 무서워서 중간에 다 팔아버리기
"""
        )
        st.markdown("### ❓ LOC / MOC")
        st.markdown(
            """
- **MOC** (종가 시장가): 마감 종가에 **무조건 체결**. → 매수, 강제청산에 사용
- **LOC** (종가 지정가): 종가가 지정가 **이상**일 때만 체결. → 목표 매도에 딱 맞음

이 전략은 모든 매매를 종가로 가정해 검증했으므로 LOC/MOC를 쓰면 백테스트와 어긋나지 않습니다.
증권사 앱에 'LOC'/'MOC' 또는 '종가지정가'/'종가시장가'가 있는지 확인하세요.
"""
        )

    with h2:
        st.markdown("### ⭐ 추천 설정 (SOXL 2010-2024 검증)")
        st.dataframe(
            pd.DataFrame([
                {"이름": n, "분할수": round(1 / p["daily_buy_pct"]), "매수비율(%)": round(p["daily_buy_pct"] * 100, 1),
                 "CAGR(%)": p["CAGR"], "MDD(%)": p["MDD"], "효율": p["효율"]}
                for n, p in PRESETS.items()
            ]),
            width="stretch", hide_index=True,
        )
        st.caption(
            "**효율 = CAGR ÷ MDD**. 높을수록 위험 대비 수익이 좋습니다. "
            "10분할(10%) 구간이 가장 효율이 높아 처음엔 여기서 시작하는 걸 권합니다. "
            "12.5%를 넘기면 수익보다 낙폭이 더 빨리 커집니다."
        )
        pc1, pc2 = st.columns([2, 1])
        with pc1:
            chosen = st.selectbox("프리셋 고르기", list(PRESETS.keys()), label_visibility="collapsed")
        with pc2:
            if st.button("적용", type="primary", width="stretch"):
                apply_preset(cfg, chosen)
                st.rerun()
        st.caption(PRESETS[chosen]["설명"])

        st.markdown("### 🔧 세부 설정")
        mode_keys = list(SELL_DAY_MODES.keys())
        m = st.selectbox(
            "매도가 있는 날에도 매수할까", mode_keys,
            index=mode_keys.index(cfg.get("sell_day_buy_mode", "never")),
            format_func=lambda k: SELL_DAY_MODES[k],
        )
        s1, s2 = st.columns(2)
        with s1:
            fit = st.checkbox("목표가에 수수료 반영", cfg.get("fee_in_target", True),
                              help="수수료를 내고도 목표%가 남도록 목표가를 올립니다.")
        with s2:
            ws = st.checkbox("정수주만 매수", cfg.get("whole_shares", True),
                             help="소수점 주식을 못 사는 증권사면 켜세요.")
        if st.button("세부 설정 저장", width="stretch"):
            cfg.update({"sell_day_buy_mode": m, "fee_in_target": bool(fit), "whole_shares": bool(ws)})
            save_config(cfg)
            st.rerun()

        st.markdown("### ⚠️ 알아둘 것")
        st.warning(
            "**SOXL은 3배 레버리지 ETF입니다.** 반도체 지수가 하루 -10% 빠지면 SOXL은 -30%입니다. "
            "과거 백테스트에서도 자산이 30~40% 줄어드는 구간이 여러 번 있었습니다. "
            "이 도구는 계산기일 뿐이고 수익을 보장하지 않습니다."
        )
        st.caption(
            "**기록을 따로 안 남기는 이유** — 시작일과 규칙이 정해지면 그날부터 오늘까지의 "
            "모든 매매가 자동으로 결정됩니다. 그래서 매번 처음부터 다시 계산합니다. "
            "규칙을 어긴 매매가 있었다면 실제 잔고와 달라집니다."
        )
