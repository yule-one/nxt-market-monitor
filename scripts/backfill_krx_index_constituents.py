from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.krx_index_constituents import (
    KRX_INDEX_SPECS,
    KrxIndexConstituentClient,
    build_index_constituent_histories,
)


def _iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜 형식은 YYYY-MM-DD입니다.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "KRX 공식 지수 구성종목과 변경내역으로 NXT 거래일별 "
            "KOSPI200·KOSDAQ150 구성종목을 DB에 적재합니다."
        )
    )
    parser.add_argument("--start", type=_iso_date, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=_iso_date)
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 적재된 거래일도 다시 계산합니다.",
    )
    args = parser.parse_args()

    store = HistoricalMarketStore()
    end_date = args.end or store.latest_final_date()
    if end_date is None:
        parser.error("확정 NXT 거래일이 DB에 없습니다.")
    summaries = store.list_nxt_eligibility_summaries(args.start, end_date)
    target_dates = sorted(item.trade_date for item in summaries)
    if not target_dates:
        print("적재할 NXT 거래일이 없습니다.")
        return 0

    targets_by_index: dict[str, list[date]] = {}
    for index_name in KRX_INDEX_SPECS:
        existing = set() if args.force else store.index_constituent_dates(index_name)
        missing = [item for item in target_dates if item not in existing]
        if missing:
            targets_by_index[index_name] = missing
    if not targets_by_index:
        print("KRX 지수 구성종목 누락 거래일이 없습니다.")
        return 0

    client = KrxIndexConstituentClient()
    history = build_index_constituent_histories(
        client,
        targets_by_index,
        anchor_date=target_dates[-1],
    )
    stored = store.replace_krx_index_constituents(history)
    details = " · ".join(
        f"{index_name} {len(dates):,}일"
        for index_name, dates in targets_by_index.items()
    )
    print(
        f"KRX 지수 구성종목 적재 완료: {details} · 구성종목 행 {stored:,}건 "
        f"· 기준일 {target_dates[-1]:%Y-%m-%d}"
    )
    if client.ssl_fallback_used:
        print("주의: KRX 지수 사이트 연결에 SSL 검증 폴백이 사용되었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
