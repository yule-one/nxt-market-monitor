from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.models import NxtChange
from src.nxt_change_store import (
    NxtChangeScheduler,
    NxtChangeStore,
    NxtChangeSyncService,
)


def _change(day: date, code: str = "005930") -> NxtChange:
    return NxtChange(
        change_date=day,
        stock_code=code,
        stock_name="삼성전자",
        market="KOSPI",
        change_type="편입",
        reason="정기변경",
        isin="KR7005930003",
        registered_at=1,
    )


class FakeNxtClient:
    def __init__(self, rows: list[NxtChange]) -> None:
        self.rows = rows
        self.calls: list[tuple[date, date, bool]] = []
        self.ssl_fallback_used = False

    def fetch_changes(
        self,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool = False,
    ) -> list[NxtChange]:
        self.calls.append((start_date, end_date, force_refresh))
        return [
            row for row in self.rows if start_date <= row.change_date <= end_date
        ]


def test_change_store_records_empty_days_and_round_trips(tmp_path: Path) -> None:
    store = NxtChangeStore(tmp_path / "history.db")
    start = date(2025, 3, 4)
    end = date(2025, 3, 6)

    store.replace_range(start, end, [_change(date(2025, 3, 5))])

    assert store.missing_ranges(start, end) == []
    assert store.list_changes(start, end) == [_change(date(2025, 3, 5))]
    stats = store.stats()
    assert stats.coverage_start == start
    assert stats.coverage_end == end
    assert stats.synced_day_count == 3
    assert stats.event_count == 1


def test_sync_service_only_fetches_missing_dates(tmp_path: Path) -> None:
    store = NxtChangeStore(tmp_path / "history.db")
    start = date(2025, 3, 4)
    end = date(2025, 3, 6)
    client = FakeNxtClient([_change(date(2025, 3, 5))])
    service = NxtChangeSyncService(store, client_factory=lambda: client)  # type: ignore[arg-type]

    first = service.sync(start, end)
    second = service.sync(start, end)

    assert first.status == "success"
    assert first.event_count == 1
    assert second.status == "noop"
    assert client.calls == [(start, end, True)]
    assert store.stats().status == "success"


def test_scheduler_syncs_through_previous_calendar_day(tmp_path: Path) -> None:
    store = NxtChangeStore(tmp_path / "history.db")
    client = FakeNxtClient([])
    service = NxtChangeSyncService(store, client_factory=lambda: client)  # type: ignore[arg-type]
    scheduler = NxtChangeScheduler(service, start_date=date(2026, 8, 4))

    result = scheduler.sync_due(
        datetime(2026, 8, 6, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    )

    assert result.status == "success"
    assert client.calls == [(date(2026, 8, 4), date(2026, 8, 5), True)]
