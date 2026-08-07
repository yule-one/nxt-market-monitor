from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scripts.sync_daily_market import _target_date


KST = ZoneInfo("Asia/Seoul")


def test_target_date_is_previous_korean_calendar_day_at_scheduled_time() -> None:
    assert _target_date(datetime(2026, 8, 7, 8, 0, tzinfo=KST)).isoformat() == "2026-08-06"


def test_target_date_converts_input_to_korean_time() -> None:
    # 23:00 UTC is 08:00 on the following day in Korea.
    assert _target_date(datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc)).isoformat() == "2026-08-06"
