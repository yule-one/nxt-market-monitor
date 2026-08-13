from __future__ import annotations

import argparse
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.krx_listed_history import KrxListedHistoryStore, apply_index_memberships
from src.krx_openapi import KrxListedSecurityDaily, KrxOpenApiClient


def _krx_key() -> str:
    path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if not path.exists():
        raise RuntimeError(f"secrets 파일이 없습니다: {path}")
    secrets = tomllib.loads(path.read_text(encoding="utf-8"))
    key = str(secrets.get("KRX_KEY") or "").strip()
    if not key:
        raise RuntimeError("KRX_KEY가 필요합니다.")
    return key


def _validate_records(
    trading_date: date,
    records: list[KrxListedSecurityDaily],
) -> None:
    market_counts = {
        market: sum(item.market == market for item in records)
        for market in ("KOSPI", "KOSDAQ")
    }
    if market_counts["KOSPI"] < 500 or market_counts["KOSDAQ"] < 500:
        raise RuntimeError(
            f"{trading_date:%Y-%m-%d} KRX 종목수가 비정상입니다: {market_counts}"
        )
    if len({item.standard_code for item in records}) != len(records):
        raise RuntimeError(f"{trading_date:%Y-%m-%d} 표준코드가 중복되었습니다.")
    if len({item.short_code for item in records}) != len(records):
        raise RuntimeError(f"{trading_date:%Y-%m-%d} 단축코드가 중복되었습니다.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NXT 개설일 이후 KOSPI·KOSDAQ 전 상장주권의 일별 거래정보를 "
            "별도 SQLite DB에 적재합니다."
        )
    )
    parser.add_argument("--start", type=date.fromisoformat, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    start_date = max(args.start, NXT_LAUNCH_DATE)
    if args.end < start_date:
        parser.error("종료일은 시작일보다 빠를 수 없습니다.")

    history_store = HistoricalMarketStore()
    listed_store = KrxListedHistoryStore()
    final_dates = [
        item.trade_date
        for item in history_store.list_metrics(start_date, args.end)
        if item.is_final
    ]
    existing_dates = set() if args.force else set(listed_store.dates())
    target_dates = [item for item in final_dates if item not in existing_dates]
    if args.limit is not None:
        target_dates = target_dates[: max(0, args.limit)]
    if not target_dates:
        print("추가로 저장할 KRX 전 상장종목 거래일이 없습니다.")
        return 0

    memberships_by_date: dict[date, dict[str, set[str]]] = {}
    for trading_date in target_dates:
        memberships = history_store.index_constituent_codes(trading_date)
        counts = {name: len(codes) for name, codes in memberships.items()}
        if not (
            190 <= counts.get("KOSPI200", 0) <= 205
            and 145 <= counts.get("KOSDAQ150", 0) <= 155
        ):
            raise RuntimeError(
                f"{trading_date:%Y-%m-%d} 지수 구성종목 이력이 완전하지 않습니다: {counts}. "
                "scripts/backfill_krx_index_constituents.py를 먼저 실행하세요."
            )
        memberships_by_date[trading_date] = memberships

    auth_key = _krx_key()
    worker_state = threading.local()

    def fetch(trading_date: date) -> tuple[date, list[KrxListedSecurityDaily]]:
        client = getattr(worker_state, "client", None)
        if client is None:
            client = KrxOpenApiClient(auth_key, persist_raw_cache=False)
            worker_state.client = client
        records = client.fetch_listed_securities(
            trading_date,
            force_refresh=True,
        )
        records = apply_index_memberships(records, memberships_by_date[trading_date])
        _validate_records(trading_date, records)
        return trading_date, records

    completed = 0
    failures: list[tuple[date, str]] = []
    total = len(target_dates)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(fetch, item): item for item in target_dates}
        for future in as_completed(futures):
            trading_date = futures[future]
            try:
                _date, records = future.result()
                stored = listed_store.replace_daily(records)
            except Exception as exc:
                failures.append((trading_date, str(exc)))
                print(f"실패 {trading_date:%Y-%m-%d}: {exc}", flush=True)
                continue
            completed += 1
            print(
                f"저장 {trading_date:%Y-%m-%d}: {stored:,}종목 "
                f"({completed + len(failures):,}/{total:,})",
                flush=True,
            )

    coverage = listed_store.coverage()
    print(
        f"완료: {completed:,}거래일 저장 · 실패 {len(failures):,}일 · "
        f"DB {coverage.first_date}~{coverage.last_date} "
        f"{coverage.trading_days:,}거래일 {coverage.rows:,}행"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
