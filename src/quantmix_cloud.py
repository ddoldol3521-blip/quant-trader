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
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
STATE_BRANCH = "quantmix-notify-state"
STATE_FILE = "outbox.enc"


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


def trading_context(now: datetime):
    """NYSE date, previous session, exact cutoff; respects DST/early closes."""
    import pandas_market_calendars as mcal

    if now.tzinfo is None:
        raise CloudError("TIMEZONE_REQUIRED")
    ny = now.astimezone(ZoneInfo("America/New_York"))
    day = ny.date()
    schedule = mcal.get_calendar("NYSE").schedule(day - timedelta(days=15), day)
    sessions = {stamp.date(): row for stamp, row in schedule.iterrows()}
    if day not in sessions:
        return None
    cutoff = sessions[day]["market_close"].to_pydatetime() - timedelta(minutes=10)
    if now >= cutoff:
        return None  # never deliver an already-expired order
    previous = max(d for d in sessions if d < day)
    return day, previous, cutoff


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
    def __init__(self, repo: str, token: str, key: str):
        if len(repo.split("/")) != 2 or any(x in repo for x in ("..", "?", "#")):
            raise CloudError("INVALID_REPOSITORY")
        self.repo = repo
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
        obj = self.api("GET", f"contents/{STATE_FILE}?ref={STATE_BRANCH}")
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
        result = self.api("PUT", f"contents/{STATE_FILE}", body=body)
        return result["content"]["sha"]


def merged_guides(profile, state):
    rows = {row["날짜"]: dict(row) for row in profile["guided_buy_qty"]}
    for day, delivery in state["deliveries"].items():
        if delivery["status"] == "sent":
            # A sent order's quantity is immutable. Real fill overrides still win
            # inside run_jongsa if the user actually filled a different quantity.
            rows[day] = {"날짜": day, "수량": delivery["buy_qty"]}
        elif delivery["status"] in ("sending", "uncertain"):
            raise CloudError("PREVIOUS_DELIVERY_UNCONFIRMED")
    return [rows[day] for day in sorted(rows)]


def telegram_html(message: str, cutoff: datetime) -> str:
    marker = "📋 복사용 주문\n"
    if marker not in message:
        raise CloudError("MISSING_ORDER_BLOCK")
    before, after = message.split(marker, 1)
    orders, detail = after.split("\n\n", 1)
    # Replace the fixed 16:00-session deadline with the actual exchange calendar.
    detail = "\n".join(line for line in detail.splitlines() if not line.startswith("⏰"))
    korea = cutoff.astimezone(ZoneInfo("Asia/Seoul"))
    result = ("<b>☁️ 퀀트믹스 서버 자동 알림</b>\n" + html.escape(before.strip())
              + "\n\n📋 복사용 주문\n<pre>" + html.escape(orders) + "</pre>\n\n"
              + html.escape(detail.strip())
              + f"\n⏰ 주문 마감: 한국 {korea:%m/%d %H:%M}"
              + "\n※ 입력 기록 기준 계산이며 증권사 계좌와 자동 연동되지 않습니다.")
    if len(result) > 4000:
        raise CloudError("MESSAGE_TOO_LONG")
    return result


def deliver_once(store, state, sha, day, message, buy_qty, send):
    existing = state["deliveries"].get(day)
    if existing:
        if existing["status"] == "sent":
            return "already_sent"
        raise CloudError("DELIVERY_UNCONFIRMED")
    delivery = {"status": "sending", "buy_qty": buy_qty,
                "digest": hashlib.sha256(message.encode()).hexdigest(),
                "created_at": datetime.now(ZoneInfo("UTC")).isoformat()}
    state["deliveries"][day] = delivery
    sha = store.write(state, sha)  # MUST reserve before touching Telegram
    # No automatic retry after an ambiguous timeout. A message may have arrived.
    message_id = send(message)
    delivery.update(status="sent", message_id=message_id)
    store.write(state, sha)
    return "sent"


def send_cloud_telegram(message: str) -> int:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
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
