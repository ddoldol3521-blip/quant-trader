"""시장 상황판 — 지금 시장 분위기를 한눈에 보기 위한 거시 지표 모음.

주의: 이건 '예측'이 아니라 '현재 상태 참고자료'다. 학계 연구는 일관되게
"시장 타이밍을 맞추려는 시도는 대부분 실패한다"고 말한다. 아래 지표들은
"지금 분위기가 어떤가"를 보는 용도로만 쓰고, 이것만 보고 전량 매수/매도 하지 말 것.
"""

import time

import pandas as pd

from src import ssl_fix

ssl_fix.apply()  # yfinance import 전에 인증서 경로부터 잡아준다 (한글 경로 문제)

import yfinance as yf  # noqa: E402

RETRY_COUNT = 3
RETRY_WAIT = 1.5  # 초. 짧은 시간에 여러 번 호출하면 야후가 일시적으로 막는다


def _history_with_retry(ticker: str, period: str):
    """야후가 일시적으로 요청을 막는 경우가 있어 몇 번 재시도한다."""
    for attempt in range(RETRY_COUNT):
        try:
            hist = yf.Ticker(ticker).history(period=period)
            if not hist.empty:
                return hist
        except Exception:
            pass
        if attempt < RETRY_COUNT - 1:
            time.sleep(RETRY_WAIT)
    return None

# yfinance 티커 정의
TICKERS = {
    "VIX": "^VIX",
    "US10Y": "^TNX",  # 값/10 아님 — yfinance는 이미 % 단위로 준다 (예: 4.6 = 4.6%)
    "US3M": "^IRX",
    "USDKRW": "USDKRW=X",
    "SOX": "^SOX",
    "NASDAQ": "^IXIC",
    "SP500": "^GSPC",
    "KOSPI": "^KS11",
}

FRED_HY_SPREAD_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"


def _latest_and_change(ticker: str, period: str = "6mo"):
    """최근 종가와 전일 대비 변화를 가져온다. 실패하면 None."""
    hist = _history_with_retry(ticker, period)
    if hist is None:
        return None
    close = hist["Close"].dropna()
    if close.empty:
        return None
    latest = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else latest
    return {"value": latest, "prev": prev, "change": latest - prev, "history": close}


def get_vix() -> dict:
    """VIX(공포지수). 보통 20 아래면 평온, 30 위면 공포."""
    data = _latest_and_change(TICKERS["VIX"])
    if not data:
        return {"이름": "VIX (공포지수)", "값": None, "해석": "조회 실패"}

    v = data["value"]
    if v < 15:
        level = "매우 평온 (오히려 방심 주의)"
    elif v < 20:
        level = "평온"
    elif v < 30:
        level = "다소 불안"
    else:
        level = "공포 (급락장에서 주로 나옴)"

    return {
        "이름": "VIX (공포지수)",
        "값": round(v, 2),
        "전일대비": round(data["change"], 2),
        "해석": level,
        "history": data["history"],
    }


def get_yield_curve() -> dict:
    """미국 10년물 - 3개월물 금리차. 마이너스(역전)면 경기침체 경고로 유명하다."""
    ten = _latest_and_change(TICKERS["US10Y"])
    three = _latest_and_change(TICKERS["US3M"])
    if not ten or not three:
        return {"이름": "미국 장단기 금리차", "값": None, "해석": "조회 실패"}

    spread = ten["value"] - three["value"]
    if spread < 0:
        level = "역전 상태 (침체 경고 신호로 해석되지만, 실제 침체까지 몇 달~2년 걸린 적도 많음)"
    elif spread < 0.5:
        level = "거의 평평함 (역전에 가까움)"
    else:
        level = "정상 (장기금리가 단기금리보다 높음)"

    return {
        "이름": "미국 장단기 금리차 (10년 - 3개월)",
        "값": round(spread, 2),
        "10년물(%)": round(ten["value"], 2),
        "3개월물(%)": round(three["value"], 2),
        "해석": level,
    }


def get_hy_spread() -> dict:
    """하이일드(정크본드) 스프레드. 벌어지면 신용 위험 상승 신호."""
    try:
        df = pd.read_csv(FRED_HY_SPREAD_URL)
        df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna()
        if df.empty:
            return {"이름": "하이일드 스프레드", "값": None, "해석": "조회 실패"}

        latest = float(df["value"].iloc[-1])
        if latest < 3.5:
            level = "평온 (신용시장 안정)"
        elif latest < 5:
            level = "보통"
        elif latest < 6:
            level = "경계 (위험 신호가 쌓이는 중)"
        else:
            level = "위험 (과거 600bp 돌파 후 12~18개월 내 침체가 온 경우가 많았음)"

        return {
            "이름": "하이일드 스프레드 (신용 위험)",
            "값": round(latest, 2),
            "해석": level,
            "기준일": str(df["date"].iloc[-1]),
        }
    except Exception:
        return {"이름": "하이일드 스프레드", "값": None, "해석": "조회 실패"}


def get_usdkrw() -> dict:
    """원달러 환율. 오르면(원화 약세) 외국인 자금 이탈 압력."""
    data = _latest_and_change(TICKERS["USDKRW"])
    if not data:
        return {"이름": "원달러 환율", "값": None, "해석": "조회 실패"}

    v = data["value"]
    if v > 1400:
        level = "원화 약세 (외국인 매도 압력이 커질 수 있음)"
    elif v > 1300:
        level = "다소 약세"
    else:
        level = "안정권"

    return {
        "이름": "원달러 환율",
        "값": round(v, 1),
        "전일대비": round(data["change"], 1),
        "해석": level,
        "history": data["history"],
    }


def get_index_trend(key: str, label: str) -> dict:
    """지수가 200일 이동평균 위인지 아래인지 본다 (장기 추세 판단의 가장 단순한 기준)."""
    try:
        hist = _history_with_retry(TICKERS[key], "2y")
        if hist is None:
            return {"이름": label, "값": None, "해석": "조회 실패 (잠시 후 다시 시도해보세요)"}
        close = hist["Close"].dropna()
        ma200 = close.rolling(200).mean()
        latest = float(close.iloc[-1])
        latest_ma = float(ma200.iloc[-1]) if not pd.isna(ma200.iloc[-1]) else None

        if latest_ma is None:
            level = "200일선 계산에 데이터 부족"
            above = None
        else:
            above = latest > latest_ma
            gap = (latest / latest_ma - 1) * 100
            level = f"200일선 {'위' if above else '아래'} ({gap:+.1f}%) — {'장기 상승추세' if above else '장기 하락추세'}"

        return {
            "이름": label,
            "값": round(latest, 2),
            "200일선": round(latest_ma, 2) if latest_ma else None,
            "200일선위": above,
            "해석": level,
            "history": close,
        }
    except Exception:
        return {"이름": label, "값": None, "해석": "조회 실패"}


def get_put_call_ratio(symbol: str = "SPY") -> dict:
    """가장 가까운 만기(위클리 포함) 옵션의 풋/콜 비율.

    1보다 크면 풋(하락 베팅)이 콜보다 많이 거래된다는 뜻.
    주의: 과거 데이터를 구할 수 없어 백테스트가 불가능하다. 현재값 참고용.
    """
    try:
        ticker = yf.Ticker(symbol)
        expirations = None
        for attempt in range(RETRY_COUNT):
            expirations = ticker.options
            if expirations:
                break
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_WAIT)
        if not expirations:
            return {
                "종목": symbol,
                "오류": "옵션 데이터를 못 가져왔습니다. 미국 상장 종목이 맞는지 확인하거나 잠시 후 다시 시도해보세요.",
            }

        nearest = expirations[0]
        chain = ticker.option_chain(nearest)
        call_vol = float(chain.calls["volume"].fillna(0).sum())
        put_vol = float(chain.puts["volume"].fillna(0).sum())
        call_oi = float(chain.calls["openInterest"].fillna(0).sum())
        put_oi = float(chain.puts["openInterest"].fillna(0).sum())

        vol_ratio = put_vol / call_vol if call_vol else None
        oi_ratio = put_oi / call_oi if call_oi else None

        if vol_ratio is None:
            level = "거래량이 없어 판단 불가"
        elif vol_ratio > 1.2:
            level = "풋 쏠림 (다들 하락에 베팅 중 = 공포. 역발상으론 바닥 신호로 보기도 함)"
        elif vol_ratio > 0.9:
            level = "중립"
        elif vol_ratio > 0.6:
            level = "콜 우세 (상승 기대가 더 큼)"
        else:
            level = "콜 쏠림 (과열 주의. 다들 상승만 기대하는 상태)"

        return {
            "종목": symbol,
            "만기일": nearest,
            "콜 거래량": int(call_vol),
            "풋 거래량": int(put_vol),
            "풋콜비율(거래량)": round(vol_ratio, 3) if vol_ratio else None,
            "풋콜비율(미결제약정)": round(oi_ratio, 3) if oi_ratio else None,
            "해석": level,
        }
    except Exception as e:
        return {"종목": symbol, "오류": f"조회 실패: {e}"}


def get_dashboard() -> dict:
    """상황판 전체를 한 번에 가져온다."""
    return {
        "vix": get_vix(),
        "yield_curve": get_yield_curve(),
        "hy_spread": get_hy_spread(),
        "usdkrw": get_usdkrw(),
        "kospi": get_index_trend("KOSPI", "코스피"),
        "sox": get_index_trend("SOX", "필라델피아 반도체지수"),
        "nasdaq": get_index_trend("NASDAQ", "나스닥"),
        "put_call": get_put_call_ratio("SPY"),
    }
