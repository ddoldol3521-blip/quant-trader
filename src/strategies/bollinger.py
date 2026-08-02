import pandas as pd

from src.indicators import bollinger_bands

DEFAULT_PARAMS = {"period": 20, "num_std": 2.0}
DESCRIPTION = "종가가 볼린저밴드 하단 아래로 떨어지면 매수, 중심선(이동평균)까지 회복하면 매도 (평균회귀)"
PARAM_GRID = {"period": [10, 20, 30], "num_std": [1.5, 2.0, 2.5]}
TYPE_LABEL = "싸졌을때 줍기"
BUY_CONDITION = "가격이 볼린저밴드 아래쪽 선 밑으로 떨어질 때 (평소보다 많이 싸짐)"
SELL_CONDITION = "가격이 중간선(이동평균)까지 다시 올라올 때"


def generate_signals(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """볼린저밴드 하단 이탈 매수 / 중심선 회복 매도 전략."""
    result = df.copy()
    ma, upper, lower = bollinger_bands(result["Close"], period, num_std)
    result["ma"] = ma
    result["upper"] = upper
    result["lower"] = lower

    position = []
    holding = 0
    for close, low_band, mid in zip(result["Close"], result["lower"], result["ma"]):
        if pd.isna(low_band):
            holding = 0
        elif holding == 0 and close < low_band:
            holding = 1
        elif holding == 1 and close > mid:
            holding = 0
        position.append(holding)
    result["position"] = position
    return result
