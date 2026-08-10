from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.cache import ResponseCache
from src.config import NXT_LAUNCH_DATE
from src.models import NxtChange
from src.nxt_change_context import (
    ChangeContext,
    NXT_CHANGE_LIST_URL,
    inferred_addition_context,
    inferred_exclusion_context,
)
from src.nxt_client import NxtClient
from src.nxt_eligibility import classify_nxt_change


KST = ZoneInfo("Asia/Seoul")
STALE_RUN_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class NxtChangeStoreStats:
    coverage_start: date | None
    coverage_end: date | None
    synced_day_count: int
    event_count: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    status: str
    message: str


@dataclass(frozen=True)
class NxtChangeSyncResult:
    status: str
    start_date: date
    end_date: date
    synced_ranges: tuple[tuple[date, date], ...] = ()
    event_count: int = 0
    ssl_fallback_used: bool = False
    message: str = ""


class NxtChangeStore:
    """NXT 정규시장 종목 변동내역과 날짜별 동기화 완료 상태를 저장합니다."""

    def __init__(self, path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "history.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
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

                CREATE TABLE IF NOT EXISTS nxt_change_sync_days (
                    sync_date TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL
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

                CREATE TABLE IF NOT EXISTS nxt_change_sync_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    last_start_date TEXT,
                    last_end_date TEXT
                );

                INSERT INTO nxt_change_sync_state (
                    singleton_id, status, message
                ) VALUES (1, 'idle', '')
                ON CONFLICT(singleton_id) DO NOTHING;

                CREATE INDEX IF NOT EXISTS idx_nxt_membership_changes_date
                    ON nxt_membership_changes(change_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_membership_changes_stock
                    ON nxt_membership_changes(stock_code, change_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_membership_adjustments_date
                    ON nxt_membership_change_adjustments(change_date);
                """
            )

    def list_changes(self, start_date: date, end_date: date) -> list[NxtChange]:
        if end_date < start_date:
            return []
        with self._lock, self._connect() as connection:
            raw_rows = connection.execute(
                """
                SELECT change_date, stock_code, stock_name, market,
                       change_type, reason, isin, registered_at
                FROM nxt_membership_changes
                WHERE change_date BETWEEN ? AND ?
                ORDER BY change_date, registered_at, stock_code
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            adjustment_rows = connection.execute(
                """
                SELECT change_date, stock_code, stock_name, market,
                       change_type, reason, display_reason, isin, basis,
                       source_title, source_url
                FROM nxt_membership_change_adjustments
                WHERE change_date BETWEEN ? AND ?
                ORDER BY change_date, stock_code
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        raw_changes = [
            NxtChange(
                change_date=date.fromisoformat(str(row["change_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                change_type=str(row["change_type"]),
                reason=str(row["reason"]),
                isin=str(row["isin"]),
                registered_at=int(row["registered_at"]),
            )
            for row in raw_rows
        ]
        raw_keys = {
            (item.change_date, item.stock_code, classify_nxt_change(item))
            for item in raw_changes
        }
        inferred_changes = [
            NxtChange(
                change_date=date.fromisoformat(str(row["change_date"])),
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                change_type=str(row["change_type"]),
                reason=str(row["reason"]),
                isin=str(row["isin"]),
                registered_at=0,
                display_reason=str(row["display_reason"]),
                source_title=str(row["source_title"]),
                source_url=str(row["source_url"]),
                basis=str(row["basis"]),
                is_inferred=True,
            )
            for row in adjustment_rows
        ]
        combined = raw_changes + [
            item
            for item in inferred_changes
            if (item.change_date, item.stock_code, classify_nxt_change(item))
            not in raw_keys
        ]
        return sorted(
            combined,
            key=lambda item: (
                item.change_date,
                item.registered_at,
                item.stock_code,
                item.change_type,
            ),
        )

    def rebuild_inferred_changes(self) -> int:
        """일별 대상 명단과 원본 변동내역을 대사해 누락 변동을 보완합니다."""

        with self._lock, self._connect() as connection:
            quote_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nxt_daily_quotes'"
            ).fetchone()
            if quote_table is None:
                connection.execute("DELETE FROM nxt_membership_change_adjustments")
                return 0
            date_rows = connection.execute(
                "SELECT DISTINCT trade_date FROM nxt_daily_quotes ORDER BY trade_date"
            ).fetchall()
            trading_dates = [
                date.fromisoformat(str(row["trade_date"])) for row in date_rows
            ]
            if not trading_dates:
                connection.execute("DELETE FROM nxt_membership_change_adjustments")
                return 0

            codes_by_date: dict[date, set[str]] = {}
            metadata: dict[str, tuple[str, str, str]] = {}
            for trading_date in trading_dates:
                rows = connection.execute(
                    """
                    SELECT stock_code, stock_name, market, isin
                    FROM nxt_daily_quotes WHERE trade_date = ?
                    """,
                    (trading_date.isoformat(),),
                ).fetchall()
                codes_by_date[trading_date] = {str(row["stock_code"]) for row in rows}
                for row in rows:
                    metadata[str(row["stock_code"])] = (
                        str(row["stock_name"]),
                        str(row["market"]),
                        str(row["isin"]),
                    )

            eligibility_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='nxt_daily_eligibility_adjustments'"
            ).fetchone()
            if eligibility_table is not None:
                for row in connection.execute(
                    """
                    SELECT trade_date, stock_code, stock_name, market
                    FROM nxt_daily_eligibility_adjustments
                    """
                ):
                    trading_date = date.fromisoformat(str(row["trade_date"]))
                    if trading_date not in codes_by_date:
                        continue
                    code = str(row["stock_code"])
                    codes_by_date[trading_date].add(code)
                    old = metadata.get(code, ("", "", ""))
                    metadata[code] = (
                        str(row["stock_name"]) or old[0],
                        str(row["market"]) or old[1],
                        old[2],
                    )

            raw_rows = connection.execute(
                """
                SELECT change_date, stock_code, stock_name, market,
                       change_type, reason, isin, registered_at
                FROM nxt_membership_changes
                ORDER BY change_date, registered_at, stock_code
                """
            ).fetchall()
            raw_changes = [
                NxtChange(
                    change_date=date.fromisoformat(str(row["change_date"])),
                    stock_code=str(row["stock_code"]),
                    stock_name=str(row["stock_name"]),
                    market=str(row["market"]),
                    change_type=str(row["change_type"]),
                    reason=str(row["reason"]),
                    isin=str(row["isin"]),
                    registered_at=int(row["registered_at"]),
                )
                for row in raw_rows
            ]
            raw_membership: dict[tuple[date, str], set[str]] = {}
            for item in raw_changes:
                group = classify_nxt_change(item)
                if group in {"편입", "편출"}:
                    raw_membership.setdefault((item.change_date, group), set()).add(
                        item.stock_code
                    )

            last_present_index: dict[str, int] = {}
            for index, trading_date in enumerate(trading_dates):
                for code in codes_by_date[trading_date]:
                    last_present_index[code] = index

            inferred: list[tuple[object, ...]] = []
            seen_codes: set[str] = set()
            previous_codes: set[str] = set()
            updated_at = datetime.now(timezone.utc).isoformat()

            def add_row(
                trading_date: date,
                stock_code: str,
                change_type: str,
                reason: str,
                context: ChangeContext,
                basis: str,
            ) -> None:
                stock_name, market, isin = metadata.get(stock_code, ("", "", ""))
                inferred.append(
                    (
                        trading_date.isoformat(),
                        stock_code,
                        stock_name,
                        market,
                        change_type,
                        reason,
                        context.display_reason,
                        isin,
                        basis,
                        context.source_title,
                        context.source_url,
                        updated_at,
                    )
                )

            for index, trading_date in enumerate(trading_dates):
                current_codes = codes_by_date[trading_date]
                additions = current_codes - previous_codes
                exclusions = previous_codes - current_codes
                raw_additions = raw_membership.get((trading_date, "편입"), set())
                raw_exclusions = raw_membership.get((trading_date, "편출"), set())

                for code in sorted(additions - raw_additions):
                    known = inferred_addition_context(trading_date, code)
                    if known is not None:
                        reason, context = known
                        add_row(
                            trading_date,
                            code,
                            "편입",
                            reason,
                            context,
                            "전일·당일 NXT 대상 명단 차이와 공식 선정 공지",
                        )
                    elif code not in seen_codes:
                        add_row(
                            trading_date,
                            code,
                            "편입",
                            "변동내역 누락(대상 명단 편입)",
                            ChangeContext(
                                "대상 명단 편입(사유 확인 필요)",
                                "NXT 일별 대상 명단 대사",
                                NXT_CHANGE_LIST_URL,
                            ),
                            "전일·당일 NXT 대상 명단 차이",
                        )

                for code in sorted(exclusions - raw_exclusions):
                    known = inferred_exclusion_context(trading_date, code)
                    if known is not None:
                        reason, context, basis = known
                        add_row(trading_date, code, "편출", reason, context, basis)
                        continue
                    has_future_row = last_present_index.get(code, -1) > index
                    enough_confirmation = len(trading_dates) - index > 5
                    if not has_future_row and enough_confirmation:
                        add_row(
                            trading_date,
                            code,
                            "편출",
                            "변동내역 누락(대상 명단 제외)",
                            ChangeContext(
                                "대상 명단 제외(사유 확인 필요)",
                                "NXT 일별 대상 명단 대사",
                                NXT_CHANGE_LIST_URL,
                            ),
                            "전일·당일 NXT 대상 명단 차이; 원본 변동내역 없음",
                        )

                seen_codes.update(current_codes)
                previous_codes = current_codes

            connection.execute("DELETE FROM nxt_membership_change_adjustments")
            connection.executemany(
                """
                INSERT INTO nxt_membership_change_adjustments (
                    change_date, stock_code, stock_name, market, change_type,
                    reason, display_reason, isin, basis, source_title,
                    source_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                inferred,
            )
        return len(inferred)

    def missing_ranges(
        self,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, date]]:
        if end_date < start_date:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sync_date FROM nxt_change_sync_days
                WHERE sync_date BETWEEN ? AND ?
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        completed = {date.fromisoformat(str(row["sync_date"])) for row in rows}
        missing: list[date] = []
        cursor = start_date
        while cursor <= end_date:
            if cursor not in completed:
                missing.append(cursor)
            cursor += timedelta(days=1)
        if not missing:
            return []

        ranges: list[tuple[date, date]] = []
        range_start = missing[0]
        previous = missing[0]
        for current in missing[1:]:
            if current != previous + timedelta(days=1):
                ranges.append((range_start, previous))
                range_start = current
            previous = current
        ranges.append((range_start, previous))
        return ranges

    def replace_range(
        self,
        start_date: date,
        end_date: date,
        changes: list[NxtChange],
    ) -> None:
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        fetched_at = datetime.now(timezone.utc).isoformat()
        in_range = [
            item for item in changes if start_date <= item.change_date <= end_date
        ]
        counts: dict[date, int] = {}
        for item in in_range:
            counts[item.change_date] = counts.get(item.change_date, 0) + 1

        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM nxt_membership_changes WHERE change_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.executemany(
                """
                INSERT INTO nxt_membership_changes (
                    change_date, stock_code, stock_name, market, change_type,
                    reason, isin, registered_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(change_date, stock_code, change_type, reason) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    market = excluded.market,
                    isin = excluded.isin,
                    registered_at = excluded.registered_at,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        item.change_date.isoformat(),
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        item.change_type,
                        item.reason,
                        item.isin,
                        item.registered_at,
                        fetched_at,
                    )
                    for item in in_range
                ],
            )
            cursor = start_date
            while cursor <= end_date:
                connection.execute(
                    """
                    INSERT INTO nxt_change_sync_days (
                        sync_date, fetched_at, event_count
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(sync_date) DO UPDATE SET
                        fetched_at = excluded.fetched_at,
                        event_count = excluded.event_count
                    """,
                    (cursor.isoformat(), fetched_at, counts.get(cursor, 0)),
                )
                cursor += timedelta(days=1)
        # 변동 원본이 갱신된 날짜의 파생 선정·거래상태도 함께 다시 계산합니다.
        # 지역 import로 저장소 모듈 간 초기화 순환을 피합니다.
        from src.historical_market import HistoricalMarketStore

        self.rebuild_inferred_changes()
        historical_store = HistoricalMarketStore(self.path)
        with self._lock, self._connect() as connection:
            coverage_row = connection.execute(
                "SELECT MAX(trade_date) AS last_date FROM nxt_daily_quotes"
            ).fetchone()
        coverage_end = (
            date.fromisoformat(str(coverage_row["last_date"]))
            if coverage_row and coverage_row["last_date"]
            else end_date
        )
        historical_store.rebuild_nxt_eligibility_history(
            NXT_LAUNCH_DATE,
            coverage_end,
        )

    def try_claim(self, start_date: date, end_date: date) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, last_attempt_at FROM nxt_change_sync_state WHERE singleton_id = 1"
            ).fetchone()
            last_attempt = (
                datetime.fromisoformat(str(row["last_attempt_at"]))
                if row is not None and row["last_attempt_at"]
                else None
            )
            if (
                row is not None
                and row["status"] == "running"
                and last_attempt is not None
                and now - last_attempt < STALE_RUN_AFTER
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE nxt_change_sync_state
                SET last_attempt_at = ?, status = 'running', message = '',
                    last_start_date = ?, last_end_date = ?
                WHERE singleton_id = 1
                """,
                (now.isoformat(), start_date.isoformat(), end_date.isoformat()),
            )
            connection.commit()
        return True

    def mark_success(self, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE nxt_change_sync_state
                SET last_success_at = ?, status = 'success', message = ?
                WHERE singleton_id = 1
                """,
                (now, message),
            )

    def mark_failure(self, message: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE nxt_change_sync_state
                SET status = 'error', message = ?
                WHERE singleton_id = 1
                """,
                (message[:1000],),
            )

    def stats(self) -> NxtChangeStoreStats:
        with self._lock, self._connect() as connection:
            coverage = connection.execute(
                """
                SELECT MIN(sync_date) AS coverage_start,
                       MAX(sync_date) AS coverage_end,
                       COUNT(*) AS synced_day_count
                FROM nxt_change_sync_days
                """
            ).fetchone()
            event_count = int(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM nxt_membership_changes) +
                        (SELECT COUNT(*) FROM nxt_membership_change_adjustments)
                    """
                ).fetchone()[0]
            )
            state = connection.execute(
                "SELECT * FROM nxt_change_sync_state WHERE singleton_id = 1"
            ).fetchone()
        return NxtChangeStoreStats(
            coverage_start=(
                date.fromisoformat(str(coverage["coverage_start"]))
                if coverage["coverage_start"]
                else None
            ),
            coverage_end=(
                date.fromisoformat(str(coverage["coverage_end"]))
                if coverage["coverage_end"]
                else None
            ),
            synced_day_count=int(coverage["synced_day_count"]),
            event_count=event_count,
            last_attempt_at=(
                datetime.fromisoformat(str(state["last_attempt_at"]))
                if state["last_attempt_at"]
                else None
            ),
            last_success_at=(
                datetime.fromisoformat(str(state["last_success_at"]))
                if state["last_success_at"]
                else None
            ),
            status=str(state["status"]),
            message=str(state["message"]),
        )


class NxtChangeSyncService:
    def __init__(
        self,
        store: NxtChangeStore,
        client_factory: Callable[[], NxtClient] | None = None,
    ) -> None:
        self.store = store
        self.client_factory = client_factory or (
            lambda: NxtClient(ResponseCache())
        )

    def sync(
        self,
        start_date: date,
        end_date: date,
        *,
        force: bool = False,
    ) -> NxtChangeSyncResult:
        if end_date < start_date:
            return NxtChangeSyncResult(
                "noop", start_date, end_date, message="동기화할 날짜가 없습니다."
            )
        ranges = (
            [(start_date, end_date)]
            if force
            else self.store.missing_ranges(start_date, end_date)
        )
        if not ranges:
            return NxtChangeSyncResult(
                "noop", start_date, end_date, message="이미 모두 적재되어 있습니다."
            )
        if not self.store.try_claim(start_date, end_date):
            return NxtChangeSyncResult(
                "locked", start_date, end_date, message="다른 동기화 작업이 실행 중입니다."
            )

        client = self.client_factory()
        total_events = 0
        try:
            for range_start, range_end in ranges:
                changes = client.fetch_changes(
                    range_start,
                    range_end,
                    force_refresh=True,
                )
                self.store.replace_range(range_start, range_end, changes)
                total_events += len(changes)
            message = (
                f"{len(ranges)}개 구간, 변동내역 {total_events:,}건을 동기화했습니다."
            )
            self.store.mark_success(message)
            return NxtChangeSyncResult(
                "success",
                start_date,
                end_date,
                tuple(ranges),
                total_events,
                client.ssl_fallback_used,
                message,
            )
        except Exception as exc:
            self.store.mark_failure(f"{type(exc).__name__}: {exc}")
            raise


class NxtChangeScheduler:
    """앱 실행 중 자정 동기화와 재시작 시 누락 보충을 담당합니다."""

    def __init__(
        self,
        service: NxtChangeSyncService,
        *,
        start_date: date = NXT_LAUNCH_DATE,
    ) -> None:
        self.service = service
        self.start_date = start_date
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="nxt-change-midnight-sync",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def sync_due(self, now: datetime | None = None) -> NxtChangeSyncResult:
        current = now.astimezone(KST) if now is not None else datetime.now(KST)
        target = current.date() - timedelta(days=1)
        return self.service.sync(self.start_date, target)

    def _run(self) -> None:
        try:
            self.sync_due()
        except Exception:
            # 오류 내용은 DB 상태에 저장되며 다음 자정 또는 앱 재시작 때 재시도합니다.
            pass
        while not self._stop_event.is_set():
            now = datetime.now(KST)
            next_midnight = datetime.combine(
                now.date() + timedelta(days=1), time.min, tzinfo=KST
            )
            wait_seconds = max(1.0, (next_midnight - now).total_seconds())
            if self._stop_event.wait(wait_seconds):
                return
            try:
                self.sync_due(next_midnight)
            except Exception:
                pass
