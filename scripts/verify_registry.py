"""앱이 직접 등록하는 명부 검사.

    .venv\\Scripts\\python.exe scripts\\verify_registry.py

관리자가 손으로 옮기지 않고 앱이 바로 저장한다. 그래서 확인할 것이 늘었다.

  · 같은 사람이 다시 신청하면 새로 만들지 않고 고치는가
    (새로 만들면 발송함이 갈려 '이미 보낸 주문' 기록을 잃는다)
  · 남의 번호를 가로채지 못하는가
  · 인원 상한이 있는가 (아무나 등록하는 화면이라 없으면 곤란하다)
  · 명부가 고장 나도 기존 사람들은 알림을 받는가
  · 봇 토큰이 저장되지 않는가

깃허브를 실제로 부르지 않고, 저장소를 흉내 낸 것으로 돌린다.
"""

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from src.jongsa_live import DEFAULT_CONFIG
from src.quantmix_cloud import CloudError, load_people
from src.quantmix_signup import build_signup
import src.quantmix_registry as reg

fail = 0


def must(ok: bool, msg: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {msg}" + (f"  -- {got}" if got else ""))


# ── 깃허브 대신 쓸 가짜 저장소 ───────────────────────────────────────
class FakeStore:
    """파일 하나를 메모리에 들고 있는 저장소. 실제 GitHubStore 와 같은 모양."""
    files: dict = {}
    broken = False

    def __init__(self, repo, token, key, state_file="outbox.enc"):
        self.state_file = state_file
        self.cipher = Fernet(key.encode())

    def read(self):
        if FakeStore.broken:
            raise CloudError("GITHUB_HTTP_500")
        if self.state_file not in FakeStore.files:
            raise CloudError("GITHUB_HTTP_404")
        raw, sha = FakeStore.files[self.state_file]
        return json.loads(self.cipher.decrypt(raw)), sha

    def write(self, state, sha=None):
        stored = FakeStore.files.get(self.state_file)
        if stored and sha != stored[1]:
            raise CloudError("GITHUB_HTTP_409")   # 남이 먼저 바꿨다
        new_sha = f"sha{len(FakeStore.files)}-{id(state)}"
        FakeStore.files[self.state_file] = (self.cipher.encrypt(
            json.dumps(state, ensure_ascii=False).encode()), new_sha)
        return new_sha


reg.GitHubStore = FakeStore
KEY = Fernet.generate_key().decode()
ARGS = ("owner/repo", "token", KEY)
CFG = {**DEFAULT_CONFIG, "initial_cash": 10000.0, "start_date": "2026-10-01"}

print("앱 직접 등록 명부 검사\n")

# ── 1. 등록과 수정 ───────────────────────────────────────────────────
print("[등록]")
minsu = build_signup("minsu", "민수", "111111111", CFG)
must(reg.add_person(*ARGS, minsu) == "added", "처음이면 새로 등록한다")
must(len(reg.registry_people(*ARGS)) == 1, "명부에 한 명")

jihye = build_signup("jihye", "지혜", "222222222", {**CFG, "initial_cash": 50000.0})
must(reg.add_person(*ARGS, jihye) == "added", "두 번째 사람도 등록한다")
must(len(reg.registry_people(*ARGS)) == 2, "명부에 두 명")

print("\n[다시 신청하면 고친다]")
again = build_signup("minsu", "민수", "111111111", {**CFG, "initial_cash": 77000.0})
must(reg.add_person(*ARGS, again) == "updated", "새로 만들지 않고 고친다")
people = reg.registry_people(*ARGS)
must(len(people) == 2, "사람 수는 그대로", str(len(people)))
found = next(p for p in people if p["id"] == "minsu")
must(found["profile"]["config"]["initial_cash"] == 77000.0, "바뀐 투자금이 반영된다")

# 새로 만들지 않아야 발송함이 그대로 유지된다
state, _ = reg.read_registry(*ARGS)
row = next(r for r in reg.people_from(state) if r["id"] == "minsu")
must(row["created_at"] != row["updated_at"], "처음 등록한 시각은 그대로 남는다")

print("\n[남의 것을 가로채지 못한다]")
try:
    reg.add_person(*ARGS, build_signup("minsu", "가짜", "222222222", CFG))
    must(False, "이름과 번호가 다른 사람이면 거절한다", "통과시켜 버림")
except CloudError as error:
    must(str(error) == "REGISTRY_CONFLICT", "이름과 번호가 다른 사람이면 거절한다", str(error))

print("\n[인원 상한]")
saved_max = reg.MAX_PEOPLE
reg.MAX_PEOPLE = 3
must(reg.add_person(*ARGS, build_signup("third", "", "333333333", CFG)) == "added",
     "상한 안에서는 등록된다")
try:
    reg.add_person(*ARGS, build_signup("fourth", "", "444444444", CFG))
    must(False, "상한을 넘으면 거절한다", "통과시켜 버림")
except CloudError as error:
    must(str(error) == "REGISTRY_FULL", "상한을 넘으면 거절한다", str(error))
must(reg.add_person(*ARGS, build_signup("minsu", "민수", "111111111", CFG)) == "updated",
     "상한을 넘어도 기존 사람 수정은 된다")
reg.MAX_PEOPLE = saved_max

# ── 2. 스스로 끄기 ───────────────────────────────────────────────────
print("\n[끄기]")
must(reg.remove_person(*ARGS, "333333333") is True, "자기 번호로 끌 수 있다")
must(len(reg.registry_people(*ARGS)) == 2, "명부에서 빠졌다")
must(reg.remove_person(*ARGS, "999999999") is False, "없는 번호는 조용히 아니라고 한다")

# ── 3. 토큰이 저장되지 않는가 ────────────────────────────────────────
print("\n[토큰을 저장하지 않는다]")
blob = json.dumps(reg.read_registry(*ARGS)[0], ensure_ascii=False).lower()
must("token" not in blob, "명부에 token 항목이 없다")
must("bot" not in blob, "명부에 bot 항목이 없다")
src = (Path(__file__).resolve().parent.parent / "src" / "quantmix_registry.py").read_text(encoding="utf-8")
must("봇 토큰은 저장하지 않는다" in src, "왜 안 받는지 적혀 있다")

# ── 4. 번호를 화면에 다 보여주지 않는가 ──────────────────────────────
print("\n[보여줄 때 가린다]")
line = reg.describe(reg.read_registry(*ARGS)[0]["people"][0])
must("111111111" not in line, "번호 전체를 보여주지 않는다", line)
must("111" in line, "뒤 3자리는 보여준다 (어느 것인지 알아야 하므로)")

# ── 5. 알림 작업이 명부를 읽는가 ─────────────────────────────────────
print("\n[알림 작업이 읽는다]")
import src.quantmix_cloud as cloud
saved_env = {k: os.environ.get(k) for k in
             ("QUANTMIX_PEOPLE_JSON", "QUANTMIX_PROFILE_JSON", "QUANTMIX_REPOSITORY",
              "GH_TOKEN", "QUANTMIX_STATE_KEY")}
os.environ.update({"QUANTMIX_REPOSITORY": "owner/repo", "GH_TOKEN": "token",
                   "QUANTMIX_STATE_KEY": KEY})
os.environ.pop("QUANTMIX_PEOPLE_JSON", None)
os.environ.pop("QUANTMIX_PROFILE_JSON", None)
try:
    loaded = load_people()
    must(len(loaded) == 2, "명부에 있는 사람들을 읽는다", str(len(loaded)))
    must({p["id"] for p in loaded} == {"minsu", "jihye"}, "두 사람 다")

    # 손으로 넣은 목록과 겹치면 손으로 넣은 쪽이 이기고, 나머지는 살아남는다
    manual = [dict(build_signup("minsu", "관리자지정", "111111111", CFG))]
    os.environ["QUANTMIX_PEOPLE_JSON"] = json.dumps(manual, ensure_ascii=False)
    merged = load_people()
    must(len(merged) == 2, "겹쳐도 사람 수가 맞는다", str(len(merged)))
    must(next(p for p in merged if p["id"] == "minsu")["label"] == "관리자지정",
         "손으로 넣은 쪽이 이긴다")
    must(any(p["id"] == "jihye" for p in merged),
         "겹치지 않은 사람은 그대로 받는다 (한 명 때문에 전부 멈추면 안 된다)")

    # 명부가 고장 나도 손으로 넣은 사람은 받아야 한다
    FakeStore.broken = True
    survived = load_people()
    must(len(survived) == 1 and survived[0]["id"] == "minsu",
         "명부가 고장 나도 기존 사람은 알림을 받는다", str(len(survived)))
    FakeStore.broken = False
finally:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
