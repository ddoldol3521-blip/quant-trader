"""한국 상장 종목 목록 조회."""

import FinanceDataReader as fdr
import pandas as pd


def get_universe(market: str = "KOSPI", limit: int | None = 100) -> pd.DataFrame:
    """시가총액 상위 순으로 종목 목록(Code, Name)을 가져온다.

    market: 'KOSPI', 'KOSDAQ', 'KRX'(전체)
    limit: 상위 몇 개까지 가져올지. None이면 전체(느릴 수 있음).

    주의: 이 목록은 '오늘 기준' 시가총액 순이라, 과거 백테스트에 쓰면 생존편향이 생긴다.
    (그때는 작았지만 지금 큰 회사가 포함되고, 그때 컸지만 지금 사라진 회사는 빠짐)
    """
    listing = fdr.StockListing(market)
    if "Marcap" in listing.columns:
        listing = listing.sort_values("Marcap", ascending=False)
    if limit:
        listing = listing.head(limit)
    return listing[["Code", "Name"]].reset_index(drop=True)


def get_full_listing(markets: list) -> pd.DataFrame:
    """여러 시장의 전체 종목 목록을 (코드, 종목명, 시장, 현재가, 등락률, 시가총액)까지 가져온다.

    종목코드를 몰라서 찾아볼 때 쓰는 용도라 limit 없이 전체를 반환한다.
    """
    frames = [fdr.StockListing(market) for market in markets]
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset="Code")

    cols = ["Code", "Name", "Market", "Close", "ChagesRatio", "Marcap"]
    cols = [c for c in cols if c in combined.columns]
    combined = combined[cols]

    if "Marcap" in combined.columns:
        combined = combined.sort_values("Marcap", ascending=False)

    return combined.reset_index(drop=True)
