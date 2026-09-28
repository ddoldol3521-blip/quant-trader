"""PC-independent notification support. Never log profiles or request URLs.

Profile and bot credentials live in Actions Secrets. The outbox (sent quantities
and delivery markers) is authenticated-encrypted before going to a data branch.
No account plaintext, bot token, or encryption key is committed to Git.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
STATE_BRANCH = "quantmix-notify-state"
STATE_FILE = "outbox.enc"


def state_file_for(person_id: str | None) -> str:
    """그 사람 전용 발송함 파일 이름.

    사람마다 **따로** 둬야 한다. 한 파일을 같이 쓰면 A 가 보낸 주문 수량이
    B 의 '이미 안내한 수량' 으로 섞여 들어간다(merged_guides 참고). 그러면
    B 는 자기 계좌에 맞지 않는 수량을 자기 기록으로 갖게 된다. 돈이 틀어진다.

    첫 사람은 이름 없이 기존 파일을 그대로 쓴다. 이미 쌓인 발송 기록을
    옮기지 않아도 되고, 옮기다 잃을 위험도 없다.
    """
    if not person_id:
        return STATE_FILE
    # 영문·숫자·-·_ 만. str.isalnum() 은 한글도 참이라 쓰면 안 된다 —
    # 파일 이름이 깃 API 경로에 들어가므로 인코딩이 얽히면 엉뚱한 파일을 본다.
    text = str(person_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", text):
        raise CloudError("INVALID_PERSON_ID")
    return f"outbox-{text}.enc"


class CloudError(RuntimeError):
    """Only a non-sensitive error code is safe to show in public logs."""


def validate_profile(profile: dict) -> dict:
    from src.jongsa_live import DEFAULT_CONFIG

    if not isinstance(profile, dict) or profile.get("version") != 1:
        raise CloudError("PROFILE_VERSION")
    cfg = profile.get("config", {})
    if not isinstance(cfg, dict) or set(DEFAULT_CONFIG) - set(cfg):
        raise CloudError("INCOMPLETE_PROFILE")
    if cfg["ticker"] != "SOXL":
        raise CloudError("UNSUPPORTED_MARKET")
    try:
        date.fromisoformat(cfg["start_date"])
        for key in ("initial_cash", "daily_buy_pct", "target_return", "stop_days",
                    "fee_rate", "buy_range_pct", "loss_reset_pct",
                    "loss_reset_threshold_pct", "ladder_step", "ladder_rungs"):
            if not math.isfinite(float(cfg[key])) or float(cfg[key]) < 0:
                raise ValueError()
        if cfg["initial_cash"] <= 0 or not 0 < cfg["daily_buy_pct"] <= 1:
            raise ValueError()
        if cfg["stop_days"] < 1 or int(cfg["stop_days"]) != cfg["stop_days"]:
            raise ValueError()
        if cfg["sell_day_buy_mode"] not in ("never", "all_loss", "any_loss"):
            raise ValueError()
        for key in ("fee_in_target", "whole_shares", "reinvest", "moc_available"):
            if not isinstance(cfg[key], bool):
                raise ValueError()
        for name, fields in (("cash_flows", ("금액",)),
                             ("actual_buy_fills", ("수량", "체결가")),
                             ("guided_buy_qty", ("수량",))):
            rows = profile[name]  # missing is an error, not silently an empty ledger
            if not isinstance(rows, list):
                raise ValueError()
            seen = set()
            for row in rows:
                day = date.fromisoformat(row["날짜"])
                if day in seen:
                    raise ValueError()
                seen.add(day)
                for field in fields:
                    value = float(row[field])
                    if not math.isfinite(value) or (field != "금액" and value < 0):
                        raise ValueError()
                if "체결가" in fields and float(row["수량"]) > 0 and float(row["체결가"]) <= 0:
                    raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise CloudError("INVALID_PROFILE") from None
    return profile


def load_people() -> list[dict]:
    """알림을 받을 사람들. [{id, label, profile, chat_id}, ...]

    두 가지 방식을 다 받는다.

      QUANTMIX_PEOPLE_JSON   여러 명. [{"id","label","chat_id","profile":{...}}, ...]
      QUANTMIX_PROFILE_JSON  한 명 (예전 방식). 그대로 두면 지금까지처럼 돈다.

    예전 방식을 남겨 두는 이유: 이미 잘 돌고 있는 알림을 새 형식으로 옮기다
    하루라도 빠뜨리면 그날 주문을 못 받는다. 새 사람은 새 형식으로 추가하고,
    기존 한 명은 건드리지 않는다.

    id 는 발송함 파일 이름이 된다. 한 번 정하면 바꾸지 않는다 — 바꾸면
    빈 발송함에서 새로 시작하게 되어 '이미 보낸 주문' 기록을 잃는다.
    """
    rows = []
    raw = os.environ.get("QUANTMIX_PEOPLE_JSON", "").strip()
    if raw:
        try:
            rows = json.loads(raw)
        except ValueError:
            raise CloudError("INVALID_PEOPLE_JSON") from None
        if not isinstance(rows, list):
            raise CloudError("INVALID_PEOPLE_JSON")
        rows = list(rows)

    # 앱에서 직접 등록한 사람들. 관리자가 손으로 옮기지 않아도 된다.
    # 명부를 못 읽어도 위 목록은 보내야 한다 — 한쪽 고장으로 전부 멈추면 안 된다.
    if os.environ.get("QUANTMIX_REPOSITORY") and os.environ.get("GH_TOKEN") \
            and os.environ.get("QUANTMIX_STATE_KEY"):
        try:
            from src.quantmix_registry import registry_people
            rows += registry_people(os.environ["QUANTMIX_REPOSITORY"],
                                    os.environ["GH_TOKEN"],
                                    os.environ["QUANTMIX_STATE_KEY"])
        except CloudError as error:
            print(f"REGISTRY_UNAVAILABLE: {error}")

    if not rows:
        # 예전 방식: 사람 하나, 기본 발송함, 기본 채팅방.
        if not os.environ.get("QUANTMIX_PROFILE_JSON"):
            raise CloudError("NO_PROFILE_CONFIGURED")
        return [{"id": None, "label": "",
                 "profile": validate_profile(json.loads(os.environ["QUANTMIX_PROFILE_JSON"])),
                 "chat_id": None}]

    # 앱 등록과 손으로 넣은 목록에 같은 사람이 있으면, 손으로 넣은 쪽이 이긴다.
    # 관리자가 일부러 적어 둔 것을 앱이 덮어쓰면 안 된다.
    people, seen_ids, seen_chats = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise CloudError("INVALID_PEOPLE_JSON")
        person_id = row.get("id")
        chat = str(row.get("chat_id", "")).strip()
        if not chat:
            raise CloudError("PERSON_CHAT_ID_REQUIRED")
        # 같은 방으로 두 번 보내면 받는 사람이 어느 게 자기 것인지 모른다.
        # 같은 id 를 두 번 쓰면 발송함을 공유하게 되어 수량이 섞인다.
        #
        # 겹치면 **건너뛴다.** 앞의 것(손으로 넣은 목록)이 이긴다.
        # 예전에는 거절했는데, 앱 등록이 생긴 뒤로는 한 사람이 겹쳤다는
        # 이유로 **모두가** 그날 주문을 못 받게 된다. 그건 더 나쁘다.
        if person_id in seen_ids or chat in seen_chats:
            print(f"SKIP_DUPLICATE: {person_id or 'primary'}")
            continue
        seen_ids.add(person_id)
        seen_chats.add(chat)
        state_file_for(person_id)          # 여기서 이름이 안전한지 먼저 확인한다
        people.append({
            "id": person_id,
            "label": str(row.get("label", "") or ""),
            "profile": validate_profile(row.get("profile", {})),
            "chat_id": chat,
        })
    return people


def trading_context(now: datetime, *, session_date: date | None = None):
    """NYSE date, previous session, exact cutoff; respects DST/early closes."""
    import pandas_market_calendars as mcal

    if now.tzinfo is None:
        raise CloudError("TIMEZONE_REQUIRED")
    ny = now.astimezone(ZoneInfo("America/New_York"))
    # Korean daytime reminders target that evening's US session. At 13:00 KST
    # in winter it is still the previous date in New York.
    day = session_date or ny.date()
    schedule = mcal.get_calendar("NYSE").schedule(day - timedelta(days=15), day)
    sessions = {stamp.date(): row for stamp, row in schedule.iterrows()}
    if day not in sessions:
        return None
    cutoff = sessions[day]["market_close"].to_pydatetime() - timedelta(minutes=10)
    if now >= cutoff:
        return None  # never deliver an already-expired order
    previous = max(d for d in sessions if d < day)
    return day, previous, cutoff


def notification_slot(now: datetime, schedule: str = ""):
    """Resolve each cron independently, rejecting delayed, expired slots."""
    if now.tzinfo is None:
        raise CloudError("TIMEZONE_REQUIRED")
    korea = now.astimezone(ZoneInfo("Asia/Seoul"))
    schedules = {"0 4 * * 1-5": "13:00", "0 10 * * 1-5": "19:00"}
    if schedule:
        if schedule not in schedules:
            raise CloudError("UNKNOWN_NOTIFICATION_SCHEDULE")
        slot = schedules[schedule]
        if not ((slot == "13:00" and 13 <= korea.hour < 19)
                or (slot == "19:00" and 19 <= korea.hour < 24)):
            return None
    else:
        slot = "13:00" if korea.hour < 19 else "19:00"
    return korea.date(), slot


def slot_sent(state, day, slot):
    delivery = state["deliveries"].get(day, {})
    return delivery.get("slots", {}).get(slot, {}).get("status") == "sent"


def github_token() -> str:
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    # Standard Git Credential Manager flow, only for this existing GitHub repo.
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=ddoldol3521-blip/quant-trader.git\n\n",
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if not values.get("password"):
        raise CloudError("GITHUB_LOGIN_REQUIRED")
    return values["password"]


class GitHubStore:
    def __init__(self, repo: str, token: str, key: str, state_file: str = STATE_FILE):
        if len(repo.split("/")) != 2 or any(x in repo for x in ("..", "?", "#")):
            raise CloudError("INVALID_REPOSITORY")
        self.repo = repo
        # 사람마다 다른 파일. state_file_for() 가 이름을 정한다.
        self.state_file = state_file
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.cipher = Fernet(key.encode())

    def api(self, method: str, path: str, *, body=None):
        try:
            response = requests.request(method, f"https://api.github.com/repos/{self.repo}/{path}",
                                        headers=self.headers, json=body, timeout=30)
        except requests.RequestException:
            raise CloudError("GITHUB_UNREACHABLE") from None
        if response.status_code not in (200, 201, 204):
            raise CloudError(f"GITHUB_HTTP_{response.status_code}")
        return response.json() if response.content else {}

    def read(self):
        obj = self.api("GET", f"contents/{self.state_file}?ref={STATE_BRANCH}")
        try:
            raw = self.cipher.decrypt(base64.b64decode(obj["content"]))
            state = json.loads(raw)
            if state.get("version") != 1 or not isinstance(state.get("deliveries"), dict):
                raise ValueError()
        except Exception:
            raise CloudError("INVALID_ENCRYPTED_STATE") from None
        return state, obj["sha"]

    def write(self, state, sha=None):
        encrypted = self.cipher.encrypt(json.dumps(state, ensure_ascii=False).encode())
        body = {"message": "Update encrypted notification outbox",
                "branch": STATE_BRANCH,
                "content": base64.b64encode(encrypted).decode()}
        if sha:
            body["sha"] = sha  # optimistic concurrency, never overwrite a newer outbox
        result = self.api("PUT", f"contents/{self.state_file}", body=body)
        return result["content"]["sha"]


def merged_guides(profile, state):
    rows = {row["날짜"]: dict(row) for row in profile["guided_buy_qty"]}
    for day, delivery in state["deliveries"].items():
        if any(item.get("status") != "sent" for item in delivery.get("slots", {}).values()):
            raise CloudError("PREVIOUS_DELIVERY_UNCONFIRMED")
        if delivery["status"] == "sent":
            # A sent order's quantity is immutable. Real fill overrides still win
            # inside run_jongsa if the user actually filled a different quantity.
            rows[day] = {"날짜": day, "수량": delivery["buy_qty"]}
        elif delivery["status"] in ("sending", "uncertain"):
            raise CloudError("PREVIOUS_DELIVERY_UNCONFIRMED")
    return [rows[day] for day in sorted(rows)]


def telegram_html(message: str, cutoff: datetime, account_note: str = "") -> str:
    marker = "📋 복사용 주문\n"
    if marker not in message:
        raise CloudError("MISSING_ORDER_BLOCK")
    before, after = message.split(marker, 1)
    orders, detail = after.split("\n\n", 1)
    # Replace the fixed 16:00-session deadline with the actual exchange calendar.
    detail = "\n".join(line for line in detail.splitlines() if not line.startswith("⏰"))
    korea = cutoff.astimezone(ZoneInfo("Asia/Seoul"))
    result = ("<b>☁️ 퀀트믹스 서버 자동 알림</b>\n" + html.escape(before.strip())
              + ("\n\n⚠️ " + html.escape(account_note) if account_note else "")
              + "\n\n📋 복사용 주문\n<pre>" + html.escape(orders) + "</pre>\n\n"
              + html.escape(detail.strip())
              + f"\n⏰ 주문 마감: 한국 {korea:%m/%d %H:%M}"
              + "\n※ 입력 기록 기준 계산이며 증권사 계좌와 자동 연동되지 않습니다.")
    if len(result) > 4000:
        raise CloudError("MESSAGE_TOO_LONG")
    return result


def deliver_once(store, state, sha, day, message, buy_qty, send, *, slot=None):
    if slot is not None and slot not in ("13:00", "19:00"):
        raise CloudError("INVALID_NOTIFICATION_SLOT")
    existing = state["deliveries"].get(day)
    if existing:
        if existing["status"] != "sent":
            raise CloudError("DELIVERY_UNCONFIRMED")
        if slot is None or slot_sent(state, day, slot):
            return "already_sent"
        if any(item.get("status") != "sent" for item in existing.get("slots", {}).values()):
            raise CloudError("DELIVERY_UNCONFIRMED")
        # Reuse the first sent order exactly; a reminder must not silently
        # change quantities or turn into an additional order.
        if not existing.get("message"):
            raise CloudError("LEGACY_DELIVERY_HAS_NO_SAVED_MESSAGE")
        message = existing["message"]
        buy_qty = existing["buy_qty"]
    base_message = message
    if slot:
        message = (f"<b>한국 {slot} 주문 알림</b>\n"
                   "※ 같은 거래일 주문표입니다. 이미 주문했다면 추가 주문하지 마세요.\n\n"
                   + message)
        if len(message) > 4096:
            raise CloudError("MESSAGE_TOO_LONG")
    delivery = {"status": "sending", "buy_qty": buy_qty,
                "digest": hashlib.sha256(message.encode()).hexdigest(),
                "created_at": datetime.now(ZoneInfo("UTC")).isoformat()}
    if existing:
        existing.setdefault("slots", {})[slot] = delivery
    else:
        state["deliveries"][day] = delivery
        if slot:
            delivery["message"] = base_message  # encrypted at rest, never logged
            delivery["slots"] = {slot: {"status": "sending"}}
    sha = store.write(state, sha)  # MUST reserve before touching Telegram
    # No automatic retry after an ambiguous timeout. A message may have arrived.
    message_id = send(message)
    delivery.update(status="sent", message_id=message_id)
    if slot and not existing:
        delivery["slots"][slot] = {"status": "sent", "message_id": message_id}
    store.write(state, sha)
    return "sent"


def send_cloud_telegram(message: str, chat_id: str | None = None) -> int:
    """텔레그램으로 보낸다. chat_id 를 주면 그 방으로, 없으면 기본 방으로.

    사람마다 방이 달라야 한다. 한 방에 둘 다 보내면 상대방 계좌 기준 수량을
    자기 것으로 오해해서 따라 넣을 수 있다.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise CloudError("TELEGRAM_NOT_CONFIGURED")
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": message, "parse_mode": "HTML",
                                       "disable_web_page_preview": True}, timeout=25)
        result = response.json()
    except (requests.RequestException, ValueError):
        raise CloudError("TELEGRAM_DELIVERY_UNCONFIRMED") from None
    if response.status_code != 200 or not result.get("ok"):
        raise CloudError("TELEGRAM_DELIVERY_FAILED")
    return int(result["result"]["message_id"])
