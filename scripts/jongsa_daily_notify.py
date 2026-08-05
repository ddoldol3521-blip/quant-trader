"""종사종팔 V5 일일 알림 — 예약 실행용 진입점.

메시지를 만드는 일은 src/jongsa_notify.py가 한다. 앱의 '알림' 탭에서도
같은 코드를 써야 테스트 발송과 실제 발송이 어긋나지 않기 때문이다.

    JONGSA_DRY_RUN=1   보내지 않고 화면에만 출력

나머지 환경변수와 설정을 읽는 순서는 src/jongsa_notify.py 설명 참고.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jongsa_notify import build_message, env, send_now


def main():
    if env("JONGSA_DRY_RUN") == "1":
        print(build_message())
        return

    try:
        msg = send_now()
    except (FileNotFoundError, ValueError) as e:
        # 어디서 돌리는 중이냐에 따라 할 일이 달라서 두 경우를 다 알려준다.
        raise SystemExit(
            f"보내지 못했습니다 — {e}\n"
            "내 PC라면: 종사종팔 앱의 '🔔 알림' 탭에서 봇 토큰과 chat_id를 저장하세요.\n"
            "GitHub Actions라면: Settings → Secrets and variables → Actions 에 등록하세요."
        ) from e

    print("보냈습니다.")
    print(msg)


if __name__ == "__main__":
    main()
