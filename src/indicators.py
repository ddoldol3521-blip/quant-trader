"""지표 계산 순수 함수 모음. 전략 모듈과 차트가 공유한다."""

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """단순이동평균."""
    return series.rolling(period).mean()


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """볼린저밴드 (중심선, 상단, 하단)를 반환한다."""
    ma = series.rolling(period).mean()
    std = series.rolling(period).std()
    return ma, ma + std * num_std, ma - std * num_std


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI(상대강도지수). 0~100 범위, 낮으면 과매도 / 높으면 과매수."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD선과 시그널선을 반환한다."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3):
    """스토캐스틱 %K, %D를 반환한다."""
    lowest = low.rolling(k_period).min()
    highest = high.rolling(k_period).max()
    percent_k = (close - lowest) / (highest - lowest) * 100
    percent_d = percent_k.rolling(d_period).mean()
    return percent_k, percent_d


def disparity(series: pd.Series, period: int = 20) -> pd.Series:
    """이격도 = 종가 / 이동평균 * 100. 100이면 평균과 같은 수준."""
    return series / series.rolling(period).mean() * 100
