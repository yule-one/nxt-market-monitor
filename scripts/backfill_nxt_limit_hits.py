from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore, NxtLimitHit
from src.kis_rest import KisRestClient
from src.kis_websocket import KisCredentials


KST = ZoneInfo("Asia/Seoul")


def _previous_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DB의 NXT 일별 OHLC로 상·하한가 도달 이력을 생성합니다."
    )
    parser.add_argument("--start", type=date.fromisoformat, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--skip-times",
        action="store_true",
        help="분봉 최초 도달시각 보강은 건너뛰고 도달 이력만 생성합니다.",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=PROJECT_ROOT / ".streamlit" / "secrets.toml",
    )
    return parser.parse_args()


def _kis_client(secrets_path: Path) -> KisRestClient:
    secrets = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    app_key = str(secrets.get("KIS_APP_KEY") or "").strip()
    app_secret = str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다.")
    return KisRestClient(KisCredentials(app_key, app_secret))


def _limit_price(hit: NxtLimitHit) -> int | None:
    if hit.direction == "상한가":
        return hit.upper_limit_price
    if hit.direction == "하한가":
        return hit.lower_limit_price
    return None


def _print_coverage(store: HistoricalMarketStore) -> None:
    coverage = store.nxt_limit_hit_coverage()
    print(
        "coverage "
        f"{coverage['min_date']}~{coverage['max_date']} "
        f"dates={coverage['date_count']:,} rows={coverage['row_count']:,} "
        f"open={coverage['open_count']:,} exact={coverage['exact_count']:,} "
        f"expired={coverage['expired_count']:,} pending={coverage['pending_count']:,}"
    )


def main() -> int:
    args = _arguments()
    store = HistoricalMarketStore()
    latest_final = store.latest_final_date()
    if latest_final is None:
        raise SystemExit("DB에 확정 NXT 일별 데이터가 없습니다.")
    end_date = min(args.end or latest_final, latest_final)
    start_date = max(args.start, NXT_LAUNCH_DATE)
    if start_date > end_date:
        raise SystemExit(f"조회 기간이 올바르지 않습니다: {start_date}~{end_date}")

    today = datetime.now(KST).date()
    retention_start = _previous_year(today)
    trading_dates = sorted(
        day for day in store.snapshot_dates() if start_date <= day <= end_date
    )
    if not trading_dates:
        raise SystemExit("선택 기간에 DB로 저장된 NXT 거래일이 없습니다.")

    total_rows = 0
    for index, trading_date in enumerate(trading_dates, start=1):
        statuses = store.load_nxt_statuses(trading_date)
        total_rows += store.replace_nxt_limit_hits(
            trading_date,
            statuses,
            retention_expired=trading_date < retention_start,
        )
        if index % 50 == 0 or index == len(trading_dates):
            print(
                f"materialized {index:,}/{len(trading_dates):,} dates, "
                f"{total_rows:,} hit rows"
            )

    _print_coverage(store)
    if args.skip_times:
        return 0

    client = _kis_client(args.secrets)
    pending = store.pending_nxt_limit_hits(
        max(start_date, retention_start),
        end_date,
    )
    grouped: dict[tuple[date, str], list[NxtLimitHit]] = defaultdict(list)
    for hit in pending:
        grouped[(hit.trade_date, hit.stock_code)].append(hit)

    completed = 0
    resolved_count = 0
    errors: list[str] = []
    for (trading_date, stock_code), hits in grouped.items():
        targets = {
            hit.direction: price
            for hit in hits
            if (price := _limit_price(hit)) is not None
        }
        if not targets:
            continue
        try:
            resolved = client.fetch_nxt_limit_hit_times(
                stock_code,
                trading_date,
                targets,
            )
            if resolved:
                store.save_nxt_limit_hit_times(
                    trading_date,
                    {
                        (stock_code, direction): hit_time
                        for direction, hit_time in resolved.items()
                    },
                    source="KIS_NXT_MINUTE_BACKFILL",
                )
                resolved_count += len(resolved)
        except Exception as exc:  # 개별 종목 실패 후에도 나머지는 계속 적재합니다.
            errors.append(f"{trading_date} {stock_code}: {exc}")
        completed += 1
        if completed % 25 == 0 or completed == len(grouped):
            print(
                f"hit times {completed:,}/{len(grouped):,} symbols, "
                f"resolved={resolved_count:,}, errors={len(errors):,}"
            )

    _print_coverage(store)
    if errors:
        print("first errors:")
        for message in errors[:10]:
            print(f"- {message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
