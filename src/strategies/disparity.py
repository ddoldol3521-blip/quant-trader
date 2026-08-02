import pandas as pd

from src.indicators import disparity as calc_disparity

DEFAULT_PARAMS = {"period": 20, "buy_th": 95, "sell_th": 102}
DESCRIPTION = "이격도(종가/이동평균*100)가 buy_th 밑으로 내려가면(평균보다 많이 빠짐) 매수, sell_th 위로 올라가면(평균 근처로 회복) 매도 (평균회귀)"
PARAM_GRID = {"period": [10, 20, 30], "buy_th": [90, 93, 95, 97], "sell_th": [100, 102, 105]}
TYPE_LABEL = "싸졌을때 줍기"
BUY_CONDITION = "가격이 평균보다 5% 넘게 떨어졌을 때 (이격도 95 밑)"
SELL_CONDITION = "가격이 평균 근처(이격도 102)까지 다시 회복했을 때"


def generate_signals(df: pd.DataFrame, period: int = 20, buy_th: float = 95, sell_th: float = 102) -> pd.DataFrame:
    """이격도 전략: 가격이 이동평균 대비 많이 빠지면 사서 평균 근처로 돌아오면 판다."""
    result = df.copy()
    disp = calc_disparity(result["Close"], period)
    result["disparity"] = disp

    position = []
    holding = 0
    for d in disp:
        if pd.isna(d):
            holding = 0
        elif holding == 0 and d < buy_th:
            holding = 1
        elif holding == 1 and d > sell_th:
            holding = 0
        position.append(holding)
    result["position"] = position
    return result
