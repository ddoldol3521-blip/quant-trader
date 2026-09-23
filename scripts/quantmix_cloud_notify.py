"""Cloud entry point. Public logs contain status codes only, never orders."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.quantmix_cloud import (CloudError, GitHubStore, deliver_once, merged_guides,
                               notification_slot, slot_sent, send_cloud_telegram,
                               telegram_html, trading_context,
                               validate_profile)
from src.data.verified_close import PriceDataError, load_notification_history


def main():
    now = datetime.now(ZoneInfo("UTC"))
    schedule = os.environ.get("QUANTMIX_SCHEDULE", "")
    notification = notification_slot(now, schedule)
    if notification is None:
        print("SKIP: notification slot expired")
        return
    session_date, slot = notification
    context = trading_context(now, session_date=session_date)
    if context is None:
        print("SKIP: exchange closed or order deadline passed")
        return
    day, previous, cutoff = context
    profile = validate_profile(json.loads(os.environ["QUANTMIX_PROFILE_JSON"]))
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        raise CloudError("TELEGRAM_NOT_CONFIGURED")
    store = GitHubStore(os.environ["QUANTMIX_REPOSITORY"], os.environ["GH_TOKEN"],
                        os.environ["QUANTMIX_STATE_KEY"])
    state, sha = store.read()
    dry_run = os.environ.get("QUANTMIX_DRY_RUN") == "1"
    if slot_sent(state, day.isoformat(), slot) and not dry_run:
        print("SKIP: already delivered for this notification slot")
        return
    guides = merged_guides(profile, state)
    existing = state["deliveries"].get(day.isoformat(), {})
    if existing.get("status") == "sent" and not dry_run:
        if datetime.now(ZoneInfo("UTC")) >= cutoff:
            raise CloudError("ORDER_DEADLINE_PASSED")
        if schedule and notification_slot(datetime.now(ZoneInfo("UTC")), schedule) != notification:
            print("SKIP: notification slot expired during processing")
            return
        result = deliver_once(store, state, sha, day.isoformat(), "",
                              existing["buy_qty"], send_cloud_telegram, slot=slot)
        print(f"REMINDER: {result}; original order reused")
        return
    # Never allow stale, separately configured legacy env vars to override the
    # full validated profile. There is exactly one source of account settings.
    for key in list(os.environ):
        if key.startswith("JONGSA_"):
            del os.environ[key]
    os.environ["JONGSA_APP_URL"] = profile.get("app_url", "")
    from src.data import kr_data
    from src.jongsa_notify import build_message

    metadata = {}
    with tempfile.TemporaryDirectory(prefix="quantmix-input-") as folder:
        # Runtime-only input; not an artifact/cache and removed after calculation.
        override_file = Path(folder) / "prices.json"
        override_file.write_text(json.dumps(profile.get("price_overrides", {})), encoding="utf-8")
        kr_data.PRICE_OVERRIDES_PATH = override_file
        history, quotes = load_notification_history(
            profile["config"]["ticker"], profile["config"]["start_date"], day.isoformat(),
            previous, state.get("verified_closes", {}))
        state["verified_closes"] = quotes
        message = build_message(day, config=profile["config"],
                                cash_flows=profile["cash_flows"],
                                actual_buy_fills=profile["actual_buy_fills"],
                                guided_buy_qty=guides, expected_close=previous,
                                metadata=metadata, price_history=history)
    if previous.isoformat() in quotes:
        message += f"\nℹ️ {previous:%m/%d} 종가는 날짜가 확인된 운용사 공식 자료로 보완했습니다."
    payload = telegram_html(message, cutoff, profile.get("account_note", ""))
    if dry_run:
        print("VALIDATED: fresh prices, complete private profile, encrypted outbox, order calculation")
        return
    # Recheck time after slow downloads; never send an expired order.
    if datetime.now(ZoneInfo("UTC")) >= cutoff:
        raise CloudError("ORDER_DEADLINE_PASSED")
    if schedule and notification_slot(datetime.now(ZoneInfo("UTC")), schedule) != notification:
        print("SKIP: notification slot expired during processing")
        return
    result = deliver_once(store, state, sha, day.isoformat(), payload,
                          metadata["buy_qty"], send_cloud_telegram, slot=slot)
    print(f"DELIVERY: {result}; private order details omitted")


def report_failure(code):
    # Only a safe machine code reaches Actions outputs and the failure message.
    import re
    code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "UNEXPECTED_ERROR"
    print(f"NOTIFICATION_FAILED: {code}", file=sys.stderr)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"failure_code={code}\n")


if __name__ == "__main__":
    try:
        main()
    except (CloudError, PriceDataError) as error:
        report_failure(str(error))
        sys.exit(1)
    except Exception as error:
        # Exception strings may contain token-bearing URLs or private inputs.
        report_failure("UNEXPECTED_ERROR")
        sys.exit(1)
