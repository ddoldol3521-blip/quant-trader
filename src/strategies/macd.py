import pandas as pd

from src.indicators import macd as calc_macd

DEFAULT_PARAMS = {"fast": 12, "slow": 26, "signal": 9}
DESCRIPTION = "MACD선이 시그널선 위에 있으면 매수 상태 유지, 아래면 매도 상태 (추세추종)"
PARAM_GRID = {"fast": [8, 12, 16], "slow": [21, 26, 34], "signal": [7, 9, 12]}
TYPE_LABEL = "오르는거 따라사기"
BUY_CONDITION = "MACD선이 시그널선을 위로 뚫고 올라갈 때 (상승 흐름이 강해지는 신호)"
SELL_CONDITION = "MACD선이 시그널선 아래로 다시 떨어질 때"


def generate_signals(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD(이동평균수렴확산) 골든크로스/데드크로스 전략."""
    result = df.copy()
    macd_line, signal_line = calc_macd(result["Close"], fast, slow, signal)
    result["macd"] = macd_line
    result["macd_signal"] = signal_line
    result["position"] = (macd_line > signal_line).astype(int)
    return result
