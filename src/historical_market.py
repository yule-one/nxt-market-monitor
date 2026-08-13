from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from src.config import NXT_CHANGE_PAGE_URL, NXT_TRADING_STATUS_PAGE_URL
from src.krx_openapi import KrxDailySnapshot
from src.krx_index_constituents import KrxIndexConstituent
from src.kis_rest import FutureQuote, IndexQuote, NxtSessionQuote, RestQuote
from src.market_realtime import KRX
from src.models import NxtChange, NxtTradingStatus
from src.nxt_eligibility import (
    NxtDailyEligibilitySummary,
    NxtDailyUnavailability,
    NxtDailyReasonCount,
    NxtEligibilityAdjustment,
    NxtUnavailabilityEvent,
    NxtUnavailabilityKindLink,
    calculate_daily_eligibility,
    classify_nxt_change_type,
    infer_missing_restriction_statuses,
)
from src.nxt_price_limits import (
    calculate_stock_price_limits,
    limit_proximity_ticks,
    reached_limit_points,
)


KST = ZoneInfo("Asia/Seoul")
NXT_INDEX_BASE_DATE = date(2025, 4, 1)
NXT_INDEX_BASE_VALUE = 100.0


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def six_calendar_month_window_start(anchor: date) -> date:
    """기준월을 포함한 최근 6개 달의 첫날을 반환합니다."""

    month_index = anchor.year * 12 + anchor.month - 1 - 5
    return date(month_index // 12, month_index % 12 + 1, 1)


@dataclass(frozen=True)
class DailyMarketMetric:
    trade_date: date
    nxt_volume: int | None
    nxt_amount: int | None
    krx_volume: int | None
    krx_amount: int | None
    volume_ratio: float | None
    amount_ratio: float | None
    nxt_stock_count: int
    is_final: bool
    krx_basis: str
    updated_at: datetime
    nxt_index_value: float | None = None
    nxt_change_rate: float | None = None
    tmi_index_value: float | None = None
    tmi_change_rate: float | None = None


@dataclass(frozen=True)
class NxtLimitHit:
    trade_date: date
    stock_code: str
    stock_name: str
    market: str
    direction: str
    reference_price: int | None
    upper_limit_price: int | None
    lower_limit_price: int | None
    open_price: int | None
    high_price: int | None
    low_price: int | None
    close_price: int | None
    hit_time: str | None
    hit_time_status: str
    updated_at: datetime


@dataclass(frozen=True)
class NxtLimitProximityHit:
    trade_date: date
    stock_code: str
    stock_name: str
    market: str
    direction: str
    distance_ticks: int
    closest_price: int
    reference_price: int | None
    upper_limit_price: int | None
    lower_limit_price: int | None
    open_price: int | None
    high_price: int | None
    low_price: int | None
    close_price: int | None
    hit_time: str | None
    updated_at: datetime


def average_market_totals(
    metrics: list[DailyMarketMetric],
) -> dict[str, object]:
    """확정 거래일 합계를 일평균으로 바꾸고 기간 누적 비율을 계산합니다."""

    def average(field_name: str) -> int | None:
        values = [
            int(value)
            for item in metrics
            if (value := getattr(item, field_name)) is not None
        ]
        return round(sum(values) / len(values)) if values else None

    volume_pairs = [
        (int(item.nxt_volume), int(item.krx_volume))
        for item in metrics
        if item.nxt_volume is not None and item.krx_volume is not None
    ]
    amount_pairs = [
        (int(item.nxt_amount), int(item.krx_amount))
        for item in metrics
        if item.nxt_amount is not None and item.krx_amount is not None
    ]
    volume_denominator = sum(krx for _nxt, krx in volume_pairs)
    amount_denominator = sum(krx for _nxt, krx in amount_pairs)
    return {
        "nxt_volume": average("nxt_volume"),
        "nxt_amount": average("nxt_amount"),
        "krx_volume": average("krx_volume"),
        "krx_amount": average("krx_amount"),
        "volume_ratio": (
            sum(nxt for nxt, _krx in volume_pairs) / volume_denominator
            if volume_denominator > 0
            else None
        ),
        "amount_ratio": (
            sum(nxt for nxt, _krx in amount_pairs) / amount_denominator
            if amount_denominator > 0
            else None
        ),
    }


def monthly_six_month_volume_ratios(
    metrics: list[DailyMarketMetric],
) -> list[dict[str, object]]:
    """월말마다 해당 월을 포함한 최근 6개월 누적 NXT/KRX 거래량비율을 계산합니다."""

    final_metrics = [item for item in metrics if item.is_final]
    month_ends: dict[tuple[int, int], date] = {}
    for item in final_metrics:
        month_ends[(item.trade_date.year, item.trade_date.month)] = item.trade_date
    results: list[dict[str, object]] = []
    for month_end in sorted(month_ends.values()):
        window_start = six_calendar_month_window_start(month_end)
        window = [
            item
            for item in final_metrics
            if window_start <= item.trade_date <= month_end
            and item.nxt_volume is not None
            and item.krx_volume is not None
        ]
        covered_months = {
            (item.trade_date.year, item.trade_date.month) for item in window
        }
        krx_volume = sum(int(item.krx_volume or 0) for item in window)
        if len(covered_months) < 6 or krx_volume <= 0:
            continue
        nxt_volume = sum(int(item.nxt_volume or 0) for item in window)
        results.append(
            {
                "month_end": month_end,
                "window_start": window_start,
                "nxt_volume": nxt_volume,
                "krx_volume": krx_volume,
                "volume_ratio": nxt_volume / krx_volume,
                "trading_days": len(window),
            }
        )
    return results


def _weighted_nxt_change_rate(
    statuses: Iterable[NxtTradingStatus],
    krx_snapshot: KrxDailySnapshot,
) -> float | None:
    numerator = 0
    denominator = 0
    for item in statuses:
        shares = int(krx_snapshot.listed_shares.get(item.stock_code) or 0)
        current_price = int(item.current_price or 0)
        krx_quote = krx_snapshot.stock_quotes.get((item.stock_code, KRX))
        comparison_price = int(
            item.reference_price
            or (krx_quote.reference_price if krx_quote is not None else 0)
            or 0
        )
        if shares <= 0 or current_price <= 0 or comparison_price <= 0:
            continue
        numerator += current_price * shares
        denominator += comparison_price * shares
    return numerator / denominator - 1 if denominator > 0 else None


class HistoricalMarketStore:
    """NXT 대상 종목의 과거 대시보드 데이터와 일별 합계를 저장합니다."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        seed_path: Path | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "history.db"
        self.seed_path = (
            seed_path
            if seed_path is not None
            else project_root / "data" / "history.db.gz"
            if path is None
            else None
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restore_seed_if_needed()
        self._lock = threading.RLock()
        self._initialize()

    def _restore_seed_if_needed(self) -> None:
        if self.seed_path is None or not self.seed_path.exists():
            return
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                prefix=".history-restore-",
                suffix=".db",
                delete=False,
            ) as target:
                temporary_path = Path(target.name)
                with gzip.open(self.seed_path, "rb") as source:
                    shutil.copyfileobj(source, target)
            seed_counts = self._database_table_counts(temporary_path)
            current_counts = self._database_table_counts(self.path)
            current_is_complete = bool(seed_counts) and bool(current_counts) and all(
                current_counts.get(table_name, 0) >= seed_count
                for table_name, seed_count in seed_counts.items()
            )
            if current_is_complete:
                return
            if self.path.exists():
                self._replace_database_from_snapshot(temporary_path)
            else:
                os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _database_table_counts(path: Path) -> dict[str, int]:
        if not path.exists():
            return {}
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            try:
                table_names = [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                ]
                return {
                    table_name: int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table_name}"'
                        ).fetchone()[0]
                    )
                    for table_name in table_names
                }
            finally:
                connection.close()
        except sqlite3.Error:
            return {}

    def _replace_database_from_snapshot(self, snapshot_path: Path) -> None:
        source = sqlite3.connect(snapshot_path, timeout=30)
        target = sqlite3.connect(self.path, timeout=30)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_market_metrics (
                    trade_date TEXT PRIMARY KEY,
                    nxt_volume INTEGER,
                    nxt_amount INTEGER,
                    krx_volume INTEGER,
                    krx_amount INTEGER,
                    volume_ratio REAL,
                    amount_ratio REAL,
                    nxt_stock_count INTEGER NOT NULL,
                    is_final INTEGER NOT NULL,
                    krx_basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    nxt_index_value REAL,
                    nxt_change_rate REAL,
                    tmi_index_value REAL,
                    tmi_change_rate REAL
                );

                CREATE TABLE IF NOT EXISTS nxt_daily_quotes (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    tradable_market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    reference_price INTEGER,
                    current_price INTEGER,
                    change_value INTEGER,
                    change_rate REAL,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    quote_time TEXT NOT NULL,
                    open_price INTEGER,
                    high_price INTEGER,
                    low_price INTEGER,
                    upper_limit_price INTEGER,
                    lower_limit_price INTEGER,
                    PRIMARY KEY (trade_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_membership_changes (
                    change_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    registered_at INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (change_date, stock_code, change_type, reason)
                );

                CREATE TABLE IF NOT EXISTS nxt_membership_change_adjustments (
                    change_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    display_reason TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (change_date, stock_code, change_type)
                );

                CREATE TABLE IF NOT EXISTS nxt_daily_eligibility_summary (
                    trade_date TEXT PRIMARY KEY,
                    target_stock_count INTEGER NOT NULL,
                    tradable_stock_count INTEGER NOT NULL,
                    unavailable_stock_count INTEGER NOT NULL,
                    target_kospi_count INTEGER NOT NULL,
                    target_kosdaq_count INTEGER NOT NULL,
                    tradable_kospi_count INTEGER NOT NULL,
                    tradable_kosdaq_count INTEGER NOT NULL,
                    inclusion_stock_count INTEGER NOT NULL,
                    exclusion_stock_count INTEGER NOT NULL,
                    restriction_start_stock_count INTEGER NOT NULL,
                    restriction_end_stock_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nxt_daily_eligibility_reason_counts (
                    trade_date TEXT NOT NULL,
                    reason_group TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    stock_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, reason_group, reason)
                );

                CREATE TABLE IF NOT EXISTS nxt_daily_eligibility_adjustments (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    restriction_start_date TEXT NOT NULL,
                    restriction_end_date TEXT,
                    basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_daily_unavailability (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    tradable_market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_unavailability_events (
                    event_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    tradable_market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_date, stock_code, event_type)
                );

                CREATE TABLE IF NOT EXISTS nxt_unavailability_kind_links (
                    event_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    report_no TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    disclosed_at TEXT NOT NULL,
                    viewer_url TEXT NOT NULL,
                    match_basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_date, stock_code, event_type)
                );

                CREATE TABLE IF NOT EXISTS krx_daily_quotes (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    current_price INTEGER NOT NULL,
                    reference_price INTEGER,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    market_cap INTEGER,
                    listed_shares INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS krx_daily_indices (
                    trade_date TEXT NOT NULL,
                    index_name TEXT NOT NULL,
                    index_code TEXT NOT NULL,
                    current_value REAL NOT NULL,
                    change_value REAL NOT NULL,
                    change_rate REAL NOT NULL,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, index_name)
                );

                CREATE TABLE IF NOT EXISTS krx_index_constituents (
                    trade_date TEXT NOT NULL,
                    index_name TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, index_name, stock_code)
                );

                CREATE TABLE IF NOT EXISTS krx_daily_futures (
                    trade_date TEXT NOT NULL,
                    future_name TEXT NOT NULL,
                    contract_code TEXT NOT NULL,
                    current_value REAL NOT NULL,
                    change_value REAL NOT NULL,
                    change_rate REAL NOT NULL,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    open_interest INTEGER,
                    settlement_price REAL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, future_name)
                );

                CREATE TABLE IF NOT EXISTS market_daily_fx (
                    trade_date TEXT NOT NULL,
                    pair_name TEXT NOT NULL,
                    pair_code TEXT NOT NULL,
                    current_value REAL NOT NULL,
                    change_value REAL NOT NULL,
                    change_rate REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, pair_name)
                );

                CREATE TABLE IF NOT EXISTS nxt_session_quotes (
                    trade_date TEXT NOT NULL,
                    session TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    reference_price INTEGER,
                    open_price INTEGER,
                    high_price INTEGER,
                    low_price INTEGER,
                    close_price INTEGER,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    last_trade_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, session, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_limit_hit_times (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    hit_time TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code, direction)
                );

                CREATE TABLE IF NOT EXISTS nxt_limit_proximity_times (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    hit_time TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code, direction)
                );

                CREATE TABLE IF NOT EXISTS nxt_limit_proximity_hits (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    distance_ticks INTEGER NOT NULL,
                    closest_price INTEGER NOT NULL,
                    reference_price INTEGER,
                    upper_limit_price INTEGER,
                    lower_limit_price INTEGER,
                    open_price INTEGER,
                    high_price INTEGER,
                    low_price INTEGER,
                    close_price INTEGER,
                    hit_time TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code, direction)
                );

                CREATE TABLE IF NOT EXISTS nxt_limit_proximity_daily (
                    trade_date TEXT PRIMARY KEY,
                    source_stock_count INTEGER NOT NULL,
                    ohlc_stock_count INTEGER NOT NULL,
                    upper_exact_count INTEGER NOT NULL,
                    lower_exact_count INTEGER NOT NULL,
                    upper_near_count INTEGER NOT NULL,
                    lower_near_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nxt_limit_hits (
                    trade_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    reference_price INTEGER,
                    upper_limit_price INTEGER,
                    lower_limit_price INTEGER,
                    open_price INTEGER,
                    high_price INTEGER,
                    low_price INTEGER,
                    close_price INTEGER,
                    hit_time TEXT,
                    hit_time_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, stock_code, direction)
                );

                CREATE INDEX IF NOT EXISTS idx_nxt_daily_quotes_date
                    ON nxt_daily_quotes(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_daily_eligibility_reason_date
                    ON nxt_daily_eligibility_reason_counts(trade_date, reason_group);
                CREATE INDEX IF NOT EXISTS idx_nxt_daily_eligibility_adjustment_date
                    ON nxt_daily_eligibility_adjustments(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_daily_unavailability_date
                    ON nxt_daily_unavailability(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_unavailability_events_date
                    ON nxt_unavailability_events(event_date, event_type);
                CREATE INDEX IF NOT EXISTS idx_nxt_unavailability_kind_links_report
                    ON nxt_unavailability_kind_links(report_no);
                CREATE INDEX IF NOT EXISTS idx_krx_daily_quotes_date
                    ON krx_daily_quotes(trade_date);
                CREATE INDEX IF NOT EXISTS idx_krx_index_constituents_code
                    ON krx_index_constituents(trade_date, stock_code, index_name);
                CREATE INDEX IF NOT EXISTS idx_krx_daily_futures_date
                    ON krx_daily_futures(trade_date);
                CREATE INDEX IF NOT EXISTS idx_market_daily_fx_date
                    ON market_daily_fx(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_session_quotes_date
                    ON nxt_session_quotes(trade_date, session);
                CREATE INDEX IF NOT EXISTS idx_nxt_limit_hit_times_date
                    ON nxt_limit_hit_times(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_limit_proximity_times_date
                    ON nxt_limit_proximity_times(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_limit_proximity_hits_date
                    ON nxt_limit_proximity_hits(trade_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_limit_hits_date
                    ON nxt_limit_hits(trade_date);
                """
            )
            existing_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(daily_market_metrics)"
                ).fetchall()
            }
            for column_name in [
                "nxt_index_value",
                "nxt_change_rate",
                "tmi_index_value",
                "tmi_change_rate",
            ]:
                if column_name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE daily_market_metrics ADD COLUMN {column_name} REAL"
                    )
            nxt_quote_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(nxt_daily_quotes)"
                ).fetchall()
            }
            for column_name in [
                "open_price",
                "high_price",
                "low_price",
                "upper_limit_price",
                "lower_limit_price",
            ]:
                if column_name not in nxt_quote_columns:
                    connection.execute(
                        f"ALTER TABLE nxt_daily_quotes ADD COLUMN {column_name} INTEGER"
                    )
            session_quote_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(nxt_session_quotes)"
                ).fetchall()
            }
            if "reference_price" not in session_quote_columns:
                connection.execute(
                    "ALTER TABLE nxt_session_quotes ADD COLUMN reference_price INTEGER"
                )

    @staticmethod
    def _replace_nxt_limit_hits(
        connection: sqlite3.Connection,
        trading_date: date,
        statuses: list[NxtTradingStatus],
        *,
        retention_expired: bool = False,
    ) -> int:
        date_key = trading_date.isoformat()
        stored_times = {
            (str(row["stock_code"]), str(row["direction"])): str(row["hit_time"])
            for row in connection.execute(
                """
                SELECT stock_code, direction, hit_time
                FROM nxt_limit_hit_times
                WHERE trade_date = ?
                """,
                (date_key,),
            ).fetchall()
        }
        updated_at = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[object, ...]] = []
        for item in statuses:
            for direction, limit_price in (
                ("상한가", item.upper_limit_price),
                ("하한가", item.lower_limit_price),
            ):
                points = reached_limit_points(
                    open_price=item.open_price,
                    high_price=item.high_price,
                    low_price=item.low_price,
                    close_price=item.current_price,
                    limit_price=limit_price,
                )
                if not points:
                    continue
                key = (item.stock_code, direction)
                hit_time = "OPEN" if "시가" in points else stored_times.get(key)
                if hit_time == "OPEN":
                    hit_time_status = "OPEN"
                elif hit_time and len(hit_time) == 6 and hit_time.isdigit():
                    hit_time_status = "EXACT"
                elif retention_expired:
                    hit_time_status = "RETENTION_EXPIRED"
                else:
                    hit_time_status = "PENDING"
                rows.append(
                    (
                        date_key,
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        direction,
                        item.reference_price,
                        item.upper_limit_price,
                        item.lower_limit_price,
                        item.open_price,
                        item.high_price,
                        item.low_price,
                        item.current_price,
                        hit_time,
                        hit_time_status,
                        updated_at,
                    )
                )
        connection.execute(
            "DELETE FROM nxt_limit_hits WHERE trade_date = ?",
            (date_key,),
        )
        connection.executemany(
            """
            INSERT INTO nxt_limit_hits (
                trade_date, stock_code, stock_name, market, direction,
                reference_price, upper_limit_price, lower_limit_price,
                open_price, high_price, low_price, close_price,
                hit_time, hit_time_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def replace_nxt_limit_hits(
        self,
        trading_date: date,
        statuses: Iterable[NxtTradingStatus],
        *,
        retention_expired: bool = False,
    ) -> int:
        status_rows = list(statuses)
        with self._lock, self._connect() as connection:
            return self._replace_nxt_limit_hits(
                connection,
                trading_date,
                status_rows,
                retention_expired=retention_expired,
            )

    @staticmethod
    def _replace_nxt_limit_proximity_hits(
        connection: sqlite3.Connection,
        trading_date: date,
        statuses: list[NxtTradingStatus],
    ) -> int:
        date_key = trading_date.isoformat()
        stored_times = {
            (str(row["stock_code"]), str(row["direction"])): str(row["hit_time"])
            for row in connection.execute(
                """
                SELECT stock_code, direction, hit_time
                FROM nxt_limit_proximity_times
                WHERE trade_date = ?
                """,
                (date_key,),
            ).fetchall()
        }
        updated_at = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[object, ...]] = []
        for item in statuses:
            price_points = (
                ("시가", item.open_price),
                ("고가", item.high_price),
                ("저가", item.low_price),
                ("종가", item.current_price),
            )
            for direction, limit_price in (
                ("상한가", item.upper_limit_price),
                ("하한가", item.lower_limit_price),
            ):
                candidates: list[tuple[int, int, str]] = []
                for label, price in price_points:
                    distance = limit_proximity_ticks(
                        price,
                        limit_price,
                        direction,
                        market=item.market,
                    )
                    if distance is not None and price is not None:
                        candidates.append((distance, int(price), label))
                if not candidates:
                    continue
                distance, closest_price, _label = min(
                    candidates,
                    key=lambda value: (
                        value[0],
                        -value[1] if direction == "상한가" else value[1],
                    ),
                )
                key = (item.stock_code, direction)
                hit_time = (
                    "OPEN"
                    if any(label == "시가" for _distance, _price, label in candidates)
                    else stored_times.get(key)
                )
                rows.append(
                    (
                        date_key,
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        direction,
                        distance,
                        closest_price,
                        item.reference_price,
                        item.upper_limit_price,
                        item.lower_limit_price,
                        item.open_price,
                        item.high_price,
                        item.low_price,
                        item.current_price,
                        hit_time,
                        updated_at,
                    )
                )
        connection.execute(
            "DELETE FROM nxt_limit_proximity_hits WHERE trade_date = ?",
            (date_key,),
        )
        connection.executemany(
            """
            INSERT INTO nxt_limit_proximity_hits (
                trade_date, stock_code, stock_name, market, direction,
                distance_ticks, closest_price, reference_price,
                upper_limit_price, lower_limit_price, open_price, high_price,
                low_price, close_price, hit_time, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        ohlc_count = sum(
            all(
                value is not None
                for value in (
                    item.open_price,
                    item.high_price,
                    item.low_price,
                    item.current_price,
                )
            )
            for item in statuses
        )
        counts = {
            (direction, exact): sum(
                str(row[4]) == direction
                and ((int(row[5]) == 0) if exact else (1 <= int(row[5]) <= 3))
                for row in rows
            )
            for direction in ("상한가", "하한가")
            for exact in (True, False)
        }
        connection.execute(
            """
            INSERT INTO nxt_limit_proximity_daily (
                trade_date, source_stock_count, ohlc_stock_count,
                upper_exact_count, lower_exact_count,
                upper_near_count, lower_near_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                source_stock_count = excluded.source_stock_count,
                ohlc_stock_count = excluded.ohlc_stock_count,
                upper_exact_count = excluded.upper_exact_count,
                lower_exact_count = excluded.lower_exact_count,
                upper_near_count = excluded.upper_near_count,
                lower_near_count = excluded.lower_near_count,
                updated_at = excluded.updated_at
            """,
            (
                date_key,
                len(statuses),
                ohlc_count,
                counts[("상한가", True)],
                counts[("하한가", True)],
                counts[("상한가", False)],
                counts[("하한가", False)],
                updated_at,
            ),
        )
        return len(rows)

    def replace_nxt_limit_proximity_hits(
        self,
        trading_date: date,
        statuses: Iterable[NxtTradingStatus],
    ) -> int:
        status_rows = list(statuses)
        with self._lock, self._connect() as connection:
            return self._replace_nxt_limit_proximity_hits(
                connection,
                trading_date,
                status_rows,
            )

    @staticmethod
    def _replace_nxt_eligibility_for_date(
        connection: sqlite3.Connection,
        trading_date: date,
        statuses: Iterable[NxtTradingStatus],
    ) -> None:
        date_key = trading_date.isoformat()
        status_rows = list(statuses)
        known_codes = {item.stock_code for item in status_rows}
        adjustment_rows = connection.execute(
            """
            SELECT stock_code, stock_name, market, unavailable_reason
            FROM nxt_daily_eligibility_adjustments
            WHERE trade_date = ?
            """,
            (date_key,),
        ).fetchall()
        status_rows.extend(
            NxtTradingStatus(
                status_date=trading_date,
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                tradable_market="거래불가",
                unavailable_reason=str(row["unavailable_reason"]),
            )
            for row in adjustment_rows
            if str(row["stock_code"]) not in known_codes
        )
        change_rows = connection.execute(
            """
            SELECT change_date, stock_code, stock_name, market, change_type,
                   reason, isin, registered_at, '' AS display_reason,
                   '' AS basis, '' AS source_title, '' AS source_url,
                   0 AS is_inferred
            FROM nxt_membership_changes WHERE change_date = ?
            UNION ALL
            SELECT change_date, stock_code, stock_name, market, change_type,
                   reason, isin, 0 AS registered_at, display_reason,
                   basis, source_title, source_url, 1 AS is_inferred
            FROM nxt_membership_change_adjustments WHERE change_date = ?
            ORDER BY registered_at, stock_code
            """,
            (date_key, date_key),
        ).fetchall()
        changes = [
            NxtChange(
                change_date=trading_date,
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                change_type=str(row["change_type"]),
                reason=str(row["reason"]),
                isin=str(row["isin"]),
                registered_at=int(row["registered_at"]),
                display_reason=str(row["display_reason"]),
                basis=str(row["basis"]),
                source_title=str(row["source_title"]),
                source_url=str(row["source_url"]),
                is_inferred=bool(row["is_inferred"]),
            )
            for row in change_rows
        ]
        summary, reason_counts = calculate_daily_eligibility(
            trading_date,
            status_rows,
            changes,
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO nxt_daily_eligibility_summary (
                trade_date, target_stock_count, tradable_stock_count,
                unavailable_stock_count, target_kospi_count,
                target_kosdaq_count, tradable_kospi_count,
                tradable_kosdaq_count, inclusion_stock_count,
                exclusion_stock_count, restriction_start_stock_count,
                restriction_end_stock_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                target_stock_count = excluded.target_stock_count,
                tradable_stock_count = excluded.tradable_stock_count,
                unavailable_stock_count = excluded.unavailable_stock_count,
                target_kospi_count = excluded.target_kospi_count,
                target_kosdaq_count = excluded.target_kosdaq_count,
                tradable_kospi_count = excluded.tradable_kospi_count,
                tradable_kosdaq_count = excluded.tradable_kosdaq_count,
                inclusion_stock_count = excluded.inclusion_stock_count,
                exclusion_stock_count = excluded.exclusion_stock_count,
                restriction_start_stock_count = excluded.restriction_start_stock_count,
                restriction_end_stock_count = excluded.restriction_end_stock_count,
                updated_at = excluded.updated_at
            """,
            (
                date_key,
                summary.target_stock_count,
                summary.tradable_stock_count,
                summary.unavailable_stock_count,
                summary.target_kospi_count,
                summary.target_kosdaq_count,
                summary.tradable_kospi_count,
                summary.tradable_kosdaq_count,
                summary.inclusion_stock_count,
                summary.exclusion_stock_count,
                summary.restriction_start_stock_count,
                summary.restriction_end_stock_count,
                updated_at,
            ),
        )
        connection.execute(
            "DELETE FROM nxt_daily_eligibility_reason_counts WHERE trade_date = ?",
            (date_key,),
        )
        connection.executemany(
            """
            INSERT INTO nxt_daily_eligibility_reason_counts (
                trade_date, reason_group, reason, stock_count, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    date_key,
                    item.reason_group,
                    item.reason,
                    item.stock_count,
                    updated_at,
                )
                for item in reason_counts
            ],
        )

    @staticmethod
    def _rebuild_nxt_unavailability_history(
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        """거래현황을 우선으로 거래불가 일별 상태와 지정·해제 이력을 재구성합니다."""

        updated_at = datetime.now(timezone.utc).isoformat()
        connection.execute("DELETE FROM nxt_daily_unavailability")
        connection.execute("DELETE FROM nxt_unavailability_events")

        daily_rows: dict[tuple[str, str], tuple[object, ...]] = {}
        quote_rows = connection.execute(
            """
            SELECT trade_date, stock_code, stock_name, market,
                   tradable_market, unavailable_reason
            FROM nxt_daily_quotes
            ORDER BY trade_date, stock_code
            """
        ).fetchall()
        statuses_by_date: dict[str, dict[str, sqlite3.Row]] = {}
        for row in quote_rows:
            date_key = str(row["trade_date"])
            stock_code = str(row["stock_code"])
            statuses_by_date.setdefault(date_key, {})[stock_code] = row
            if str(row["tradable_market"]).strip() != "거래불가":
                continue
            unavailable_reason = str(row["unavailable_reason"]).strip()
            if unavailable_reason in {"", "-"}:
                unavailable_reason = "사유 미제공"
            daily_rows[(date_key, stock_code)] = (
                date_key,
                stock_code,
                str(row["stock_name"]),
                str(row["market"]),
                "거래불가",
                unavailable_reason,
                "OFFICIAL_STATUS",
                "NXT 거래현황",
                NXT_TRADING_STATUS_PAGE_URL,
                "NXT 거래현황의 거래가능시장·거래불가사유",
                updated_at,
            )

        adjustment_rows = connection.execute(
            """
            SELECT trade_date, stock_code, stock_name, market,
                   unavailable_reason, basis
            FROM nxt_daily_eligibility_adjustments
            ORDER BY trade_date, stock_code
            """
        ).fetchall()
        for row in adjustment_rows:
            date_key = str(row["trade_date"])
            stock_code = str(row["stock_code"])
            key = (date_key, stock_code)
            if key in daily_rows:
                continue
            unavailable_reason = str(row["unavailable_reason"]).strip()
            if unavailable_reason in {"", "-"}:
                unavailable_reason = "사유 미제공"
            daily_rows[key] = (
                date_key,
                stock_code,
                str(row["stock_name"]),
                str(row["market"]),
                "거래불가",
                unavailable_reason,
                "LEGACY_CHANGE",
                "NXT 종목 변동내역",
                NXT_CHANGE_PAGE_URL,
                str(row["basis"]),
                updated_at,
            )

        legacy_events: dict[tuple[str, str, str], sqlite3.Row] = {}
        for row in connection.execute(
            """
            SELECT change_date, stock_code, change_type, reason
            FROM nxt_membership_changes
            ORDER BY change_date, registered_at, stock_code
            """
        ).fetchall():
            event_type = classify_nxt_change_type(
                str(row["change_type"]),
                str(row["reason"]),
            )
            if event_type in {"거래불가", "거래불가 해제"}:
                legacy_events[
                    (
                        str(row["change_date"]),
                        str(row["stock_code"]),
                        event_type,
                    )
                ] = row

        connection.executemany(
            """
            INSERT INTO nxt_daily_unavailability (
                trade_date, stock_code, stock_name, market, tradable_market,
                unavailable_reason, source_type, source_title, source_url,
                basis, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            list(daily_rows.values()),
        )

        unavailable_by_date: dict[str, dict[str, tuple[object, ...]]] = {}
        for row in daily_rows.values():
            unavailable_by_date.setdefault(str(row[0]), {})[str(row[1])] = row

        event_rows: list[tuple[object, ...]] = []
        previous_unavailable: dict[str, tuple[object, ...]] = {}
        for date_key in sorted(statuses_by_date):
            current_unavailable = unavailable_by_date.get(date_key, {})
            current_statuses = statuses_by_date[date_key]
            for stock_code in sorted(
                current_unavailable.keys() - previous_unavailable.keys()
            ):
                row = current_unavailable[stock_code]
                legacy_event = legacy_events.get((date_key, stock_code, "거래불가"))
                if legacy_event is not None:
                    source_type = "LEGACY_CHANGE"
                    source_title = "NXT 종목 변동내역"
                    source_url = NXT_CHANGE_PAGE_URL
                    basis = f"NXT 변동내역: {legacy_event['reason']}"
                else:
                    source_type = str(row[6])
                    source_title = str(row[7])
                    source_url = str(row[8])
                    basis = str(row[9])
                event_rows.append(
                    (
                        date_key,
                        stock_code,
                        row[2],
                        row[3],
                        "거래불가",
                        row[4],
                        row[5],
                        source_type,
                        source_title,
                        source_url,
                        basis,
                        updated_at,
                    )
                )
            for stock_code in sorted(
                previous_unavailable.keys() - current_unavailable.keys()
            ):
                current_status = current_statuses.get(stock_code)
                if current_status is None:
                    continue
                tradable_market = str(current_status["tradable_market"]).strip()
                if tradable_market == "거래불가":
                    continue
                previous_row = previous_unavailable[stock_code]
                legacy_event = legacy_events.get(
                    (date_key, stock_code, "거래불가 해제")
                )
                if legacy_event is not None:
                    source_type = "LEGACY_CHANGE"
                    source_title = "NXT 종목 변동내역"
                    source_url = NXT_CHANGE_PAGE_URL
                    basis = f"NXT 변동내역: {legacy_event['reason']}"
                else:
                    source_type = "OFFICIAL_STATUS"
                    source_title = "NXT 거래현황"
                    source_url = NXT_TRADING_STATUS_PAGE_URL
                    basis = (
                        "전 거래일 거래불가 → 당일 거래가능시장 "
                        f"{tradable_market or '전체'}"
                    )
                event_rows.append(
                    (
                        date_key,
                        stock_code,
                        str(current_status["stock_name"]),
                        str(current_status["market"]),
                        "거래불가 해제",
                        tradable_market or "전체",
                        previous_row[5],
                        source_type,
                        source_title,
                        source_url,
                        basis,
                        updated_at,
                    )
                )
            previous_unavailable = current_unavailable

        connection.executemany(
            """
            INSERT INTO nxt_unavailability_events (
                event_date, stock_code, stock_name, market, event_type,
                tradable_market, unavailable_reason, source_type,
                source_title, source_url, basis, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            event_rows,
        )
        connection.execute(
            """
            DELETE FROM nxt_unavailability_kind_links
            WHERE NOT EXISTS (
                SELECT 1
                FROM nxt_unavailability_events AS event
                WHERE event.event_date = nxt_unavailability_kind_links.event_date
                  AND event.stock_code = nxt_unavailability_kind_links.stock_code
                  AND event.event_type = nxt_unavailability_kind_links.event_type
            )
            """
        )
        return {
            "daily_rows": len(daily_rows),
            "event_rows": len(event_rows),
        }

    def rebuild_nxt_unavailability_history(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            return self._rebuild_nxt_unavailability_history(connection)

    def rebuild_nxt_eligibility_history(
        self,
        start_date: date,
        end_date: date,
    ) -> int:
        """저장된 NXT 종목 현황과 변동 원본으로 일별 선정·거래상태를 재계산합니다."""

        if end_date < start_date:
            return 0
        with self._lock, self._connect() as connection:
            date_rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM nxt_daily_quotes
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            trading_dates = [
                date.fromisoformat(str(row["trade_date"])) for row in date_rows
            ]
            statuses_by_date: dict[date, list[NxtTradingStatus]] = {}
            for trading_date in trading_dates:
                status_rows = connection.execute(
                    """
                    SELECT * FROM nxt_daily_quotes
                    WHERE trade_date = ?
                    ORDER BY market, stock_code
                    """,
                    (trading_date.isoformat(),),
                ).fetchall()
                statuses_by_date[trading_date] = [
                    self._nxt_status_from_row(trading_date, status_row)
                    for status_row in status_rows
                ]
            change_rows = connection.execute(
                """
                SELECT change_date, stock_code, stock_name, market, change_type,
                       reason, isin, registered_at, '' AS display_reason,
                       '' AS basis, '' AS source_title, '' AS source_url,
                       0 AS is_inferred
                FROM nxt_membership_changes WHERE change_date <= ?
                UNION ALL
                SELECT change_date, stock_code, stock_name, market, change_type,
                       reason, isin, 0 AS registered_at, display_reason,
                       basis, source_title, source_url, 1 AS is_inferred
                FROM nxt_membership_change_adjustments WHERE change_date <= ?
                ORDER BY change_date, registered_at, stock_code
                """,
                (end_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            changes = [
                NxtChange(
                    change_date=date.fromisoformat(str(row["change_date"])),
                    stock_code=str(row["stock_code"]),
                    stock_name=str(row["stock_name"]),
                    market=str(row["market"]),
                    change_type=str(row["change_type"]),
                    reason=str(row["reason"]),
                    isin=str(row["isin"]),
                    registered_at=int(row["registered_at"]),
                    display_reason=str(row["display_reason"]),
                    basis=str(row["basis"]),
                    source_title=str(row["source_title"]),
                    source_url=str(row["source_url"]),
                    is_inferred=bool(row["is_inferred"]),
                )
                for row in change_rows
            ]
            adjustments_by_date = infer_missing_restriction_statuses(
                trading_dates,
                statuses_by_date,
                changes,
            )
            connection.execute(
                "DELETE FROM nxt_daily_eligibility_summary "
                "WHERE trade_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.execute(
                "DELETE FROM nxt_daily_eligibility_reason_counts "
                "WHERE trade_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.execute(
                "DELETE FROM nxt_daily_eligibility_adjustments "
                "WHERE trade_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            updated_at = datetime.now(timezone.utc).isoformat()
            for trading_date in trading_dates:
                adjustments = adjustments_by_date.get(trading_date, [])
                statuses = list(statuses_by_date[trading_date])
                statuses.extend(
                    NxtTradingStatus(
                        status_date=trading_date,
                        stock_code=item.stock_code,
                        stock_name=item.stock_name,
                        market=item.market,
                        tradable_market="거래불가",
                        unavailable_reason=item.unavailable_reason,
                    )
                    for item in adjustments
                )
                connection.executemany(
                    """
                    INSERT INTO nxt_daily_eligibility_adjustments (
                        trade_date, stock_code, stock_name, market,
                        unavailable_reason, restriction_start_date,
                        restriction_end_date, basis, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            trading_date.isoformat(),
                            item.stock_code,
                            item.stock_name,
                            item.market,
                            item.unavailable_reason,
                            item.restriction_start_date.isoformat(),
                            (
                                item.restriction_end_date.isoformat()
                                if item.restriction_end_date
                                else None
                            ),
                            item.basis,
                            updated_at,
                        )
                        for item in adjustments
                    ],
                )
                self._replace_nxt_eligibility_for_date(
                    connection,
                    trading_date,
                    statuses,
                )
            self._rebuild_nxt_unavailability_history(connection)
        return len(trading_dates)

    def save_historical_snapshot(
        self,
        trading_date: date,
        statuses: Iterable[NxtTradingStatus],
        krx_snapshot: KrxDailySnapshot,
    ) -> DailyMarketMetric:
        status_rows = list(statuses)
        nxt_volume = sum(item.cumulative_volume for item in status_rows)
        nxt_amount = sum(item.cumulative_amount for item in status_rows)
        kospi = krx_snapshot.index_quotes.get("KOSPI")
        kosdaq = krx_snapshot.index_quotes.get("KOSDAQ")
        if kospi is not None and kosdaq is not None:
            krx_volume = kospi.cumulative_volume + kosdaq.cumulative_volume
            krx_amount = kospi.cumulative_amount + kosdaq.cumulative_amount
        else:
            krx_volume = None
            krx_amount = None
        updated_at = datetime.now(timezone.utc)
        tmi = krx_snapshot.index_quotes.get("KRX TMI")
        metric = DailyMarketMetric(
            trade_date=trading_date,
            nxt_volume=nxt_volume,
            nxt_amount=nxt_amount,
            krx_volume=krx_volume,
            krx_amount=krx_amount,
            volume_ratio=_ratio(nxt_volume, krx_volume),
            amount_ratio=_ratio(nxt_amount, krx_amount),
            nxt_stock_count=len(status_rows),
            is_final=True,
            krx_basis="KRX_OPENAPI_INDEX",
            updated_at=updated_at,
            nxt_change_rate=_weighted_nxt_change_rate(status_rows, krx_snapshot),
            tmi_index_value=tmi.current_value if tmi is not None else None,
            tmi_change_rate=tmi.change_rate / 100 if tmi is not None else None,
        )
        symbols = {item.stock_code for item in status_rows}

        with self._lock, self._connect() as connection:
            self._upsert_metric(connection, metric)
            date_key = trading_date.isoformat()
            connection.execute(
                "DELETE FROM nxt_daily_quotes WHERE trade_date = ?", (date_key,)
            )
            connection.execute(
                "DELETE FROM krx_daily_quotes WHERE trade_date = ?", (date_key,)
            )
            connection.execute(
                "DELETE FROM krx_daily_indices WHERE trade_date = ?", (date_key,)
            )
            connection.executemany(
                """
                INSERT INTO nxt_daily_quotes (
                    trade_date, stock_code, stock_name, market, tradable_market,
                    unavailable_reason, isin, reference_price, current_price,
                    change_value, change_rate, cumulative_volume,
                    cumulative_amount, quote_time, open_price, high_price,
                    low_price, upper_limit_price, lower_limit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        date_key,
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        item.tradable_market,
                        item.unavailable_reason,
                        item.isin,
                        item.reference_price,
                        item.current_price,
                        item.change_value,
                        item.change_rate,
                        item.cumulative_volume,
                        item.cumulative_amount,
                        item.quote_time,
                        item.open_price,
                        item.high_price,
                        item.low_price,
                        item.upper_limit_price,
                        item.lower_limit_price,
                    )
                    for item in status_rows
                ],
            )
            self._replace_nxt_limit_hits(connection, trading_date, status_rows)
            self._replace_nxt_limit_proximity_hits(
                connection,
                trading_date,
                status_rows,
            )
            self._replace_nxt_eligibility_for_date(
                connection,
                trading_date,
                status_rows,
            )
            self._rebuild_nxt_unavailability_history(connection)
            krx_rows: list[tuple[object, ...]] = []
            for symbol in symbols:
                quote = krx_snapshot.stock_quotes.get((symbol, KRX))
                if quote is None:
                    continue
                krx_rows.append(
                    (
                        date_key,
                        symbol,
                        quote.name,
                        quote.current_price,
                        quote.reference_price,
                        quote.cumulative_volume,
                        quote.cumulative_amount,
                        quote.market_cap,
                        krx_snapshot.listed_shares.get(symbol),
                        quote.updated_at.isoformat(),
                    )
                )
            connection.executemany(
                """
                INSERT INTO krx_daily_quotes (
                    trade_date, stock_code, stock_name, current_price,
                    reference_price, cumulative_volume, cumulative_amount,
                    market_cap, listed_shares, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                krx_rows,
            )
            connection.executemany(
                """
                INSERT INTO krx_daily_indices (
                    trade_date, index_name, index_code, current_value,
                    change_value, change_rate, cumulative_volume,
                    cumulative_amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        date_key,
                        name,
                        quote.code,
                        quote.current_value,
                        quote.change_value,
                        quote.change_rate,
                        quote.cumulative_volume,
                        quote.cumulative_amount,
                        quote.updated_at.isoformat(),
                    )
                    for name, quote in krx_snapshot.index_quotes.items()
                ],
            )
            # Accept snapshots created by an older running Streamlit process,
            # before KrxDailySnapshot gained the future_quotes field.
            future_quotes = getattr(krx_snapshot, "future_quotes", {})
            if future_quotes:
                self._replace_future_quotes(
                    connection,
                    trading_date,
                    future_quotes,
                )
        return metric

    def save_nxt_statuses(
        self,
        trading_date: date,
        statuses: Iterable[NxtTradingStatus],
    ) -> int:
        """NXT 일별 종목 시세만 교체해 OHLC 백필에 사용합니다."""

        status_rows = list(statuses)
        date_key = trading_date.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM nxt_daily_quotes WHERE trade_date = ?",
                (date_key,),
            )
            connection.executemany(
                """
                INSERT INTO nxt_daily_quotes (
                    trade_date, stock_code, stock_name, market, tradable_market,
                    unavailable_reason, isin, reference_price, current_price,
                    change_value, change_rate, cumulative_volume,
                    cumulative_amount, quote_time, open_price, high_price,
                    low_price, upper_limit_price, lower_limit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        date_key,
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        item.tradable_market,
                        item.unavailable_reason,
                        item.isin,
                        item.reference_price,
                        item.current_price,
                        item.change_value,
                        item.change_rate,
                        item.cumulative_volume,
                        item.cumulative_amount,
                        item.quote_time,
                        item.open_price,
                        item.high_price,
                        item.low_price,
                        item.upper_limit_price,
                        item.lower_limit_price,
                    )
                    for item in status_rows
                ],
            )
            self._replace_nxt_limit_hits(connection, trading_date, status_rows)
            self._replace_nxt_limit_proximity_hits(
                connection,
                trading_date,
                status_rows,
            )
            self._replace_nxt_eligibility_for_date(
                connection,
                trading_date,
                status_rows,
            )
            self._rebuild_nxt_unavailability_history(connection)
        return len(status_rows)

    def save_nxt_session_quotes(
        self,
        trading_date: date,
        session: str,
        quotes: Iterable[NxtSessionQuote],
    ) -> int:
        """완전히 수집한 NXT 세션별 종목 OHLC를 한 트랜잭션으로 교체합니다."""

        quote_rows = list(quotes)
        date_key = trading_date.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM nxt_session_quotes WHERE trade_date = ? AND session = ?",
                (date_key, session),
            )
            connection.executemany(
                """
                INSERT INTO nxt_session_quotes (
                    trade_date, session, stock_code, stock_name,
                    reference_price, open_price, high_price, low_price, close_price,
                    cumulative_volume, cumulative_amount, last_trade_time,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        date_key,
                        session,
                        quote.symbol,
                        quote.name,
                        quote.reference_price,
                        quote.open_price,
                        quote.high_price,
                        quote.low_price,
                        quote.close_price,
                        quote.cumulative_volume,
                        quote.cumulative_amount,
                        quote.last_trade_time,
                        quote.updated_at.isoformat(),
                    )
                    for quote in quote_rows
                ],
            )
        return len(quote_rows)

    def load_nxt_session_quotes(
        self,
        trading_date: date,
        session: str,
    ) -> list[NxtSessionQuote]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_session_quotes
                WHERE trade_date = ? AND session = ?
                ORDER BY stock_code
                """,
                (trading_date.isoformat(), session),
            ).fetchall()
        return [
            NxtSessionQuote(
                trading_date=trading_date,
                session=str(row["session"]),
                symbol=str(row["stock_code"]),
                name=str(row["stock_name"]),
                reference_price=row["reference_price"],
                open_price=row["open_price"],
                high_price=row["high_price"],
                low_price=row["low_price"],
                close_price=row["close_price"],
                cumulative_volume=int(row["cumulative_volume"]),
                cumulative_amount=int(row["cumulative_amount"]),
                last_trade_time=str(row["last_trade_time"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def nxt_session_dates(self, session: str) -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM nxt_session_quotes WHERE session = ?",
                (session,),
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def save_nxt_limit_hit_times(
        self,
        trading_date: date,
        hit_times: dict[tuple[str, str], str],
        *,
        source: str = "KIS_NXT_MINUTE",
    ) -> int:
        """종목·상하한가 구분별 최초 도달시각을 누적 저장합니다."""

        if not hit_times:
            return 0
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO nxt_limit_hit_times (
                    trade_date, stock_code, direction, hit_time, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, stock_code, direction) DO UPDATE SET
                    hit_time = excluded.hit_time,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        trading_date.isoformat(),
                        stock_code,
                        direction,
                        hit_time,
                        source,
                        updated_at,
                    )
                    for (stock_code, direction), hit_time in hit_times.items()
                ],
            )
            connection.executemany(
                """
                UPDATE nxt_limit_hits
                SET hit_time = ?,
                    hit_time_status = ?,
                    updated_at = ?
                WHERE trade_date = ? AND stock_code = ? AND direction = ?
                """,
                [
                    (
                        hit_time,
                        "OPEN" if hit_time == "OPEN" else "EXACT",
                        updated_at,
                        trading_date.isoformat(),
                        stock_code,
                        direction,
                    )
                    for (stock_code, direction), hit_time in hit_times.items()
                ],
            )
        return len(hit_times)

    def load_nxt_limit_hit_times(
        self,
        trading_date: date,
    ) -> dict[tuple[str, str], str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, direction, hit_time
                FROM nxt_limit_hit_times
                WHERE trade_date = ?
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return {
            (str(row["stock_code"]), str(row["direction"])): str(row["hit_time"])
            for row in rows
        }

    def save_nxt_limit_proximity_times(
        self,
        trading_date: date,
        hit_times: dict[tuple[str, str], str],
        *,
        source: str = "KIS_NXT_MINUTE",
    ) -> int:
        """종목·방향별 3호가 근접 범위 최초 진입시각을 누적 저장합니다."""

        if not hit_times:
            return 0
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO nxt_limit_proximity_times (
                    trade_date, stock_code, direction, hit_time, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, stock_code, direction) DO UPDATE SET
                    hit_time = excluded.hit_time,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        trading_date.isoformat(),
                        stock_code,
                        direction,
                        hit_time,
                        source,
                        updated_at,
                    )
                    for (stock_code, direction), hit_time in hit_times.items()
                ],
            )
            connection.executemany(
                """
                UPDATE nxt_limit_proximity_hits
                SET hit_time = ?, updated_at = ?
                WHERE trade_date = ? AND stock_code = ? AND direction = ?
                """,
                [
                    (
                        hit_time,
                        updated_at,
                        trading_date.isoformat(),
                        stock_code,
                        direction,
                    )
                    for (stock_code, direction), hit_time in hit_times.items()
                ],
            )
        return len(hit_times)

    def load_nxt_limit_proximity_times(
        self,
        trading_date: date,
    ) -> dict[tuple[str, str], str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, direction, hit_time
                FROM nxt_limit_proximity_times
                WHERE trade_date = ?
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return {
            (str(row["stock_code"]), str(row["direction"])): str(row["hit_time"])
            for row in rows
        }

    def load_nxt_limit_proximity_hits(
        self,
        trading_date: date,
    ) -> list[NxtLimitProximityHit]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_limit_proximity_hits
                WHERE trade_date = ?
                ORDER BY direction, distance_ticks, stock_code
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return [
            NxtLimitProximityHit(
                trade_date=trading_date,
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                direction=str(row["direction"]),
                distance_ticks=int(row["distance_ticks"]),
                closest_price=int(row["closest_price"]),
                reference_price=row["reference_price"],
                upper_limit_price=row["upper_limit_price"],
                lower_limit_price=row["lower_limit_price"],
                open_price=row["open_price"],
                high_price=row["high_price"],
                low_price=row["low_price"],
                close_price=row["close_price"],
                hit_time=str(row["hit_time"]) if row["hit_time"] else None,
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def rebuild_nxt_limit_proximity_hits(
        self,
        start_date: date,
        end_date: date,
    ) -> int:
        """저장된 NXT 일별 OHLC에서 0~3틱 근접 이력을 다시 생성합니다."""

        with self._lock, self._connect() as connection:
            date_rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM nxt_daily_quotes
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            connection.execute(
                "DELETE FROM nxt_limit_proximity_hits WHERE trade_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.execute(
                "DELETE FROM nxt_limit_proximity_daily WHERE trade_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
        total_rows = 0
        for row in date_rows:
            trading_date = date.fromisoformat(str(row["trade_date"]))
            total_rows += self.replace_nxt_limit_proximity_hits(
                trading_date,
                self.load_nxt_statuses(trading_date),
            )
        return total_rows

    def nxt_limit_proximity_hit_coverage(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            daily_row = connection.execute(
                """
                SELECT COUNT(*) AS trading_days,
                       MIN(trade_date) AS first_date,
                       MAX(trade_date) AS last_date
                FROM nxt_limit_proximity_daily
                """
            ).fetchone()
            hit_row = connection.execute(
                """
                SELECT COUNT(*) AS row_count,
                       COUNT(DISTINCT trade_date) AS hit_days,
                       SUM(CASE WHEN distance_ticks = 0 THEN 1 ELSE 0 END)
                           AS exact_count,
                       SUM(CASE WHEN distance_ticks BETWEEN 1 AND 3 THEN 1 ELSE 0 END)
                           AS near_count
                FROM nxt_limit_proximity_hits
                """
            ).fetchone()
        return {
            "row_count": int(hit_row["row_count"] or 0),
            "trading_days": int(daily_row["trading_days"] or 0),
            "hit_days": int(hit_row["hit_days"] or 0),
            "first_date": (
                date.fromisoformat(str(daily_row["first_date"]))
                if daily_row["first_date"]
                else None
            ),
            "last_date": (
                date.fromisoformat(str(daily_row["last_date"]))
                if daily_row["last_date"]
                else None
            ),
            "exact_count": int(hit_row["exact_count"] or 0),
            "near_count": int(hit_row["near_count"] or 0),
        }

    def load_nxt_limit_hits(self, trading_date: date) -> list[NxtLimitHit]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_limit_hits
                WHERE trade_date = ?
                ORDER BY direction, stock_code
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return [
            NxtLimitHit(
                trade_date=trading_date,
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                direction=str(row["direction"]),
                reference_price=row["reference_price"],
                upper_limit_price=row["upper_limit_price"],
                lower_limit_price=row["lower_limit_price"],
                open_price=row["open_price"],
                high_price=row["high_price"],
                low_price=row["low_price"],
                close_price=row["close_price"],
                hit_time=row["hit_time"],
                hit_time_status=str(row["hit_time_status"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def pending_nxt_limit_hits(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtLimitHit]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_limit_hits
                WHERE trade_date BETWEEN ? AND ?
                  AND hit_time_status = 'PENDING'
                ORDER BY trade_date, stock_code, direction
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtLimitHit(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                direction=str(row["direction"]),
                reference_price=row["reference_price"],
                upper_limit_price=row["upper_limit_price"],
                lower_limit_price=row["lower_limit_price"],
                open_price=row["open_price"],
                high_price=row["high_price"],
                low_price=row["low_price"],
                close_price=row["close_price"],
                hit_time=row["hit_time"],
                hit_time_status=str(row["hit_time_status"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def nxt_limit_hit_coverage(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(trade_date) AS min_date,
                       MAX(trade_date) AS max_date,
                       COUNT(DISTINCT trade_date) AS date_count,
                       COUNT(*) AS row_count,
                       SUM(hit_time_status = 'OPEN') AS open_count,
                       SUM(hit_time_status = 'EXACT') AS exact_count,
                       SUM(hit_time_status = 'RETENTION_EXPIRED') AS expired_count,
                       SUM(hit_time_status = 'PENDING') AS pending_count
                FROM nxt_limit_hits
                """
            ).fetchone()
        return {
            "min_date": date.fromisoformat(str(row["min_date"])) if row["min_date"] else None,
            "max_date": date.fromisoformat(str(row["max_date"])) if row["max_date"] else None,
            "date_count": int(row["date_count"] or 0),
            "row_count": int(row["row_count"] or 0),
            "open_count": int(row["open_count"] or 0),
            "exact_count": int(row["exact_count"] or 0),
            "expired_count": int(row["expired_count"] or 0),
            "pending_count": int(row["pending_count"] or 0),
        }

    def save_future_quotes(
        self,
        trading_date: date,
        future_quotes: dict[str, FutureQuote],
    ) -> None:
        with self._lock, self._connect() as connection:
            self._replace_future_quotes(connection, trading_date, future_quotes)

    def save_index_quotes(
        self,
        trading_date: date,
        index_quotes: dict[str, IndexQuote],
    ) -> None:
        date_key = trading_date.isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO krx_daily_indices (
                    trade_date, index_name, index_code, current_value,
                    change_value, change_rate, cumulative_volume,
                    cumulative_amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, index_name) DO UPDATE SET
                    index_code = excluded.index_code,
                    current_value = excluded.current_value,
                    change_value = excluded.change_value,
                    change_rate = excluded.change_rate,
                    cumulative_volume = excluded.cumulative_volume,
                    cumulative_amount = excluded.cumulative_amount,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        date_key,
                        name,
                        quote.code,
                        quote.current_value,
                        quote.change_value,
                        quote.change_rate,
                        quote.cumulative_volume,
                        quote.cumulative_amount,
                        quote.updated_at.isoformat(),
                    )
                    for name, quote in index_quotes.items()
                ],
            )

    def save_fx_quotes(self, quotes: dict[date, IndexQuote]) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_daily_fx (
                    trade_date, pair_name, pair_code, current_value,
                    change_value, change_rate, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, pair_name) DO UPDATE SET
                    pair_code = excluded.pair_code,
                    current_value = excluded.current_value,
                    change_value = excluded.change_value,
                    change_rate = excluded.change_rate,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        trading_date.isoformat(),
                        quote.name,
                        quote.code,
                        quote.current_value,
                        quote.change_value,
                        quote.change_rate,
                        quote.updated_at.isoformat(),
                    )
                    for trading_date, quote in quotes.items()
                ],
            )

    def load_fx_quote(
        self,
        trading_date: date,
        pair_name: str = "달러-원",
    ) -> IndexQuote | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM market_daily_fx
                WHERE trade_date = ? AND pair_name = ?
                """,
                (trading_date.isoformat(), pair_name),
            ).fetchone()
        if row is None:
            return None
        return IndexQuote(
            name=str(row["pair_name"]),
            code=str(row["pair_code"]),
            current_value=float(row["current_value"]),
            change_value=float(row["change_value"]),
            change_rate=float(row["change_rate"]),
            cumulative_volume=0,
            cumulative_amount=0,
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def index_dates(self, index_name: str) -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT trade_date FROM krx_daily_indices WHERE index_name = ?",
                (index_name,),
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def replace_krx_index_constituents(
        self,
        history: Mapping[
            date,
            Mapping[str, Iterable[KrxIndexConstituent]],
        ],
    ) -> int:
        """조회일자별 KRX 지수 구성종목을 원자적으로 교체합니다."""

        updated_at = datetime.now(timezone.utc).isoformat()
        stored = 0
        with self._lock, self._connect() as connection:
            for trading_date, indices in history.items():
                date_key = trading_date.isoformat()
                for index_name, constituents in indices.items():
                    rows = [
                        (
                            date_key,
                            index_name,
                            item.stock_code,
                            item.stock_name,
                            updated_at,
                        )
                        for item in constituents
                        if item.stock_code
                    ]
                    connection.execute(
                        "DELETE FROM krx_index_constituents "
                        "WHERE trade_date = ? AND index_name = ?",
                        (date_key, index_name),
                    )
                    connection.executemany(
                        """
                        INSERT INTO krx_index_constituents (
                            trade_date, index_name, stock_code, stock_name,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    stored += len(rows)
        return stored

    def index_constituent_dates(self, index_name: str) -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM krx_index_constituents
                WHERE index_name = ?
                """,
                (index_name,),
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def index_constituent_codes(
        self,
        trading_date: date,
    ) -> dict[str, set[str]]:
        """기준일의 KOSPI200·KOSDAQ150 구성종목 단축코드를 반환합니다."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT index_name, stock_code
                FROM krx_index_constituents
                WHERE trade_date = ?
                  AND index_name IN ('KOSPI200', 'KOSDAQ150')
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        result = {"KOSPI200": set(), "KOSDAQ150": set()}
        for row in rows:
            result[str(row["index_name"])].add(str(row["stock_code"]))
        return result

    def list_nxt_index_member_counts(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, dict[str, int]]:
        """일별 NXT 대상 종목 중 KRX 대표지수 구성종목 수를 반환합니다."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                WITH nxt_symbols AS (
                    SELECT trade_date, stock_code
                    FROM nxt_daily_quotes
                    WHERE trade_date BETWEEN ? AND ?
                    UNION
                    SELECT trade_date, stock_code
                    FROM nxt_daily_eligibility_adjustments
                    WHERE trade_date BETWEEN ? AND ?
                )
                SELECT member.trade_date, member.index_name,
                       COUNT(DISTINCT member.stock_code) AS stock_count
                FROM krx_index_constituents AS member
                JOIN nxt_symbols AS nxt
                  ON nxt.trade_date = member.trade_date
                 AND nxt.stock_code = member.stock_code
                WHERE member.trade_date BETWEEN ? AND ?
                  AND member.index_name IN ('KOSPI200', 'KOSDAQ150')
                GROUP BY member.trade_date, member.index_name
                ORDER BY member.trade_date, member.index_name
                """,
                (
                    start_date.isoformat(),
                    end_date.isoformat(),
                    start_date.isoformat(),
                    end_date.isoformat(),
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchall()
        result: dict[date, dict[str, int]] = {}
        for row in rows:
            trading_date = date.fromisoformat(str(row["trade_date"]))
            result.setdefault(trading_date, {})[str(row["index_name"])] = int(
                row["stock_count"]
            )
        return result

    def fx_dates(self, pair_name: str = "달러-원") -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT trade_date FROM market_daily_fx WHERE pair_name = ?",
                (pair_name,),
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def rebuild_derived_metrics(self) -> None:
        """저장된 종목 종가로 NXT 가중 등락률·기준지수와 TMI 값을 재계산합니다."""

        with self._lock, self._connect() as connection:
            metric_rows = connection.execute(
                """
                SELECT trade_date FROM daily_market_metrics
                WHERE is_final = 1
                ORDER BY trade_date
                """
            ).fetchall()
            for metric_row in metric_rows:
                date_key = str(metric_row["trade_date"])
                quote_rows = connection.execute(
                    """
                    SELECT n.current_price AS nxt_price,
                           n.reference_price AS nxt_reference_price,
                           k.reference_price AS krx_reference_price,
                           k.listed_shares AS listed_shares
                    FROM nxt_daily_quotes n
                    JOIN krx_daily_quotes k
                      ON k.trade_date = n.trade_date
                     AND k.stock_code = n.stock_code
                    WHERE n.trade_date = ?
                    """,
                    (date_key,),
                ).fetchall()
                numerator = 0
                denominator = 0
                for row in quote_rows:
                    shares = int(row["listed_shares"] or 0)
                    current_price = int(row["nxt_price"] or 0)
                    comparison_price = int(
                        row["nxt_reference_price"]
                        or row["krx_reference_price"]
                        or 0
                    )
                    if shares <= 0 or current_price <= 0 or comparison_price <= 0:
                        continue
                    numerator += current_price * shares
                    denominator += comparison_price * shares
                nxt_change_rate = (
                    numerator / denominator - 1 if denominator > 0 else None
                )
                tmi_row = connection.execute(
                    """
                    SELECT current_value, change_rate
                    FROM krx_daily_indices
                    WHERE trade_date = ? AND index_name = 'KRX TMI'
                    """,
                    (date_key,),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE daily_market_metrics
                    SET nxt_change_rate = ?,
                        tmi_index_value = ?,
                        tmi_change_rate = ?
                    WHERE trade_date = ?
                    """,
                    (
                        nxt_change_rate,
                        float(tmi_row["current_value"]) if tmi_row else None,
                        float(tmi_row["change_rate"]) / 100 if tmi_row else None,
                        date_key,
                    ),
                )

            chain_rows = connection.execute(
                """
                SELECT trade_date, nxt_change_rate
                FROM daily_market_metrics
                WHERE is_final = 1
                ORDER BY trade_date
                """
            ).fetchall()
            raw_index_value: float | None = None
            chain_valid = True
            raw_index_values: dict[str, float | None] = {}
            for position, row in enumerate(chain_rows):
                rate = row["nxt_change_rate"]
                if position == 0:
                    raw_index_value = 100.0
                elif rate is None or not chain_valid or raw_index_value is None:
                    raw_index_value = None
                    chain_valid = False
                else:
                    raw_index_value *= 1 + float(rate)
                raw_index_values[str(row["trade_date"])] = raw_index_value

            base_raw_value = raw_index_values.get(NXT_INDEX_BASE_DATE.isoformat())
            scale = (
                NXT_INDEX_BASE_VALUE / base_raw_value
                if base_raw_value is not None and base_raw_value > 0
                else None
            )
            for row in chain_rows:
                date_key = str(row["trade_date"])
                raw_value = raw_index_values[date_key]
                index_value = (
                    raw_value * scale
                    if raw_value is not None and scale is not None
                    else None
                )
                connection.execute(
                    """
                    UPDATE daily_market_metrics
                    SET nxt_index_value = ?
                    WHERE trade_date = ?
                    """,
                    (index_value, date_key),
                )

    def rebuild_nxt_price_limits(self) -> int:
        """저장된 NXT 기준가격으로 전 종목의 상·하한가를 다시 산출합니다."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, stock_code, reference_price, market
                FROM nxt_daily_quotes
                """
            ).fetchall()
            updates = []
            for row in rows:
                upper_price, lower_price = calculate_stock_price_limits(
                    row["reference_price"],
                    str(row["market"]),
                )
                updates.append(
                    (
                        upper_price,
                        lower_price,
                        str(row["trade_date"]),
                        str(row["stock_code"]),
                    )
                )
            connection.executemany(
                """
                UPDATE nxt_daily_quotes
                SET upper_limit_price = ?, lower_limit_price = ?
                WHERE trade_date = ? AND stock_code = ?
                """,
                updates,
            )
        return len(updates)

    @staticmethod
    def _replace_future_quotes(
        connection: sqlite3.Connection,
        trading_date: date,
        future_quotes: dict[str, FutureQuote],
    ) -> None:
        date_key = trading_date.isoformat()
        connection.execute(
            "DELETE FROM krx_daily_futures WHERE trade_date = ?",
            (date_key,),
        )
        connection.executemany(
            """
            INSERT INTO krx_daily_futures (
                trade_date, future_name, contract_code, current_value,
                change_value, change_rate, cumulative_volume,
                cumulative_amount, open_interest, settlement_price, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    date_key,
                    name,
                    quote.code,
                    quote.current_value,
                    quote.change_value,
                    quote.change_rate,
                    quote.cumulative_volume,
                    quote.cumulative_amount,
                    quote.open_interest,
                    quote.settlement_price,
                    quote.updated_at.isoformat(),
                )
                for name, quote in future_quotes.items()
            ],
        )

    def save_live_metric(
        self,
        trading_date: date,
        *,
        nxt_volume: int | None,
        nxt_amount: int | None,
        krx_volume: int | None,
        krx_amount: int | None,
        nxt_stock_count: int,
        krx_basis: str = "KIS_CURRENT_INDEX",
    ) -> DailyMarketMetric:
        metric = DailyMarketMetric(
            trade_date=trading_date,
            nxt_volume=nxt_volume,
            nxt_amount=nxt_amount,
            krx_volume=krx_volume,
            krx_amount=krx_amount,
            volume_ratio=_ratio(nxt_volume, krx_volume),
            amount_ratio=_ratio(nxt_amount, krx_amount),
            nxt_stock_count=nxt_stock_count,
            is_final=False,
            krx_basis=krx_basis,
            updated_at=datetime.now(timezone.utc),
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM daily_market_metrics WHERE trade_date = ?",
                (trading_date.isoformat(),),
            ).fetchone()
            if existing is not None and bool(existing["is_final"]):
                return self._metric_from_row(existing)
            self._upsert_metric(connection, metric)
        return metric

    @staticmethod
    def _upsert_metric(
        connection: sqlite3.Connection,
        metric: DailyMarketMetric,
    ) -> None:
        connection.execute(
            """
            INSERT INTO daily_market_metrics (
                trade_date, nxt_volume, nxt_amount, krx_volume, krx_amount,
                volume_ratio, amount_ratio, nxt_stock_count, is_final,
                krx_basis, updated_at, nxt_index_value, nxt_change_rate,
                tmi_index_value, tmi_change_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                nxt_volume = excluded.nxt_volume,
                nxt_amount = excluded.nxt_amount,
                krx_volume = excluded.krx_volume,
                krx_amount = excluded.krx_amount,
                volume_ratio = excluded.volume_ratio,
                amount_ratio = excluded.amount_ratio,
                nxt_stock_count = excluded.nxt_stock_count,
                is_final = excluded.is_final,
                krx_basis = excluded.krx_basis,
                updated_at = excluded.updated_at,
                nxt_index_value = excluded.nxt_index_value,
                nxt_change_rate = excluded.nxt_change_rate,
                tmi_index_value = excluded.tmi_index_value,
                tmi_change_rate = excluded.tmi_change_rate
            """,
            (
                metric.trade_date.isoformat(),
                metric.nxt_volume,
                metric.nxt_amount,
                metric.krx_volume,
                metric.krx_amount,
                metric.volume_ratio,
                metric.amount_ratio,
                metric.nxt_stock_count,
                int(metric.is_final),
                metric.krx_basis,
                metric.updated_at.isoformat(),
                metric.nxt_index_value,
                metric.nxt_change_rate,
                metric.tmi_index_value,
                metric.tmi_change_rate,
            ),
        )

    @staticmethod
    def _nxt_status_from_row(
        trading_date: date,
        row: sqlite3.Row,
    ) -> NxtTradingStatus:
        return NxtTradingStatus(
            status_date=trading_date,
            stock_code=str(row["stock_code"]),
            stock_name=str(row["stock_name"]),
            market=str(row["market"]),
            tradable_market=str(row["tradable_market"]),
            unavailable_reason=str(row["unavailable_reason"]),
            isin=str(row["isin"]),
            reference_price=row["reference_price"],
            current_price=row["current_price"],
            change_value=row["change_value"],
            change_rate=row["change_rate"],
            cumulative_volume=int(row["cumulative_volume"]),
            cumulative_amount=int(row["cumulative_amount"]),
            quote_time=str(row["quote_time"]),
            open_price=row["open_price"],
            high_price=row["high_price"],
            low_price=row["low_price"],
            upper_limit_price=row["upper_limit_price"],
            lower_limit_price=row["lower_limit_price"],
        )

    def load_nxt_statuses(self, trading_date: date) -> list[NxtTradingStatus]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_daily_quotes
                WHERE trade_date = ?
                ORDER BY market, stock_code
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return [self._nxt_status_from_row(trading_date, row) for row in rows]

    def load_latest_nxt_statuses(
        self,
        on_or_before: date,
    ) -> tuple[date | None, list[NxtTradingStatus]]:
        """기준일 이하에서 가장 최근에 저장된 NXT 종목 현황을 반환합니다."""

        with self._lock, self._connect() as connection:
            date_row = connection.execute(
                """
                SELECT MAX(trade_date) AS trade_date
                FROM nxt_daily_quotes
                WHERE trade_date <= ?
                """,
                (on_or_before.isoformat(),),
            ).fetchone()
            if date_row is None or not date_row["trade_date"]:
                return None, []
            trading_date = date.fromisoformat(str(date_row["trade_date"]))
            rows = connection.execute(
                """
                SELECT * FROM nxt_daily_quotes
                WHERE trade_date = ?
                ORDER BY market, stock_code
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return trading_date, [
            self._nxt_status_from_row(trading_date, row) for row in rows
        ]

    def list_nxt_eligibility_summaries(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtDailyEligibilitySummary]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM nxt_daily_eligibility_summary
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtDailyEligibilitySummary(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                target_stock_count=int(row["target_stock_count"]),
                tradable_stock_count=int(row["tradable_stock_count"]),
                unavailable_stock_count=int(row["unavailable_stock_count"]),
                target_kospi_count=int(row["target_kospi_count"]),
                target_kosdaq_count=int(row["target_kosdaq_count"]),
                tradable_kospi_count=int(row["tradable_kospi_count"]),
                tradable_kosdaq_count=int(row["tradable_kosdaq_count"]),
                inclusion_stock_count=int(row["inclusion_stock_count"]),
                exclusion_stock_count=int(row["exclusion_stock_count"]),
                restriction_start_stock_count=int(
                    row["restriction_start_stock_count"]
                ),
                restriction_end_stock_count=int(row["restriction_end_stock_count"]),
            )
            for row in rows
        ]

    def list_nxt_eligibility_reason_counts(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtDailyReasonCount]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, reason_group, reason, stock_count
                FROM nxt_daily_eligibility_reason_counts
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date, reason_group, reason
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtDailyReasonCount(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                reason_group=str(row["reason_group"]),
                reason=str(row["reason"]),
                stock_count=int(row["stock_count"]),
            )
            for row in rows
        ]

    def list_nxt_eligibility_adjustments(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtEligibilityAdjustment]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, stock_code, stock_name, market,
                       unavailable_reason, restriction_start_date,
                       restriction_end_date, basis
                FROM nxt_daily_eligibility_adjustments
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date, stock_code
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtEligibilityAdjustment(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                unavailable_reason=str(row["unavailable_reason"]),
                restriction_start_date=date.fromisoformat(
                    str(row["restriction_start_date"])
                ),
                restriction_end_date=(
                    date.fromisoformat(str(row["restriction_end_date"]))
                    if row["restriction_end_date"]
                    else None
                ),
                basis=str(row["basis"]),
            )
            for row in rows
        ]

    def list_nxt_daily_unavailability(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtDailyUnavailability]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, stock_code, stock_name, market,
                       tradable_market, unavailable_reason, source_type,
                       source_title, source_url, basis
                FROM nxt_daily_unavailability
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date, stock_code
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtDailyUnavailability(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                tradable_market=str(row["tradable_market"]),
                unavailable_reason=str(row["unavailable_reason"]),
                source_type=str(row["source_type"]),
                source_title=str(row["source_title"]),
                source_url=str(row["source_url"]),
                basis=str(row["basis"]),
            )
            for row in rows
        ]

    def list_nxt_unavailability_events(
        self,
        start_date: date,
        end_date: date,
    ) -> list[NxtUnavailabilityEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event.event_date, event.stock_code, event.stock_name,
                       event.market, event.event_type, event.tradable_market,
                       event.unavailable_reason, event.source_type,
                       event.source_title, event.source_url, event.basis,
                       kind.report_no AS kind_report_no,
                       kind.category AS kind_category,
                       kind.title AS kind_title,
                       kind.disclosed_at AS kind_disclosed_at,
                       kind.viewer_url AS kind_viewer_url,
                       kind.match_basis AS kind_match_basis
                FROM nxt_unavailability_events AS event
                LEFT JOIN nxt_unavailability_kind_links AS kind
                  ON kind.event_date = event.event_date
                 AND kind.stock_code = event.stock_code
                 AND kind.event_type = event.event_type
                WHERE event.event_date BETWEEN ? AND ?
                ORDER BY event.event_date, event.stock_code, event.event_type
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            NxtUnavailabilityEvent(
                event_date=date.fromisoformat(str(row["event_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                event_type=str(row["event_type"]),
                tradable_market=str(row["tradable_market"]),
                unavailable_reason=str(row["unavailable_reason"]),
                source_type=str(row["source_type"]),
                source_title=str(row["source_title"]),
                source_url=str(row["source_url"]),
                basis=str(row["basis"]),
                kind_report_no=str(row["kind_report_no"] or ""),
                kind_category=str(row["kind_category"] or ""),
                kind_title=str(row["kind_title"] or ""),
                kind_disclosed_at=(
                    datetime.fromisoformat(str(row["kind_disclosed_at"]))
                    if row["kind_disclosed_at"]
                    else None
                ),
                kind_viewer_url=str(row["kind_viewer_url"] or ""),
                kind_match_basis=str(row["kind_match_basis"] or ""),
            )
            for row in rows
        ]

    def replace_nxt_unavailability_kind_links(
        self,
        start_date: date,
        end_date: date,
        links: Iterable[NxtUnavailabilityKindLink],
    ) -> int:
        if end_date < start_date:
            return 0
        selected = [
            item for item in links if start_date <= item.event_date <= end_date
        ]
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM nxt_unavailability_kind_links
                WHERE event_date BETWEEN ? AND ?
                """,
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.executemany(
                """
                INSERT INTO nxt_unavailability_kind_links (
                    event_date, stock_code, event_type, report_no, category,
                    title, disclosed_at, viewer_url, match_basis, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.event_date.isoformat(),
                        item.stock_code,
                        item.event_type,
                        item.report_no,
                        item.category,
                        item.title,
                        item.disclosed_at.isoformat(),
                        item.viewer_url,
                        item.match_basis,
                        updated_at,
                    )
                    for item in selected
                ],
            )
        return len(selected)

    def nxt_unavailability_history_coverage(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            daily = connection.execute(
                """
                SELECT COUNT(*) AS row_count,
                       COUNT(DISTINCT trade_date) AS trading_days,
                       MIN(trade_date) AS first_date,
                       MAX(trade_date) AS last_date
                FROM nxt_daily_unavailability
                """
            ).fetchone()
            events = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       SUM(event_type = '거래불가') AS restriction_starts,
                       SUM(event_type = '거래불가 해제') AS restriction_ends
                FROM nxt_unavailability_events
                """
            ).fetchone()
            kind_links = connection.execute(
                "SELECT COUNT(*) AS link_count FROM nxt_unavailability_kind_links"
            ).fetchone()
        return {
            "row_count": int(daily["row_count"] or 0),
            "trading_days": int(daily["trading_days"] or 0),
            "first_date": (
                date.fromisoformat(str(daily["first_date"]))
                if daily["first_date"]
                else None
            ),
            "last_date": (
                date.fromisoformat(str(daily["last_date"]))
                if daily["last_date"]
                else None
            ),
            "event_count": int(events["event_count"] or 0),
            "restriction_starts": int(events["restriction_starts"] or 0),
            "restriction_ends": int(events["restriction_ends"] or 0),
            "kind_link_count": int(kind_links["link_count"] or 0),
        }

    def nxt_eligibility_history_coverage(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS trading_days, MIN(trade_date) AS first_date,
                       MAX(trade_date) AS last_date,
                       SUM(inclusion_stock_count) AS inclusions,
                       SUM(exclusion_stock_count) AS exclusions,
                       SUM(restriction_start_stock_count) AS restrictions,
                       SUM(restriction_end_stock_count) AS restriction_releases
                FROM nxt_daily_eligibility_summary
                """
            ).fetchone()
            reason_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nxt_daily_eligibility_reason_counts"
                ).fetchone()[0]
            )
            adjustment_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nxt_daily_eligibility_adjustments"
                ).fetchone()[0]
            )
        return {
            "trading_days": int(summary["trading_days"] or 0),
            "first_date": (
                date.fromisoformat(str(summary["first_date"]))
                if summary["first_date"]
                else None
            ),
            "last_date": (
                date.fromisoformat(str(summary["last_date"]))
                if summary["last_date"]
                else None
            ),
            "inclusions": int(summary["inclusions"] or 0),
            "exclusions": int(summary["exclusions"] or 0),
            "restrictions": int(summary["restrictions"] or 0),
            "restriction_releases": int(summary["restriction_releases"] or 0),
            "reason_rows": reason_rows,
            "adjustment_rows": adjustment_rows,
        }

    def load_historical_snapshot(
        self,
        trading_date: date,
    ) -> tuple[list[NxtTradingStatus], KrxDailySnapshot] | None:
        date_key = trading_date.isoformat()
        with self._lock, self._connect() as connection:
            metric = connection.execute(
                "SELECT is_final FROM daily_market_metrics WHERE trade_date = ?",
                (date_key,),
            ).fetchone()
            nxt_rows = connection.execute(
                "SELECT * FROM nxt_daily_quotes WHERE trade_date = ? ORDER BY market, stock_code",
                (date_key,),
            ).fetchall()
            krx_rows = connection.execute(
                "SELECT * FROM krx_daily_quotes WHERE trade_date = ?",
                (date_key,),
            ).fetchall()
            index_rows = connection.execute(
                "SELECT * FROM krx_daily_indices WHERE trade_date = ?",
                (date_key,),
            ).fetchall()
            future_rows = connection.execute(
                "SELECT * FROM krx_daily_futures WHERE trade_date = ?",
                (date_key,),
            ).fetchall()
        if metric is None or not bool(metric["is_final"]) or not nxt_rows or not index_rows:
            return None

        statuses = [
            self._nxt_status_from_row(trading_date, row) for row in nxt_rows
        ]
        quotes: dict[tuple[str, str], RestQuote] = {}
        listed_shares: dict[str, int] = {}
        fallback_updated_at = datetime.combine(trading_date, time(18), tzinfo=KST)
        for row in krx_rows:
            symbol = str(row["stock_code"])
            updated_at = (
                datetime.fromisoformat(str(row["updated_at"]))
                if row["updated_at"]
                else fallback_updated_at
            )
            quotes[(symbol, KRX)] = RestQuote(
                market=KRX,
                symbol=symbol,
                name=str(row["stock_name"]),
                current_price=int(row["current_price"]),
                reference_price=row["reference_price"],
                cumulative_volume=int(row["cumulative_volume"]),
                cumulative_amount=int(row["cumulative_amount"]),
                updated_at=updated_at,
                market_cap=row["market_cap"],
            )
            if row["listed_shares"]:
                listed_shares[symbol] = int(row["listed_shares"])
        indices = {
            str(row["index_name"]): IndexQuote(
                name=str(row["index_name"]),
                code=str(row["index_code"]),
                current_value=float(row["current_value"]),
                change_value=float(row["change_value"]),
                change_rate=float(row["change_rate"]),
                cumulative_volume=int(row["cumulative_volume"]),
                cumulative_amount=int(row["cumulative_amount"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in index_rows
        }
        futures = {
            str(row["future_name"]): FutureQuote(
                name=str(row["future_name"]),
                code=str(row["contract_code"]),
                current_value=float(row["current_value"]),
                change_value=float(row["change_value"]),
                change_rate=float(row["change_rate"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                cumulative_volume=int(row["cumulative_volume"]),
                cumulative_amount=int(row["cumulative_amount"]),
                open_interest=(
                    int(row["open_interest"])
                    if row["open_interest"] is not None
                    else None
                ),
                settlement_price=(
                    float(row["settlement_price"])
                    if row["settlement_price"] is not None
                    else None
                ),
            )
            for row in future_rows
        }
        return statuses, KrxDailySnapshot(
            quotes,
            indices,
            listed_shares,
            futures,
        )

    def list_metrics(
        self,
        start_date: date,
        end_date: date,
    ) -> list[DailyMarketMetric]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM daily_market_metrics
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [self._metric_from_row(row) for row in rows]

    def snapshot_dates(self) -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date FROM daily_market_metrics
                WHERE is_final = 1
                  AND EXISTS (
                      SELECT 1 FROM nxt_daily_quotes n
                      WHERE n.trade_date = daily_market_metrics.trade_date
                  )
                  AND EXISTS (
                      SELECT 1 FROM krx_daily_indices i
                      WHERE i.trade_date = daily_market_metrics.trade_date
                  )
                """
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def future_dates(self) -> set[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM krx_daily_futures"
            ).fetchall()
        return {date.fromisoformat(str(row["trade_date"])) for row in rows}

    def latest_final_date(self) -> date | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM daily_market_metrics WHERE is_final = 1"
            ).fetchone()
        return date.fromisoformat(str(row[0])) if row and row[0] else None

    @staticmethod
    def _metric_from_row(row: sqlite3.Row) -> DailyMarketMetric:
        return DailyMarketMetric(
            trade_date=date.fromisoformat(str(row["trade_date"])),
            nxt_volume=row["nxt_volume"],
            nxt_amount=row["nxt_amount"],
            krx_volume=row["krx_volume"],
            krx_amount=row["krx_amount"],
            volume_ratio=row["volume_ratio"],
            amount_ratio=row["amount_ratio"],
            nxt_stock_count=int(row["nxt_stock_count"]),
            is_final=bool(row["is_final"]),
            krx_basis=str(row["krx_basis"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            nxt_index_value=row["nxt_index_value"],
            nxt_change_rate=row["nxt_change_rate"],
            tmi_index_value=row["tmi_index_value"],
            tmi_change_rate=row["tmi_change_rate"],
        )
