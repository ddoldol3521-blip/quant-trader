"""텔레그램 봇으로 스캔 결과를 보내는 모듈."""

import json
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "telegram_config.json"
MAX_MESSAGE_LEN = 3500  # 텔레그램 제한(4096자)보다 여유 있게


def save_telegram_config(bot_token: str, chat_id: str) -> None:
    """봇 토큰과 chat_id를 telegram_config.json에 저장한다."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"bot_token": bot_token, "chat_id": chat_id}, f, ensure_ascii=False, indent=2)


def load_telegram_config() -> tuple:
    """telegram_config.json에서 봇 토큰과 chat_id를 읽어온다."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH.name}가 없습니다. 웹앱의 '텔레그램 알림' 탭에서 봇 토큰과 chat_id를 입력하고 저장하세요."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    token = cfg.get("bot_token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        raise ValueError("telegram_config.json에 실제 봇 토큰/chat_id가 채워져 있지 않습니다.")
    return token, chat_id


def find_chat_id(bot_token: str) -> str:
    """봇에게 보낸 최근 메시지에서 chat_id를 찾아준다."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"텔레그램 조회 실패 ({resp.status_code}): {resp.text}")
    data = resp.json()
    results = data.get("result", [])
    if not results:
        raise RuntimeError("받은 메시지가 없습니다. 텔레그램에서 본인 봇을 찾아 아무 메시지나 먼저 보내주세요.")
    return str(results[-1]["message"]["chat"]["id"])


def send_telegram_message(text: str, token: str = None, chat_id: str = None) -> None:
    """텔레그램으로 메시지를 보낸다. 너무 길면 여러 개로 나눠서 보낸다."""
    if not token or not chat_id:
        token, chat_id = load_telegram_config()

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i : i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [text]

    for chunk in chunks:
        resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"텔레그램 전송 실패 ({resp.status_code}): {resp.text}")
