"""미국 상장 종목 목록 조회.

한국과 달리 시가총액 컬럼이 제공되지 않지만, FinanceDataReader가 돌려주는 순서 자체가
시가총액 내림차순이라 앞에서부터 자르면 대형주 위주가 된다.
"""

import FinanceDataReader as fdr
import pandas as pd

# 화면에 보여줄 이름 -> FinanceDataReader가 쓰는 시장 코드
US_MARKETS = {
    "S&P500": "S&P500",
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
}


def _normalize(listing: pd.DataFrame) -> pd.DataFrame:
    """미국 목록의 컬럼명을 한국 쪽(Code/Name)과 똑같이 맞춘다."""
    df = listing.rename(columns={"Symbol": "Code"})
    cols = [c for c in ["Code", "Name", "Sector", "Industry"] if c in df.columns]
    return df[cols]


def get_us_universe(market: str = "S&P500", limit: int | None = 100) -> pd.DataFrame:
    """미국 종목 목록(Code, Name)을 가져온다. 앞에서부터 자르면 대형주 위주가 된다."""
    listing = fdr.StockListing(US_MARKETS.get(market, market))
    df = _normalize(listing)
    if limit:
        df = df.head(limit)
    return df[["Code", "Name"]].reset_index(drop=True)


def get_us_full_listing(markets: list) -> pd.DataFrame:
    """여러 미국 시장의 전체 종목 목록을 합쳐서 가져온다 (종목 검색용)."""
    frames = []
    for m in markets:
        listing = fdr.StockListing(US_MARKETS.get(m, m))
        df = _normalize(listing)
        df["Market"] = m
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset="Code")
    cols = [c for c in ["Code", "Name", "Market", "Sector", "Industry"] if c in combined.columns]
    return combined[cols].reset_index(drop=True)
