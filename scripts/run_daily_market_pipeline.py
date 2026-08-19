from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.publish_daily_seeds import publish_daily_seeds
from scripts.sync_daily_market import main as sync_daily_market


def main() -> int:
    sync_result = sync_daily_market()
    if sync_result != 0:
        return sync_result
    try:
        publish_daily_seeds()
        return 0
    except Exception:
        # publish_daily_seeds의 CLI와 같은 로그 파일에 상세 오류를 남깁니다.
        from scripts.publish_daily_seeds import _logger

        _logger().exception("일일 시장 적재 후 자동 배포 실패")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
