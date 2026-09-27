"""기본 설정과 프리셋이 어긋나지 않는지 검사.

    .venv\\Scripts\\python.exe scripts\\verify_presets.py

겪은 일 (2026-09-28).

  화면:  "현재 실제 적용: 균형형 ⭐ 추천"
  실제:  목표 2.75% / 16일 아닌 10일 / 사다리 3칸 / 손실 리셋 없음

기본 설정(DEFAULT_CONFIG)이 어떤 프리셋과도 안 맞는 값이었는데,
matching_preset_name() 이 '맞는 게 없으면 균형형' 을 돌려주는 바람에
균형형 이름표가 붙었다. 처음 들어온 사람은 균형형 성과표(37.16% / -40.40%)를
보면서 전혀 다른 설정으로 계산된 주문을 받는다. 돈이 걸린 화면이라 그냥 둘 수 없다.

여기서 지키는 것
  1. DEFAULT_CONFIG 의 전략값 = 균형형 프리셋
  2. matching_preset_name() 은 맞는 게 없으면 '균형형' 이라고 거짓말하지 않는다
  3. '직접 설정' 라벨은 PRESETS 에 없다 (selectbox 에 넣으면 터지므로)
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jongsa_live import DEFAULT_CONFIG, PRESETS

fail = 0


def must(ok: bool, msg: str, got: str = "") -> None:
    global fail
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}: {msg}" + (f"  -- {got}" if got else ""))


print("기본 설정 · 프리셋 대조\n")

BASE = "균형형 ⭐ 추천"
must(BASE in PRESETS, f"기준 프리셋이 있다: {BASE}")
preset = PRESETS.get(BASE, {})

KEYS = ("daily_buy_pct", "target_return", "stop_days", "sell_day_buy_mode",
        "loss_reset_pct", "loss_reset_threshold_pct", "ladder_rungs",
        "ladder_step", "buy_range_pct")

print(f"\n[기본 설정이 {BASE} 과 같은가]")
for k in KEYS:
    a, b = DEFAULT_CONFIG.get(k), preset.get(k)
    same = (abs(float(a) - float(b)) < 1e-9
            if isinstance(a, (int, float)) and isinstance(b, (int, float))
            else a == b)
    must(same, f"{k}", f"기본 {a} vs 프리셋 {b}")

print("\n[맞는 프리셋이 없을 때]")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "_app", Path(__file__).resolve().parent.parent / "jongsa_app.py")
# jongsa_app 은 streamlit 을 불러오므로 통째로 실행하지 않고 소스만 본다.
src = (Path(__file__).resolve().parent.parent / "jongsa_app.py").read_text(encoding="utf-8")

must("CUSTOM_PRESET_LABEL" in src, "'직접 설정' 라벨 상수가 있다")
must("return CUSTOM_PRESET_LABEL" in src,
     "맞는 게 없으면 그 라벨을 돌려준다 (균형형이라고 하지 않는다)")
must('return "균형형 ⭐ 추천"' not in src,
     "'맞는 게 없으면 균형형' 이라는 옛 동작이 남아 있지 않다")

label_line = [l for l in src.splitlines() if l.startswith("CUSTOM_PRESET_LABEL")]
must(bool(label_line), "라벨 값을 찾았다")
if label_line:
    label = label_line[0].split("=", 1)[1].strip().strip('"').strip("'")
    must(label not in PRESETS,
         "그 라벨은 PRESETS 에 없다 (selectbox 에 넣으면 터지므로)", label)
    must("_is_custom" in src and "st.session_state.quick_preset = _current_preset" not in src.replace(
         "            if not _is_custom:\n                st.session_state.quick_preset = _current_preset", ""),
         "직접 설정일 때는 selectbox 상태를 건드리지 않는다")

print()
print("모두 통과" if fail == 0 else f"{fail}개 실패")
sys.exit(0 if fail == 0 else 1)
