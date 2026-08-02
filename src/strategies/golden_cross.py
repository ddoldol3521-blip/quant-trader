import pandas as pd

from src.indicators import sma

DEFAULT_PARAMS = {"short": 5, "long": 20}
DESCRIPTION = "단기 이동평균이 장기 이동평균 위에 있으면 매수 상태 유지, 아래면 매도 상태 (추세추종)"
PARAM_GRID = {"short": [5, 10, 15], "long": [20, 40, 60]}
TYPE_LABEL = "오르는거 따라사기"
BUY_CONDITION = "최근 짧은 기간(기본 5일) 평균 가격이 긴 기간(기본 20일) 평균 가격을 위로 뚫고 올라갈 때 (골든크로스)"
SELL_CONDITION = "반대로 짧은 기간 평균이 긴 기간 평균 아래로 다시 떨어질 때 (데드크로스)"


def generate_signals(df: pd.DataFrame, short: int = 5, long: int = 20) -> pd.DataFrame:
    """이동평균 골든크로스/데드크로스 기준으로 보유 여부(position)를 결정한다.

    단기 이동평균 > 장기 이동평균이면 position=1(보유), 아니면 0(미보유).
    이동평균을 계산할 데이터가 부족한 초반 구간은 자동으로 0(미보유)이 된다.
    """
    result = df.copy()
    result["ma_short"] = sma(result["Close"], short)
    result["ma_long"] = sma(result["Close"], long)
    result["position"] = (result["ma_short"] > result["ma_long"]).astype(int)
    return result
