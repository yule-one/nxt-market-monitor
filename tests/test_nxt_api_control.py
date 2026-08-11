from datetime import datetime
from zoneinfo import ZoneInfo

from src.nxt_api_control import (
    NxtMinuteLookupGate,
    is_nxt_api_collection_window,
    is_nxt_morning_break,
    seconds_until_nxt_morning_resume,
)


KST = ZoneInfo("Asia/Seoul")


def test_nxt_api_collection_window_uses_half_open_window() -> None:
    assert not is_nxt_api_collection_window(
        datetime(2026, 8, 10, 7, 59, 59, tzinfo=KST)
    )
    assert is_nxt_api_collection_window(
        datetime(2026, 8, 10, 8, 0, tzinfo=KST)
    )
    assert is_nxt_api_collection_window(
        datetime(2026, 8, 10, 20, 4, 59, tzinfo=KST)
    )
    assert not is_nxt_api_collection_window(
        datetime(2026, 8, 10, 20, 5, tzinfo=KST)
    )


def test_nxt_morning_break_uses_half_open_window() -> None:
    assert not is_nxt_morning_break(datetime(2026, 8, 10, 8, 49, 59, tzinfo=KST))
    assert is_nxt_morning_break(datetime(2026, 8, 10, 8, 50, tzinfo=KST))
    assert is_nxt_morning_break(datetime(2026, 8, 10, 8, 59, 59, tzinfo=KST))
    assert not is_nxt_morning_break(datetime(2026, 8, 10, 9, 0, tzinfo=KST))


def test_seconds_until_nxt_morning_resume() -> None:
    assert seconds_until_nxt_morning_resume(
        datetime(2026, 8, 10, 8, 55, tzinfo=KST)
    ) == 300
    assert seconds_until_nxt_morning_resume(
        datetime(2026, 8, 10, 9, 0, tzinfo=KST)
    ) == 0


def test_minute_lookup_gate_deduplicates_five_sessions() -> None:
    gate = NxtMinuteLookupGate()
    key = ("2026-08-10", "488900")

    permits = [gate.claim(key, now=100.0) for _ in range(5)]

    assert [item.allowed for item in permits] == [True, False, False, False, False]
    assert all(item.reason == "다른 접속자가 조회 중" for item in permits[1:])


def test_minute_lookup_gate_reclaims_abandoned_lookup() -> None:
    gate = NxtMinuteLookupGate(in_flight_timeout_seconds=30)
    key = ("2026-08-10", "488900")

    assert gate.claim(key, now=100.0).allowed
    assert not gate.claim(key, now=129.9).allowed
    reclaimed = gate.claim(key, now=130.0)
    assert reclaimed.allowed
    assert reclaimed.reason == "만료된 조회 잠금 회수"


def test_minute_lookup_gate_backs_off_failures() -> None:
    gate = NxtMinuteLookupGate(
        failure_base_seconds=60,
        failure_max_seconds=300,
    )
    key = ("2026-08-10", "488900")

    assert gate.claim(key, now=100.0).allowed
    assert gate.fail(key, "HTTP 500", now=101.0) == 60
    blocked = gate.claim(key, now=120.0)
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 41
    assert gate.claim(key, now=161.0).allowed
    assert gate.fail(key, "HTTP 500", now=162.0) == 120
    assert not gate.claim(key, now=200.0).allowed
    assert gate.claim(key, now=282.0).allowed


def test_empty_minute_result_waits_before_retry() -> None:
    gate = NxtMinuteLookupGate(empty_retry_seconds=60)
    key = ("2026-08-10", "488900")

    assert gate.claim(key, now=100.0).allowed
    assert gate.complete(key, has_result=False, now=101.0) == 60
    assert not gate.claim(key, now=160.0).allowed
    assert gate.claim(key, now=161.0).allowed
