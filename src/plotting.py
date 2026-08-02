"""matplotlib 한글 폰트 공용 설정."""

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")  # GUI 없이 파일/버퍼로만 그림


def setup_korean_font():
    """윈도우 기본 한글 폰트(맑은 고딕)를 쓰고, 마이너스 기호 깨짐을 막는다."""
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
