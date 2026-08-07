from __future__ import annotations

import argparse
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cache import ResponseCache
from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.krx_openapi import KrxOpenApiClient


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _load_krx_key(path: Path) -> str:
    secrets = tomllib.loads(path.read_text(encoding="utf-8"))
    key = str(secrets.get("KRX_KEY") or "").strip()
    if not key:
        raise SystemExit("KRX_KEY가 필요합니다.")
    return key


def _fetch(auth_key: str, trading_date: date):
    return KrxOpenApiClient(
        auth_key,
        ResponseCache(),
        persist_raw_cache=False,
    ).fetch_named_index_quote("KRX TMI", trading_date, force_refresh=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="KRX TMI 일별 지수를 DB에 적재합니다.")
    parser.add_argument("--start", type=_parse_date, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--secrets",
        type=Path,
        default=PROJECT_ROOT / ".streamlit" / "secrets.toml",
    )
    args = parser.parse_args()
    if args.end < args.start:
        raise SystemExit("종료일은 시작일보다 빠를 수 없습니다.")
    auth_key = _load_krx_key(args.secrets)
    store = HistoricalMarketStore()
    existing = set() if args.force else store.index_dates("KRX TMI")
    dates = sorted(
        item
        for item in store.snapshot_dates()
        if args.start <= item <= args.end and item not in existing
    )
    if not dates:
        store.rebuild_derived_metrics()
        print("추가로 저장할 KRX TMI 거래일이 없습니다.")
        return 0

    completed = 0
    failures: list[tuple[date, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(_fetch, auth_key, trading_date): trading_date
            for trading_date in dates
        }
        for future in as_completed(futures):
            trading_date = futures[future]
            try:
                quote = future.result()
                if quote is None:
                    raise RuntimeError("KRX TMI 데이터 없음")
                store.save_index_quotes(trading_date, {"KRX TMI": quote})
                completed += 1
            except Exception as exc:
                failures.append((trading_date, str(exc)))
            processed = completed + len(failures)
            if processed % 20 == 0 or processed == len(dates):
                print(
                    f"[{processed:,}/{len(dates):,}] 저장 {completed:,}일 · "
                    f"실패 {len(failures):,}일",
                    flush=True,
                )

    store.rebuild_derived_metrics()
    print(f"완료: KRX TMI {completed:,}거래일 저장 · 실패 {len(failures):,}일")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
