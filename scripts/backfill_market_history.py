from __future__ import annotations

import argparse
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cache import ResponseCache
from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.kis_rest import KisRestClient
from src.kis_websocket import KisCredentials
from src.krx_openapi import KrxDailySnapshot, KrxOpenApiClient
from src.models import NxtTradingStatus
from src.nxt_client import NxtClient


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _load_secrets(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _fetch_krx_snapshot(auth_key: str, trading_date: date) -> KrxDailySnapshot:
    client = KrxOpenApiClient(
        auth_key,
        ResponseCache(),
        persist_raw_cache=False,
    )
    snapshot = client.fetch_daily_snapshot(
        trading_date,
        force_refresh=True,
        include_futures=False,
    )
    if "KOSPI" not in snapshot.index_quotes or "KOSDAQ" not in snapshot.index_quotes:
        raise RuntimeError(f"{trading_date:%Y-%m-%d} KRX 대표지수 데이터가 없습니다.")
    return snapshot


def _save_live_metric(
    store: HistoricalMarketStore,
    secrets: dict[str, object],
    statuses_by_date: dict[date, list[NxtTradingStatus]],
    end_date: date,
) -> bool:
    today = date.today()
    statuses = statuses_by_date.get(today, [])
    if end_date < today or not statuses:
        return False
    app_key = str(secrets.get("KIS_APP_KEY") or "").strip()
    app_secret = str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        print("오늘 값은 KIS 키가 없어 건너뜁니다.", flush=True)
        return False
    indices = KisRestClient(KisCredentials(app_key, app_secret)).fetch_index_quotes()
    kospi = indices.get("KOSPI")
    kosdaq = indices.get("KOSDAQ")
    if kospi is None or kosdaq is None:
        print("오늘 KRX 대표지수 누적값을 받지 못해 건너뜁니다.", flush=True)
        return False
    store.save_live_metric(
        today,
        nxt_volume=sum(int(item.cumulative_volume) for item in statuses),
        nxt_amount=sum(int(item.cumulative_amount) for item in statuses),
        krx_volume=kospi.cumulative_volume + kosdaq.cumulative_volume,
        krx_amount=kospi.cumulative_amount + kosdaq.cumulative_amount,
        nxt_stock_count=len(statuses),
    )
    print(f"{today:%Y-%m-%d} 장중/당일 누적 합계를 저장했습니다.", flush=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NXT 개장일 이후 대시보드 과거 데이터를 SQLite에 저장합니다."
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
    secrets = _load_secrets(args.secrets)
    krx_key = str(secrets.get("KRX_KEY") or "").strip()
    if not krx_key:
        raise SystemExit("KRX_KEY가 필요합니다.")

    store = HistoricalMarketStore()
    print(
        f"NXT 거래일 확인: {args.start:%Y-%m-%d}~{args.end:%Y-%m-%d}",
        flush=True,
    )
    statuses_by_date = NxtClient(ResponseCache()).fetch_trading_status_range(
        args.start,
        args.end,
        force_refresh=args.force,
        max_workers=max(1, min(args.workers * 2, 8)),
    )
    past_cutoff = min(args.end, date.today() - timedelta(days=1))
    existing = set() if args.force else store.snapshot_dates()
    dates = [
        trading_date
        for trading_date in statuses_by_date
        if trading_date <= past_cutoff and trading_date not in existing
    ]
    print(
        f"NXT 거래일 {len(statuses_by_date):,}일 확인 · "
        f"KRX 상세 저장 대상 {len(dates):,}일 · 기존 {len(existing):,}일",
        flush=True,
    )

    completed = 0
    failures: list[tuple[date, str]] = []
    if dates:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_fetch_krx_snapshot, krx_key, trading_date): trading_date
                for trading_date in dates
            }
            for future in as_completed(futures):
                trading_date = futures[future]
                try:
                    snapshot = future.result()
                    store.save_historical_snapshot(
                        trading_date,
                        statuses_by_date[trading_date],
                        snapshot,
                    )
                    completed += 1
                    print(
                        f"[{completed + len(failures):,}/{len(dates):,}] "
                        f"{trading_date:%Y-%m-%d} 저장 완료",
                        flush=True,
                    )
                except Exception as exc:
                    failures.append((trading_date, str(exc)))
                    print(
                        f"[{completed + len(failures):,}/{len(dates):,}] "
                        f"{trading_date:%Y-%m-%d} 실패: {exc}",
                        flush=True,
                    )

    store.rebuild_derived_metrics()
    _save_live_metric(store, secrets, statuses_by_date, args.end)
    total_metrics = store.list_metrics(args.start, args.end)
    print(
        f"완료: 이번 실행 {completed:,}일 저장 · DB 일별 합계 {len(total_metrics):,}일 · "
        f"실패 {len(failures):,}일 · {store.path}",
        flush=True,
    )
    if failures:
        for trading_date, message in failures:
            print(f"FAIL {trading_date:%Y-%m-%d}: {message}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
