"""Explicit local-to-cloud profile sync, using the existing GitHub login.

Private files stay gitignored. No GitHub credential is saved by this module.
"""
import base64
import hashlib
import json
from datetime import datetime, timezone

from src.quantmix_cloud import (ROOT, STATE_BRANCH, CloudError, GitHubStore,
                               github_token, merged_guides, validate_profile)

CONNECTION = ROOT / "jongsa_cloud.json"
RECEIPT = ROOT / "jongsa_cloud_sync.json"


def local_profile():
    def read(name):
        try:
            return json.loads((ROOT / name).read_text(encoding="utf-8-sig"))
        except Exception:
            raise CloudError("LOCAL_ACCOUNT_FILE_UNAVAILABLE") from None
    profile = {"version": 1, "config": read("jongsa_settings.json"),
               "cash_flows": read("jongsa_flows.json"),
               "actual_buy_fills": read("jongsa_actual_fills.json"),
               "guided_buy_qty": read("jongsa_order_guides.json"),
               "price_overrides": read("jongsa_price_overrides.json"),
               # A bare public app URL opens default settings, not this account.
               "app_url": ""}
    note_file = ROOT / "jongsa_reconciliation.json"
    if note_file.exists():
        profile["account_note"] = json.loads(note_file.read_text(encoding="utf-8"))["notification_warning"]
    return validate_profile(profile)


def put_secret(store, name, value):
    from nacl.public import PublicKey, SealedBox
    key = store.api("GET", "actions/secrets/public-key")
    encrypted = SealedBox(PublicKey(base64.b64decode(key["key"]))).encrypt(value.encode())
    store.api("PUT", f"actions/secrets/{name}", body={
        "encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key["key_id"]})


def initialize(repo="ddoldol3521-blip/quant-trader"):
    from cryptography.fernet import Fernet
    from src.telegram_notify import load_telegram_config
    if CONNECTION.exists():
        connection = json.loads(CONNECTION.read_text(encoding="utf-8"))
    else:
        connection = {"repository": repo, "state_key": Fernet.generate_key().decode()}
        # Exclusive create: never replace an existing decryption key.
        with CONNECTION.open("x", encoding="utf-8") as stream:
            json.dump(connection, stream)
    store = GitHubStore(connection["repository"], github_token(), connection["state_key"])
    try:
        store.read()
    except CloudError as error:
        if str(error) != "GITHUB_HTTP_404":
            raise
        try:
            store.api("GET", f"git/ref/heads/{STATE_BRANCH}")
        except CloudError as branch_error:
            if str(branch_error) != "GITHUB_HTTP_404":
                raise
            head = store.api("GET", "git/ref/heads/main")["object"]["sha"]
            store.api("POST", "git/refs", body={"ref": f"refs/heads/{STATE_BRANCH}", "sha": head})
        store.write({"version": 1, "deliveries": {}})
    token, chat = load_telegram_config()
    put_secret(store, "TELEGRAM_BOT_TOKEN", token)
    put_secret(store, "TELEGRAM_CHAT_ID", str(chat))
    put_secret(store, "QUANTMIX_STATE_KEY", connection["state_key"])
    return sync_profile(force=True)


def sync_profile(force=False):
    profile = local_profile()
    raw = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if RECEIPT.exists() and not force:
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if receipt.get("digest") == digest:
            return "unchanged"
    if not CONNECTION.exists():
        raise CloudError("CLOUD_NOT_CONFIGURED")
    connection = json.loads(CONNECTION.read_text(encoding="utf-8"))
    store = GitHubStore(connection["repository"], github_token(), connection["state_key"])
    # Auth/decryption check before uploading an account profile.
    store.read()
    put_secret(store, "QUANTMIX_PROFILE_JSON", raw)
    RECEIPT.write_text(json.dumps({"digest": digest, "updated_at": datetime.now(timezone.utc).isoformat()}),
                       encoding="utf-8")
    return "synced"


def cloud_order_guides():
    """Retrieve frozen quantities on demand without changing a local file."""
    connection = json.loads(CONNECTION.read_text(encoding="utf-8"))
    store = GitHubStore(connection["repository"], github_token(), connection["state_key"])
    state, _ = store.read()
    return merged_guides({"guided_buy_qty": []}, state)
