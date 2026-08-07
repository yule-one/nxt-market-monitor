from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.historical_market import HistoricalMarketStore
from src.nxt_client import NxtClient


def _date_value(raw: str) -> date:
    return date.fromisoformat(raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="저장된 확정 거래일의 NXT OHLC·상하한가를 다시 수집합니다."
    )
    parser.add_argument("--start", type=_date_value, default=date(2025, 3, 4))
    parser.add_argument("--end", type=_date_value)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    store = HistoricalMarketStore()
    final_dates = sorted(store.snapshot_dates())
    if not final_dates:
        print("백필할 확정 거래일이 없습니다.")
        return 0
    end_date = args.end or final_dates[-1]
    target_dates = [
        trading_date
        for trading_date in final_dates
        if args.start <= trading_date <= end_date
    ]
    if not target_dates:
        print("선택 범위에 백필할 거래일이 없습니다.")
        return 0

    client = NxtClient()
    failures: list[tuple[date, str]] = []
    saved_dates = 0
    saved_rows = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                client.fetch_trading_status,
                trading_date,
                force_refresh=args.force,
            ): trading_date
            for trading_date in target_dates
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            trading_date = futures[future]
            try:
                statuses = future.result()
                if not statuses:
                    raise RuntimeError("NXT 일별 종목 데이터 없음")
                saved_rows += store.save_nxt_statuses(trading_date, statuses)
                saved_dates += 1
            except Exception as exc:
                failures.append((trading_date, str(exc)))
            if completed % 25 == 0 or completed == len(target_dates):
                print(
                    f"진행 {completed:,}/{len(target_dates):,}일 · "
                    f"저장 {saved_dates:,}일 · 실패 {len(failures):,}일"
                )

    store.rebuild_nxt_price_limits()
    store.rebuild_derived_metrics()
    print(f"완료: {saved_dates:,}거래일, {saved_rows:,}종목행 저장")
    if failures:
        for trading_date, message in sorted(failures)[:20]:
            print(f"실패 {trading_date}: {message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
