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
    ).fetch_future_quotes(trading_date, force_refresh=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KRX OPEN API KOSPI200 최근월물 일별 데이터를 DB에 적재합니다."
    )
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
    if not args.secrets.exists():
        raise SystemExit(f"secrets 파일이 없습니다: {args.secrets}")

    auth_key = _load_krx_key(args.secrets)
    store = HistoricalMarketStore()
    existing = set() if args.force else store.future_dates()
    dates = sorted(
        item
        for item in store.snapshot_dates()
        if args.start <= item <= args.end and item not in existing
    )
    if not dates:
        print("추가로 저장할 KOSPI200 선물 거래일이 없습니다.")
        return 0

    # 권한 오류가 있으면 1회 호출에서 즉시 중단해 불필요한 반복 요청을 막습니다.
    try:
        first_quotes = _fetch(auth_key, dates[0])
    except Exception as exc:
        print(f"KRX 선물 API 사전 확인 실패: {exc}")
        return 1
    if not first_quotes:
        print(f"{dates[0]:%Y-%m-%d} KOSPI200 최근월물 데이터가 없습니다.")
        return 1
    store.save_future_quotes(dates[0], first_quotes)

    completed = 1
    failures: list[tuple[date, str]] = []
    remaining = dates[1:]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(_fetch, auth_key, trading_date): trading_date
            for trading_date in remaining
        }
        for future in as_completed(futures):
            trading_date = futures[future]
            try:
                quotes = future.result()
                if not quotes:
                    raise RuntimeError("KOSPI200 최근월물 데이터 없음")
                store.save_future_quotes(trading_date, quotes)
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

    print(
        f"완료: KOSPI200 선물 {completed:,}거래일 저장 · 실패 {len(failures):,}일"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
