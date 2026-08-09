from __future__ import annotations

import gzip
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.historical_market import (
    DailyMarketMetric,
    HistoricalMarketStore,
    average_market_totals,
    monthly_six_month_volume_ratios,
    six_calendar_month_window_start,
)
from src.kis_rest import FutureQuote, IndexQuote, NxtSessionQuote, RestQuote
from src.krx_openapi import KrxDailySnapshot
from src.market_realtime import KRX
from src.models import NxtChange, NxtTradingStatus
from src.nxt_change_store import NxtChangeStore


KST = ZoneInfo("Asia/Seoul")


def _snapshot(trading_date: date) -> tuple[list[NxtTradingStatus], KrxDailySnapshot]:
    statuses = [
        NxtTradingStatus(
            status_date=trading_date,
            stock_code="005930",
            stock_name="삼성전자",
            market="KOSPI",
            tradable_market="NXT",
            unavailable_reason="",
            reference_price=70_000,
            current_price=71_000,
            cumulative_volume=100,
            cumulative_amount=7_100_000,
            open_price=70_000,
            high_price=91_000,
            low_price=69_500,
            upper_limit_price=91_000,
            lower_limit_price=49_000,
        ),
        NxtTradingStatus(
            status_date=trading_date,
            stock_code="000660",
            stock_name="SK하이닉스",
            market="KOSPI",
            tradable_market="거래불가",
            unavailable_reason="거래정지",
            reference_price=190_000,
            current_price=195_000,
            cumulative_volume=200,
            cumulative_amount=39_000_000,
        ),
    ]
    updated_at = datetime(2026, 8, 5, 18, tzinfo=KST)
    quotes = {
        ("005930", KRX): RestQuote(
            market=KRX,
            symbol="005930",
            name="삼성전자",
            current_price=70_500,
            reference_price=70_000,
            cumulative_volume=1_000,
            cumulative_amount=70_500_000,
            updated_at=updated_at,
            market_cap=420_000_000_000_000,
        ),
        ("000660", KRX): RestQuote(
            market=KRX,
            symbol="000660",
            name="SK하이닉스",
            current_price=194_000,
            reference_price=190_000,
            cumulative_volume=2_000,
            cumulative_amount=388_000_000,
            updated_at=updated_at,
            market_cap=140_000_000_000_000,
        ),
        ("999999", KRX): RestQuote(
            market=KRX,
            symbol="999999",
            name="비대상",
            current_price=1_000,
            reference_price=900,
            cumulative_volume=3_000,
            cumulative_amount=3_000_000,
            updated_at=updated_at,
        ),
    }
    indices = {
        "KRX TMI": IndexQuote(
            "KRX TMI", "KRX", 3916.03, -199.10, -4.84, 0, 0, updated_at
        ),
        "KOSPI": IndexQuote(
            "KOSPI", "0001", 2800.0, 10.0, 0.36, 10_000, 1_000_000, updated_at
        ),
        "KOSDAQ": IndexQuote(
            "KOSDAQ", "1001", 900.0, -2.0, -0.22, 20_000, 2_000_000, updated_at
        ),
    }
    futures = {
        "KOSPI200 선물": FutureQuote(
            "KOSPI200 선물",
            "101S9000",
            1025.10,
            -16.95,
            -1.63,
            updated_at,
            cumulative_volume=123_456,
            cumulative_amount=789_012_345,
            open_interest=456_789,
            settlement_price=1025.20,
        )
    }
    return statuses, KrxDailySnapshot(
        quotes,
        indices,
        {"005930": 5_969_782_550, "000660": 728_002_365, "999999": 1},
        futures,
    )


def test_historical_store_builds_reclassified_daily_eligibility(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    trading_date = date(2026, 8, 6)
    change_store = NxtChangeStore(database_path)
    change_store.replace_range(
        trading_date,
        trading_date,
        [
            NxtChange(
                change_date=trading_date,
                stock_code="005930",
                stock_name="삼성전자",
                market="KOSPI",
                change_type="편출",
                reason="투자경고/위험 지정",
            ),
            NxtChange(
                change_date=trading_date,
                stock_code="123450",
                stock_name="실제편출",
                market="KOSDAQ",
                change_type="편출",
                reason="정기변경",
            ),
        ],
    )
    store = HistoricalMarketStore(database_path)
    statuses, _snapshot_data = _snapshot(trading_date)
    store.save_nxt_statuses(trading_date, statuses)

    summary = store.list_nxt_eligibility_summaries(
        trading_date,
        trading_date,
    )[0]
    assert summary.target_stock_count == 2
    assert summary.tradable_stock_count == 1
    assert summary.unavailable_stock_count == 1
    assert summary.restriction_start_stock_count == 1
    assert summary.exclusion_stock_count == 1
    reason_counts = {
        (item.reason_group, item.reason): item.stock_count
        for item in store.list_nxt_eligibility_reason_counts(
            trading_date,
            trading_date,
        )
    }
    assert reason_counts[("거래불가사유", "거래정지")] == 1
    assert reason_counts[("거래불가", "투자경고/위험 지정")] == 1
    assert reason_counts[("편출", "정기변경")] == 1


def test_historical_store_round_trip_and_metrics(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 5)
    statuses, snapshot = _snapshot(trading_date)

    metric = store.save_historical_snapshot(trading_date, statuses, snapshot)

    assert metric.nxt_volume == 300
    assert metric.nxt_amount == 46_100_000
    assert metric.krx_volume == 30_000
    assert metric.krx_amount == 3_000_000
    assert metric.volume_ratio == 0.01
    assert metric.tmi_index_value == 3916.03
    assert metric.tmi_change_rate == -0.0484
    assert metric.nxt_change_rate is not None
    assert metric.is_final is True
    store.rebuild_derived_metrics()
    rebuilt = store.list_metrics(trading_date, trading_date)[0]
    assert rebuilt.nxt_index_value is None
    loaded = store.load_historical_snapshot(trading_date)
    assert loaded is not None
    loaded_statuses, loaded_snapshot = loaded
    assert len(loaded_statuses) == 2
    assert set(loaded_snapshot.stock_quotes) == {("005930", KRX), ("000660", KRX)}
    assert loaded_snapshot.stock_quotes[("005930", KRX)].market_cap == 420_000_000_000_000
    loaded_by_code = {item.stock_code: item for item in loaded_statuses}
    assert loaded_by_code["005930"].open_price == 70_000
    assert loaded_by_code["005930"].high_price == 91_000
    assert loaded_by_code["005930"].upper_limit_price == 91_000
    assert loaded_snapshot.listed_shares["000660"] == 728_002_365
    future = loaded_snapshot.future_quotes["KOSPI200 선물"]
    assert future.code == "101S9000"
    assert future.cumulative_volume == 123_456
    assert future.settlement_price == 1025.20
    assert store.snapshot_dates() == {trading_date}
    assert store.future_dates() == {trading_date}
    assert store.latest_final_date() == trading_date


def test_historical_store_round_trips_nxt_session_quotes(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 6)
    quote = NxtSessionQuote(
        trading_date=trading_date,
        session="PRE",
        symbol="005930",
        name="삼성전자",
        reference_price=230_500,
        open_price=241_500,
        high_price=241_500,
        low_price=235_000,
        close_price=239_000,
        cumulative_volume=123_456,
        cumulative_amount=29_123_456_789,
        last_trade_time="084900",
        updated_at=datetime(2026, 8, 6, 8, 51, tzinfo=KST),
    )

    assert store.save_nxt_session_quotes(trading_date, "PRE", [quote]) == 1
    assert store.nxt_session_dates("PRE") == {trading_date}
    loaded = store.load_nxt_session_quotes(trading_date, "PRE")

    assert loaded == [quote]


def test_historical_store_restores_compressed_seed(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    source_store = HistoricalMarketStore(source_path)
    trading_date = date(2026, 8, 6)
    statuses, snapshot = _snapshot(trading_date)
    source_store.save_historical_snapshot(trading_date, statuses, snapshot)

    seed_path = tmp_path / "history.db.gz"
    with source_store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with source_path.open("rb") as source, gzip.open(seed_path, "wb") as target:
        target.write(source.read())
    restored_path = tmp_path / "restored" / "history.db"

    restored_store = HistoricalMarketStore(
        restored_path,
        seed_path=seed_path,
    )

    assert restored_path.exists()
    assert restored_store.snapshot_dates() == {trading_date}
    assert len(restored_store.load_nxt_statuses(trading_date)) == 2


def test_historical_store_replaces_incomplete_database_with_seed(
    tmp_path: Path,
) -> None:
    seed_source_path = tmp_path / "seed-source.db"
    seed_source_store = HistoricalMarketStore(seed_source_path)
    first_date = date(2026, 8, 5)
    second_date = date(2026, 8, 6)
    for trading_date in (first_date, second_date):
        statuses, snapshot = _snapshot(trading_date)
        seed_source_store.save_historical_snapshot(
            trading_date,
            statuses,
            snapshot,
        )
    with seed_source_store._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    seed_path = tmp_path / "history.db.gz"
    with seed_source_path.open("rb") as source, gzip.open(seed_path, "wb") as target:
        target.write(source.read())

    deployed_path = tmp_path / "deployed" / "history.db"
    deployed_store = HistoricalMarketStore(deployed_path)
    statuses, snapshot = _snapshot(second_date)
    deployed_store.save_historical_snapshot(second_date, statuses, snapshot)

    restored_store = HistoricalMarketStore(
        deployed_path,
        seed_path=seed_path,
    )

    assert restored_store.snapshot_dates() == {first_date, second_date}


def test_historical_store_round_trips_nxt_limit_hit_times(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 6)
    hit_times = {
        ("005930", "상한가"): "081000",
        ("000660", "하한가"): "OPEN",
    }

    assert store.save_nxt_limit_hit_times(trading_date, hit_times) == 2
    assert store.load_nxt_limit_hit_times(trading_date) == hit_times

    assert store.save_nxt_limit_hit_times(
        trading_date,
        {("005930", "상한가"): "081100"},
    ) == 1
    assert store.load_nxt_limit_hit_times(trading_date)[
        ("005930", "상한가")
    ] == "081100"


def test_historical_store_round_trips_nxt_limit_proximity_times(
    tmp_path: Path,
) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 6)
    hit_times = {
        ("005930", "상한가"): "080500",
        ("000660", "하한가"): "OPEN",
    }

    assert store.save_nxt_limit_proximity_times(trading_date, hit_times) == 2
    assert store.load_nxt_limit_proximity_times(trading_date) == hit_times

    assert store.save_nxt_limit_proximity_times(
        trading_date,
        {("005930", "상한가"): "080600"},
    ) == 1
    assert store.load_nxt_limit_proximity_times(trading_date)[
        ("005930", "상한가")
    ] == "080600"


def test_historical_store_materializes_limit_proximity_hits(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 6)
    statuses, _snapshot_data = _snapshot(trading_date)
    statuses.append(
        NxtTradingStatus(
            status_date=trading_date,
            stock_code="051360",
            stock_name="토비스",
            market="KOSDAQ",
            tradable_market="NXT",
            unavailable_reason="",
            reference_price=10_000,
            current_price=11_000,
            cumulative_volume=1_000,
            cumulative_amount=11_000_000,
            open_price=10_100,
            high_price=12_990,
            low_price=10_000,
            upper_limit_price=13_000,
            lower_limit_price=7_000,
        )
    )

    store.save_nxt_statuses(trading_date, statuses)
    hits = {
        (item.stock_code, item.direction): item
        for item in store.load_nxt_limit_proximity_hits(trading_date)
    }

    assert hits[("005930", "상한가")].distance_ticks == 0
    assert hits[("051360", "상한가")].distance_ticks == 1
    assert hits[("051360", "상한가")].closest_price == 12_990
    with store._connect() as connection:
        daily = connection.execute(
            "SELECT * FROM nxt_limit_proximity_daily WHERE trade_date = ?",
            (trading_date.isoformat(),),
        ).fetchone()
    assert daily["upper_exact_count"] == 1
    assert daily["upper_near_count"] == 1
    assert daily["lower_exact_count"] == 0
    assert daily["lower_near_count"] == 0


def test_rebuild_nxt_limit_proximity_hits_restores_all_dates(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    first_date = date(2026, 8, 5)
    second_date = date(2026, 8, 6)
    for trading_date in (first_date, second_date):
        statuses, _snapshot_data = _snapshot(trading_date)
        store.save_nxt_statuses(trading_date, statuses)
    with store._connect() as connection:
        connection.execute("DELETE FROM nxt_limit_proximity_hits")

    assert store.rebuild_nxt_limit_proximity_hits(first_date, second_date) == 2
    coverage = store.nxt_limit_proximity_hit_coverage()
    assert coverage["trading_days"] == 2
    assert coverage["first_date"] == first_date
    assert coverage["last_date"] == second_date


def test_historical_store_materializes_and_updates_nxt_limit_hits(
    tmp_path: Path,
) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 6)
    statuses, _snapshot_data = _snapshot(trading_date)

    assert store.replace_nxt_limit_hits(trading_date, statuses) == 1
    hits = store.load_nxt_limit_hits(trading_date)
    assert len(hits) == 1
    assert hits[0].stock_code == "005930"
    assert hits[0].direction == "상한가"
    assert hits[0].close_price == 71_000
    assert hits[0].hit_time_status == "PENDING"

    store.save_nxt_limit_hit_times(
        trading_date,
        {("005930", "상한가"): "101500"},
    )
    updated = store.load_nxt_limit_hits(trading_date)[0]
    assert updated.hit_time == "101500"
    assert updated.hit_time_status == "EXACT"


def test_historical_store_marks_unrecoverable_hit_times(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2025, 3, 13)
    statuses, _snapshot_data = _snapshot(trading_date)

    store.replace_nxt_limit_hits(
        trading_date,
        statuses,
        retention_expired=True,
    )

    assert store.load_nxt_limit_hits(trading_date)[0].hit_time_status == (
        "RETENTION_EXPIRED"
    )


def test_rebuild_nxt_index_rebases_april_first_to_100(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_dates = [date(2025, 3, 31), date(2025, 4, 1), date(2025, 4, 2)]
    for trading_date in trading_dates:
        statuses, snapshot = _snapshot(trading_date)
        store.save_historical_snapshot(trading_date, statuses, snapshot)

    store.rebuild_derived_metrics()
    metrics = {
        item.trade_date: item
        for item in store.list_metrics(trading_dates[0], trading_dates[-1])
    }
    rate = metrics[date(2025, 4, 1)].nxt_change_rate

    assert rate is not None
    assert metrics[date(2025, 4, 1)].nxt_index_value == pytest.approx(100.0)
    assert metrics[date(2025, 3, 31)].nxt_index_value == pytest.approx(
        100 / (1 + rate)
    )
    assert metrics[date(2025, 4, 2)].nxt_index_value == pytest.approx(
        100 * (1 + rate)
    )


def test_live_metric_is_replaced_by_final_snapshot(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 5)
    store.save_live_metric(
        trading_date,
        nxt_volume=10,
        nxt_amount=20,
        krx_volume=100,
        krx_amount=200,
        nxt_stock_count=2,
    )
    assert store.list_metrics(trading_date, trading_date)[0].is_final is False

    statuses, snapshot = _snapshot(trading_date)
    store.save_historical_snapshot(trading_date, statuses, snapshot)

    metric = store.list_metrics(trading_date, trading_date)[0]
    assert metric.is_final is True
    assert metric.nxt_volume == 300

    store.save_live_metric(
        trading_date,
        nxt_volume=1,
        nxt_amount=2,
        krx_volume=3,
        krx_amount=4,
        nxt_stock_count=1,
    )
    preserved = store.list_metrics(trading_date, trading_date)[0]
    assert preserved.is_final is True
    assert preserved.nxt_volume == 300


def test_save_accepts_snapshot_cached_before_futures_support(tmp_path: Path) -> None:
    store = HistoricalMarketStore(tmp_path / "history.db")
    trading_date = date(2026, 8, 5)
    statuses, snapshot = _snapshot(trading_date)
    legacy_snapshot = SimpleNamespace(
        stock_quotes=snapshot.stock_quotes,
        index_quotes=snapshot.index_quotes,
        listed_shares=snapshot.listed_shares,
    )

    metric = store.save_historical_snapshot(  # type: ignore[arg-type]
        trading_date,
        statuses,
        legacy_snapshot,
    )

    assert metric.is_final is True
    assert store.future_dates() == set()


def test_recent_six_month_window_and_average_totals() -> None:
    assert six_calendar_month_window_start(date(2026, 8, 5)) == date(2026, 3, 1)
    updated_at = datetime(2026, 8, 5, 18, tzinfo=KST)
    metrics = [
        DailyMarketMetric(
            date(2026, 8, 4),
            100,
            1_000,
            400,
            2_000,
            0.25,
            0.5,
            600,
            True,
            "KRX_OPENAPI_INDEX",
            updated_at,
        ),
        DailyMarketMetric(
            date(2026, 8, 5),
            300,
            3_000,
            600,
            6_000,
            0.5,
            0.5,
            600,
            True,
            "KRX_OPENAPI_INDEX",
            updated_at,
        ),
    ]

    result = average_market_totals(metrics)

    assert result["nxt_volume"] == 200
    assert result["krx_volume"] == 500
    assert result["nxt_amount"] == 2_000
    assert result["krx_amount"] == 4_000
    assert result["volume_ratio"] == 0.4
    assert result["amount_ratio"] == 0.5


def test_monthly_six_month_volume_ratio_uses_ratio_of_sums() -> None:
    updated_at = datetime(2026, 8, 5, 18, tzinfo=KST)
    metrics = [
        DailyMarketMetric(
            date(2026, month, 28),
            month * 10,
            0,
            month * 100,
            0,
            0.1,
            None,
            1,
            True,
            "KRX_OPENAPI_INDEX",
            updated_at,
        )
        for month in range(3, 9)
    ]

    rows = monthly_six_month_volume_ratios(metrics)

    assert len(rows) == 1
    assert rows[0]["month_end"] == date(2026, 8, 28)
    assert rows[0]["volume_ratio"] == 0.1
    assert rows[0]["trading_days"] == 6
