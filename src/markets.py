"""한국/미국 시장을 하나의 인터페이스로 다루기 위한 디스패치 층.

시세 조회(fdr.DataReader)는 한국·미국이 동일하지만, 종목 목록을 가져오는 방법과
시장 이름·통화 표시가 달라서 여기서 갈라준다.
"""

import pandas as pd

from src.data.kr_universe import get_full_listing as _kr_full_listing
from src.data.kr_universe import get_universe as _kr_universe
from src.data.us_universe import get_us_full_listing as _us_full_listing
from src.data.us_universe import get_us_universe as _us_universe

KR = "한국"
US = "미국"
REGIONS = [KR, US]

REGION_INFO = {
    KR: {
        "sub_markets": ["KOSPI", "KOSDAQ"],
        "default_sub_markets": ["KOSPI", "KOSDAQ"],
        "currency": "원",
        "currency_symbol": "₩",
        "default_cash": 10_000_000,
        "cash_step": 1_000_000,
        "sample_code": "005930",
        "code_hint": "종목코드 (예: 005930)",
        "search_hint": "종목명 또는 코드 검색 (예: 삼성전자, 005930)",
        "universe_note": "시가총액 상위 순으로 가져옵니다.",
    },
    US: {
        "sub_markets": ["S&P500", "NASDAQ", "NYSE"],
        "default_sub_markets": ["S&P500"],
        "currency": "달러",
        "currency_symbol": "$",
        "default_cash": 10_000,
        "cash_step": 1_000,
        "sample_code": "AAPL",
        "code_hint": "티커 (예: AAPL, NVDA)",
        "search_hint": "회사명 또는 티커 검색 (예: Apple, AAPL)",
        "universe_note": "S&P500은 알파벳순, NASDAQ·NYSE는 규모가 큰 순서로 가져옵니다.",
    },
}

# 하위 시장별로 목록이 어떤 순서로 오는지 (limit으로 자를 때 무슨 일이 벌어지는지 알려주기 위함)
SUB_MARKET_ORDERING = {
    "KOSPI": ("marcap", "시가총액 큰 순"),
    "KOSDAQ": ("marcap", "시가총액 큰 순"),
    "NASDAQ": ("marcap", "시가총액 큰 순"),
    "NYSE": ("marcap", "시가총액 큰 순"),
    "S&P500": ("alpha", "알파벳순 (S&P500은 503개 전부가 미국 대표 대형주라 순위 개념이 없음)"),
}


def ordering_note(sub_markets: list) -> str:
    """선택한 시장들이 어떤 순서로 정렬되는지 한 줄 설명."""
    notes = []
    for m in sub_markets:
        kind, label = SUB_MARKET_ORDERING.get(m, ("marcap", "시가총액 큰 순"))
        notes.append(f"{m}: {label}")
    return " / ".join(notes)


def has_alpha_ordering(sub_markets: list) -> bool:
    """알파벳순으로 오는 시장이 섞여 있는지 (그러면 '상위 N개'가 규모 상위가 아님)."""
    return any(SUB_MARKET_ORDERING.get(m, ("marcap", ""))[0] == "alpha" for m in sub_markets)


def info(region: str) -> dict:
    """그 시장의 표시 설정(통화, 기본 시장 목록 등)을 돌려준다."""
    return REGION_INFO[region]


def get_universe(region: str, sub_market: str, limit: int | None = 100) -> pd.DataFrame:
    """한 시장의 종목 목록(Code, Name)을 가져온다."""
    if region == KR:
        return _kr_universe(sub_market, limit)
    return _us_universe(sub_market, limit)


def get_multi_universe(region: str, sub_markets: list, limit: int = 100) -> pd.DataFrame:
    """여러 하위 시장을 합쳐서 가져온다 (중복 종목 제거)."""
    frames = [get_universe(region, m, limit) for m in sub_markets]
    if not frames:
        return pd.DataFrame(columns=["Code", "Name"])
    combined = pd.concat(frames, ignore_index=True)
    return combined.drop_duplicates(subset="Code").reset_index(drop=True)


def get_full_listing(region: str, sub_markets: list) -> pd.DataFrame:
    """종목 검색용 전체 목록."""
    if region == KR:
        return _kr_full_listing(sub_markets)
    return _us_full_listing(sub_markets)
