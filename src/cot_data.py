"""CFTC COT(Commitment of Traders) — 미국 선물시장의 투자자 유형별 포지션.

미국 상품선물거래위원회(CFTC)가 매주 공개하는 공식 데이터다. 누가 얼마나
롱(매수)/숏(매도)을 들고 있는지를 투자자 유형별로 보여준다.

주의 두 가지:
1. 실시간이 아니다. 매주 화요일 기준으로 집계해서 금요일에 발표하므로 3일 지연된다.
2. 한국은 이런 데이터를 무료로 제공하지 않는다 (KRX가 로그인을 요구). 그래서 미국 전용이다.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime

import pandas as pd

API_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
TIMEOUT = 30

# 화면에 보여줄 이름 -> CFTC가 쓰는 정확한 시장명
COT_MARKETS = {
    "S&P500 선물": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "나스닥100 선물": "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",
    "러셀2000 선물": "RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE",
    "VIX 선물": "VIX FUTURES - CBOE FUTURES EXCHANGE",
}

# COT 시장 -> 그 선물의 실제 가격을 볼 수 있는 yfinance 티커
COT_PRICE_TICKERS = {
    "S&P500 선물": "ES=F",
    "나스닥100 선물": "NQ=F",
    "러셀2000 선물": "RTY=F",
    "VIX 선물": "^VIX",
}

# 투자자 유형 설명 (쉬운 말로)
TRADER_TYPES = {
    "투기세력": "헤지펀드·자산운용사 등. 방향에 베팅해서 돈 벌려는 쪽이라 '스마트머니'로 불리기도 한다.",
    "헤저": "실제 사업·자산을 보유해서 위험을 줄이려는 쪽 (기업, 딜러 등). 방향 베팅이 목적이 아니다.",
    "소액투자자": "보고 의무가 없는 작은 참가자들.",
}


def _fetch(where: str, limit: int = 200) -> list:
    params = {
        "$where": where,
        "$limit": str(limit),
        "$order": "report_date_as_yyyy_mm_dd DESC",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def _to_int(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def get_cot_history(market_label: str, weeks: int = 104) -> pd.DataFrame:
    """한 시장의 COT 시계열을 가져온다.

    반환 컬럼: 날짜, 투기_롱, 투기_숏, 투기_순, 헤저_롱, 헤저_숏, 헤저_순, 미결제약정
    '순'은 롱에서 숏을 뺀 값 — 플러스면 상승 쪽, 마이너스면 하락 쪽에 기울어져 있다는 뜻.
    """
    market_name = COT_MARKETS.get(market_label, market_label)
    where = f"market_and_exchange_names = '{market_name}'"
    rows = _fetch(where, limit=weeks)
    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        nl = _to_int(r, "noncomm_positions_long_all")
        ns = _to_int(r, "noncomm_positions_short_all")
        cl = _to_int(r, "comm_positions_long_all")
        cs = _to_int(r, "comm_positions_short_all")
        records.append(
            {
                "날짜": pd.to_datetime(r["report_date_as_yyyy_mm_dd"][:10]),
                "투기_롱": nl,
                "투기_숏": ns,
                "투기_순": nl - ns,
                "헤저_롱": cl,
                "헤저_숏": cs,
                "헤저_순": cl - cs,
                "미결제약정": _to_int(r, "open_interest_all"),
            }
        )

    df = pd.DataFrame(records).sort_values("날짜").reset_index(drop=True)
    return df


def summarize_latest(df: pd.DataFrame) -> dict:
    """가장 최근 주의 상태를 사람이 읽을 수 있게 정리한다."""
    if df.empty:
        return {}

    latest = df.iloc[-1]
    net = int(latest["투기_순"])
    total = int(latest["투기_롱"]) + int(latest["투기_숏"])
    tilt_pct = (net / total * 100) if total else 0

    # 최근 2년 범위에서 지금이 어느 수준인지 (백분위)
    percentile = float((df["투기_순"] <= net).mean() * 100)

    if tilt_pct > 15:
        stance = "롱 우위 (상승 쪽에 강하게 베팅)"
    elif tilt_pct > 3:
        stance = "약한 롱 우위"
    elif tilt_pct > -3:
        stance = "중립"
    elif tilt_pct > -15:
        stance = "약한 숏 우위"
    else:
        stance = "숏 우위 (하락 쪽에 강하게 베팅)"

    if percentile >= 90:
        extreme = "2년래 최고 수준으로 롱에 쏠려 있음 (과열/역발상 주의 구간)"
    elif percentile <= 10:
        extreme = "2년래 최저 수준으로 숏에 쏠려 있음 (공포/역발상 주의 구간)"
    else:
        extreme = "극단적인 쏠림은 아님"

    prev_net = int(df.iloc[-2]["투기_순"]) if len(df) >= 2 else net

    return {
        "기준일": latest["날짜"].strftime("%Y-%m-%d"),
        "투기_순포지션": net,
        "전주대비": net - prev_net,
        "롱비중(%)": round(tilt_pct, 1),
        "2년내_백분위": round(percentile, 0),
        "방향": stance,
        "쏠림": extreme,
        "헤저_순포지션": int(latest["헤저_순"]),
        "미결제약정": int(latest["미결제약정"]),
    }


def get_price_with_cot(market_label: str, weeks: int = 104) -> pd.DataFrame:
    """COT 포지션에 그 선물의 실제 가격을 붙여서 돌려준다.

    포지션 숫자만 봐서는 해석이 안 된다. '큰손이 롱을 늘리는 동안 가격은 어땠나'를
    같이 봐야 의미가 생긴다. COT는 주간이라 가격도 주간(금요일 종가)으로 맞춘다.
    """
    from src import ssl_fix

    ssl_fix.apply()
    import yfinance as yf

    cot = get_cot_history(market_label, weeks)
    if cot.empty:
        return cot

    ticker = COT_PRICE_TICKERS.get(market_label)
    if not ticker:
        return cot

    # COT 기간을 덮을 만큼 넉넉히 받아온다
    span_days = (datetime.now() - cot["날짜"].min().to_pydatetime()).days + 30
    period = "10y" if span_days > 1825 else ("5y" if span_days > 730 else "2y")

    try:
        hist = yf.Ticker(ticker).history(period=period)
    except Exception:
        return cot
    if hist.empty:
        return cot

    price = hist["Close"].dropna()
    price.index = price.index.tz_localize(None)

    # COT 보고일(화요일) 시점의 가장 가까운 이전 종가를 붙인다
    merged = pd.merge_asof(
        cot.sort_values("날짜"),
        price.rename("가격").reset_index().rename(columns={price.index.name or "Date": "날짜"}).sort_values("날짜"),
        on="날짜",
        direction="backward",
    )
    return merged


def get_all_markets_summary(weeks: int = 104) -> dict:
    """주요 시장 전부의 최신 요약을 한 번에 가져온다."""
    out = {}
    for label in COT_MARKETS:
        try:
            df = get_cot_history(label, weeks)
            out[label] = {"df": df, "summary": summarize_latest(df)}
        except Exception as e:
            out[label] = {"df": pd.DataFrame(), "summary": {}, "error": str(e)}
    return out
