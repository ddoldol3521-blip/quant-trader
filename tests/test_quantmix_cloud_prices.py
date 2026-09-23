import copy
import unittest
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd

from src.data.verified_close import (ISSUER_URL, PriceDataError, fetch_issuer_close,
                                     load_notification_history, parse_issuer_close)


PAGE = """<script>Market Price Closing $ 999.99 Market</script>
<p>NAV and Market Price information as of 09/22/2026.</p>
SOXL Direxion Daily Semiconductor Bull 3X ETF
Net Asset Value (NAV) $152.11 Nav
<div>Market Price Closing</div><span>$</span><span>151.95</span> Market
SOXS Direxion Daily Semiconductor Bear 3X ETF
Market Price Closing $32.42 Market"""


def quote():
    return parse_issuer_close(PAGE, date(2026, 9, 22))


def history(days=("2026-09-18", "2026-09-21"), closes=(123.67, 141.93)):
    return pd.DataFrame({"Close": list(closes)}, index=pd.to_datetime(list(days)))


class VerifiedCloseTests(unittest.TestCase):
    def test_exact_dated_market_close_not_nav_or_soxs_or_script(self):
        item = quote()
        self.assertEqual(item["close"], 151.95)
        self.assertEqual(item["date"], "2026-09-22")
        self.assertEqual(item["symbol"], "SOXL")
        self.assertEqual(item["source"], ISSUER_URL)

    def test_stale_or_future_issuer_date_rejected(self):
        for day in (date(2026, 9, 21), date(2026, 9, 23)):
            with self.assertRaisesRegex(PriceDataError, "ISSUER_DATE_MISMATCH"):
                parse_issuer_close(PAGE, day)

    def test_ambiguous_missing_or_zero_market_close_rejected(self):
        for page in (PAGE.replace("151.95", "0.00"),
                     PAGE.replace("Market Price Closing", "NAV"),
                     PAGE + PAGE,
                     PAGE.replace("SOXL Direxion", "SOXS Direxion")):
            with self.assertRaises(PriceDataError):
                parse_issuer_close(page, date(2026, 9, 22))

    def test_network_errors_do_not_leak_request_urls(self):
        import requests
        with patch("src.data.verified_close.requests.get", side_effect=requests.Timeout("secret URL")):
            with self.assertRaisesRegex(PriceDataError, "^ISSUER_UNAVAILABLE$"):
                fetch_issuer_close(date(2026, 9, 22))

    def test_nan_last_close_repaired_without_inventing_ohlc(self):
        raw = history(("2026-09-18", "2026-09-21", "2026-09-22"), (123.67, 141.93, float("nan")))
        fetch, issuer = Mock(return_value=raw), Mock(return_value=quote())
        frame, cache = load_notification_history("SOXL", "2026-09-18", "2026-09-23",
            date(2026, 9, 22), fetch=fetch, issuer=issuer, sleep=Mock())
        self.assertEqual(frame.loc["2026-09-22", "Close"], 151.95)
        self.assertEqual(list(frame.columns), ["Close"])
        self.assertTrue(pd.isna(raw.loc["2026-09-22", "Close"]))
        self.assertEqual(cache["2026-09-22"]["close"], 151.95)
        issuer.assert_called_once()

    def test_valid_primary_needs_no_fallback_and_discards_future_day(self):
        raw = history(("2026-09-21", "2026-09-22", "2026-09-23"), (141.93, 151.95, 999.0))
        issuer = Mock()
        frame, _ = load_notification_history("SOXL", "2026-09-21", "2026-09-23",
            date(2026, 9, 22), fetch=Mock(return_value=raw), issuer=issuer)
        issuer.assert_not_called()
        self.assertEqual(frame.index[-1].date(), date(2026, 9, 22))

    def test_retry_recovers_and_is_bounded(self):
        raw = history(("2026-09-21",), (141.93,))
        issuer = Mock(side_effect=[PriceDataError("ISSUER_UNAVAILABLE"),
                                  PriceDataError("ISSUER_UNAVAILABLE"), quote()])
        sleep = Mock()
        frame, _ = load_notification_history("SOXL", "2026-09-21", "2026-09-23",
            date(2026, 9, 22), fetch=Mock(return_value=raw), issuer=issuer, sleep=sleep)
        self.assertEqual(frame.iloc[-1]["Close"], 151.95)
        self.assertEqual(issuer.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5])

    def test_issuer_still_stale_after_three_attempts_fails_closed(self):
        issuer = Mock(side_effect=PriceDataError("ISSUER_DATE_MISMATCH"))
        with self.assertRaisesRegex(PriceDataError, "ISSUER_DATE_MISMATCH"):
            load_notification_history("SOXL", "2026-09-18", "2026-09-23",
                date(2026, 9, 22), fetch=Mock(return_value=history()), issuer=issuer, sleep=Mock())
        self.assertEqual(issuer.call_count, 3)

    def test_verified_missing_close_survives_next_days(self):
        raw = history(("2026-09-21", "2026-09-23"), (141.93, 150.0))
        saved = {"2026-09-22": quote()}
        original = copy.deepcopy(saved)
        issuer = Mock()
        frame, _ = load_notification_history("SOXL", "2026-09-21", "2026-09-24",
            date(2026, 9, 23), saved, fetch=Mock(return_value=raw), issuer=issuer)
        self.assertEqual(frame.loc["2026-09-22", "Close"], 151.95)
        self.assertEqual(saved, original)
        issuer.assert_not_called()

    def test_old_missing_session_does_not_silently_disappear(self):
        raw = history(("2026-09-18", "2026-09-22"), (123.67, 151.95))
        with self.assertRaisesRegex(PriceDataError, "PRICE_HISTORY_INCOMPLETE"):
            load_notification_history("SOXL", "2026-09-18", "2026-09-23",
                date(2026, 9, 22), fetch=Mock(return_value=raw), issuer=Mock(), sleep=Mock())

    def test_source_disagreement_fails_closed_but_float_rounding_is_ok(self):
        for close in (150.0, 151.949997):
            kwargs = dict(cached_quotes={"2026-09-22": quote()},
                          fetch=Mock(return_value=history(("2026-09-22",), (close,))), issuer=Mock())
            if close == 150.0:
                with self.assertRaisesRegex(PriceDataError, "VERIFIED_CLOSE_CONFLICT"):
                    load_notification_history("SOXL", "2026-09-22", "2026-09-23", date(2026, 9, 22), **kwargs)
            else:
                load_notification_history("SOXL", "2026-09-22", "2026-09-23", date(2026, 9, 22), **kwargs)

    def test_invalid_saved_source_rejected(self):
        item = quote()
        item["source"] = "https://untrusted.example"
        with self.assertRaisesRegex(PriceDataError, "INVALID_VERIFIED_CLOSE"):
            load_notification_history("SOXL", "2026-09-18", "2026-09-23", date(2026, 9, 22),
                {"2026-09-22": item}, fetch=Mock(return_value=history()), issuer=Mock())

    def test_injected_verified_history_reaches_order_engine(self):
        from src.jongsa_notify import build_message
        from src.jongsa_live import DEFAULT_CONFIG
        meta = {}
        raw = history(("2026-09-21", "2026-09-22"), (141.93, 151.95))
        with patch("src.jongsa_notify.get_kr_ohlcv") as fetch, \
             patch("src.jongsa_notify.get_dividends", return_value=pd.Series(dtype=float)):
            build_message(date(2026, 9, 23), config={**DEFAULT_CONFIG, "start_date": "2026-09-21"},
                cash_flows=[], actual_buy_fills=[], guided_buy_qty=[],
                expected_close=date(2026, 9, 22), metadata=meta, price_history=raw)
        fetch.assert_not_called()
        self.assertEqual(meta["close_date"], "2026-09-22")


if __name__ == "__main__":
    unittest.main()
