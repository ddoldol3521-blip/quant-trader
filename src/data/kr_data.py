"""한국 주식 시세 데이터 수집 (FinanceDataReader 사용)."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import FinanceDataReader as fdr
import pandas as pd

MAX_WORKERS = 10  # 동시에 받아올 종목 수 (너무 크면 차단될 수 있음)


def get_kr_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """종목 하나의 일봉 OHLCV를 가져온다."""
    df = fdr.DataReader(code, start, end)
    return df


def fetch_universe_data(universe: pd.DataFrame, start: str, end: str, show_progress: bool = True) -> dict:
    """여러 종목의 시세를 병렬로 받아온다. 반환: {종목코드: DataFrame}

    한 종목씩 순서대로 받으면 100종목에 몇 분씩 걸려서, 스레드로 동시에 받는다.
    """
    codes = list(universe["Code"])
    total = len(codes)
    result = {}
    done = 0

    def _fetch(code):
        try:
            return code, fdr.DataReader(code, start, end)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_fetch, c) for c in codes]
        for future in as_completed(futures):
            code, df = future.result()
            done += 1
            if show_progress:
                print(f"\r[{done}/{total}] 데이터 수신 중...", end="", flush=True)
            if df is not None and not df.empty:
                result[code] = df

    if show_progress:
        print("\r" + " " * 40 + "\r", end="", flush=True)
    return result


def resample_ohlcv(df: pd.DataFrame, timeframe: str = "D") -> pd.DataFrame:
    """일봉 데이터를 주봉/월봉으로 변환한다.

    timeframe: 'D'(일봉, 변환 없음) / 'W'(주봉) / 'M'(월봉)
    """
    if timeframe == "D":
        return df

    rule = {"W": "W-FRI", "M": "ME"}[timeframe]
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        agg["Volume"] = "sum"

    resampled = df.resample(rule).agg(agg).dropna(subset=["Close"])
    return resampled
