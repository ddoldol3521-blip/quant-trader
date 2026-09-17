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
                               send_cloud_telegram, telegram_html, trading_context,
                               validate_profile)


def main():
    context = trading_context(datetime.now(ZoneInfo("UTC")))
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
    if state["deliveries"].get(day.isoformat(), {}).get("status") == "sent":
        print("SKIP: already delivered for this US session")
        return
    guides = merged_guides(profile, state)
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
        message = build_message(day, config=profile["config"],
                                cash_flows=profile["cash_flows"],
                                actual_buy_fills=profile["actual_buy_fills"],
                                guided_buy_qty=guides, expected_close=previous,
                                metadata=metadata)
    payload = telegram_html(message, cutoff)
    if os.environ.get("QUANTMIX_DRY_RUN") == "1":
        print("VALIDATED: fresh prices, complete private profile, encrypted outbox, order calculation")
        return
    # Recheck time after slow downloads; never send an expired order.
    if datetime.now(ZoneInfo("UTC")) >= cutoff:
        raise CloudError("ORDER_DEADLINE_PASSED")
    result = deliver_once(store, state, sha, day.isoformat(), payload,
                          metadata["buy_qty"], send_cloud_telegram)
    print(f"DELIVERY: {result}; private order details omitted")


if __name__ == "__main__":
    try:
        main()
    except CloudError as error:
        print(f"NOTIFICATION_FAILED: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # Exception strings may contain token-bearing URLs or private inputs.
        print(f"NOTIFICATION_FAILED: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
