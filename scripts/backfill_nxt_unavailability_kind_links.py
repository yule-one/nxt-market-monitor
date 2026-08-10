from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cache import ResponseCache
from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.kind_client import KindClient
from src.nxt_kind_links import (
    KIND_LINK_CATEGORIES,
    KIND_LINK_LOOKBACK_DAYS,
    match_unavailability_events,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NXT 거래불가 지정·해제 이력에 KIND 공시 원문을 연결합니다."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "history.db",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=KIND_LINK_LOOKBACK_DAYS,
        help="NXT 이벤트일 이전 KIND 공시 조회 일수",
    )
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("종료일은 시작일보다 빠를 수 없습니다.")
    if args.lookback_days < 1:
        parser.error("조회 일수는 1일 이상이어야 합니다.")

    store = HistoricalMarketStore(args.database.resolve())
    events = store.list_nxt_unavailability_events(args.start, args.end)
    disclosures = KindClient(ResponseCache()).fetch_disclosures(
        max(date(2000, 1, 1), args.start - timedelta(days=args.lookback_days)),
        args.end,
        categories=KIND_LINK_CATEGORIES,
        force_refresh=args.force_refresh,
        max_workers=3,
    )
    links = match_unavailability_events(
        events,
        disclosures,
        lookback_days=args.lookback_days,
    )
    stored = store.replace_nxt_unavailability_kind_links(
        args.start,
        args.end,
        links,
    )

    matched_keys = {
        (item.event_date, item.stock_code, item.event_type) for item in links
    }
    totals = Counter((item.unavailable_reason, item.event_type) for item in events)
    matched = Counter(
        (item.unavailable_reason, item.event_type)
        for item in events
        if (item.event_date, item.stock_code, item.event_type) in matched_keys
    )
    print(
        f"KIND 원문 연결 완료: 이벤트 {len(events):,}건, "
        f"연결 {stored:,}건, 미연결 {len(events) - stored:,}건"
    )
    for key in sorted(totals):
        count = totals[key]
        linked = matched[key]
        print(
            f"- {key[0]} / {key[1]}: {linked:,}/{count:,}건 "
            f"({linked / count:.1%})"
        )


if __name__ == "__main__":
    main()
