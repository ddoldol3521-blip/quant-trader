"""한글 경로 때문에 생기는 SSL 인증서 오류를 우회한다.

이 프로젝트는 'D:\\1. 재건정보\\ai빌더\\...' 같은 한글 경로에 설치되어 있는데,
yfinance가 내부적으로 쓰는 curl_cffi가 한글이 섞인 경로의 인증서 파일(cacert.pem)을
읽지 못해서 'curl: (77) error adding trust anchors' 오류가 난다.

그래서 인증서를 영문 경로(임시 폴더)로 한 번 복사해두고, 관련 환경변수가 그 사본을
가리키도록 바꾼다. yfinance를 import 하기 전에 apply()를 먼저 호출해야 한다.
"""

import os
import shutil
import tempfile
from pathlib import Path

_applied = False


def apply() -> str | None:
    """인증서를 영문 경로로 복사하고 환경변수를 설정한다. 복사된 경로를 반환."""
    global _applied
    if _applied:
        return os.environ.get("CURL_CA_BUNDLE")

    try:
        import certifi

        src = Path(certifi.where())
        if not src.exists():
            return None

        # 원래 경로가 전부 ASCII면 굳이 복사할 필요가 없다
        if str(src).isascii():
            _applied = True
            return str(src)

        dest_dir = Path(tempfile.gettempdir()) / "quant_trader_certs"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "cacert.pem"

        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dest)

        if not str(dest).isascii():
            # 사용자 이름에도 한글이 있는 경우 — 여기까지 오면 우회 불가
            return None

        os.environ["SSL_CERT_FILE"] = str(dest)
        os.environ["CURL_CA_BUNDLE"] = str(dest)
        os.environ["REQUESTS_CA_BUNDLE"] = str(dest)
        _applied = True
        return str(dest)
    except Exception:
        return None
