"""Optional local UI hooks; public Streamlit sessions never read owner secrets."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def merge_sent_guides(st):
    if not (ROOT / "jongsa_cloud.json").exists():
        return
    now = datetime.now(timezone.utc).timestamp()
    if now - st.session_state.get("cloud_guides_checked_at", 0) < 300:
        return
    try:
        from src.quantmix_cloud_sync import cloud_order_guides
        cloud = cloud_order_guides()
        rows = {str(row["날짜"]): row for row in st.session_state.order_guides}
        for row in cloud:
            rows[row["날짜"]] = {"날짜": datetime.fromisoformat(row["날짜"]).date(),
                                  "수량": row["수량"]}
        st.session_state.order_guides = [rows[day] for day in sorted(rows)]
        # Keep the local on-demand Telegram bot on the same frozen quantities.
        (ROOT / "jongsa_order_guides.json").write_text(
            json.dumps(st.session_state.order_guides, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8")
        st.session_state.cloud_guides_error = False
    except Exception:
        st.session_state.cloud_guides_error = True
    st.session_state.cloud_guides_checked_at = now
    if st.session_state.get("cloud_guides_error"):
        st.warning("서버에서 보낸 주문 수량을 동기화하지 못했습니다. 텔레그램 주문표와 대조해 주세요.")


def render_cloud_panel(st):
    if not (ROOT / "jongsa_cloud.json").exists():
        return
    st.markdown("### ☁️ PC를 꺼도 받는 자동 알림")
    try:
        from src.quantmix_cloud_sync import sync_profile
        sync_profile()
        st.success("서버 알림 연결 · 미국 거래일 한국 오후 1시·오후 7시 각각 1회 예약")
        st.caption("설정·입출금·매수 체결 기록의 변경사항을 서버에 동기화했습니다. "
                   "미국 휴장일 제외 · 실행이 지연될 수 있습니다. 증권사 잔고 자동 연동은 아닙니다.")
    except Exception:
        st.warning("서버 설정 동기화 실패: 서버는 마지막으로 동기화한 설정을 사용합니다. "
                   "지금 바꾼 금액/체결 기록으로 주문하기 전 연결을 확인하세요.")
    st.caption("아래 ‘주문’ 답장 기능은 PC 실행 중에만 되지만, 위 정기 발송은 PC와 무관합니다.")
    st.link_button("서버 알림 실행 기록", "https://github.com/ddoldol3521-blip/quant-trader/actions/workflows/jongsa-daily.yml")
    st.divider()
