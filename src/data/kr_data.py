"""주식 시세 데이터 수집 (FinanceDataReader 사용, 실패하면 yfinance로 대체)."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# FinanceDataReader는 딸린 패키지가 많아서, 배포 환경에서 그중 하나만 어긋나도
# import 자체가 터진다. 그때 앱 전체가 죽지 않도록 감싸두고, 시세는 yfinance로
# 받아온다. (실제로 Streamlit Cloud에서 import 단계 오류로 앱이 멈춘 적이 있다)
try:
    import FinanceDataReader as fdr

    FDR_IMPORT_ERROR = None
except Exception as _e:  # ImportError뿐 아니라 내부 초기화 오류까지 잡는다
    fdr = None
    FDR_IMPORT_ERROR = _e

MAX_WORKERS = 10  # 동시에 받아올 종목 수 (너무 크면 차단될 수 있음)


def _yf_symbols(code: str) -> list[str]:
    """yfinance용 심볼 후보. 한국 6자리 코드는 .KS(코스피)/.KQ(코스닥) 둘 다 시도."""
    if code.isdigit() and len(code) == 6:
        return [f"{code}.KS", f"{code}.KQ"]
    return [code]


def _yf_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """yfinance로 일봉을 받아 FinanceDataReader와 같은 모양으로 맞춘다."""
    from src import ssl_fix

    ssl_fix.apply()  # 한글 경로 인증서 문제 (윈도우 로컬용, 리눅스에선 무해)
    import yfinance as yf

    last_err = None
    for sym in _yf_symbols(code):
        try:
            df = yf.Ticker(sym).history(start=start, end=end, auto_adjust=False)
        except Exception as e:
            last_err = e
            continue
        if df is not None and not df.empty:
            df = df.rename(columns={"Stock Splits": "Splits"})
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_localize(None)
            df.index.name = "Date"
            return df[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]]
    if last_err:
        raise last_err
    raise ValueError(f"{code} 시세를 받지 못했습니다.")


def _clip(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """요청한 기간 밖의 날짜를 잘라낸다.

    데이터 제공처가 시작일 앞뒤로 며칠을 더 얹어 주는 경우가 있다. 그대로 두면
    '내가 시작하지도 않은 날'에 매수가 잡혀 결과가 통째로 어긋난다.
    """
    if df is None or df.empty:
        return df
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
        df = df.copy()
        df.index = idx
    return df[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]


def get_kr_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """종목 하나의 일봉 OHLCV를 가져온다.

    FinanceDataReader를 먼저 쓰고, 못 쓰거나 빈 값이면 yfinance로 넘어간다.
    """
    if fdr is not None:
        try:
            df = _clip(fdr.DataReader(code, start, end), start, end)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass  # 아래 yfinance로 재시도
    return _clip(_yf_ohlcv(code, start, end), start, end)


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
            return code, get_kr_ohlcv(code, start, end)
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
