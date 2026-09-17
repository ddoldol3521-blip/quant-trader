"""Stdlib-only failure alert, including dependency-install failures."""
import json
import os
import urllib.request


def main():
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Failure alert unavailable: Telegram credentials missing")
        return 1
    run = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    text = ("⚠️ 퀀트믹스 서버 알림 확인 필요\n"
            "오늘 주문표 계산 또는 전송을 완료하지 못했습니다. 이전 주문표를 재사용하지 마세요.\n"
            "주문표가 이미 도착했다면 중복 주문하지 말고 내용을 확인해 주세요.\n"
            f"실행 기록: https://github.com/{repo}/actions/runs/{run}")
    data = json.dumps({"chat_id": chat, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 0 if json.load(response).get("ok") else 1
    except Exception:
        print("Failure alert could not be delivered; see Actions failure status")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
