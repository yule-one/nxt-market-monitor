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


def _date_value(raw: str) -> date:
    return date.fromisoformat(raw)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="저장된 NXT 일별 OHLC로 상·하한가 0~3틱 근접 이력을 생성합니다."
    )
    parser.add_argument(
        "--start-date",
        type=_date_value,
        default=NXT_LAUNCH_DATE,
    )
    parser.add_argument(
        "--end-date",
        type=_date_value,
        default=date.today(),
    )
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("종료일은 시작일보다 빠를 수 없습니다.")

    store = HistoricalMarketStore()
    inserted = store.rebuild_nxt_limit_proximity_hits(
        args.start_date,
        args.end_date,
    )
    coverage = store.nxt_limit_proximity_hit_coverage()
    print(
        "NXT 상·하한가 근접 이력 백필 완료: "
        f"{inserted:,}행 · {coverage['first_date']}~{coverage['last_date']} · "
        f"처리 {coverage['trading_days']:,}거래일 · 후보 발생 "
        f"{coverage['hit_days']:,}거래일 · 정확 {coverage['exact_count']:,}행 · "
        f"순수 근접 {coverage['near_count']:,}행"
    )


if __name__ == "__main__":
    main()
