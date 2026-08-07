from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.historical_market import HistoricalMarketStore
from src.kis_rest import KisRestClient
from src.kis_websocket import KisCredentials
from src.market_realtime import WatchSymbol


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KIS NXT 분봉으로 프리마켓 종목별 OHLC를 DB에 저장합니다."
    )
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _client() -> KisRestClient:
    secrets_path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    secrets = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    app_key = str(secrets.get("KIS_APP_KEY") or "").strip()
    app_secret = str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다.")
    return KisRestClient(KisCredentials(app_key, app_secret))


def main() -> int:
    args = _arguments()
    store = HistoricalMarketStore()
    latest = store.latest_final_date()
    if latest is None:
        raise RuntimeError("DB에 확정 NXT 일별 종목 데이터가 없습니다.")
    start_date = args.start or latest
    end_date = args.end or latest
    if start_date > end_date:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    earliest_available = date.today() - timedelta(days=366)
    if start_date < earliest_available:
        raise ValueError(
            f"KIS 과거 분봉은 최대 1년만 보관됩니다. {earliest_available} 이후를 선택하세요."
        )

    client = _client()
    existing_dates = store.nxt_session_dates("PRE")
    completed_dates = 0
    current = start_date
    while current <= end_date:
        statuses = store.load_nxt_statuses(current)
        if statuses and (args.force or current not in existing_dates):
            symbols = [
                WatchSymbol(item.stock_code, item.stock_name) for item in statuses
            ]

            def progress(completed: int, total: int) -> None:
                if completed == total or completed % 100 == 0:
                    print(f"{current} {completed:,}/{total:,}종목")

            quotes = client.fetch_nxt_pre_market_quotes(
                symbols,
                current,
                progress=progress,
            )
            store.save_nxt_session_quotes(current, "PRE", quotes)
            completed_dates += 1
            print(f"{current} 저장 완료: {len(quotes):,}종목")
        current += timedelta(days=1)
    print(f"프리마켓 OHLC 백필 완료: {completed_dates:,}거래일")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
