"""윈도우 작업 스케줄러(schtasks) 연동 — 매일 정해진 시간에 자동 스캔+알림."""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_CONFIG_PATH = PROJECT_ROOT / "notify_schedule.json"
TASK_NAME = "QuantTraderDailyScan"


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


def register_windows_task(time_str: str, market: str, limit: int, min_match: int) -> None:
    """매일 지정 시간에 실행되는 스캔+알림 작업을 등록한다 (이미 있으면 갱신).

    time_str: "HH:MM" 형식
    """
    script_path = PROJECT_ROOT / "scripts" / "daily_scan_notify.py"
    tr = f'"{_python_exe()}" "{script_path}" --market {market} --limit {limit} --min-match {min_match}'

    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr, "/sc", "daily", "/st", time_str, "/f"]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"작업 스케줄러 등록 실패: {result.stderr or result.stdout}")


def remove_windows_task() -> None:
    """등록된 예약 작업을 삭제한다. 원래 없었어도 에러 내지 않는다."""
    result = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        not_found = "찾을 수 없" in combined or "cannot find" in combined.lower()
        if not not_found:
            raise RuntimeError(f"작업 스케줄러 삭제 실패: {combined}")


def get_task_status() -> dict:
    """등록된 예약 작업이 있는지 확인한다."""
    result = _run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"])
    if result.returncode != 0:
        return {"exists": False}
    return {"exists": True, "raw": result.stdout}
