from __future__ import annotations

import argparse
import logging
import os
import sys
import tomllib
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cache import ResponseCache
from src.historical_market import HistoricalMarketStore
from src.kis_rest import KisRestClient
from src.kis_universe import KisNxtUniverseResolver, KisNxtUniverseStore
from src.kis_websocket import KisCredentials
from src.nxt_client import NxtClient


def _credentials() -> KisCredentials:
    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    secrets_path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if (not app_key or not app_secret) and secrets_path.exists():
        secrets = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
        app_key = app_key or str(secrets.get("KIS_APP_KEY") or "").strip()
        app_secret = app_secret or str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다.")
    return KisCredentials(app_key, app_secret)


def _logger() -> logging.Logger:
    logger = logging.getLogger("kis-nxt-universe-sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path = PROJECT_ROOT / "data" / "kis_nxt_universe_sync.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KIS 마스터·NXT REST로 대상종목을 계산하고 선택적으로 NXT 공식 현황과 대사합니다."
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="기준일(YYYY-MM-DD, 기본값: 오늘)",
    )
    parser.add_argument(
        "--force-kis",
        action="store_true",
        help="같은 날짜의 KIS 계산 결과가 있어도 다시 계산",
    )
    parser.add_argument(
        "--reconcile-official",
        action="store_true",
        help="현재 PC에서 NXT 공식 홈페이지를 1회 조회해 KIS 결과와 대사",
    )
    args = parser.parse_args()

    logger = _logger()
    credentials = _credentials()
    cache = ResponseCache()
    history = HistoricalMarketStore()
    store = KisNxtUniverseStore()
    resolver = KisNxtUniverseResolver(
        KisRestClient(credentials, response_cache=cache, min_request_interval=0.2),
        store,
        previous_status_loader=history.load_latest_nxt_statuses,
    )

    rows = resolver.refresh(args.date, force=args.force_kis)
    logger.info("%s KIS NXT 대상 계산 완료: %s종목", args.date, len(rows))
    if args.reconcile_official:
        summary = resolver.reconcile_with_official(
            args.date,
            NxtClient(cache),
        )
        logger.info(
            "%s NXT 공식 대사 완료: KIS=%s 공식=%s 일치=%s KIS만=%s 공식만=%s 상태차이=%s",
            args.date,
            summary.kis_count,
            summary.official_count,
            summary.matched_count,
            summary.kis_only_count,
            summary.official_only_count,
            summary.metadata_difference_count,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
