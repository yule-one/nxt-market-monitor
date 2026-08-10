from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="저장된 NXT 거래현황으로 거래불가 상태·지정·해제 이력을 재구성합니다."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "history.db",
    )
    args = parser.parse_args()

    store = HistoricalMarketStore(args.database.resolve())
    snapshot_dates = store.snapshot_dates()
    if not snapshot_dates:
        raise SystemExit("NXT 일별 거래현황이 저장되어 있지 않습니다.")

    store.rebuild_nxt_eligibility_history(
        NXT_LAUNCH_DATE,
        max(snapshot_dates),
    )
    coverage = store.nxt_unavailability_history_coverage()
    print(
        "NXT 거래불가 이력 재구성 완료: "
        f"일별 상태 {coverage['row_count']:,}건, "
        f"지정·해제 {coverage['event_count']:,}건, "
        f"범위 {coverage['first_date']}~{coverage['last_date']}"
    )


if __name__ == "__main__":
    main()
