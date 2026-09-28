"""자동 알림 신청서 만들기.

여러 사람이 각자 자기 숫자로 텔레그램 알림을 받게 하는 흐름의 앞부분이다.

  친구  봇에게 /start -> 앱에서 자기 번호(chat_id)와 설정 입력
        -> [신청서 만들기] -> 나온 내용을 관리자에게 보냄
  관리자 그 내용을 GitHub Secret(QUANTMIX_PEOPLE_JSON)에 붙여넣음
        -> 다음 날부터 그 친구 폰으로 자동 발송

여기서 지키는 것.

**봇 토큰은 받지 않는다.** 봇 하나로 여러 사람에게 보낼 수 있으므로 친구가
자기 봇을 만들 이유가 없다. 토큰은 봇의 비밀번호라서, 남의 것을 받아
공개 저장소에 (암호화하더라도) 두면 기록이 영원히 남는다. 나중에 열쇠가
새면 그때까지 저장된 토큰이 전부 드러난다.

chat_id 는 비밀이 아니다. 대화방을 가리키는 번호일 뿐이고, 알려져도
그 사람 봇을 조종할 수 없다. 그래서 이것만 받는다.

**앱은 아무것도 저장하지 않는다.** 공유 서버에는 사람마다 따로 저장할 곳이
없다. 신청서를 글자로 만들어 보여주고, 옮기는 일은 사람이 한다. 그 한
단계가 문지기 역할도 한다 — 아무나 등록해서 봇을 쓰지 못한다.
"""
from __future__ import annotations

import json
import re

from src.jongsa_live import DEFAULT_CONFIG

# 텔레그램 chat_id. 개인 대화는 양수, 그룹은 음수라 앞에 -가 올 수 있다.
CHAT_ID_PATTERN = re.compile(r"-?\d{5,20}")
# 사람 구분용 이름. 발송함 파일 이름이 되므로 영문·숫자만 받는다.
PERSON_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")


def check_chat_id(text: str) -> str:
    """chat_id 가 번호 모양인지 본다. 틀리면 왜 틀렸는지 한국어로 알려준다."""
    value = (text or "").strip()
    if not value:
        return "번호를 입력해 주세요."
    if value.startswith("@"):
        return "@아이디가 아니라 **숫자 번호**가 필요합니다. 봇에게 /start 를 보내면 알 수 있습니다."
    if not CHAT_ID_PATTERN.fullmatch(value):
        return "숫자만 입력해 주세요 (예: 123456789)."
    return ""


def check_person_id(text: str) -> str:
    """사람 구분 이름 검사. 한글은 파일 이름이 되므로 받지 않는다."""
    value = (text or "").strip()
    if not value:
        return "이름을 입력해 주세요."
    if not PERSON_ID_PATTERN.fullmatch(value):
        return "영문·숫자만 쓸 수 있습니다 (예: minsu, jg2). 한글과 빈칸은 안 됩니다."
    return ""


def build_signup(person_id: str, label: str, chat_id: str, config: dict) -> dict:
    """관리자에게 보낼 신청서 한 사람 몫.

    quantmix_cloud.load_people() 이 읽는 모양과 똑같아야 한다. 관리자는 이걸
    목록에 넣기만 하면 된다.
    """
    for check, value in ((check_person_id, person_id), (check_chat_id, chat_id)):
        message = check(value)
        if message:
            raise ValueError(message)
    # 전략값은 기본값을 바탕으로 화면에서 정한 것만 덮어쓴다. 빠진 항목이
    # 있으면 서버가 INCOMPLETE_PROFILE 로 거절하므로 여기서 채워 둔다.
    merged = {**DEFAULT_CONFIG, **{k: v for k, v in (config or {}).items()
                                   if k in DEFAULT_CONFIG}}
    return {
        "id": person_id.strip(),
        "label": (label or "").strip(),
        "chat_id": chat_id.strip(),
        "profile": {
            "version": 1,
            "config": merged,
            "cash_flows": [],
            "actual_buy_fills": [],
            "guided_buy_qty": [],
            "price_overrides": {},
            "app_url": "",
        },
    }


def signup_text(entry: dict) -> str:
    """복사해서 보낼 글자. 사람이 옮기므로 읽기 좋게 들여쓴다."""
    return json.dumps(entry, ensure_ascii=False, indent=2)


def merge_into_people(existing_raw: str, entry: dict) -> str:
    """관리자가 쓰는 함수. 기존 목록에 한 사람을 더한다.

    같은 id 나 같은 chat_id 가 이미 있으면 **덮어쓰지 않고** 알려준다.
    덮어쓰면 그 사람의 발송함이 엉뚱한 설정과 이어져 수량이 틀어진다.
    """
    rows = []
    text = (existing_raw or "").strip()
    if text:
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError("기존 목록이 [ ] 모양이 아닙니다.")
    for row in rows:
        if row.get("id") == entry["id"]:
            raise ValueError(f"이미 있는 이름입니다: {entry['id']}")
        if str(row.get("chat_id")) == str(entry["chat_id"]):
            raise ValueError("이미 있는 번호입니다. 같은 사람이 두 번 등록되면 안 됩니다.")
    rows.append(entry)
    return json.dumps(rows, ensure_ascii=False, indent=2)
