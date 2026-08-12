from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app


def test_format_auth_time_converts_utc_to_kst() -> None:
    value = datetime(2026, 8, 12, 5, 30, tzinfo=timezone.utc)

    assert app._format_auth_time(value) == "2026-08-12 14:30"


def test_format_auth_time_keeps_kst_clock_time() -> None:
    kst = timezone(timedelta(hours=9))
    value = datetime(2026, 8, 12, 14, 30, tzinfo=kst)

    assert app._format_auth_time(value) == "2026-08-12 14:30"


def test_format_auth_time_treats_legacy_naive_value_as_utc() -> None:
    value = datetime(2026, 8, 12, 5, 30)

    assert app._format_auth_time(value) == "2026-08-12 14:30"


def test_format_auth_time_handles_missing_value() -> None:
    assert app._format_auth_time(None) == "-"
