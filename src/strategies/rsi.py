import pandas as pd

from src.indicators import rsi as calc_rsi

DEFAULT_PARAMS = {"period": 14, "buy_th": 30, "sell_th": 70}
DESCRIPTION = "RSI가 buy_th 밑으로 내려가면(과매도) 매수, sell_th 위로 올라가면(과매수) 매도 (평균회귀)"
PARAM_GRID = {"period": [7, 14, 21], "buy_th": [20, 25, 30, 35], "sell_th": [65, 70, 75, 80]}
TYPE_LABEL = "싸졌을때 줍기"
BUY_CONDITION = "RSI 지표가 30 밑으로 떨어질 때 (많이 떨어져서 '과매도' 상태)"
SELL_CONDITION = "RSI 지표가 70 위로 올라갈 때 (많이 올라서 '과매수' 상태)"


def generate_signals(df: pd.DataFrame, period: int = 14, buy_th: float = 30, sell_th: float = 70) -> pd.DataFrame:
    """RSI(상대강도지수) 과매도/과매수 구간을 이용한 평균회귀 전략.

    RSI < buy_th 상태가 되면 매수, 그 뒤 RSI > sell_th가 될 때까지 보유.
    """
    result = df.copy()
    result["rsi"] = calc_rsi(result["Close"], period)

    position = []
    holding = 0
    for rsi_val in result["rsi"]:
        if pd.isna(rsi_val):
            holding = 0
        elif holding == 0 and rsi_val < buy_th:
            holding = 1
        elif holding == 1 and rsi_val > sell_th:
            holding = 0
        position.append(holding)
    result["position"] = position
    return result
