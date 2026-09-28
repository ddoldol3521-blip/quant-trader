"""여러 사람에게 알림을 보낼 때 서로 섞이지 않는지 검사.

    .venv\\Scripts\\python.exe scripts\\verify_multi_person.py

왜 이 검사가 필요한가.

알림은 사람마다 '발송함(outbox)' 에 보낸 주문 수량을 적어 둔다. 그 기록은
다음 날 계산에 그대로 쓰인다(merged_guides). 두 사람이 발송함 하나를 같이
쓰면 **A 가 보낸 수량이 B 의 기록으로 들어간다.** B 는 자기 계좌에 맞지 않는
수량을 자기가 산 것으로 알고 다음 주문을 계산한다. 돈이 틀어진다.

그래서 지키는 것
  1. 사람마다 발송함 파일이 다르다
  2. 사람마다 텔레그램 방이 다르다
  3. 같은 방·같은 id 를 두 번 쓰면 거부한다
  4. 파일 이름에 이상한 글자를 넣으면 거부한다 (경로 탈출 방지)
  5. 예전 한 사람 설정은 그대로 돈다 (기존 발송함을 계속 쓴다)
"""

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jongsa_live import DEFAULT_CONFIG
from src.quantmix_cloud import (STATE_FILE, CloudError, GitHubStore, load_people,
                                state_file_for)

fail = 0


def must(ok: bool, msg: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {msg}" + (f"  -- {got}" if got else ""))


def sample_profile(cash: float, start: str) -> dict:
    return {"version": 1, "config": {**DEFAULT_CONFIG, "initial_cash": cash,
                                     "start_date": start},
            "cash_flows": [], "actual_buy_fills": [], "guided_buy_qty": []}


def with_env(**kw):
    """환경변수를 바꿔 끼우고 되돌린다."""
    class Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in kw}
            for k, v in kw.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        def __exit__(self, *a):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return Ctx()


print("여러 사람 알림 검사\n")

# ── 1. 발송함 파일이 사람마다 다른가 ──────────────────────────────────
print("[발송함 분리]")
must(state_file_for(None) == STATE_FILE,
     "첫 사람은 기존 파일을 그대로 쓴다 (기록을 옮기지 않는다)", state_file_for(None))
a, b = state_file_for("jg"), state_file_for("gf")
must(a != b, "사람이 다르면 파일이 다르다", f"{a} vs {b}")
must(a != STATE_FILE and b != STATE_FILE, "둘 다 기본 파일과 겹치지 않는다")

for bad in ("../secret", "a/b", "a b", "..", "a.enc", "한글"):
    try:
        state_file_for(bad)
        must(False, f"이상한 id 를 거부한다: {bad!r}", "통과시켜 버림")
    except CloudError:
        must(True, f"이상한 id 를 거부한다: {bad!r}")

# ── 2. GitHubStore 가 그 파일을 실제로 쓰는가 ────────────────────────
print("\n[저장소가 그 파일을 쓴다]")
store = GitHubStore("owner/repo", "t", "0" * 43 + "=", state_file="outbox-gf.enc")
must(store.state_file == "outbox-gf.enc", "넘긴 파일 이름을 들고 있다")
default_store = GitHubStore("owner/repo", "t", "0" * 43 + "=")
must(default_store.state_file == STATE_FILE, "안 넘기면 기본 파일")

src = (Path(__file__).resolve().parent.parent / "src" / "quantmix_cloud.py").read_text(encoding="utf-8")
must("contents/{self.state_file}?ref=" in src, "읽을 때 자기 파일을 본다")
must('f"contents/{self.state_file}"' in src, "쓸 때 자기 파일에 쓴다")
must("contents/{STATE_FILE}" not in src, "고정 파일 이름이 남아 있지 않다")

# ── 3. 사람 목록 읽기 ────────────────────────────────────────────────
print("\n[사람 목록]")
two = [
    {"id": "jg", "label": "재건", "chat_id": "111", "profile": sample_profile(55152, "2025-01-02")},
    {"id": "gf", "label": "여친", "chat_id": "222", "profile": sample_profile(10000, "2026-10-01")},
]
with with_env(QUANTMIX_PEOPLE_JSON=json.dumps(two), QUANTMIX_PROFILE_JSON=None):
    people = load_people()
must(len(people) == 2, "두 사람을 읽었다", str(len(people)))
must(people[0]["chat_id"] != people[1]["chat_id"], "방이 서로 다르다")
must(state_file_for(people[0]["id"]) != state_file_for(people[1]["id"]),
     "발송함이 서로 다르다")
must(people[0]["profile"]["config"]["initial_cash"] == 55152
     and people[1]["profile"]["config"]["initial_cash"] == 10000,
     "각자 자기 투자금을 갖는다")

print("\n[겹치면 앞의 것만 쓰고 건너뛴다]")
# 예전에는 거절했다. 앱에서 직접 등록하는 길이 생긴 뒤로는, 한 사람이
# 겹쳤다는 이유로 **모두가** 그날 주문을 못 받게 된다. 그게 더 나쁘다.
# 그래서 건너뛴다. 중요한 것은 '거절' 이 아니라 **섞이지 않는 것** 이다.
same_chat = [dict(two[0]), dict(two[1], chat_id="111")]
with with_env(QUANTMIX_PEOPLE_JSON=json.dumps(same_chat), QUANTMIX_PROFILE_JSON=None):
    got = load_people()
must(len(got) == 1, "같은 방이 겹치면 한 번만 보낸다", f"{len(got)}명")
must(got[0]["id"] == "jg", "앞의 것이 남는다", got[0]["id"])

same_id = [dict(two[0]), dict(two[1], id="jg")]
with with_env(QUANTMIX_PEOPLE_JSON=json.dumps(same_id), QUANTMIX_PROFILE_JSON=None):
    got = load_people()
must(len(got) == 1, "같은 이름이 겹치면 한 명만 남는다", f"{len(got)}명")
must(got[0]["chat_id"] == "111", "앞의 것이 남는다 (발송함을 공유하지 않는다)", got[0]["chat_id"])

# 핵심: 끝까지 살아남은 사람들은 방도 발송함도 절대 안 겹친다
print("\n[끝까지 섞이지 않는다]")
messy = [dict(two[0]), dict(two[1]),
         dict(two[0], label="중복1"), dict(two[1], id="gf2")]
with with_env(QUANTMIX_PEOPLE_JSON=json.dumps(messy), QUANTMIX_PROFILE_JSON=None):
    got = load_people()
chats = [p["chat_id"] for p in got]
files = [state_file_for(p["id"]) for p in got]
must(len(chats) == len(set(chats)), "방이 하나도 안 겹친다", ", ".join(chats))
must(len(files) == len(set(files)), "발송함이 하나도 안 겹친다", ", ".join(files))

no_chat = [{"id": "x", "profile": sample_profile(1000, "2026-01-02")}]
with with_env(QUANTMIX_PEOPLE_JSON=json.dumps(no_chat), QUANTMIX_PROFILE_JSON=None):
    try:
        load_people(); must(False, "방 번호가 없으면 거부한다", "통과시켜 버림")
    except CloudError as e:
        must(str(e) == "PERSON_CHAT_ID_REQUIRED", "방 번호가 없으면 거부한다", str(e))

# ── 4. 예전 한 사람 설정이 그대로 도는가 ─────────────────────────────
print("\n[예전 방식 그대로]")
with with_env(QUANTMIX_PEOPLE_JSON=None,
              QUANTMIX_PROFILE_JSON=json.dumps(sample_profile(55152, "2025-01-02"))):
    old = load_people()
must(len(old) == 1, "한 사람으로 읽는다")
must(old[0]["id"] is None, "id 가 없다 (기존 발송함을 쓴다)")
must(state_file_for(old[0]["id"]) == STATE_FILE, "기존 발송함 파일 그대로")
must(old[0]["chat_id"] is None, "기본 채팅방을 쓴다")

with with_env(QUANTMIX_PEOPLE_JSON=None, QUANTMIX_PROFILE_JSON=None):
    try:
        load_people(); must(False, "설정이 아예 없으면 거부한다", "통과시켜 버림")
    except CloudError as e:
        must(str(e) == "NO_PROFILE_CONFIGURED", "설정이 아예 없으면 거부한다", str(e))

# ── 5. 보내는 쪽이 방을 받는가 ───────────────────────────────────────
print("\n[보내는 쪽]")
must("def send_cloud_telegram(message: str, chat_id: str | None = None)" in src,
     "보내기 함수가 방 번호를 받는다")
must("chat = chat_id or os.environ.get(\"TELEGRAM_CHAT_ID\")" in src,
     "방을 주면 그쪽으로, 없으면 기본 방으로")

notify = (Path(__file__).resolve().parent.parent / "scripts"
          / "quantmix_cloud_notify.py").read_text(encoding="utf-8")
must("send_cloud_telegram(message, chat_id)" in notify,
     "알림 스크립트가 그 사람 방으로 보낸다")
must("for index, person in enumerate(people" in notify, "사람마다 돈다")
must("failures.append" in notify and "raise CloudError(failures[0])" in notify,
     "한 명이 실패해도 나머지를 보내고, 실패는 삼키지 않는다")
must("계좌 기준" in notify, "메시지에 누구 것인지 적는다")

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
