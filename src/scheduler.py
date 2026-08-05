"""윈도우 작업 스케줄러(schtasks) 연동 — 매일 정해진 시간에 자동 스캔+알림."""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_CONFIG_PATH = PROJECT_ROOT / "notify_schedule.json"
TASK_NAME = "QuantTraderDailyScan"

# 종사종팔 일일 알림은 스캔 알림과 시간·주기가 달라서 작업을 따로 둔다.
JONGSA_TASK_NAME = "JongsaDailyNotify"
JONGSA_CONFIG_PATH = PROJECT_ROOT / "jongsa_notify.json"


def save_schedule_config(config: dict) -> None:
    """알림 예약 설정(시간, 시장, limit, min_match)을 로컬 파일에 저장한다."""
    with open(SCHEDULE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_schedule_config() -> dict:
    """저장된 알림 예약 설정을 불러온다. 없으면 빈 딕셔너리."""
    if not SCHEDULE_CONFIG_PATH.exists():
        return {}
    with open(SCHEDULE_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _python_exe() -> str:
    """콘솔 창이 안 뜨는 pythonw.exe를 우선 쓰고, 없으면 현재 파이썬을 쓴다."""
    venv_pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pythonw.exists():
        return str(venv_pythonw)
    return sys.executable


def _run(cmd: list):
    """schtasks 실행. 콘솔 인코딩이 상황에 따라 달라서 errors='replace'로 크래시를 막는다."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="cp949", errors="replace")


def _create_task(task_name: str, command: str, time_str: str, weekdays_only: bool = False) -> None:
    """schtasks에 매일(또는 평일만) 도는 작업을 등록한다. 같은 이름이 있으면 덮어쓴다."""
    when = ["/sc", "weekly", "/d", "MON,TUE,WED,THU,FRI"] if weekdays_only else ["/sc", "daily"]
    result = _run(["schtasks", "/create", "/tn", task_name, "/tr", command, *when,
                   "/st", time_str, "/f"])
    if result.returncode != 0:
        raise RuntimeError(f"작업 스케줄러 등록 실패: {result.stderr or result.stdout}")


def _delete_task(task_name: str) -> None:
    """예약 작업을 삭제한다. 원래 없었어도 에러 내지 않는다."""
    result = _run(["schtasks", "/delete", "/tn", task_name, "/f"])
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        not_found = "찾을 수 없" in combined or "cannot find" in combined.lower()
        if not not_found:
            raise RuntimeError(f"작업 스케줄러 삭제 실패: {combined}")


def _query_task(task_name: str) -> dict:
    """예약 작업이 등록돼 있는지 확인한다."""
    result = _run(["schtasks", "/query", "/tn", task_name, "/fo", "LIST"])
    if result.returncode != 0:
        return {"exists": False}
    return {"exists": True, "raw": result.stdout}


def register_windows_task(time_str: str, market: str, limit: int, min_match: int, region: str = "한국") -> None:
    """매일 지정 시간에 실행되는 스캔+알림 작업을 등록한다 (이미 있으면 갱신).

    time_str: "HH:MM" 형식
    """
    script_path = PROJECT_ROOT / "scripts" / "daily_scan_notify.py"
    tr = (
        f'"{_python_exe()}" "{script_path}" --market {market} --region {region} '
        f"--limit {limit} --min-match {min_match}"
    )
    _create_task(TASK_NAME, tr, time_str)


def remove_windows_task() -> None:
    """등록된 예약 작업을 삭제한다. 원래 없었어도 에러 내지 않는다."""
    _delete_task(TASK_NAME)


def get_task_status() -> dict:
    """등록된 예약 작업이 있는지 확인한다."""
    return _query_task(TASK_NAME)


# ---------------------------------------------------------------- 종사종팔 알림


def save_jongsa_notify_config(config: dict) -> None:
    """종사종팔 알림 설정(시각, 앱 주소)을 저장한다."""
    with open(JONGSA_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_jongsa_notify_config() -> dict:
    """저장된 종사종팔 알림 설정을 불러온다. 없으면 기본값."""
    if not JONGSA_CONFIG_PATH.exists():
        return {"time": "21:00", "app_url": ""}
    try:
        with open(JONGSA_CONFIG_PATH, encoding="utf-8") as f:
            return {"time": "21:00", "app_url": "", **json.load(f)}
    except (json.JSONDecodeError, OSError):
        return {"time": "21:00", "app_url": ""}


def register_jongsa_task(time_str: str) -> None:
    """매 평일 지정 시각에 종사종팔 알림을 보내는 작업을 등록한다.

    주말에는 미국장이 안 열려 금요일과 같은 내용이 또 오므로 평일만 돌린다.
    """
    script_path = PROJECT_ROOT / "scripts" / "jongsa_daily_notify.py"
    _create_task(
        JONGSA_TASK_NAME, f'"{_python_exe()}" "{script_path}"', time_str, weekdays_only=True
    )


def remove_jongsa_task() -> None:
    """종사종팔 알림 예약을 해제한다."""
    _delete_task(JONGSA_TASK_NAME)


def get_jongsa_task_status() -> dict:
    """종사종팔 알림 예약이 걸려 있는지 확인한다."""
    return _query_task(JONGSA_TASK_NAME)
