"""종사종팔 V5 전용 앱.

핵심 발상: **시작일만 정하면 규칙이 나머지를 전부 결정한다.**
그래서 매매를 하나하나 기록할 필요가 없다. 설정값만 넣으면
시작일부터 오늘까지를 매번 다시 계산해서 현재 상태와 오늘 할 일을 보여준다.
"""

import sys
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src.data.kr_data import get_dividends, get_kr_ohlcv
from src.jongsa_backtest import run_buy_and_hold, run_jongsa
from src.jongsa_live import BUY_RANGE_SKIPS_15Y, BUY_RANGE_VS_NOLIMIT, PRESETS, SELL_DAY_MODES
from src.jongsa_live import apply_preset, is_shared_server, load_config, save_config
from src.jongsa_live import make_held_counter, order_plan, target_price_for
from src.jongsa_notify import build_message, send_now
from src.scheduler import (JONGSA_TASK_NAME, get_jongsa_task_status, load_jongsa_notify_config,
                           register_jongsa_task, remove_jongsa_task, save_jongsa_notify_config)
from src.telegram_notify import find_chat_id, load_telegram_config, save_telegram_config

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
      /* 델타(+91% 같은 것)가 있는 카드와 없는 카드의 아래 끝을 맞춘다 */
      min-height: 92px;
      display: flex; flex-direction: column; justify-content: center;
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

  /* 탭을 상단에 크게. 이 스트림릿 버전에는 data-baseweb="tab" 속성이 없어서
     [role="tab"]으로 잡아야 한다. */
  [role="tablist"] {gap: 4px; margin-bottom: 0.4rem;}
  [role="tab"] {padding: 10px 20px !important;}
  [role="tab"] p {font-size: 1.0rem !important; font-weight: 600;}
  [role="tabpanel"] [role="tab"] {padding: 6px 14px !important;}
  [role="tabpanel"] [role="tab"] p {font-size: 0.9rem !important;}

  /* '분할수'만 도움말(?) 아이콘 때문에 라벨이 두 줄이 되어 입력칸이
     아래로 밀렸다. 높이를 맞추고 아이콘을 같은 줄에 세운다. */
  label[data-testid="stWidgetLabel"] {
      display: inline-flex; align-items: center; gap: 5px; min-height: 26px;
  }
  label[data-testid="stWidgetLabel"] > div {display: flex; align-items: center;}
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
    "mo": ("moc_available", None),   # 1 / 0
    "br": ("buy_range_pct", None),   # % 로 넣고 소수로 바꾼다
    "lr": ("ladder_rungs", int),     # 사다리 칸 수
    "ls": ("ladder_step", None),     # % 로 넣고 소수로 바꾼다
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
            elif key in ("r", "f", "br", "ls"):
                cfg[name] = float(raw) / 100
            elif key in ("ri", "mo"):
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
        "mo": "1" if cfg.get("moc_available", True) else "0",
        "br": f"{cfg.get('buy_range_pct', 0.10) * 100:g}",
        "lr": f"{int(cfg.get('ladder_rungs', 0))}",
        "ls": f"{cfg.get('ladder_step', 0.03) * 100:g}",
    }
    if flows:
        params["c"] = flows_to_param(flows)
    st.query_params.from_dict(params)


if "cfg" not in st.session_state:
    st.session_state.cfg = cfg_from_url(load_config())
cfg = st.session_state.cfg

# 원 작성자가 공유한 V5 규칙을, 처음 하는 사람이 읽을 수 있게 옮긴 것.
RULES_MD = """
### 하루에 딱 두 가지만 봅니다

**① 팔 것이 있나** → **② 없으면 산다.** 이게 전부입니다.

---

### 1) 팔 것 — 두 종류뿐입니다

| | 언제 | 주문 |
|---|---|---|
| 🎯 **익절** | 산 가격보다 **{tgt}% 위**로 종가가 마감되면 | **LOC 매도** (목표가 지정) |
| 🛑 **손절** | 산 지 **{stop}영업일**이 되면 (가격 무관) | **MOC 매도** (무조건 체결) |

**손절일 세는 법** — 달력 날짜가 아니라 **장 열린 날**로 셉니다.

```
1월 3일 매수 체결   → 0일  (산 날은 0일)
1월 6일             → 1일  (다음 거래일부터 1일)
   ...
1월 17일            → 10일 ← 이날 MOC 매도
```

주말·공휴일은 안 셉니다.

---

### 2) 살 것 — 하루 **{pct}%** ({splits}분할)

**매수는 항상 LOC입니다. MOC는 매도에만 씁니다.**
지정가를 무엇으로 잡느냐만 두 가지로 갈립니다.

| 상황 | 매수 LOC 지정가 |
|---|---|
| **팔 물량이 없다** (첫날, 다 팔린 다음날) | 어제 종가 × (1 + **매수 범위 {rng}%**) |
| **팔 물량이 있다** | (**매도 목표가 중 가장 낮은 값**) − $0.01 |

**"판 게 있는 날은 사지 않는다"** 가 핵심 규칙인데, 주문 넣을 때는 오늘 종가를 모릅니다.
그래서 아래 지정가가 그 규칙을 대신 지켜줍니다.

> ### 매수 LOC = (최저 목표가) − $0.01
>
> - 종가가 목표가에 **닿으면** → 매도 체결, **매수는 자동 미체결** ✅
> - 종가가 목표가에 **안 닿으면** → 매도 없음, **매수만 체결** ✅
>
> 주문 하나로 **매수와 매도 중 하나만** 일어납니다.

**매수 범위란** — 팔 물량이 없는 날엔 위 장치를 쓸 수 없으니, 대신 "어제보다 {rng}% 넘게 오른 날은 안 산다"는 상한을 둡니다. 카페에서는 **+5~10%** 를 씁니다.

**손절일({stop}영업일)이 걸린 날**은 가격과 무관하게 무조건 파는 날이라, **매수 주문을 아예 넣지 않습니다.** 날짜만 보면 미리 알 수 있어요.

> 💡 **LOC 지정가는 '살 가격'이 아니라 '살지 말지의 기준'입니다.**
> 실제 체결은 언제나 **그날 종가**로 됩니다. 지정가 $126에 걸어도 종가가 $115면 $115에 삽니다.

---

### 3) 몇 분할로 할까

| 분할 | 하루 매수 | 누구에게 |
|---|---|---|
| **10분할** | 총자산의 10% | **기본값. 처음이면 여기서 시작** |
| 7분할 | 약 14% | 익숙해진 뒤 |
| 5분할 | 20% | 원문 표현으로 "용감하면" |

10분할이 기본인 이유: 매도하는 날은 안 사기 때문에 시드가 다 소진되는 일이 잘 없습니다.

---

### 4) 복리 — V5는 **100% 복리**입니다

번 돈도 잃은 돈도 **전부 다음 매수금에 반영**합니다.
어제 마감 총자산의 {pct}%를 오늘 삽니다. 계산이 제일 단순합니다.

(앱의 **수익 재투자 ⭕** 가 이 방식입니다)

---

### 5) 퉁치기? V5는 신경 안 써도 됩니다

매수가를 매도가보다 높게 걸 일이 없어서 **같은 날 사고파는 일이 안 생깁니다.**
(V3에서만 생기는 문제입니다)

---

### 📌 원문 기준 성적

**CAGR 약 30% / MDD 약 -30%**

MDD -30%는 **한때 내 돈이 30% 줄어드는 구간이 있었다**는 뜻입니다.
{ticker}가 3배 레버리지라 실제로 그런 구간이 여러 번 있었습니다.

---

### ⛔ 절대 하면 안 되는 것

- 목표가 왔는데 "더 오를 것 같아서" 안 팔기
- 손절일인데 "곧 오를 것 같아서" 안 팔기
- 무서워서 중간에 다 팔아버리기

**이걸 어기면 위 성적은 아무 의미가 없습니다.**
"""

if "flows" not in st.session_state:
    st.session_state.flows = flows_from_url()


@st.cache_data(ttl=1800, show_spinner=False)
def load_price_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    return get_kr_ohlcv(ticker, start, end)


@st.cache_data(ttl=1800, show_spinner=False)
def load_dividends(ticker: str, start: str, end: str) -> pd.Series:
    """배당 내역. 못 받아와도 빈 값이 오므로 계산은 그대로 된다."""
    return get_dividends(ticker, start, end)


@st.cache_data(ttl=1800, show_spinner=False)
def simulate(ticker, start, today, cash, pct, tgt, stop, fee, fee_in_tgt, whole, mode,
             reinvest, flows, buy_range=None, ladder_rungs=0, ladder_step=0.03):
    """시작일부터 오늘까지 규칙대로 돌린다. 설정이 같으면 캐시에서 바로 나온다.

    today를 인자로 받는 이유: 캐시 키에 날짜가 들어가야 날이 바뀌는 순간
    무조건 다시 계산된다. 함수 안에서 date.today()를 부르면 캐시가 그대로
    남아 어제 결과를 보여줄 수 있다.
    flows는 캐시 키가 되어야 하므로 튜플로 받는다.
    존버(그냥 사서 놔두기)도 같은 입출금 조건으로 같이 돌려서 비교한다.
    """
    hist = load_price_history(ticker, start, today)
    div = load_dividends(ticker, start, today)
    res = run_jongsa(
        hist, "V5",
        initial_cash=cash, target_return=tgt, daily_buy_pct=pct, stop_days=int(stop),
        fee_rate=fee, whole_shares=whole, fee_in_target=fee_in_tgt,
        sell_day_buy_mode=mode, reinvest=reinvest, cash_flows=list(flows),
        buy_range_pct=buy_range, dividends=div,
        ladder_rungs=ladder_rungs, ladder_step=ladder_step,
    )
    bh = run_buy_and_hold(
        hist, initial_cash=cash, fee_rate=fee, whole_shares=whole,
        cash_flows=list(flows), dividends=div,
    )
    return res, hist, bh


st.markdown("# 🔁 종사종팔 V5")
if is_shared_server():
    st.caption(
        "SOXL 분할매매 계산기입니다. **투자 자문이 아니고 수익을 보장하지 않습니다.** "
        "설정은 사람마다 따로 유지됩니다 — **지금 주소를 즐겨찾기 해두면** 다음에 열 때도 "
        "이 설정 그대로 뜹니다 (주소창을 보면 설정값이 붙어 있습니다)."
    )

tab_home, tab_grid, tab_year, tab_help, tab_notify = st.tabs(
    ["📅 오늘 할 일 (주문 시트)", "📋 일별 기록", "📊 백테스트", "📖 규칙 · 설정", "🔔 알림"]
)

# ============================================================ 오늘 할 일
# 설정과 현황 카드도 이 탭 안에서 그린다. 아래 탭들은 여기서 만든
# 결과 변수(res/hist/bh_curve...)를 그대로 쓴다 — 설정이 한 곳뿐이라
# 탭마다 따로 맞출 필요가 없다.
ready = True
with tab_home:
    # ============================================================ 설정 (맨 위)


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
    recent = None   # 계산이 안 될 때만 쓰는 최근 시세
    try:
        with st.spinner("계산 중..."):
            res, hist, bh_curve = simulate(
                ticker, start_d.isoformat(), date.today().isoformat(),
                float(seed), daily_pct, tgt_pct / 100, int(stop_days),
                fee_pct / 100, cfg.get("fee_in_target", True), cfg.get("whole_shares", True),
                cfg.get("sell_day_buy_mode", "never"), bool(reinvest), flow_tuples,
                cfg.get("buy_range_pct", 0.10),
                int(cfg.get("ladder_rungs", 0)), float(cfg.get("ladder_step", 0.03)),
            )
    except Exception as e:
        ready = False   # 아래 탭들이 쓸 계산 결과가 없다는 뜻

        # 시작일이 정말 최근이라 돌릴 게 없는 것과, 시세를 못 받은 것은 다른 문제다.
        # 예전에는 둘을 구분하지 않아서, 시세를 못 받았는데도 '오늘부터
        # 시작하시는군요'가 뜨고 그 뒤에서 엉뚱하게 터졌다.
        too_recent = (date.today() - start_d).days <= 21

        try:
            recent = load_price_history(
                ticker, (date.today() - timedelta(days=40)).isoformat(), date.today().isoformat()
            )
        except Exception:
            recent = None
        if recent is not None and len(recent) == 0:
            recent = None

        if not too_recent:
            # 시세를 못 받은 것과 계산 중 터진 것은 다른 문제다. 섞어서 안내하면
            # 엉뚱한 데를 고치게 된다.
            no_data = recent is None
            st.error(
                (f"**{ticker} 시세를 받아오지 못했습니다.** 종목코드를 확인해보시고, "
                 f"맞다면 시세 서버가 잠시 막힌 것이니 1~2분 뒤 새로고침해보세요."
                 if no_data else
                 f"**계산 중 오류가 났습니다.** 시세는 받아졌는데 처리에 실패했습니다. "
                 f"새로고침해도 같으면 이 메시지를 그대로 알려주세요.")
                + f"\n\n시작일 {start_d} · 원인: `{e}`"
            )
            recent = None
        elif recent is None:
            st.error(
                f"**{ticker} 시세를 못 받았습니다.** 종목코드를 확인해주세요. "
                f"(미국 종목은 티커, 한국 종목은 6자리 숫자)\n\n원인: `{e}`"
            )

    # ---------- 오늘부터 시작하는 경우 ----------
    # 지나간 날이 없으니 백테스트는 못 하지만, '오늘 얼마 사면 되는지'는 알려줄 수 있다.
    if not ready and recent is not None:
        px = float(pd.to_numeric(recent["Close"], errors="coerce").dropna().iloc[-1])
        px_date = recent.index[-1].date()
        st.info(
            f"**{start_d}부터 시작하시는군요.** 아직 지나간 날이 없어서 성적표는 없습니다. "
            "대신 **오늘 넣을 첫 주문**을 아래에 정리했습니다."
        )

        # 주문에 필요한 값은 전부 규칙으로 정해진다. 입력받을 게 없다.
        #   지정가 = 어제 종가 x (1 + 매수 범위)
        #   수량   = 예산 / 어제 종가
        # 목표가만 '실제 체결가'가 있어야 나오는데, 체결가는 오늘 종가라 지금은 모른다.
        # 그래서 목표가 계산은 주문 안내 아래로 따로 뺐다.
        _rng = cfg.get("buy_range_pct", 0.10)
        _limit = round(px * (1 + _rng), 2)
        first_budget = seed * daily_pct
        _raw_qty = first_budget * (1 - fee_pct / 100) / px
        first_qty = int(_raw_qty) if cfg.get("whole_shares", True) else _raw_qty
        first_cost = first_qty * px * (1 + fee_pct / 100)

        f1, f2, f3, f4 = st.columns(4)
        f1.metric("시드", f"${seed:,.0f}")
        f2.metric("오늘 살 금액", f"${first_cost:,.2f}", f"시드의 {daily_pct*100:.1f}%")
        f3.metric("수량", f"{first_qty:,.0f}주")
        f4.metric("LOC 지정가", f"${_limit:,.2f}", f"어제 종가 +{_rng*100:.0f}%")

        st.markdown("### 📋 오늘 넣을 주문")
        st.success(
            f"### {ticker} **{first_qty:,.0f}주** 매수 — **LOC 지정가 \\${_limit:,.2f}**\n\n"
            f"약 **\\${first_cost:,.2f}** 어치입니다. "
            f"지정가 = 어제 종가 \\${px:,.2f} × (1 + **매수 범위 {_rng*100:.0f}%**)"
        )
        st.caption(
            f"**처음이라 팔 물량이 없으니** 매수 범위를 씌워서 겁니다. "
            f"어제 종가보다 {_rng*100:.0f}% 넘게 오르면 안 사고 넘어갑니다. "
            f"그 안에서 마감하면 **종가에** 체결됩니다 (지정가에 사는 게 아닙니다). "
            f"매수 범위는 **규칙·설정 탭**에서 바꿀 수 있습니다."
        )
        st.caption(
            f"**⏰ 주문 마감**: 미 동부 15:50 (한국시간 새벽 4:50, 서머타임 해제 시 5:50)까지. "
            f"저녁에 미리 걸어두면 됩니다. · 수량은 예산을 넘지 않게 내림했습니다."
        )

        st.info(
            f"**오늘은 이 주문 하나만 넣으면 끝입니다.** 목표가는 지금 몰라도 됩니다.\n\n"
            f"목표가는 실제 체결가(= 오늘 종가)의 +{tgt_pct:.2f}% 인데, 오늘 산 물량은 "
            f"**내일부터** 매도 대상이라 오늘 밤 걸 매도 주문이 없습니다.\n\n"
            f"**내일 이 화면을 다시 열면** — 시작일을 오늘({start_d})로 둔 채 — "
            f"오늘 종가가 반영돼서 걸어야 할 **LOC 매도가**와 다음 매수 주문이 같이 나옵니다. "
            f"**아무것도 입력하실 필요 없습니다.**"
        )
        with st.expander("📖 이 전략 규칙 한눈에 보기", expanded=True):
            st.markdown(RULES_MD.format(
                tgt=f"{tgt_pct:.2f}", stop=int(stop_days), splits=int(splits),
                pct=f"{daily_pct*100:.1f}", ticker=ticker,
                rng=f"{cfg.get('buy_range_pct', 0.10)*100:.0f}",
            ))

if ready:
  with tab_home:
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

    # 배당은 받은 게 있을 때만 칸을 내준다. 0이면 자리만 차지한다.
    has_div = res.total_dividends > 0

    k = st.columns(6 + int(has_flows) + int(has_div))
    i = 0
    k[i].metric("총자산", f"${total:,.0f}"); i += 1
    if has_flows:
        k[i].metric("넣은 돈", f"${put_in:,.0f}", f"시드 ${seed:,.0f}"); i += 1
    k[i].metric("누적 손익금", f"${profit:+,.0f}"); i += 1
    if has_div:
        k[i].metric(
            "받은 배당", f"${res.total_dividends:,.2f}",
            f"손익의 {res.total_dividends / profit * 100:.1f}%" if profit > 0 else None,
            help="배당은 따로 쌓아두고 매매에 쓰지 않습니다 (재투자 안 함). 총자산에는 포함됩니다.",
        ); i += 1
    k[i].metric("누적 수익률", f"{res.net_return_pct:+.2f}%"); i += 1
    k[i].metric("남은 현금", f"${cash:,.0f}", f"{cash/total*100:.0f}%" if total else None); i += 1
    # '현재가'가 아니라 바로 직전 거래일의 종가다. 라벨과 날짜를 명확히 한다.
    k[i].metric(
        f"{ticker} 전 거래일 종가", f"${price:,.2f}",
        (f"{price_date:%m/%d} · 전일대비 {(price/log.iloc[-2]['종가']-1)*100:+.2f}%"
         if len(log) > 1 else f"{price_date:%m/%d}"),
        help="바로 직전 거래일의 종가입니다. 오늘 넣을 주문은 이 값을 기준으로 계산합니다.",
    ); i += 1
    k[i].metric("현재 보유", f"{shares:,.0f}주", f"${equity:,.0f} · {int(last['보유건수'])}건")

    # 기간이 짧으면 연 환산이 의미가 없어 엔진이 CAGR을 안 준다
    short_period = pd.isna(res.cagr_pct)
    cagr_txt = "연평균 —" if short_period else f"연평균 {res.cagr_pct:.1f}%"
    st.caption(
        f"**{start_d} → {price_date}** 기준 · 거래일 {len(log)}일 · "
        f"매매 {res.num_trades}회 (익절 {res.num_target_sells} / 손절 {res.num_forced_sells}) · "
        f"승률 {res.win_rate_pct:.1f}% · **{cagr_txt} · 최대낙폭 {res.mdd_pct:.1f}%**"
        + ("  (연평균·최대낙폭은 입출금 효과를 뺀 전략 자체의 성적입니다)" if has_flows else "")
    )
    if short_period:
        st.info(
            f"**아직 {len(log)}거래일밖에 안 됐습니다.** 총자산·손익은 정확하지만, "
            f"연평균 수익률은 기간이 짧으면 뻥튀기돼서 표시하지 않습니다 "
            f"(약 3개월 지나면 나옵니다). 지금은 **오늘 할 일**만 보시면 됩니다."
            + (f" 청산 {stop_days}영업일이 아직 한 번도 안 지나 손절 기록이 없을 수 있습니다."
               if len(log) <= int(stop_days) else "")
        )
    st.caption(
        f"마지막 반영 종가는 **{price_date}의 \\${price:,.2f}** 입니다. "
        "아래는 **오늘 마감 전에 미리 걸어둘 주문**입니다. 오늘 종가를 몰라도 되게 만들어져 있습니다."
    )

    # 오늘 종가를 모르는 상태에서 주문을 짠다 (원문 요령: 매수 LOC = 최저 목표가 - 0.01)
    plan_cfg = {**cfg, "_last_close": price}
    # 하루 매수금 기준액에서 배당은 뺀다. 넣으면 그게 배당 재투자가 되고,
    # 엔진(prev_total_assets)과도 어긋나 안내 수량이 실제와 달라진다.
    trading_assets = total - res.total_dividends
    plan = order_plan(
        res.final_lots, cash, (trading_assets if reinvest else put_in), plan_cfg,
        date.today().isoformat(), trading_dates=hist.index,
    )
    forced, pending, buy = plan["강제매도"], plan["목표매도"], plan["매수"]

    a1, a2 = st.columns(2)
    with a1:
        st.markdown("### 🔴 팔 주문")
        if forced or pending:
            rows = []
            for s in forced:
                alt = s.get("대체지정가")
                rows.append({
                    "주문": "🛑 LOC 매도" if alt else "🛑 MOC 매도",
                    "지정가": f"${alt:.2f} (사실상 무조건)" if alt else "— (무조건 체결)",
                    "수량": f"{s['qty']:,.0f}주",
                    "매수일": s["buy_date"],
                    "매수가": f"${s['buy_price']:.2f}",
                    "사유": f"{s['보유영업일']}영업일 경과 — 손절일",
                })
            for s in pending:
                rows.append({
                    "주문": "🎯 LOC 매도",
                    "지정가": f"${s['target_price']:.2f}",
                    "수량": f"{s['qty']:,.0f}주",
                    "매수일": s["buy_date"],
                    "매수가": f"${s['buy_price']:.2f}",
                    "사유": f"{s['보유영업일']}일차 · 종가가 지정가 이상이면 체결",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            if forced:
                st.error(
                    f"**{sum(s['qty'] for s in forced):,.0f}주는 오늘 무조건 팔립니다** "
                    f"(손절일 도래). 가격이 얼마든 팝니다."
                )
            if pending:
                st.caption(
                    f"LOC 매도 {len(pending)}건은 **종가가 지정가 이상일 때만** 체결됩니다. "
                    "안 되면 그냥 미체결이고 내일 다시 겁니다."
                )
        else:
            st.success("**팔 주문 없음** — 어제 산 것뿐이라 아직 매도 대상이 아닙니다.")

    with a2:
        st.markdown("### 🟢 살 주문")
        if buy["type"] == "LOC":
            st.info(
                f"### {buy['qty']:,.0f}주 매수 — **LOC 지정가 \\${buy['limit']:.2f}**\n\n"
                f"약 **\\${buy['cost']:,.2f}** · 지정가 = 위 목표가 중 최저 "
                f"**\\${buy['limit'] + 0.01:.2f}** 에서 **−$0.01**"
            )
            st.caption(
                "**왜 이렇게 거나** — 종가가 목표가에 닿으면 매도가 체결되면서 이 매수는 "
                "지정가를 넘어 자동으로 미체결됩니다. 안 닿으면 매도 없이 매수만 됩니다. "
                "**'판 날은 안 산다'는 규칙이 주문 하나로 지켜집니다.**"
            )
        elif buy["type"] == "LOC_RANGE":
            _rng = cfg.get("buy_range_pct", 0.10)
            st.info(
                f"### {buy['qty']:,.0f}주 매수 — **LOC 지정가 \\${buy['limit']:.2f}**\n\n"
                f"약 **\\${buy['cost']:,.2f}** · 오늘은 팔 물량이 없습니다. "
                f"지정가 = 어제 종가 \\${price:,.2f} × (1 + **매수 범위 {_rng*100:.0f}%**)"
            )
            st.caption(
                f"**매수 범위란** — 어제 종가보다 {_rng*100:.0f}% 넘게 오른 날은 사지 않겠다는 뜻입니다. "
                f"그 안에서 마감하면 **종가에** 체결됩니다 (지정가에 사는 게 아닙니다). "
                + (f"과거 SOXL 15년(3,919거래일)에서 이 설정으로 건너뛴 날은 "
                   f"**{BUY_RANGE_SKIPS_15Y[round(_rng, 2)]}일**뿐이었습니다."
                   if round(_rng, 2) in BUY_RANGE_SKIPS_15Y else "")
            )
        else:
            st.error(f"### 오늘은 매수 없음\n\n{buy['reason']}")

        # 사다리 주문 — 기본 주문 아래로 더 걸어서 남는 예산을 채운다
        if buy.get("사다리"):
            rows = []
            cum = buy["qty"]
            for q, px in buy["사다리"]:
                cum += q
                rows.append({
                    "추가 주문": f"{q:,.0f}주",
                    "LOC 지정가": f"${px:,.2f}",
                    "이 가격 이하면 총": f"{cum:,.0f}주",
                    "쓰는 돈": f"${cum * px:,.0f}",
                })
            st.markdown("**➕ 사다리 주문** — 아래 주문도 같이 걸어두세요")
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                         height=45 + 35 * len(rows))
            _last_px = buy["사다리"][-1][1]
            st.caption(
                f"**왜 거나** — 종가가 지정가보다 낮게 끝나면 예산이 남습니다. "
                f"아래에 미리 걸어두면 그만큼 더 사서 예산을 채웁니다. "
                f"**예산을 넘기지 않습니다** (지정가를 내림으로 잡았습니다). "
                f"기본 지정가보다 낮으니 **매도가 일어나는 날엔 하나도 안 걸립니다.** "
                f"종가가 \\${_last_px:,.2f} 밑으로 더 빠지면 그때는 예산이 조금 남습니다."
            )

        if plan["부족"] and buy["type"]:
            st.warning(
                f"예수금 부족 — 목표 \\${plan['목표금액']:,.0f} 중 \\${cash:,.0f}만 가능합니다."
            )

    if buy["type"]:
        st.caption(
            "**오늘 산 물량의 목표가는 지금 몰라도 됩니다.** 목표가는 실제 체결가(=오늘 종가) 기준인데, "
            f"오늘 산 건 **내일부터** 매도 대상이라 오늘 밤 걸 주문이 없습니다. "
            f"내일 이 화면을 열면 오늘 종가가 반영돼서 걸어야 할 **LOC 매도가**가 위 '팔 주문'에 나옵니다. "
            f"미체결이면 아무것도 안 하시면 됩니다."
        )

    b1, b2 = st.columns([1.25, 1])
    with b1:
        st.markdown(f"### 📦 현재 보유 ({len(res.final_lots)}건)")
        if res.final_lots:
            hold = []
            held_of = make_held_counter(date.today().isoformat(), hist.index)
            for lot in sorted(res.final_lots, key=lambda x: x["buy_date"]):
                held = held_of(lot["buy_date"])
                left = int(stop_days) - held
                hold.append({
                    "매수일": lot["buy_date"],
                    "수량": round(lot["qty"]),
                    "매수가($)": round(lot["buy_price"], 2),
                    "목표가($)": round(lot["target_price"], 2),
                    "현재손익(%)": round((price / lot["buy_price"] - 1) * 100, 2),
                    "목표까지(%)": round((lot["target_price"] / price - 1) * 100, 2),
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

    with b2:
        st.markdown("### 🧾 최근 매매")
        rt = res.trades.tail(10).iloc[::-1].copy()
        if not rt.empty:
            rt["매도일"] = pd.to_datetime(rt["매도일"]).dt.strftime("%m-%d")
            rt["매수일"] = pd.to_datetime(rt["매수일"]).dt.strftime("%m-%d")
            st.dataframe(
                rt[["매수일", "매도일", "수량", "매수가", "매도가", "손익", "수익률(%)", "청산사유"]],
                width="stretch", hide_index=True, height=min(390, 45 + 35 * len(rt)),
                column_config={
                    "매수가": st.column_config.NumberColumn(format="$%.2f"),
                    "매도가": st.column_config.NumberColumn(format="$%.2f"),
                    "손익": st.column_config.NumberColumn(format="$%+.2f"),
                    "수익률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )
        else:
            st.caption("아직 청산된 매매가 없습니다.")

    st.caption(
        "⏰ LOC/MOC는 **미 동부 15:50(한국시간 새벽 4:50, 서머타임 해제 시 5:50)** 까지 넣어야 합니다. "
        "저녁에 미리 걸어두면 됩니다. · 보유일은 실제로 장이 열린 날만 셉니다 (주말·미국 공휴일 제외). "
        "· 성과 그래프와 존버 비교는 **📊 백테스트** 탭에 있습니다."
    )

# ============================================================ 일별 기록
# 아래 탭들은 '오늘 할 일' 탭에서 만든 계산 결과를 그대로 쓴다.
# 설정이 한 곳(오늘 할 일)뿐이라 탭마다 따로 맞출 필요가 없다.
if not ready:
    with tab_grid:
        st.info(
            "아직 쌓인 기록이 없습니다. **📅 오늘 할 일** 탭에서 시작일을 "
            "조금 더 앞으로 잡으면 하루하루가 여기에 채워집니다."
        )

if ready:
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
        "⚠️ 체결가를 모두 종가로 가정했습니다. 배당은 받은 날짜에 따로 쌓이고 "
        "매매에는 쓰지 않습니다 (재투자 안 함). 실제 거래하셨다면 증권사 기록이 우선입니다."
    )

# ============================================================ 백테스트
def year_table(twr, close_s, trades, tk):
    """연도별 성과표. 수익률은 TWR 기준(입출금 효과 제거)."""
    tr = trades.copy()
    if not tr.empty:
        tr["연도"] = pd.to_datetime(tr["매도일"]).dt.year
    rows, years = [], sorted(set(twr.index.year))
    for yr, grp in twr.groupby(twr.index.year):
        if len(grp) < 5:
            continue
        bh = close_s.loc[grp.index]
        yt = tr[tr["연도"] == yr] if not tr.empty else pd.DataFrame()
        partial = "  (진행중)" if yr == years[-1] else (
            "  (일부)" if yr == years[0] and len(grp) < 200 else "")
        rows.append({
            "연도": f"{yr}{partial}",
            "수익률(%)": round((grp.iloc[-1] / grp.iloc[0] - 1) * 100, 1),
            "연내 MDD(%)": round(((grp - grp.cummax()) / grp.cummax()).min() * 100, 1),
            "승률(%)": round((yt["손익"] > 0).mean() * 100, 1) if len(yt) else None,
            "매매": len(yt),
            f"존버({tk}) 수익률(%)": round((bh.iloc[-1] / bh.iloc[0] - 1) * 100, 1),
        })
    return pd.DataFrame(rows)


def show_result(r, h, bh, tk, put, label):
    """백테스트 결과 한 벌을 그린다 (지표 -> 연도별 -> 존버 비교)."""
    short = pd.isna(r.cagr_pct)
    m = st.columns(5)
    m[0].metric("최종 자산", f"${r.final_value:,.0f}")
    m[1].metric("총 수익률", f"{r.net_return_pct:+.1f}%")
    m[2].metric("연평균(CAGR)", "—" if short else f"{r.cagr_pct:.2f}%")
    m[3].metric("최대 낙폭(MDD)", f"{r.mdd_pct:.2f}%")
    m[4].metric("효율 (CAGR/MDD)",
                "—" if (short or not r.mdd_pct) else f"{r.cagr_pct / -r.mdd_pct:.2f}")
    st.caption(
        f"{label} · 매매 {r.num_trades}회 (익절 {r.num_target_sells} / 손절 {r.num_forced_sells})"
        f" · 승률 {r.win_rate_pct:.1f}% · 평균 주식비중 {r.avg_exposure_pct:.0f}%"
        + (f" · **매수 범위를 넘겨 건너뛴 날 {r.buy_range_skips}일**"
           if r.buy_range_skips else " · 매수 범위 때문에 건너뛴 날 없음")
    )

    ydf = year_table(r.twr_curve, h["Close"], r.trades, tk)
    c1, c2 = st.columns([1.05, 1])
    with c1:
        st.markdown("##### 연도별 성과")
        st.dataframe(ydf, width="stretch", hide_index=True,
                     height=min(400, 60 + 36 * max(len(ydf), 1)))
    with c2:
        st.markdown("##### 자산 추이 — 전략 vs 존버")
        st.line_chart(pd.DataFrame({"전략": r.equity_curve, f"존버({tk})": bh}), height=340)

    full = ydf[~ydf["연도"].astype(str).str.contains(r"\(")] if not ydf.empty else ydf
    if len(full) >= 2:
        st.info(
            f"**온전한 {len(full)}개년 중 {int((full['수익률(%)'] > 0).sum())}년 플러스** · "
            f"평균 {full['수익률(%)'].mean():.1f}% · 최고 {full['수익률(%)'].max():.1f}% / "
            f"최저 {full['수익률(%)'].min():.1f}% · 연내 낙폭 평균 "
            f"{full['연내 MDD(%)'].mean():.1f}% (최악 {full['연내 MDD(%)'].min():.1f}%) "
            f"— (일부)·(진행중) 표시된 해는 제외"
        )

    bhf = float(bh.iloc[-1])
    bhdd = float(((bh / bh.cummax()) - 1).min() * 100)
    d1, d2 = st.columns([1, 1.4])
    with d1:
        st.dataframe(pd.DataFrame([
            {"항목": "최종 자산", "전략": f"${r.final_value:,.0f}", "존버": f"${bhf:,.0f}"},
            {"항목": "순손익", "전략": f"${r.net_profit:+,.0f}", "존버": f"${bhf - put:+,.0f}"},
            {"항목": "수익률", "전략": f"{r.net_return_pct:+.1f}%",
             "존버": f"{(bhf / put - 1) * 100:+.1f}%" if put > 0 else "—"},
            {"항목": "최대 낙폭", "전략": f"{r.mdd_pct:.1f}%", "존버": f"{bhdd:.1f}%"},
            {"항목": "주식 비중", "전략": f"{r.avg_exposure_pct:.0f}%", "존버": "100%"},
        ]), width="stretch", hide_index=True, height=220)
    with d2:
        if bhf > r.final_value:
            st.warning(
                f"**이 기간엔 존버가 \\${bhf - r.final_value:,.0f} 더 벌었습니다.** "
                f"대신 존버는 한때 **{bhdd:.0f}%** 까지 빠졌고 이 전략은 **{r.mdd_pct:.0f}%** 였습니다.\n\n"
                f"이 전략은 평균 자산의 **{r.avg_exposure_pct:.0f}%만 주식**이고 나머지는 현금입니다. "
                f"상승장에서 존버에 지는 건 정상입니다. **낙폭 차이를 보고 판단하세요.**"
            )
        else:
            st.success(
                f"**이 기간엔 전략이 \\${r.final_value - bhf:,.0f} 더 벌었습니다.** "
                f"낙폭도 존버 {bhdd:.0f}% 대비 **{r.mdd_pct:.0f}%** 로 얕았습니다."
            )


with tab_year:
    bt1, bt2 = st.tabs(["🧪 조건 바꿔서 돌려보기", "📌 내 설정 그대로"])

    # ---------- 조건을 직접 바꿔서 ----------
    # 백테스트 설정은 내 설정과 완전히 별개로 들고 간다.
    # 위젯 기본값을 내 설정에 묶어두면 맨 위를 건드릴 때 여기 값도 따라 움직여서
    # '따로 돌려보는' 의미가 없어진다. 그래서 자체 딕셔너리에 저장한다.
    if "btcfg" not in st.session_state:
        st.session_state.btcfg = {
            "ticker": cfg["ticker"], "start": "2011-01-03",
            "end": date.today().isoformat(), "seed": 10000.0,
            "splits": 10, "tgt": 2.75, "stop": 10, "fee": 0.0, "reinvest": True,
            "range": cfg.get("buy_range_pct", 0.10) * 100,
        }
    bt = st.session_state.btcfg
    bt.setdefault("end", date.today().isoformat())   # 예전 세션 대비
    bt.setdefault("range", cfg.get("buy_range_pct", 0.10) * 100)

    with bt1:
        h1, h2 = st.columns([3, 1])
        with h1:
            st.caption(
                "**내 설정과 완전히 따로 노는 화면입니다.** 여기서 뭘 바꾸든 맨 위 내 설정과 "
                "'오늘 할 일'은 그대로입니다. 값을 바꾸면 바로 다시 계산됩니다."
            )
        with h2:
            if st.button("📥 내 설정 복사해오기", width="stretch"):
                st.session_state.btcfg = {
                    "ticker": ticker, "start": start_d.isoformat(),
                    "end": date.today().isoformat(), "seed": float(seed),
                    "splits": int(splits), "tgt": float(tgt_pct), "stop": int(stop_days),
                    "fee": float(fee_pct), "reinvest": bool(reinvest),
                    "range": cfg.get("buy_range_pct", 0.10) * 100,
                }
                st.rerun()

        e1, e2, e3 = st.columns([1, 1, 1])
        with e1:
            bt["ticker"] = st.text_input("종목", value=bt["ticker"], key="bt_tk").strip().upper()
        with e2:
            _b = st.date_input(
                "시작일", value=pd.Timestamp(bt["start"]).date(),
                min_value=date(2010, 3, 11), max_value=date.today() - timedelta(days=1),
                key="bt_st",
            )
            bt["start"] = _b.isoformat()
        with e3:
            _e = st.date_input(
                "종료일", value=pd.Timestamp(bt["end"]).date(),
                min_value=date(2010, 3, 12), max_value=date.today(), key="bt_ed",
                help="과거 특정 구간만 잘라서 보고 싶을 때 씁니다. 기본값은 오늘입니다.",
            )
            bt["end"] = _e.isoformat()

        e4, e5, e6, e7, e8, e9 = st.columns(6)
        with e4:
            bt["seed"] = st.number_input("시드 ($)", 100.0, value=float(bt["seed"]),
                                         step=1000.0, key="bt_sd")
        with e5:
            bt["splits"] = st.number_input("분할수", 2, 60, int(bt["splits"]), key="bt_sp")
        with e6:
            bt["tgt"] = st.number_input("목표수익률 (%)", 0.5, 20.0, float(bt["tgt"]),
                                        0.05, key="bt_tg")
        with e7:
            bt["stop"] = st.number_input("청산 영업일", 2, 60, int(bt["stop"]), key="bt_sv")
        with e8:
            bt["fee"] = st.number_input("수수료 (%)", 0.0, 1.0, float(bt["fee"]), 0.001,
                                        format="%.4f", key="bt_fe")
        with e9:
            bt["range"] = st.number_input(
                "매수 범위 (%)", 3.0, 30.0, float(bt["range"]), 1.0, key="bt_rg",
                help="팔 물량이 없는 날, 어제 종가보다 이만큼 넘게 오르면 사지 않습니다.",
            )

        bt["reinvest"] = st.radio(
            "수익 재투자", [True, False], index=0 if bt["reinvest"] else 1,
            format_func=lambda v: "⭕ 함 (복리)" if v else "❌ 안 함 (고정)",
            horizontal=True, key="bt_re",
        )

        if pd.Timestamp(bt["end"]) <= pd.Timestamp(bt["start"]):
            st.error("**종료일이 시작일보다 빠릅니다.** 날짜를 다시 잡아주세요.")
            st.stop()

        # 내 설정과 어디가 다른지 한눈에
        diffs = []
        if bt["ticker"] != ticker:
            diffs.append(f"종목 {ticker} → {bt['ticker']}")
        if bt["start"] != start_d.isoformat():
            diffs.append(f"시작일 {start_d} → {bt['start']}")
        if bt["end"] != date.today().isoformat():
            diffs.append(f"종료일 오늘 → {bt['end']}")
        if abs(bt["seed"] - seed) > 1e-9:
            diffs.append(f"시드 ${seed:,.0f} → ${bt['seed']:,.0f}")
        if int(bt["splits"]) != int(splits):
            diffs.append(f"{splits}분할 → {int(bt['splits'])}분할")
        if abs(bt["tgt"] - tgt_pct) > 1e-9:
            diffs.append(f"목표 {tgt_pct:.2f}% → {bt['tgt']:.2f}%")
        if int(bt["stop"]) != int(stop_days):
            diffs.append(f"청산 {stop_days}일 → {int(bt['stop'])}일")
        if bool(bt["reinvest"]) != bool(reinvest):
            diffs.append(f"재투자 {'O' if reinvest else 'X'} → {'O' if bt['reinvest'] else 'X'}")
        if abs(bt["range"] / 100 - cfg.get("buy_range_pct", 0.10)) > 1e-9:
            diffs.append(f"매수 범위 {cfg.get('buy_range_pct', 0.10)*100:.0f}% → {bt['range']:.0f}%")

        if diffs:
            st.caption("**내 설정과 다른 점** — " + " · ".join(diffs))
        else:
            st.caption("지금은 내 설정과 같은 조건입니다. 값을 바꿔가며 비교해보세요.")

        try:
            with st.spinner("돌리는 중..."):
                br, bh_, bbh = simulate(
                    bt["ticker"], bt["start"], bt["end"], float(bt["seed"]),
                    1 / int(bt["splits"]), bt["tgt"] / 100, int(bt["stop"]), bt["fee"] / 100,
                    cfg.get("fee_in_target", True), cfg.get("whole_shares", True),
                    cfg.get("sell_day_buy_mode", "never"), bool(bt["reinvest"]), (),
                    bt["range"] / 100,
                )
        except Exception as ex:
            st.error(f"백테스트 실패: {ex}  — 종목코드와 기간을 확인하세요.")
        else:
            st.divider()
            show_result(
                br, bh_, bbh, bt["ticker"], float(bt["seed"]),
                f"**{bt['ticker']}** {bt['start']} ~ {bh_.index[-1].date()} · "
                f"시드 \\${bt['seed']:,.0f} · {int(bt['splits'])}분할 · 목표 {bt['tgt']:.2f}% · "
                f"청산 {int(bt['stop'])}일 · {'복리' if bt['reinvest'] else '고정'}",
            )

    # ---------- 내 설정 그대로 ----------
    with bt2:
      if not ready:
        st.info(
            "**📅 오늘 할 일** 탭의 설정으로는 아직 돌릴 기간이 없습니다. "
            "시작일을 조금 더 앞으로 잡아보세요. (왼쪽 '조건 바꿔서 돌려보기'는 그대로 됩니다)"
        )
      else:
        st.caption(
            f"**📅 오늘 할 일** 탭의 설정 그대로입니다 — **{ticker}** · 시작 {start_d} · 시드 \\${seed:,.0f} · "
            f"{splits}분할 · 목표 {tgt_pct:.2f}% · 청산 {stop_days}영업일 · "
            f"{'복리' if reinvest else '재투자 안 함'}"
        )
        show_result(res, hist, bh_curve, ticker, put_in,
                    f"{start_d} ~ {price_date} · 거래일 {len(log)}일")
        if has_flows:
            st.caption("연도별 수치는 입출금 효과를 뺀 값이고, 그래프는 실제 자산 금액입니다.")

# ============================================================ 규칙 · 설정
with tab_help:
    h1, h2 = st.columns([1, 1])

    with h1:
        st.markdown("## 📖 종사종팔 V5 — 처음이면 이것만 읽으세요")
        st.markdown(RULES_MD.format(
            tgt=f"{tgt_pct:.2f}", stop=int(stop_days), splits=int(splits),
            pct=f"{daily_pct*100:.1f}", ticker=ticker,
            rng=f"{cfg.get('buy_range_pct', 0.10)*100:.0f}",
        ))
        st.markdown("### ❓ LOC / MOC가 뭔가요")
        st.markdown(
            """
둘 다 **장 마감 종가에 체결**되는 주문입니다. 장중에 신경 쓸 필요가 없습니다.

| | 뜻 | 언제 체결되나 | 여기서 쓰는 곳 |
|---|---|---|---|
| **MOC** | 종가 시장가 | **무조건 체결** | 손절 매도, 팔 게 없는 날의 매수 |
| **LOC** | 종가 지정가 | 조건 맞을 때만 | 목표 매도, 매도 가능성 있는 날의 매수 |

**LOC 방향이 헷갈리면**
- **LOC 매도**: 종가가 지정가 **이상**이면 체결 (비싸게 팔고 싶으니까)
- **LOC 매수**: 종가가 지정가 **이하**면 체결 (싸게 사고 싶으니까)

이 전략은 모든 매매를 종가로 가정해 검증했습니다. LOC/MOC를 쓰면 **백테스트와 실제 체결이 어긋나지 않습니다.**

증권사 앱에 'LOC'/'MOC' 또는 '종가지정가'/'종가시장가'가 있는지 먼저 확인하세요. 없으면 이 전략은 실행이 어렵습니다.
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
        br = st.slider(
            "매수 범위 (%)", 3, 30, int(round(cfg.get("buy_range_pct", 0.10) * 100)),
            help="팔 물량이 없는 날 LOC 매수를 어제 종가보다 몇 % 위에 걸지. 그보다 더 오르면 안 삽니다.",
        )
        st.caption(f"어제 종가보다 **{br}% 넘게 오른 날은 사지 않습니다.** 카페에서는 +5~10%를 씁니다.")
        _k = round(br / 100, 2)
        if _k in BUY_RANGE_SKIPS_15Y:
            _sk, _vs = BUY_RANGE_SKIPS_15Y[_k], BUY_RANGE_VS_NOLIMIT[_k]
            st.caption(
                f"**SOXL 15년(3,919거래일) 백테스트** — 이 설정으로 건너뛴 날 **{_sk}일**, "
                f"제한을 아예 안 뒀을 때 대비 최종자산 **{_vs:+.1f}%**"
            )
        with st.expander("📊 매수 범위별 성적 (SOXL 2011~2026)"):
            st.dataframe(
                pd.DataFrame([
                    {"매수 범위": ("제한 없음" if k >= 0.20 and k != 0.20 else f"+{int(k*100)}%"),
                     "건너뛴 날": f"{BUY_RANGE_SKIPS_15Y[k]}일",
                     "제한없음 대비": f"{BUY_RANGE_VS_NOLIMIT[k]:+.1f}%"}
                    for k in (0.03, 0.05, 0.07, 0.10, 0.15, 0.20)
                ]), width="stretch", hide_index=True,
            )
            st.caption(
                "**+10%가 사실상 손해가 없습니다** (15년에 11일 건너뛰고 +0.2%). "
                "**+5% 이하로 조이면 불리해집니다** — 급등 직후 재진입을 자주 놓쳐서 "
                "15년 기준 −5.8%, 상승장(2023~)만 보면 −10.9%까지 벌어졌습니다. "
                "반대로 +20% 이상은 사실상 제한이 없는 것과 같습니다."
            )

        st.markdown("### ➕ 사다리 주문 (정액매수)")
        l1, l2 = st.columns(2)
        with l1:
            lr = st.number_input(
                "사다리 칸 수", 0, 10, int(cfg.get("ladder_rungs", 3)), 1,
                help="기본 매수 아래로 몇 개를 더 걸지. 0이면 안 씁니다.",
            )
        with l2:
            ls = st.number_input(
                "칸 간격 (%)", 0.5, 10.0, float(cfg.get("ladder_step", 0.03)) * 100, 0.5,
                help="한 칸마다 몇 %씩 내려갈지.",
            )
        if lr:
            st.caption(
                f"기준가에서 **−{lr * ls:.0f}%**까지 덮습니다. "
                "LOC는 수량을 미리 적어내는데 종가가 지정가보다 낮게 끝나면 예산이 남습니다. "
                "아래에 주문을 더 걸어 그만큼 채웁니다. **예산은 절대 안 넘깁니다.** "
                "계좌가 커져도 주문 개수는 그대로이고, 칸마다 담기는 수량이 늘어납니다."
            )
        else:
            st.caption("**안 씁니다.** 주문은 하루 하나로 단순해지지만 예산의 4% 정도가 남습니다.")

        moc = st.checkbox(
            "내 증권사에 **MOC(종가 시장가)**가 있다", cfg.get("moc_available", True),
            help="손절일 매도에만 씁니다. 없으면 LOC로 대신하는 방법을 알려줍니다.",
        )
        if not moc:
            st.success(
                "**LOC만 있어도 됩니다.** 매수는 원래 LOC라 문제없고, "
                "**손절일 매도**만 LOC로 대신합니다 (종가보다 30% 아래 지정가 = 사실상 무조건 체결).\n\n"
                "**LOC는 지정가가 아니라 종가에 체결**되므로 싸게 파는 게 아닙니다. "
                "지정가는 '체결 여부'만 정합니다."
            )
        s1, s2 = st.columns(2)
        with s1:
            fit = st.checkbox("목표가에 수수료 반영", cfg.get("fee_in_target", True),
                              help="수수료를 내고도 목표%가 남도록 목표가를 올립니다.")
        with s2:
            ws = st.checkbox("정수주만 매수", cfg.get("whole_shares", True),
                             help="소수점 주식을 못 사는 증권사면 켜세요.")
        if st.button("세부 설정 저장", width="stretch"):
            cfg.update({
                "sell_day_buy_mode": m, "fee_in_target": bool(fit),
                "whole_shares": bool(ws), "moc_available": bool(moc),
                "buy_range_pct": br / 100,
                "ladder_rungs": int(lr), "ladder_step": ls / 100,
            })
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

# ============================================================ 알림
with tab_notify:
    st.markdown("### 🔔 매일 텔레그램으로 받기")
    st.caption(
        "매 평일 정해진 시각에 **어젯밤 마감 결과 + 오늘 넣을 주문**을 한 통으로 보냅니다. "
        "손절일이 3영업일 안으로 다가온 물량도 미리 알려줍니다 — 목표 매도는 주문만 걸어두면 "
        "알아서 체결되지만, 손절은 날짜를 직접 세야 해서 제일 놓치기 쉽습니다."
    )

if is_shared_server():
    with tab_notify:
        st.info(
            "**여기는 여러 사람이 함께 쓰는 서버라 알림을 설정할 수 없습니다.** "
            "봇 토큰을 저장하면 다른 접속자와 섞이고, 예약도 이 서버에서는 걸리지 않습니다.\n\n"
            "알림은 **내 PC에서 켠 앱**에서 설정하세요."
        )
else:
    with tab_notify:
        # 계산이 실패해도(ready=False) 이 탭은 열어둔다. 봇 연결은 시세와
        # 무관하고, 메시지 만들기는 아래에서 따로 예외를 잡는다.
        # 저장된 값이 있으면 채워서 보여준다. 없어도 에러는 아니다.
        try:
            saved_token, saved_chat = load_telegram_config()
        except (FileNotFoundError, ValueError):
            saved_token, saved_chat = "", ""
        nt = load_jongsa_notify_config()

        # ---------------------------------------------- 1단계: 봇 연결
        st.markdown("#### 1단계 — 봇 연결")
        with st.expander("봇을 아직 안 만들었다면 (5분)"):
            st.markdown(
                "1. 텔레그램에서 **@BotFather** 를 찾습니다 (파란 체크 ✔ 붙은 계정).\n"
                "2. `/newbot` 을 보냅니다.\n"
                "3. 봇 이름은 아무거나. 아이디는 **끝이 `bot`** 이어야 합니다.\n"
                "4. `Use this token to access the HTTP API:` 아래 긴 문자열이 **봇 토큰**입니다.\n"
                "5. **만든 봇에게 아무 말이나 한 번 보내세요.** "
                "봇은 먼저 말을 건 사람에게만 보낼 수 있습니다."
            )

        token = st.text_input(
            "봇 토큰", value=saved_token, type="password",
            help="BotFather가 준 긴 문자열. 비밀번호와 같으니 남에게 보여주지 마세요.",
        )

        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("내 chat_id 찾기", width="stretch", disabled=not token):
                try:
                    st.session_state.found_chat = find_chat_id(token)
                except (RuntimeError, OSError) as e:
                    st.session_state.found_chat = ""
                    st.error(f"찾지 못했습니다 — {e}")
        with c2:
            chat_id = st.text_input(
                "chat_id", value=st.session_state.get("found_chat", saved_chat),
                help="받을 사람 번호. 위 버튼으로 자동으로 채울 수 있습니다.",
            )

        if st.button("💾 봇 정보 저장", type="primary", width="stretch",
                     disabled=not (token and chat_id)):
            save_telegram_config(token.strip(), chat_id.strip())
            st.success("저장했습니다.")

        if saved_token and saved_chat:
            st.caption(f"저장돼 있습니다 — chat_id `{saved_chat}`")
        else:
            st.caption("아직 저장된 봇 정보가 없습니다.")

        st.divider()

        # ---------------------------------------------- 2단계: 확인
        st.markdown("#### 2단계 — 내용 확인하고 보내보기")
        st.caption(
            "**'오늘 할 일' 탭에 저장된 설정 그대로** 계산합니다. "
            f"지금은 {cfg['ticker']} · 시드 \\${cfg['initial_cash']:,.0f} · "
            f"{round(1 / cfg['daily_buy_pct'])}분할 · 시작일 {cfg['start_date']} 입니다."
        )

        p1, p2 = st.columns(2)
        with p1:
            if st.button("👀 메시지 미리보기", width="stretch"):
                try:
                    st.session_state.preview_msg = build_message()
                except (ValueError, RuntimeError) as e:
                    st.session_state.preview_msg = ""
                    st.error(f"메시지를 만들지 못했습니다 — {e}")
        with p2:
            if st.button("📨 지금 한 통 보내기", width="stretch",
                         disabled=not (saved_token and saved_chat)):
                try:
                    st.session_state.preview_msg = send_now()
                    st.success("보냈습니다. 텔레그램을 확인해보세요.")
                except (FileNotFoundError, ValueError, RuntimeError, OSError) as e:
                    st.error(f"보내지 못했습니다 — {e}")

        if st.session_state.get("preview_msg"):
            st.code(st.session_state.preview_msg, language=None)

        st.divider()

        # ---------------------------------------------- 3단계: 예약
        st.markdown("#### 3단계 — 매일 자동으로 받기")

        task = get_jongsa_task_status()
        if task["exists"]:
            st.success(f"**예약이 켜져 있습니다** — 매 평일 {nt['time']} 에 보냅니다.")
        else:
            st.warning("아직 예약이 꺼져 있습니다. 아래에서 켜세요.")

        t1, t2 = st.columns([1, 2])
        with t1:
            hh, mm = (nt["time"].split(":") + ["0"])[:2]
            send_at = st.time_input("보낼 시각", value=dtime(int(hh), int(mm)), step=1800)
        with t2:
            app_url = st.text_input(
                "앱 주소 (선택)", value=nt.get("app_url", ""),
                help="넣으면 메시지 아래에 링크로 붙습니다. 휴대폰에서 바로 열 때 편합니다.",
            )

        r1, r2 = st.columns(2)
        with r1:
            if st.button("⏰ 자동 발송 켜기", type="primary", width="stretch",
                         disabled=not (saved_token and saved_chat)):
                hhmm = send_at.strftime("%H:%M")
                try:
                    save_jongsa_notify_config({"time": hhmm, "app_url": app_url.strip()})
                    register_jongsa_task(hhmm)
                    st.success(f"켰습니다 — 매 평일 {hhmm}.")
                    st.rerun()
                except (RuntimeError, OSError) as e:
                    st.error(f"예약을 걸지 못했습니다 — {e}")
        with r2:
            if st.button("예약 끄기", width="stretch", disabled=not task["exists"]):
                try:
                    remove_jongsa_task()
                    st.success("껐습니다.")
                    st.rerun()
                except (RuntimeError, OSError) as e:
                    st.error(f"해제하지 못했습니다 — {e}")

        st.caption(
            f"윈도우 작업 스케줄러에 **{JONGSA_TASK_NAME}** 이름으로 등록됩니다. "
            "**이 PC가 켜져 있고 로그인돼 있어야** 보내집니다. 주말에는 미국장이 안 열려 "
            "금요일과 같은 내용이 또 오므로 평일만 돌립니다.\n\n"
            "PC를 꺼놔도 받고 싶다면 GitHub에서 돌리는 방법이 **TELEGRAM.md**에 있습니다."
        )
        st.caption(
            "봇 토큰은 `telegram_config.json`, 알림 설정은 `jongsa_notify.json`에 "
            "저장되며 둘 다 깃허브에 올라가지 않습니다."
        )
