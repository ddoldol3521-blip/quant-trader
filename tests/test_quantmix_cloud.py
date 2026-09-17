import copy
import json
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from cryptography.fernet import Fernet

from src.jongsa_live import DEFAULT_CONFIG
from src.quantmix_cloud import (CloudError, GitHubStore, deliver_once, merged_guides,
                               telegram_html, trading_context, validate_profile)


def profile():
    return {"version": 1, "config": {**DEFAULT_CONFIG, "ticker": "SOXL",
            "start_date": "2026-09-01"}, "cash_flows": [],
            "actual_buy_fills": [], "guided_buy_qty": []}


class CloudTests(unittest.TestCase):
    def test_profile_complete(self):
        self.assertEqual(validate_profile(profile())["version"], 1)

    def test_no_defaults_for_missing_ledger(self):
        item = profile()
        del item["cash_flows"]
        with self.assertRaises(CloudError):
            validate_profile(item)

    def test_no_defaults_for_missing_settings(self):
        item = profile()
        del item["config"]["loss_reset_pct"]
        with self.assertRaises(CloudError):
            validate_profile(item)

    def test_nan_rejected(self):
        item = profile()
        item["config"]["daily_buy_pct"] = float("nan")
        with self.assertRaises(CloudError):
            validate_profile(item)

    def test_duplicate_guide_rejected(self):
        item = profile()
        item["guided_buy_qty"] = [{"날짜": "2026-09-01", "수량": 10}] * 2
        with self.assertRaises(CloudError):
            validate_profile(item)

    def test_labor_day_closed(self):
        self.assertIsNone(trading_context(datetime(2026, 9, 7, 12, tzinfo=ZoneInfo("UTC"))))

    def test_weekend_closed(self):
        self.assertIsNone(trading_context(datetime(2026, 9, 12, 12, tzinfo=ZoneInfo("UTC"))))

    def test_after_holiday_previous_friday(self):
        day, prev, cutoff = trading_context(datetime(2026, 9, 8, 12, tzinfo=ZoneInfo("UTC")))
        self.assertEqual((day, prev), (date(2026, 9, 8), date(2026, 9, 4)))
        self.assertEqual(cutoff.astimezone(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M"),
                         "09/09 04:50")

    def test_korean_midnight_is_previous_us_session(self):
        day, _, _ = trading_context(datetime(2026, 9, 18, 2, tzinfo=ZoneInfo("Asia/Seoul")))
        self.assertEqual(day, date(2026, 9, 17))

    def test_early_close_deadline(self):
        context = trading_context(datetime(2026, 11, 27, 12, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(context[2].hour, 17)
        self.assertEqual(context[2].minute, 50)
        self.assertIsNone(trading_context(datetime(2026, 11, 27, 17, 51, tzinfo=ZoneInfo("UTC"))))

    def test_special_mourning_holiday(self):
        self.assertIsNone(trading_context(datetime(2025, 1, 9, 12, tzinfo=ZoneInfo("UTC"))))

    def test_sent_quantity_wins_over_recalculation(self):
        item = profile()
        item["guided_buy_qty"] = [{"날짜": "2026-09-01", "수량": 41}]
        state = {"deliveries": {"2026-09-01": {"status": "sent", "buy_qty": 40}}}
        self.assertEqual(merged_guides(item, state)[0]["수량"], 40)

    def test_unknown_delivery_blocks_future_orders(self):
        with self.assertRaises(CloudError):
            merged_guides(profile(), {"deliveries": {"2026-09-01": {"status": "sending"}}})

    def test_duplicate_delivery_does_not_send(self):
        state = {"deliveries": {"2026-09-01": {"status": "sent"}}}
        result = deliver_once(None, state, "sha", "2026-09-01", "private", 40,
                              lambda _: self.fail("duplicate send"))
        self.assertEqual(result, "already_sent")

    def test_reservation_before_send_and_no_retry_on_timeout(self):
        writes = []
        class Store:
            def write(self, state, sha):
                writes.append(copy.deepcopy(state))
                return "nextsha"
        state = {"deliveries": {}}
        def timeout(_):
            self.assertEqual(len(writes), 1)
            raise TimeoutError()
        with self.assertRaises(TimeoutError):
            deliver_once(Store(), state, "sha", "2026-09-01", "private", 40, timeout)
        with self.assertRaises(CloudError):
            deliver_once(Store(), state, "sha", "2026-09-01", "private", 40, timeout)

    def test_success_records_message_id(self):
        from unittest.mock import Mock
        store = Mock()
        store.write.return_value = "sha2"
        state = {"deliveries": {}}
        self.assertEqual(deliver_once(store, state, "sha", "2026-09-01", "private", 40,
                                      lambda _: 123), "sent")
        self.assertEqual(state["deliveries"]["2026-09-01"]["message_id"], 123)
        self.assertEqual(store.write.call_count, 2)

    def test_storage_is_encrypted(self):
        from unittest.mock import Mock
        key = Fernet.generate_key()
        store = GitHubStore("owner/repo", "fake-token", key.decode())
        store.api = Mock(return_value={"content": {"sha": "newsha"}})
        store.write({"private_amount": 1234567}, "oldsha")
        body = store.api.call_args.kwargs["body"]
        import base64
        cipher = base64.b64decode(body["content"])
        self.assertNotIn(b"1234567", cipher)
        self.assertEqual(json.loads(Fernet(key).decrypt(cipher))["private_amount"], 1234567)
        self.assertEqual(body["sha"], "oldsha")

    def test_stale_price_fails_before_calculating(self):
        from src.jongsa_notify import build_message
        hist = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-09-01"]))
        with patch("src.jongsa_notify.get_kr_ohlcv", return_value=hist), \
             patch("src.jongsa_notify.run_jongsa") as engine:
            with self.assertRaisesRegex(ValueError, "STALE_PRICES"):
                build_message(date(2026, 9, 3), config=profile()["config"],
                              expected_close=date(2026, 9, 2))
            engine.assert_not_called()

    def test_html_copy_block_and_dynamic_cutoff(self):
        text = "미국 2026-11-27 주문\n\n📋 복사용 주문\nLOC 매수 | 40주\n\n상세 <test>\n⏰ 틀린 고정시간"
        out = telegram_html(text, datetime(2026, 11, 27, 17, 50, tzinfo=ZoneInfo("UTC")))
        self.assertIn("<pre>LOC 매수 | 40주</pre>", out)
        self.assertIn("&lt;test&gt;", out)
        self.assertIn("11/28 02:50", out)
        self.assertNotIn("틀린 고정시간", out)

    def test_account_inputs_reach_engine_and_metadata(self):
        from src.jongsa_notify import build_message
        from src.jongsa_backtest import run_jongsa
        cfg = {**profile()["config"], "stop_days": 16, "ladder_rungs": 0}
        hist = pd.DataFrame({"Close": [100., 99.]},
                            index=pd.to_datetime(["2026-09-01", "2026-09-02"]))
        meta = {}
        with patch("src.jongsa_notify.get_kr_ohlcv", return_value=hist), \
             patch("src.jongsa_notify.get_dividends", return_value=pd.Series(dtype=float)), \
             patch("src.jongsa_notify.run_jongsa", wraps=run_jongsa) as engine:
            message = build_message(date(2026, 9, 3), config=cfg,
                cash_flows=[{"날짜": "2026-09-01", "금액": 1000}],
                actual_buy_fills=[{"날짜": "2026-09-01", "수량": 1, "체결가": 98}],
                guided_buy_qty=[{"날짜": "2026-09-02", "수량": 4}],
                expected_close=date(2026, 9, 2), metadata=meta)
        self.assertEqual(engine.call_args.kwargs["cash_flows"], [("2026-09-01", 1000.)])
        self.assertEqual(engine.call_args.kwargs["actual_buy_fills"], [("2026-09-01", 1., 98.)])
        self.assertEqual(engine.call_args.kwargs["guided_buy_qty"], [("2026-09-02", 4.)])
        self.assertEqual(meta["order_date"], "2026-09-03")
        self.assertEqual(meta["held_qty"], 5.)
        self.assertIn("미국 2026-09-03", message)

    def test_reconciliation_warning_precedes_copyable_orders(self):
        text = "미국 주문\n\n📋 복사용 주문\nLOC 매수 | 40주\n\n상세"
        out = telegram_html(text, datetime(2026, 9, 17, 19, 50, tzinfo=ZoneInfo("UTC")),
                            "잔고 미정산 <확인>")
        self.assertLess(out.index("잔고 미정산 &lt;확인&gt;"), out.index("<pre>"))


if __name__ == "__main__":
    unittest.main()
