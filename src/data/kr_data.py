"""주식 시세 데이터 수집 (FinanceDataReader 사용, 실패하면 yfinance로 대체)."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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
PRICE_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "jongsa_price_overrides.json"


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
    """요청한 기간 밖의 날짜를 잘라내고, 종가가 비어 있는 줄을 버린다.

    - 데이터 제공처가 시작일 앞뒤로 며칠을 더 얹어 주는 경우가 있다. 그대로 두면
      '내가 시작하지도 않은 날'에 매수가 잡혀 결과가 통째로 어긋난다.
    - 종가가 NaN인 줄이 섞여 들어오기도 한다. 그대로 계산에 넣으면 수량이
      nan/inf가 되어 엉뚱한 곳에서 터진다.
    """
    if df is None or len(df) == 0:
        return df
    df = df.copy()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    df = df[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    if "Close" in df.columns:
        close = pd.to_numeric(df["Close"], errors="coerce")
        df = df[close.notna() & (close > 0)]
    return df


def _apply_price_overrides(df: pd.DataFrame, code: str, start: str, end: str) -> pd.DataFrame:
    """공급처에서 빠진 확정 일봉을 사용자가 확인한 값으로 보완한다.

    일봉 공급처가 최신 거래일을 간혹 늦게 반영한다. 그 상태로 계산을 계속하면
    실제로 끝난 매도가 앱에서는 보유 중으로 남는다. 보정값은 별도 JSON에 두어
    소스코드 수정 없이 추가/삭제할 수 있게 한다.
    """
    if not PRICE_OVERRIDES_PATH.exists():
        return df
    try:
        raw = json.loads(PRICE_OVERRIDES_PATH.read_text(encoding="utf-8"))
        rows = raw.get(str(code).upper(), {})
    except Exception:
        return df

    out = pd.DataFrame() if df is None else df.copy()
    for day, values in rows.items():
        ts = pd.Timestamp(day)
        if not (pd.Timestamp(start) <= ts <= pd.Timestamp(end)):
            continue
        close = float(values["Close"])
        for col in ("Open", "High", "Low", "Close"):
            out.loc[ts, col] = float(values.get(col, close))
        out.loc[ts, "Volume"] = float(values.get("Volume", 0))
    return _clip(out.sort_index(), start, end)


def get_kr_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """종목 하나의 일봉 OHLCV를 가져온다.

    미국 종목은 FinanceDataReader가 값은 주면서 최신 일봉만 누락하는 경우가
    있으므로 yfinance도 함께 확인하고 두 결과를 합친다. 같은 날짜는 yfinance를
    우선하고, 마지막으로 사용자가 확인한 누락 일봉 보정값을 적용한다.
    """
    frames = []
    if fdr is not None:
        try:
            df = _clip(fdr.DataReader(code, start, end), start, end)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            pass

    is_korean_code = code.isdigit() and len(code) == 6
    # 한국 종목은 기존처럼 FDR 성공 시 추가 호출을 생략한다. 미국 종목은
    # stale-but-nonempty 응답을 잡기 위해 두 공급처를 모두 확인한다.
    if not frames or not is_korean_code:
        try:
            yf_df = _clip(_yf_ohlcv(code, start, end), start, end)
            if yf_df is not None and not yf_df.empty:
                frames.append(yf_df)
        except Exception:
            pass

    if not frames:
        raise ValueError(f"{code} 시세를 받지 못했습니다.")
    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return _apply_price_overrides(merged, code, start, end)


def get_dividends(code: str, start: str, end: str) -> pd.Series:
    """배당락일별 주당 배당금. 없거나 못 받아오면 빈 Series를 준다.

    시세(FinanceDataReader / yfinance auto_adjust=False)의 종가는 **배당이
    반영되지 않은 값**이다. 배당락일에 주가가 그만큼 떨어진 채로 들어온다는 뜻이라,
    받은 배당을 따로 더해줘야 실제 계좌와 맞는다. (확인: SOXL 2024-06-25
    배당락일 기준 FDR 종가 = yfinance 미조정 종가, 조정 종가와는 $0.90 차이)

    배당은 yfinance에서만 받을 수 있어서 FinanceDataReader 경로가 없다.
    못 받아도 시세 계산은 그대로 되어야 하므로 조용히 빈 값을 돌려준다.
    """
    empty = pd.Series(dtype="float64")
    try:
        from src import ssl_fix

        ssl_fix.apply()
        import yfinance as yf

        for sym in _yf_symbols(code):
            div = yf.Ticker(sym).dividends
            if div is None or len(div) == 0:
                continue
            idx = pd.to_datetime(div.index)
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_localize(None)
            div = pd.Series(div.to_numpy(dtype="float64"), index=idx.normalize())
            div = div[(div.index >= pd.Timestamp(start)) & (div.index <= pd.Timestamp(end))]
            return div[div > 0]
    except Exception:
        pass
    return empty


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
