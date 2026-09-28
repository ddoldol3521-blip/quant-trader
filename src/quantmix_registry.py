"""앱에서 직접 자동 알림을 등록하는 명부.

관리자가 손으로 옮기지 않아도 되게, 앱이 바로 저장한다.

  친구  앱에서 번호·설정 입력 -> [자동 알림 켜기]
        -> 앱이 명부에 써 넣음
        -> 다음 거래일부터 그 친구 폰으로 자동 발송

저장 위치는 이미 쓰고 있는 암호화 저장소(state 브랜치)다. 새 데이터베이스를
붙이지 않는다 — 깔 것이 늘면 그만큼 고장 날 곳도 는다.

**봇 토큰은 저장하지 않는다.** 명부에 들어가는 것은 chat_id(대화방 번호)와
투자 설정뿐이다. chat_id 는 비밀이 아니라 전화번호 같은 것이고, 알려져도
그 사람 봇을 조종할 수 없다. 반면 토큰은 봇의 비밀번호라서, 공개 저장소에
(암호화하더라도) 두면 기록이 영원히 남는다. 그래서 받지 않는다.

앱이 쓰려면 GitHub 토큰이 필요하다. Streamlit Secrets 에 넣어 두면 화면을
보는 사람에게는 노출되지 않는다. 없으면 등록 기능이 꺼지고, 신청서를 복사해
관리자에게 보내는 예전 방식으로 돌아간다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.quantmix_cloud import CloudError, GitHubStore, state_file_for

REGISTRY_FILE = "people.enc"

# 한 봇이 감당할 사람 수. 넘으면 등록을 막는다.
#
# 아무나 등록할 수 있는 화면이라 상한이 없으면 곤란하다. 텔레그램이 봇을
# 스팸으로 볼 수도 있고, 매일 도는 작업 시간도 사람 수에 비례해 늘어난다.
MAX_PEOPLE = 20


def _store(repo: str, token: str, key: str) -> GitHubStore:
    return GitHubStore(repo, token, key, state_file=REGISTRY_FILE)


def read_registry(repo: str, token: str, key: str):
    """명부를 읽는다. 아직 없으면 빈 명부로 시작한다."""
    store = _store(repo, token, key)
    try:
        state, sha = store.read()
    except CloudError as error:
        if str(error) == "GITHUB_HTTP_404":
            return {"version": 1, "deliveries": {}, "people": []}, None
        raise
    state.setdefault("people", [])
    return state, sha


def people_from(state) -> list[dict]:
    rows = state.get("people", [])
    return rows if isinstance(rows, list) else []


def add_person(repo: str, token: str, key: str, entry: dict) -> str:
    """명부에 한 사람을 더한다. 이미 있으면 설정만 바꾼다.

    같은 사람이 다시 신청하면(설정을 바꿔서) 새로 만들지 않고 고친다.
    새로 만들면 발송함이 둘로 갈려 '이미 보낸 주문' 기록을 잃는다.
    """
    state, sha = read_registry(repo, token, key)
    rows = people_from(state)

    same_id = next((r for r in rows if r.get("id") == entry["id"]), None)
    same_chat = next((r for r in rows if str(r.get("chat_id")) == str(entry["chat_id"])), None)

    # 남의 이름으로 남의 방을 덮어쓰지 못하게 한다. 이름과 번호가 서로
    # 다른 사람을 가리키면 어느 쪽을 고쳐야 할지 알 수 없으므로 거절한다.
    if same_id and same_chat and same_id is not same_chat:
        raise CloudError("REGISTRY_CONFLICT")
    existing = same_id or same_chat
    if existing is None and len(rows) >= MAX_PEOPLE:
        raise CloudError("REGISTRY_FULL")

    now = datetime.now(timezone.utc).isoformat()
    record = {**entry, "updated_at": now}
    if existing is None:
        record["created_at"] = now
        rows.append(record)
        outcome = "added"
    else:
        record["created_at"] = existing.get("created_at", now)
        rows[rows.index(existing)] = record
        outcome = "updated"

    state["people"] = rows
    _store(repo, token, key).write(state, sha)
    return outcome


def remove_person(repo: str, token: str, key: str, chat_id: str) -> bool:
    """스스로 끄기. 자기 번호를 아는 사람만 끌 수 있다."""
    state, sha = read_registry(repo, token, key)
    rows = people_from(state)
    left = [r for r in rows if str(r.get("chat_id")) != str(chat_id)]
    if len(left) == len(rows):
        return False
    state["people"] = left
    _store(repo, token, key).write(state, sha)
    return True


def describe(entry: dict) -> str:
    """화면에 보여줄 한 줄 요약. 번호는 뒤 3자리만."""
    chat = str(entry.get("chat_id", ""))
    masked = ("…" + chat[-3:]) if len(chat) > 3 else chat
    config = entry.get("profile", {}).get("config", {})
    when = entry.get("updated_at", "")[:10]
    return (f"{entry.get('label') or entry.get('id')} · 번호 {masked} · "
            f"${config.get('initial_cash', 0):,.0f} · {config.get('start_date', '')} · {when}")


def registry_people(repo: str, token: str, key: str) -> list[dict]:
    """알림 작업이 쓰는 목록. 발송함 이름이 안전한지 먼저 확인한다."""
    state, _ = read_registry(repo, token, key)
    out = []
    for row in people_from(state):
        if not isinstance(row, dict) or not row.get("chat_id"):
            continue
        state_file_for(row.get("id"))
        out.append({"id": row.get("id"), "label": row.get("label", ""),
                    "profile": row.get("profile", {}), "chat_id": str(row["chat_id"])})
    return out
