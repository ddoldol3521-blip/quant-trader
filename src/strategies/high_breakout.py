import pandas as pd

DEFAULT_PARAMS = {"window": 252}
DESCRIPTION = "최근 window거래일(기본 약 1년) 최고가를 오늘 종가가 갱신하면 매수 상태 유지, 갱신 못하면 매도 (추세추종, 신고가 돌파)"
PARAM_GRID = {"window": [60, 120, 180, 252]}
TYPE_LABEL = "가장 안전"
BUY_CONDITION = "최근 1년 중 가장 높았던 가격을 오늘 종가가 넘어설 때 (신고가 경신)"
SELL_CONDITION = "오늘 종가가 최근 1년 최고가 밑으로 떨어질 때"


def generate_signals(df: pd.DataFrame, window: int = 252) -> pd.DataFrame:
    """N일 최고가 돌파 전략 (터틀 트레이딩 계열)."""
    result = df.copy()
    rolling_high = result["Close"].rolling(window).max().shift(1)
    result["rolling_high"] = rolling_high
    result["position"] = (result["Close"] >= rolling_high).astype(int)
    return result
