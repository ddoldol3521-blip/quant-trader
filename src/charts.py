"""자산 곡선 등 matplotlib 기반 차트."""

import matplotlib.pyplot as plt
import pandas as pd

from src.plotting import setup_korean_font


def equity_curve_figure(equity_curve: pd.Series, title: str = "자산 곡선", currency: str = "원"):
    """백테스트 자산 곡선 Figure를 만들어 반환한다 (Streamlit st.pyplot용)."""
    setup_korean_font()
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(equity_curve.index, equity_curve.values, linewidth=1.5)
    ax.set_title(title)
    ax.set_ylabel(f"평가금액({currency})")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig
