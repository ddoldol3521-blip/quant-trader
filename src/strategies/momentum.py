import pandas as pd

DEFAULT_PARAMS = {"lookback": 20}
DESCRIPTION = "최근 lookback일간 수익률이 양수면 매수, 음수면 매도 (추세추종)"
PARAM_GRID = {"lookback": [5, 10, 20, 40, 60]}
TYPE_LABEL = "오르는거 따라사기"
BUY_CONDITION = "최근 20일 동안 가격이 올랐을 때 (수익률이 플러스로 바뀔 때)"
SELL_CONDITION = "최근 20일 동안 가격이 내렸을 때 (수익률이 마이너스로 바뀔 때)"


def generate_signals(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """단순 모멘텀 전략: N일 전 대비 상승 중이면 보유, 하락 중이면 미보유."""
    result = df.copy()
    result["momentum"] = result["Close"].pct_change(lookback)
    result["position"] = (result["momentum"] > 0).astype(int)
    return result
