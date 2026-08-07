from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.kis_rest import KisRestClient
from src.kis_websocket import KisCredentials


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS 달러-원(KMB) 일별 환율을 DB에 적재합니다.")
    parser.add_argument("--start", type=_parse_date, default=NXT_LAUNCH_DATE)
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument(
        "--secrets",
        type=Path,
        default=PROJECT_ROOT / ".streamlit" / "secrets.toml",
    )
    args = parser.parse_args()
    if args.end < args.start:
        raise SystemExit("종료일은 시작일보다 빠를 수 없습니다.")
    secrets = tomllib.loads(args.secrets.read_text(encoding="utf-8"))
    app_key = str(secrets.get("KIS_APP_KEY") or "").strip()
    app_secret = str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise SystemExit("KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다.")

    client = KisRestClient(KisCredentials(app_key, app_secret))
    quotes = client.fetch_usd_krw_history(args.start, args.end)
    HistoricalMarketStore().save_fx_quotes(quotes)
    print(
        f"완료: 달러-원(KMB) {len(quotes):,}일 저장 · "
        f"범위 {args.start:%Y-%m-%d}~{args.end:%Y-%m-%d}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
