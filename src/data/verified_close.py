"""Fail-closed closing prices for SOXL notification calculations only.

Issuer prices supplement missing closes, never NAV, intraday prices, or OHLC.
Verified quotes are retained in the encrypted outbox, not in the user's ledger.
"""
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

import pandas as pd
import requests

ISSUER_URL = "https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs"


class PriceDataError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def parse_issuer_close(page, expected_date):
    parser = _VisibleText()
    parser.feed(page)
    text = re.sub(r"\s+", " ", " ".join(parser.parts))
    # Bind the price to the dated Pricing & Performance section, not to another
    # ticker, the daily NAV, a performance table, or an undated quote elsewhere.
    matches = list(re.finditer(
        r"NAV and Market Price information as of (\d{2}/\d{2}/\d{4})\s*\.", text))
    if len(matches) != 1:
        raise PriceDataError("ISSUER_FORMAT_CHANGED")
    try:
        quote_date = datetime.strptime(matches[0][1], "%m/%d/%Y").date()
    except ValueError:
        raise PriceDataError("ISSUER_FORMAT_CHANGED") from None
    if quote_date != expected_date:
        raise PriceDataError("ISSUER_DATE_MISMATCH")
    section = text[matches[0].end():]
    section_match = re.search(
        r"SOXL Direxion Daily Semiconductor Bull 3X ETF(.*?)"
        r"SOXS Direxion Daily Semiconductor Bear 3X ETF", section)
    if not section_match:
        raise PriceDataError("ISSUER_FORMAT_CHANGED")
    prices = re.findall(r"Market Price Closing\s*\$\s*([\d,]+\.\d{2})\s+Market\b",
                        section_match[1])
    if len(prices) != 1:
        raise PriceDataError("ISSUER_FORMAT_CHANGED")
    close = float(prices[0].replace(",", ""))
    if not math.isfinite(close) or close <= 0:
        raise PriceDataError("ISSUER_INVALID_CLOSE")
    return {"symbol": "SOXL", "date": quote_date.isoformat(), "close": close,
            "source": ISSUER_URL, "verified_at": datetime.now(timezone.utc).isoformat()}


def fetch_issuer_close(expected_date):
    try:
        response = requests.get(ISSUER_URL, timeout=(10, 20))
        response.raise_for_status()
    except requests.RequestException:
        raise PriceDataError("ISSUER_UNAVAILABLE") from None
    return parse_issuer_close(response.text, expected_date)


def _supplement(hist, quotes, start, previous):
    out = hist.copy()
    for day, quote in quotes.items():
        try:
            stamp = pd.Timestamp(date.fromisoformat(day))
            close = float(quote["close"])
            valid = (quote["symbol"] == "SOXL" and quote["date"] == day
                     and quote["source"] == ISSUER_URL and math.isfinite(close) and close > 0)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise PriceDataError("INVALID_VERIFIED_CLOSE")
        if not pd.Timestamp(start) <= stamp <= pd.Timestamp(previous):
            continue
        if stamp in out.index and pd.notna(out.at[stamp, "Close"]):
            if abs(float(out.at[stamp, "Close"]) - close) > 0.005:
                raise PriceDataError("VERIFIED_CLOSE_CONFLICT")
        else:
            # No invented Open/High/Low/Volume: QuantMix's core uses Close only.
            out.loc[stamp, "Close"] = close
    return out.sort_index()


def load_notification_history(symbol, start, end, previous, cached_quotes=None,
                              *, fetch=None, issuer=None, sleep=time.sleep):
    from src.data.kr_data import get_kr_ohlcv
    import pandas_market_calendars as mcal

    if symbol != "SOXL":
        raise PriceDataError("UNSUPPORTED_PRICE_FALLBACK")
    fetch = fetch or get_kr_ohlcv
    issuer = issuer or fetch_issuer_close
    quotes = dict(cached_quotes or {})
    expected = mcal.get_calendar("NYSE").schedule(start, previous.isoformat()).index
    if len(expected) == 0 or expected[-1].date() != previous:
        raise PriceDataError("INVALID_EXPECTED_SESSION")
    error_code = "PRICE_HISTORY_INCOMPLETE"
    for attempt in range(3):
        if attempt:
            sleep((2, 5)[attempt - 1])
        frames = []
        # A fresh short-window request may update sooner than a cached long one.
        for query_start in (start, max(date.fromisoformat(start), previous - timedelta(days=7)).isoformat()):
            try:
                frame = fetch(symbol, query_start, end)
                if frame is not None and not frame.empty:
                    frame = frame.copy()
                    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
                    close = pd.to_numeric(frame["Close"], errors="coerce")
                    frame["Close"] = close
                    frame = frame[close.map(lambda value: math.isfinite(value) and value > 0)]
                    frames.append(frame)
            except Exception:
                continue
        hist = pd.concat(frames).sort_index(kind="stable") if frames else pd.DataFrame(columns=["Close"])
        hist = hist[~hist.index.duplicated(keep="last")]
        hist = _supplement(hist, quotes, start, previous)
        if pd.Timestamp(previous) not in hist.index:
            try:
                quote = issuer(previous)
                quotes[previous.isoformat()] = quote
                hist = _supplement(hist, quotes, start, previous)
            except PriceDataError as error:
                error_code = error.code
        # Never silently skip an older missing session while repairing the tail.
        hist = hist.loc[hist.index.isin(expected)]
        if expected.difference(hist.index).empty:
            return hist, quotes
    raise PriceDataError(error_code)
