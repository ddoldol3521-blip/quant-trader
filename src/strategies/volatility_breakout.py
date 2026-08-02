import pandas as pd

DEFAULT_PARAMS = {"k": 0.5}
DESCRIPTION = (
    "전일 변동폭(고가-저가)의 k배만큼 오늘 시가 위로 상승하면(변동성 돌파) 매수, "
    "그 다음 돌파가 없는 날 매도 (래리 윌리엄스 전략을 일봉 스윙 방식으로 단순화한 버전, "
    "원래는 당일 매수/당일 청산하는 단타 전략)"
)
PARAM_GRID = {"k": [0.3, 0.4, 0.5, 0.6, 0.7]}
TYPE_LABEL = "오르는거 따라사기"
BUY_CONDITION = "오늘 가격이 어제 하루 변동폭의 절반(k=0.5)만큼 시가보다 올랐을 때 (강하게 튀어오를 때)"
SELL_CONDITION = "그 다음 날, 같은 조건을 다시 채우지 못하면 바로 판다 (하루~며칠만 짧게 들고 있는 방식)"


def generate_signals(df: pd.DataFrame, k: float = 0.5) -> pd.DataFrame:
    """변동성 돌파: 오늘 고가가 (오늘 시가 + 전일 변동폭 * k)를 넘으면 그날 매수 상태."""
    result = df.copy()
    prev_range = (result["High"] - result["Low"]).shift(1)
    target = result["Open"] + prev_range * k
    result["target"] = target
    result["position"] = (result["High"] >= target).astype(int)
    return result
