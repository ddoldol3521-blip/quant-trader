import pandas as pd

from src.indicators import stochastic as calc_stochastic

DEFAULT_PARAMS = {"k_period": 14, "d_period": 3, "buy_th": 20, "sell_th": 80}
DESCRIPTION = "스토캐스틱 %K가 buy_th 밑으로 내려가면(과매도) 매수, sell_th 위로 올라가면(과매수) 매도 (평균회귀, RSI와 비슷하지만 계산식이 다름)"
PARAM_GRID = {"k_period": [9, 14, 21], "d_period": [3, 5], "buy_th": [15, 20, 25], "sell_th": [75, 80, 85]}
TYPE_LABEL = "싸졌을때 줍기"
BUY_CONDITION = "스토캐스틱 %K 지표가 20 밑으로 떨어질 때 (과매도 상태)"
SELL_CONDITION = "스토캐스틱 %K 지표가 80 위로 올라갈 때 (과매수 상태)"


def generate_signals(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3, buy_th: float = 20, sell_th: float = 80
) -> pd.DataFrame:
    """스토캐스틱 오실레이터 과매도/과매수 전략."""
    result = df.copy()
    percent_k, percent_d = calc_stochastic(result["High"], result["Low"], result["Close"], k_period, d_period)
    result["percent_k"] = percent_k
    result["percent_d"] = percent_d

    position = []
    holding = 0
    for k in percent_k:
        if pd.isna(k):
            holding = 0
        elif holding == 0 and k < buy_th:
            holding = 1
        elif holding == 1 and k > sell_th:
            holding = 0
        position.append(holding)
    result["position"] = position
    return result
