from . import (
    bollinger,
    disparity,
    golden_cross,
    high_breakout,
    macd,
    momentum,
    rsi,
    stochastic,
    volatility_breakout,
)

STRATEGIES = {
    "golden_cross": golden_cross,
    "rsi": rsi,
    "bollinger": bollinger,
    "macd": macd,
    "momentum": momentum,
    "volatility_breakout": volatility_breakout,
    "high_breakout": high_breakout,
    "disparity": disparity,
    "stochastic": stochastic,
}
