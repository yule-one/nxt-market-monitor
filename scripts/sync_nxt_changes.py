from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import NXT_LAUNCH_DATE
from src.nxt_change_store import NxtChangeStore, NxtChangeSyncService


KST = ZoneInfo("Asia/Seoul")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _logger() -> logging.Logger:
    logger = logging.getLogger("nxt-change-sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path = PROJECT_ROOT / "data" / "nxt_change_sync.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NXT 정규시장 종목 변동내역을 SQLite에 증분 적재합니다."
    )
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    parser.add_argument(
        "--force",
        action="store_true",
        help="이미 적재된 날짜도 공식 원본에서 다시 받아 교체합니다.",
    )
    args = parser.parse_args()

    default_end = datetime.now(KST).date() - timedelta(days=1)
    start_date = args.start or NXT_LAUNCH_DATE
    end_date = args.end or default_end
    logger = _logger()
    service = NxtChangeSyncService(NxtChangeStore())
    try:
        result = service.sync(start_date, end_date, force=args.force)
    except Exception:
        logger.exception("동기화 실패: %s ~ %s", start_date, end_date)
        return 1

    logger.info(
        "동기화 결과=%s 범위=%s~%s 건수=%s 메시지=%s",
        result.status,
        start_date,
        end_date,
        result.event_count,
        result.message,
    )
    print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
