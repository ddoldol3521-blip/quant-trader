"""신청서가 서버가 읽는 모양과 맞는지 검사.

    .venv\\Scripts\\python.exe scripts\\verify_signup.py

앱이 만들어 준 신청서를 관리자가 GitHub Secret 에 붙여넣는다. 그 모양이
서버(load_people)가 읽는 것과 조금만 달라도 **등록해도 알림이 안 온다.**
그때 어디가 틀렸는지 찾기 어렵다. 그래서 여기서 실제로 통과시켜 본다.

여기서 지키는 것
  1. 앱이 만든 신청서를 서버가 그대로 받아들인다
  2. 봇 토큰은 신청서에 들어가지 않는다 (남의 비밀번호를 받지 않는다)
  3. 잘못된 입력은 한국어로 이유를 알려준다
  4. 같은 사람을 두 번 등록하면 막는다
"""

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jongsa_live import DEFAULT_CONFIG
from src.quantmix_cloud import CloudError, load_people, state_file_for
from src.quantmix_signup import (build_signup, check_chat_id, check_person_id,
                                 merge_into_people, signup_text)

fail = 0


def must(ok: bool, msg: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {msg}" + (f"  -- {got}" if got else ""))


print("자동 알림 신청서 검사\n")

# ── 1. 입력 검사가 한국어로 이유를 말하는가 ──────────────────────────
print("[입력 검사]")
must(check_chat_id("123456789") == "", "정상 번호는 통과")
must(check_chat_id("-1001234567890") == "", "그룹방 번호(음수)도 통과")
must("숫자 번호" in check_chat_id("@minsu"), "@아이디를 넣으면 이유를 알려준다",
     check_chat_id("@minsu"))
must(check_chat_id("abc") != "", "글자는 거부")
must(check_chat_id("") != "", "빈 칸은 거부")

must(check_person_id("minsu") == "", "영문 이름은 통과")
must(check_person_id("jg2") == "", "영문+숫자도 통과")
must("한글" in check_person_id("민수"), "한글 이름은 이유를 알려준다", check_person_id("민수"))
must(check_person_id("min su") != "", "빈칸이 들어가면 거부")
must(check_person_id("../x") != "", "경로 글자는 거부")

# ── 2. 앱이 만든 신청서를 서버가 받아들이는가 ────────────────────────
print("\n[앱 -> 서버]")
cfg = {**DEFAULT_CONFIG, "initial_cash": 10000.0, "start_date": "2026-10-01"}
entry = build_signup("minsu", "민수", "123456789", cfg)

must(entry["id"] == "minsu" and entry["chat_id"] == "123456789", "신청서에 이름과 번호가 들어간다")
must(entry["label"] == "민수", "표시용 한글 이름은 그대로 (파일 이름이 아니라 괜찮다)")
must(entry["profile"]["config"]["initial_cash"] == 10000.0, "내 투자금이 들어간다")
must(entry["profile"]["config"]["start_date"] == "2026-10-01", "내 시작일이 들어간다")
must(set(DEFAULT_CONFIG) <= set(entry["profile"]["config"]),
     "설정이 빠짐없이 들어간다 (빠지면 서버가 INCOMPLETE_PROFILE 로 거절한다)")

# 진짜 검사: 서버가 실제로 읽어 들이는가
raw = json.dumps([entry], ensure_ascii=False)
old = os.environ.get("QUANTMIX_PEOPLE_JSON"), os.environ.get("QUANTMIX_PROFILE_JSON")
os.environ["QUANTMIX_PEOPLE_JSON"] = raw
os.environ.pop("QUANTMIX_PROFILE_JSON", None)
try:
    people = load_people()
    must(len(people) == 1, "서버가 신청서를 그대로 읽는다")
    must(people[0]["label"] == "민수", "표시 이름이 전달된다")
    must(state_file_for(people[0]["id"]) == "outbox-minsu.enc",
         "전용 발송함이 생긴다", state_file_for(people[0]["id"]))
except CloudError as error:
    must(False, "서버가 신청서를 그대로 읽는다", f"거절됨: {error}")
finally:
    for key, value in zip(("QUANTMIX_PEOPLE_JSON", "QUANTMIX_PROFILE_JSON"), old):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

# ── 3. 봇 토큰이 섞여 들어가지 않는가 ────────────────────────────────
print("\n[토큰을 받지 않는다]")
text = signup_text(entry)
must("token" not in text.lower(), "신청서에 token 이라는 항목이 없다")
must("bot" not in text.lower(), "신청서에 bot 관련 항목이 없다")

app = (Path(__file__).resolve().parent.parent / "jongsa_app.py").read_text(encoding="utf-8")
shared = app.split("if is_shared_server():", 1)[1].split("\nelse:", 1)[0]
must("봇 토큰은 필요 없습니다" in shared, "공유 서버 화면이 '토큰 필요 없음' 을 알린다")

# 진짜 확인할 것은 '입력칸' 이다. 주석과 안내문에는 토큰 이야기가 나와도 된다.
# (오히려 왜 안 받는지 적어 두는 편이 낫다)
input_labels = [
    line.split("st.text_input", 1)[1][:80]
    for line in shared.splitlines() if "st.text_input" in line
]
must(bool(input_labels), "입력칸이 있긴 하다", f"{len(input_labels)}개")
must(not any("토큰" in label or "token" in label.lower() for label in input_labels),
     "그중 토큰을 받는 칸은 없다")
must(not any("password" in label for label in input_labels),
     "비밀번호로 가려 받는 칸도 없다 (받을 비밀이 없어야 정상이다)")

# ── 4. 중복 등록을 막는가 ────────────────────────────────────────────
print("\n[중복 막기]")
merged = merge_into_people("", entry)
must(len(json.loads(merged)) == 1, "빈 목록에 한 사람을 더한다")

second = build_signup("jihye", "지혜", "987654321", cfg)
merged2 = merge_into_people(merged, second)
must(len(json.loads(merged2)) == 2, "두 번째 사람을 더한다")

for dup, why in ((entry, "같은 이름"), (build_signup("other", "", "123456789", cfg), "같은 번호")):
    try:
        merge_into_people(merged2, dup)
        must(False, f"{why}을 막는다", "통과시켜 버림")
    except ValueError as error:
        must(True, f"{why}을 막는다", str(error))

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
