"""Cloud entry point. Public logs contain status codes only, never orders."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.quantmix_cloud import (CloudError, GitHubStore, deliver_once, load_people,
                                merged_guides, notification_slot, slot_sent,
                                send_cloud_telegram, state_file_for, telegram_html,
                                trading_context)
from src.data.verified_close import PriceDataError, load_notification_history


def deliver_for(person, notification, context, dry_run, schedule):
    """한 사람 몫을 계산해서 그 사람 방으로 보낸다.

    사람마다 설정도 발송함도 완전히 따로다. 섞이면 남의 계좌 기준 수량을
    자기 주문으로 넣게 된다.
    """
    _, slot = notification
    day, previous, cutoff = context
    profile = person["profile"]
    chat_id = person["chat_id"]
    send = lambda message: send_cloud_telegram(message, chat_id)  # noqa: E731

    store = GitHubStore(os.environ["QUANTMIX_REPOSITORY"], os.environ["GH_TOKEN"],
                        os.environ["QUANTMIX_STATE_KEY"],
                        state_file=state_file_for(person["id"]))
    state, sha = store.read()
    if slot_sent(state, day.isoformat(), slot) and not dry_run:
        return "SKIP: already delivered for this notification slot"

    guides = merged_guides(profile, state)
    existing = state["deliveries"].get(day.isoformat(), {})
    if existing.get("status") == "sent" and not dry_run:
        if datetime.now(ZoneInfo("UTC")) >= cutoff:
            raise CloudError("ORDER_DEADLINE_PASSED")
        if schedule and notification_slot(datetime.now(ZoneInfo("UTC")), schedule) != notification:
            return "SKIP: notification slot expired during processing"
        result = deliver_once(store, state, sha, day.isoformat(), "",
                              existing["buy_qty"], send, slot=slot)
        return f"REMINDER: {result}; original order reused"

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

    # 누구 것인지 메시지에 박는다. 둘이 같은 봇을 쓰므로, 방을 잘못 봤을 때
    # 남의 수량을 자기 것으로 오해하는 일을 막는다.
    note = profile.get("account_note", "")
    if person["label"]:
        note = f"{person['label']} 계좌 기준" + (f" · {note}" if note else "")
    payload = telegram_html(message, cutoff, note)

    if dry_run:
        return ("VALIDATED: fresh prices, complete private profile, "
                "encrypted outbox, order calculation")
    # Recheck time after slow downloads; never send an expired order.
    if datetime.now(ZoneInfo("UTC")) >= cutoff:
        raise CloudError("ORDER_DEADLINE_PASSED")
    if schedule and notification_slot(datetime.now(ZoneInfo("UTC")), schedule) != notification:
        return "SKIP: notification slot expired during processing"
    result = deliver_once(store, state, sha, day.isoformat(), payload,
                          metadata["buy_qty"], send, slot=slot)
    return f"DELIVERY: {result}; private order details omitted"


def main():
    now = datetime.now(ZoneInfo("UTC"))
    schedule = os.environ.get("QUANTMIX_SCHEDULE", "")
    notification = notification_slot(now, schedule)
    if notification is None:
        print("SKIP: notification slot expired")
        return
    session_date, _ = notification
    context = trading_context(now, session_date=session_date)
    if context is None:
        print("SKIP: exchange closed or order deadline passed")
        return
    people = load_people()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise CloudError("TELEGRAM_NOT_CONFIGURED")
    # 예전 한 사람 방식은 기본 채팅방을 쓴다. 그게 없으면 보낼 곳이 없다.
    if people[0]["chat_id"] is None and not os.environ.get("TELEGRAM_CHAT_ID"):
        raise CloudError("TELEGRAM_NOT_CONFIGURED")
    dry_run = os.environ.get("QUANTMIX_DRY_RUN") == "1"

    # 한 사람이 실패해도 나머지는 받아야 한다. 한 명의 시세 오류나 설정 실수로
    # 다른 사람이 그날 주문을 통째로 못 받으면 안 된다.
    # 대신 실패를 삼키지는 않는다 — 전부 돌린 뒤 실패가 있으면 실패로 끝낸다.
    failures = []
    total = len(people)
    for index, person in enumerate(people, start=1):
        who = person["id"] or "primary"
        try:
            outcome = deliver_for(person, notification, context, dry_run, schedule)
            print(f"[{index}/{total}] {who}: {outcome}")
        except (CloudError, PriceDataError) as error:
            failures.append(str(error))
            print(f"[{index}/{total}] {who}: FAILED {error}", file=sys.stderr)
        except Exception:
            # Exception strings may contain token-bearing URLs or private inputs.
            failures.append("UNEXPECTED_ERROR")
            print(f"[{index}/{total}] {who}: FAILED UNEXPECTED_ERROR", file=sys.stderr)
    if failures:
        raise CloudError(failures[0])


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
