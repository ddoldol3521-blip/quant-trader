"""전체 스캔 후 결과를 텔레그램으로 보내는 진입점 (작업 스케줄러가 이걸 실행한다)."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.screening import scan as scan_stocks
from src.strategies import STRATEGIES
from src.telegram_notify import send_telegram_message


def build_message(matches: list, market_label: str, min_match: int, region: str = "한국") -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [
        f"[퀀트 트레이더] {today} {region} 주식 스캔 결과",
        f"대상: {market_label} / 최소 {min_match}개 전략 일치",
        "",
    ]
    if not matches:
        lines.append("조건에 맞는 종목이 없습니다.")
    else:
        lines.append(f"총 {len(matches)}개 종목:")
        for m in matches:
            buys = ", ".join(m["buy_strategies"])
            lines.append(f"- {m['name']}({m['code']}) [{len(m['buy_strategies'])}개] {buys}")
    lines.append("")
    lines.append("※ 과거 통계 기반 참고자료입니다. 자동 주문되지 않으며, 최종 판단은 본인이 하세요.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="전체 스캔 후 텔레그램으로 결과 전송")
    parser.add_argument("--market", default="KOSPI,KOSDAQ", help="시장, 콤마로 여러 개")
    parser.add_argument("--region", default="한국", choices=["한국", "미국"], help="한국 / 미국")
    parser.add_argument("--limit", type=int, default=100, help="시장별 상위 몇 개")
    parser.add_argument("--min-match", type=int, default=1, help="최소 몇 개 전략이 일치해야 알릴지")
    parser.add_argument("--dry-run", action="store_true", help="전송 없이 콘솔에만 출력")
    args = parser.parse_args()

    # 미국 티커는 대소문자를 그대로 두어야 하지만 시장 이름은 대문자로 통일한다
    markets = [m.strip().upper() for m in args.market.split(",")]
    strategy_names = list(STRATEGIES.keys())

    results = scan_stocks(
        markets, strategy_names, limit=args.limit, show_progress=False, region=args.region
    )

    matches = []
    for r in results:
        buys = [s for s, sig in r["signals"].items() if sig == "BUY"]
        if len(buys) >= args.min_match:
            matches.append({"code": r["code"], "name": r["name"], "buy_strategies": buys})

    matches.sort(key=lambda m: len(m["buy_strategies"]), reverse=True)
    message = build_message(matches, "+".join(markets), args.min_match, args.region)

    if args.dry_run:
        print(message)
    else:
        send_telegram_message(message)
        print(f"전송 완료: {len(matches)}개 종목")


if __name__ == "__main__":
    main()
