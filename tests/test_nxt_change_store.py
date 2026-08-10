from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.historical_market import HistoricalMarketStore
from src.models import NxtChange, NxtTradingStatus
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


def _status(day: date, code: str, name: str) -> NxtTradingStatus:
    return NxtTradingStatus(
        status_date=day,
        stock_code=code,
        stock_name=name,
        market="KOSPI",
        tradable_market="전체",
        unavailable_reason="",
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


def test_silent_delisting_is_reconstructed_from_daily_membership(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    change_store = NxtChangeStore(database_path)
    change_store.replace_range(
        date(2025, 7, 30),
        date(2025, 7, 30),
        [
            _change(date(2025, 7, 30), "005930"),
            NxtChange(
                change_date=date(2025, 7, 30),
                stock_code="049770",
                stock_name="동원F&B",
                market="KOSPI",
                change_type="편입",
                reason="정기변경",
            ),
        ],
    )
    history = HistoricalMarketStore(database_path)
    trading_dates = [
        date(2025, 7, 30),
        date(2025, 7, 31),
        date(2025, 8, 1),
        date(2025, 8, 4),
        date(2025, 8, 5),
        date(2025, 8, 6),
        date(2025, 8, 7),
    ]
    history.save_nxt_statuses(
        trading_dates[0],
        [
            _status(trading_dates[0], "005930", "삼성전자"),
            _status(trading_dates[0], "049770", "동원F&B"),
        ],
    )
    for trading_date in trading_dates[1:]:
        history.save_nxt_statuses(
            trading_date,
            [_status(trading_date, "005930", "삼성전자")],
        )

    store = NxtChangeStore(database_path)
    assert store.rebuild_inferred_changes() == 1
    changes = store.list_changes(date(2025, 7, 31), date(2025, 7, 31))
    dongwon = next(item for item in changes if item.stock_code == "049770")
    assert dongwon.change_type == "편출"
    assert dongwon.reason == "상장폐지"
    assert dongwon.display_reason == "상장폐지(포괄적 주식교환)"
    assert dongwon.is_inferred


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
