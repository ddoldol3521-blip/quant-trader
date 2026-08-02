"""HTS 스타일 인터랙티브 캔들차트 (plotly)."""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.indicators import bollinger_bands, disparity, macd, rsi, stochastic

AVAILABLE_OSCILLATORS = ["RSI", "MACD", "스토캐스틱", "이격도", "모멘텀"]

MA_COLORS = {
    5: "#e74c3c",
    10: "#e67e22",
    20: "#f1c40f",
    60: "#2ecc71",
    120: "#3498db",
    240: "#9b59b6",
}


def strategy_chart_config(strategy_names: list) -> dict:
    """일치한 전략들에 맞춰 어떤 보조지표를 보여줄지 자동 결정한다.

    캔들과 거래량은 항상 동일하게 나오고, 여기서 정하는 건 그 외 부분이다.
    """
    oscillators = []
    show_bollinger = False
    show_breakout_target = False

    for name in strategy_names:
        if name == "rsi" and "RSI" not in oscillators:
            oscillators.append("RSI")
        elif name == "macd" and "MACD" not in oscillators:
            oscillators.append("MACD")
        elif name == "stochastic" and "스토캐스틱" not in oscillators:
            oscillators.append("스토캐스틱")
        elif name == "disparity" and "이격도" not in oscillators:
            oscillators.append("이격도")
        elif name == "momentum" and "모멘텀" not in oscillators:
            oscillators.append("모멘텀")
        elif name == "bollinger":
            show_bollinger = True
        elif name == "volatility_breakout":
            show_breakout_target = True

    return {
        "oscillators": oscillators,
        "show_bollinger": show_bollinger,
        "show_breakout_target": show_breakout_target,
    }


def build_price_chart(
    df: pd.DataFrame,
    mas: list = None,
    show_bollinger: bool = False,
    show_volume: bool = True,
    oscillators: list = None,
    show_breakout_target: bool = False,
    unit_label: str = "일",
    title: str = "",
):
    """캔들차트 + 이동평균 + (선택) 볼린저밴드/거래량/보조지표를 그린다.

    unit_label: 이동평균 범례에 쓸 단위 ('일'/'주'/'월')
    """
    mas = mas or []
    oscillators = oscillators or []

    row_count = 1 + (1 if show_volume else 0) + len(oscillators)
    heights = [0.55] + [0.45 / (row_count - 1)] * (row_count - 1) if row_count > 1 else [1.0]

    fig = make_subplots(
        rows=row_count,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=heights,
    )

    # 1행: 캔들
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name="가격",
            increasing_line_color="#e74c3c",
            decreasing_line_color="#3498db",
        ),
        row=1,
        col=1,
    )

    for period in mas:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["Close"].rolling(period).mean(),
                name=f"{period}{unit_label}선",
                line=dict(width=1, color=MA_COLORS.get(period)),
            ),
            row=1,
            col=1,
        )

    if show_bollinger:
        ma, upper, lower = bollinger_bands(df["Close"])
        for series, name, dash in [(upper, "볼린저 상단", "dot"), (lower, "볼린저 하단", "dot")]:
            fig.add_trace(
                go.Scatter(x=df.index, y=series, name=name, line=dict(width=1, dash=dash, color="#95a5a6")),
                row=1,
                col=1,
            )

    if show_breakout_target:
        # 순환 import를 피하려고 함수 안에서 import
        from src.strategies import volatility_breakout

        vb_df = volatility_breakout.generate_signals(df)
        fig.add_trace(
            go.Scatter(
                x=vb_df.index,
                y=vb_df["target"],
                name="돌파 목표가",
                line=dict(width=1, dash="dash", color="#e67e22"),
            ),
            row=1,
            col=1,
        )

    current_row = 2

    if show_volume and "Volume" in df.columns:
        colors = ["#e74c3c" if c >= o else "#3498db" for o, c in zip(df["Open"], df["Close"])]
        fig.add_trace(
            go.Bar(x=df.index, y=df["Volume"], name="거래량", marker_color=colors, showlegend=False),
            row=current_row,
            col=1,
        )
        fig.update_yaxes(title_text="거래량", row=current_row, col=1)
        current_row += 1

    for osc in oscillators:
        if osc == "RSI":
            fig.add_trace(go.Scatter(x=df.index, y=rsi(df["Close"]), name="RSI"), row=current_row, col=1)
            fig.add_hline(y=70, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
            fig.add_hline(y=30, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
        elif osc == "MACD":
            macd_line, signal_line = macd(df["Close"])
            fig.add_trace(go.Scatter(x=df.index, y=macd_line, name="MACD"), row=current_row, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=signal_line, name="시그널"), row=current_row, col=1)
        elif osc == "스토캐스틱":
            k, d = stochastic(df["High"], df["Low"], df["Close"])
            fig.add_trace(go.Scatter(x=df.index, y=k, name="%K"), row=current_row, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=d, name="%D"), row=current_row, col=1)
            fig.add_hline(y=80, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
            fig.add_hline(y=20, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
        elif osc == "이격도":
            fig.add_trace(go.Scatter(x=df.index, y=disparity(df["Close"]), name="이격도"), row=current_row, col=1)
            fig.add_hline(y=100, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
        elif osc == "모멘텀":
            fig.add_trace(
                go.Scatter(x=df.index, y=df["Close"].pct_change(20) * 100, name="20봉 모멘텀(%)"),
                row=current_row,
                col=1,
            )
            fig.add_hline(y=0, line=dict(dash="dot", color="#95a5a6"), row=current_row, col=1)
        fig.update_yaxes(title_text=osc, row=current_row, col=1)
        current_row += 1

    # 거래일이 아닌 날(주말·공휴일)은 x축에서 건너뛰어 캔들 사이 빈칸을 없앤다
    if len(df) > 1:
        all_days = pd.date_range(df.index.min(), df.index.max(), freq="D")
        missing_days = all_days.difference(df.index)
        if len(missing_days) > 0:
            fig.update_xaxes(rangebreaks=[dict(values=missing_days)])

    # 맨 아래 행에만 기간 조절용 미니 스크롤바를 남긴다
    for r in range(1, row_count + 1):
        fig.update_xaxes(rangeslider_visible=(r == row_count), row=r, col=1)

    fig.update_layout(
        title=title,
        height=300 + 150 * (row_count - 1),
        margin=dict(l=40, r=20, t=50 if title else 20, b=20),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
    )
    return fig
