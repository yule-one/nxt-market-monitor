from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.krx_listed_history import KrxListedHistoryStore, apply_index_memberships
from src.krx_openapi import KrxListedSecurityDaily


def _records(trading_date: date) -> list[KrxListedSecurityDaily]:
    return [
        KrxListedSecurityDaily(
            trade_date=trading_date,
            standard_code="KR7005930003",
            short_code="005930",
            stock_name="삼성전자",
            market="KOSPI",
            stock_type="보통주",
            security_type="주권",
            listed_shares=5_969_782_550,
            listing_date=date(1975, 6, 11),
            cumulative_volume=12_345_678,
            cumulative_amount=876_543_210_000,
        ),
        KrxListedSecurityDaily(
            trade_date=trading_date,
            standard_code="KR7067280006",
            short_code="067280",
            stock_name="멀티캠퍼스",
            market="KOSDAQ",
            stock_type="보통주",
            security_type="주권",
            listed_shares=5_926_779,
            listing_date=date(2006, 11, 16),
            cumulative_volume=123_456,
            cumulative_amount=5_432_100_000,
        ),
    ]


def test_applies_date_specific_kospi200_and_kosdaq150_memberships() -> None:
    records = apply_index_memberships(
        _records(date(2026, 8, 11)),
        {
            "KOSPI200": {"005930"},
            "KOSDAQ150": {"067280"},
        },
    )

    assert records[0].is_kospi200 is True
    assert records[0].is_kosdaq150 is False
    assert records[1].is_kospi200 is False
    assert records[1].is_kosdaq150 is True


def test_store_replaces_and_loads_one_daily_snapshot(tmp_path: Path) -> None:
    database_path = tmp_path / "krx_listed_history.db"
    store = KrxListedHistoryStore(database_path)
    trading_date = date(2026, 8, 11)
    records = apply_index_memberships(
        _records(trading_date),
        {"KOSPI200": {"005930"}, "KOSDAQ150": {"067280"}},
    )

    assert store.replace_daily(records) == 2
    assert store.dates() == [trading_date]
    assert {item.short_code: item for item in store.load_daily(trading_date)} == {
        item.short_code: item for item in records
    }
    assert store.coverage().first_date == trading_date
    assert store.coverage().last_date == trading_date
    assert store.coverage().trading_days == 1
    assert store.coverage().rows == 2

    revised = [
        item
        for item in records
        if item.short_code == "005930"
    ]
    assert store.replace_daily(revised) == 1
    assert store.load_daily(trading_date) == revised
    assert store.coverage().rows == 1


def test_store_rejects_multiple_dates_in_one_replace(tmp_path: Path) -> None:
    store = KrxListedHistoryStore(tmp_path / "krx_listed_history.db")
    records = [
        _records(date(2026, 8, 11))[0],
        _records(date(2026, 8, 12))[0],
    ]

    with pytest.raises(ValueError, match="거래일"):
        store.replace_daily(records)
