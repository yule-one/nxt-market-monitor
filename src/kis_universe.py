from __future__ import annotations

import gzip
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from src.kis_master import KisMasterSecurity, fetch_master_securities
from src.kis_rest import KisRestClient, RestQuote
from src.market_realtime import NXT
from src.models import NxtTradingStatus


if TYPE_CHECKING:
    from src.nxt_client import NxtClient


KST = ZoneInfo("Asia/Seoul")
MASTER_BATCH_SIZE = 30


@dataclass(frozen=True)
class KisUniverseRefreshStatus:
    state: str
    message: str
    snapshot_date: date | None
    universe_count: int
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class NxtUniverseReconciliation:
    run_at: datetime
    status_date: date
    kis_count: int
    official_count: int
    matched_count: int
    kis_only_count: int
    official_only_count: int
    metadata_difference_count: int


def _chunks(values: list[KisMasterSecurity], size: int) -> Iterable[list[KisMasterSecurity]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _quote_is_nxt_candidate(quote: RestQuote) -> bool:
    return any(
        value is not None and int(value) > 0
        for value in (
            quote.current_price,
            quote.reference_price,
            quote.upper_limit_price,
            quote.lower_limit_price,
        )
    )


def master_unavailable_reason(item: KisMasterSecurity) -> str:
    """Convert KIS master market flags into an NXT trading-unavailable reason."""

    if item.trading_halt:
        return "거래정지"
    if item.liquidation:
        return "정리매매"
    if item.management:
        return "관리종목"
    if item.investment_caution:
        return "투자주의환기"
    if item.short_term_overheat in {"2", "3"}:
        return "단기과열"
    warning_codes = (
        {"02", "03"}
        if item.snapshot_market == "KOSPI"
        else {"01", "02", "03"}
    )
    if item.market_warning in warning_codes:
        return "투자경고/위험"
    return ""


class KisNxtUniverseStore:
    """Durable KIS-derived universe and local NXT reconciliation store."""

    def __init__(self, path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "kis_universe.db"
        self.seed_path = self.path.with_suffix(f"{self.path.suffix}.gz")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._restore_seed_if_needed()
        self._initialize()

    def _restore_seed_if_needed(self) -> None:
        if self.path.exists() or not self.seed_path.exists():
            return
        temporary = self.path.with_suffix(f"{self.path.suffix}.restore.tmp")
        with gzip.open(self.seed_path, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kis_master_security (
                    snapshot_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    security_group TEXT NOT NULL,
                    reference_price INTEGER,
                    listed_shares INTEGER,
                    trading_halt INTEGER NOT NULL,
                    liquidation INTEGER NOT NULL,
                    management INTEGER NOT NULL,
                    market_warning TEXT NOT NULL,
                    warning_preannouncement INTEGER NOT NULL,
                    short_term_overheat TEXT NOT NULL,
                    investment_caution INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_date, symbol)
                );

                CREATE TABLE IF NOT EXISTS kis_nxt_universe (
                    snapshot_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    tradable_market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    reference_price INTEGER,
                    current_price INTEGER,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    open_price INTEGER,
                    high_price INTEGER,
                    low_price INTEGER,
                    upper_limit_price INTEGER,
                    lower_limit_price INTEGER,
                    quote_valid INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_official_universe_snapshot (
                    snapshot_date TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    tradable_market TEXT NOT NULL,
                    unavailable_reason TEXT NOT NULL,
                    isin TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_date, stock_code)
                );

                CREATE TABLE IF NOT EXISTS nxt_universe_reconciliation_run (
                    run_at TEXT PRIMARY KEY,
                    status_date TEXT NOT NULL,
                    kis_count INTEGER NOT NULL,
                    official_count INTEGER NOT NULL,
                    matched_count INTEGER NOT NULL,
                    kis_only_count INTEGER NOT NULL,
                    official_only_count INTEGER NOT NULL,
                    metadata_difference_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nxt_universe_reconciliation_item (
                    run_at TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    difference_type TEXT NOT NULL,
                    kis_tradable_market TEXT NOT NULL,
                    kis_unavailable_reason TEXT NOT NULL,
                    official_tradable_market TEXT NOT NULL,
                    official_unavailable_reason TEXT NOT NULL,
                    PRIMARY KEY (run_at, stock_code, difference_type),
                    FOREIGN KEY (run_at)
                        REFERENCES nxt_universe_reconciliation_run(run_at)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_kis_nxt_universe_date
                ON kis_nxt_universe(snapshot_date);
                CREATE INDEX IF NOT EXISTS idx_nxt_official_universe_date
                ON nxt_official_universe_snapshot(snapshot_date);
                """
            )

    def save_master(
        self,
        snapshot_date: date,
        securities: Iterable[KisMasterSecurity],
    ) -> int:
        rows = list(securities)
        now = datetime.now(KST).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM kis_master_security WHERE snapshot_date = ?",
                (snapshot_date.isoformat(),),
            )
            connection.executemany(
                """
                INSERT INTO kis_master_security VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        snapshot_date.isoformat(),
                        item.symbol,
                        item.isin,
                        item.name,
                        item.snapshot_market,
                        item.security_group,
                        item.reference_price,
                        item.listed_shares,
                        int(item.trading_halt),
                        int(item.liquidation),
                        int(item.management),
                        item.market_warning,
                        int(item.warning_preannouncement),
                        item.short_term_overheat,
                        int(item.investment_caution),
                        now,
                    )
                    for item in rows
                ],
            )
        return len(rows)

    def save_universe(
        self,
        snapshot_date: date,
        statuses: Iterable[NxtTradingStatus],
        *,
        quote_valid_codes: set[str],
        basis_by_code: dict[str, str],
    ) -> int:
        rows = list(statuses)
        now = datetime.now(KST).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM kis_nxt_universe WHERE snapshot_date = ?",
                (snapshot_date.isoformat(),),
            )
            connection.executemany(
                """
                INSERT INTO kis_nxt_universe VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        snapshot_date.isoformat(),
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        item.tradable_market,
                        item.unavailable_reason,
                        item.isin,
                        item.reference_price,
                        item.current_price,
                        item.cumulative_volume,
                        item.cumulative_amount,
                        item.open_price,
                        item.high_price,
                        item.low_price,
                        item.upper_limit_price,
                        item.lower_limit_price,
                        int(item.stock_code in quote_valid_codes),
                        basis_by_code.get(item.stock_code, "KIS NXT 시세"),
                        now,
                    )
                    for item in rows
                ],
            )
        return len(rows)

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> NxtTradingStatus:
        return NxtTradingStatus(
            status_date=date.fromisoformat(str(row["snapshot_date"])),
            stock_code=str(row["stock_code"]),
            stock_name=str(row["stock_name"]),
            market=str(row["market"]),
            tradable_market=str(row["tradable_market"]),
            unavailable_reason=str(row["unavailable_reason"]),
            isin=str(row["isin"]),
            reference_price=row["reference_price"],
            current_price=row["current_price"],
            cumulative_volume=int(row["cumulative_volume"]),
            cumulative_amount=int(row["cumulative_amount"]),
            open_price=row["open_price"],
            high_price=row["high_price"],
            low_price=row["low_price"],
            upper_limit_price=row["upper_limit_price"],
            lower_limit_price=row["lower_limit_price"],
        )

    def load_universe(self, snapshot_date: date) -> list[NxtTradingStatus]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM kis_nxt_universe
                WHERE snapshot_date = ?
                ORDER BY market, stock_code
                """,
                (snapshot_date.isoformat(),),
            ).fetchall()
        return [self._status_from_row(row) for row in rows]

    def load_latest_universe(
        self,
        on_or_before: date,
    ) -> tuple[date | None, list[NxtTradingStatus]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM kis_nxt_universe WHERE snapshot_date <= ?
                """,
                (on_or_before.isoformat(),),
            ).fetchone()
        if row is None or not row["snapshot_date"]:
            return None, []
        snapshot_date = date.fromisoformat(str(row["snapshot_date"]))
        return snapshot_date, self.load_universe(snapshot_date)

    def save_official_snapshot(
        self,
        snapshot_date: date,
        statuses: Iterable[NxtTradingStatus],
    ) -> int:
        rows = list(statuses)
        fetched_at = datetime.now(KST).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM nxt_official_universe_snapshot WHERE snapshot_date = ?",
                (snapshot_date.isoformat(),),
            )
            connection.executemany(
                """
                INSERT INTO nxt_official_universe_snapshot VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        snapshot_date.isoformat(),
                        item.stock_code,
                        item.stock_name,
                        item.market,
                        item.tradable_market,
                        item.unavailable_reason,
                        item.isin,
                        fetched_at,
                    )
                    for item in rows
                ],
            )
        return len(rows)

    def apply_official_metadata(
        self,
        snapshot_date: date,
        statuses: Iterable[NxtTradingStatus],
    ) -> int:
        """Apply only matched official status metadata; keep KIS membership intact."""

        rows = list(statuses)
        updated = 0
        with self._lock, self._connect() as connection:
            for item in rows:
                cursor = connection.execute(
                    """
                    UPDATE kis_nxt_universe
                    SET tradable_market = ?, unavailable_reason = ?,
                        basis = CASE
                            WHEN basis LIKE '%NXT 대사 메타데이터%' THEN basis
                            ELSE basis || ' + NXT 대사 메타데이터'
                        END
                    WHERE snapshot_date = ? AND stock_code = ?
                    """,
                    (
                        item.tradable_market or "전체",
                        item.unavailable_reason or "",
                        snapshot_date.isoformat(),
                        item.stock_code,
                    ),
                )
                updated += cursor.rowcount
        return updated

    def load_latest_official_snapshot(
        self,
        on_or_before: date,
    ) -> tuple[date | None, list[NxtTradingStatus]]:
        with self._lock, self._connect() as connection:
            date_row = connection.execute(
                """
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM nxt_official_universe_snapshot WHERE snapshot_date <= ?
                """,
                (on_or_before.isoformat(),),
            ).fetchone()
            if date_row is None or not date_row["snapshot_date"]:
                return None, []
            snapshot_date = date.fromisoformat(str(date_row["snapshot_date"]))
            rows = connection.execute(
                """
                SELECT * FROM nxt_official_universe_snapshot
                WHERE snapshot_date = ? ORDER BY market, stock_code
                """,
                (snapshot_date.isoformat(),),
            ).fetchall()
        return snapshot_date, [
            NxtTradingStatus(
                status_date=snapshot_date,
                stock_code=str(row["stock_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                tradable_market=str(row["tradable_market"]),
                unavailable_reason=str(row["unavailable_reason"]),
                isin=str(row["isin"]),
            )
            for row in rows
        ]

    def save_reconciliation(
        self,
        summary: NxtUniverseReconciliation,
        differences: Iterable[dict[str, str]],
    ) -> None:
        run_key = summary.run_at.isoformat()
        rows = list(differences)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO nxt_universe_reconciliation_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_key,
                    summary.status_date.isoformat(),
                    summary.kis_count,
                    summary.official_count,
                    summary.matched_count,
                    summary.kis_only_count,
                    summary.official_only_count,
                    summary.metadata_difference_count,
                ),
            )
            connection.execute(
                "DELETE FROM nxt_universe_reconciliation_item WHERE run_at = ?",
                (run_key,),
            )
            connection.executemany(
                """
                INSERT INTO nxt_universe_reconciliation_item VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        run_key,
                        item["stock_code"],
                        item["stock_name"],
                        item["difference_type"],
                        item["kis_tradable_market"],
                        item["kis_unavailable_reason"],
                        item["official_tradable_market"],
                        item["official_unavailable_reason"],
                    )
                    for item in rows
                ],
            )


class KisNxtUniverseResolver:
    def __init__(
        self,
        client: KisRestClient,
        store: KisNxtUniverseStore,
        *,
        previous_status_loader: Callable[
            [date], tuple[date | None, list[NxtTradingStatus]]
        ]
        | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.previous_status_loader = previous_status_loader

    def _previous_statuses(
        self,
        status_date: date,
    ) -> tuple[date | None, list[NxtTradingStatus]]:
        prior_date = status_date - timedelta(days=1)
        stored_date, stored = self.store.load_latest_universe(prior_date)
        if stored:
            return stored_date, stored
        if self.previous_status_loader is not None:
            return self.previous_status_loader(prior_date)
        return None, []

    def refresh(
        self,
        status_date: date,
        *,
        force: bool = False,
    ) -> list[NxtTradingStatus]:
        if not force:
            cached = self.store.load_universe(status_date)
            if cached:
                return cached

        master_rows = fetch_master_securities(self.client.session)
        self.store.save_master(status_date, master_rows)
        candidates = {
            item.symbol: item
            for item in master_rows
            if item.is_common_stock_candidate
        }

        quote_by_code: dict[str, RestQuote] = {}
        valid_codes: set[str] = set()
        candidate_rows = sorted(candidates.values(), key=lambda item: item.symbol)
        for batch in _chunks(candidate_rows, MASTER_BATCH_SIZE):
            quotes = self.client.fetch_quotes([(NXT, item.symbol) for item in batch])
            for quote in quotes:
                quote_by_code[quote.symbol] = quote
                if _quote_is_nxt_candidate(quote):
                    valid_codes.add(quote.symbol)

        _previous_date, previous = self._previous_statuses(status_date)
        previous_codes = {item.stock_code for item in previous}
        restricted_previous_codes = {
            code
            for code in previous_codes
            if code in candidates and master_unavailable_reason(candidates[code])
        }
        included_codes = valid_codes | restricted_previous_codes

        official_date, official_rows = self.store.load_latest_official_snapshot(
            status_date
        )
        official_is_fresh = (
            official_date is not None
            and status_date - official_date <= timedelta(days=3)
        )
        official_by_code = (
            {item.stock_code: item for item in official_rows}
            if official_is_fresh
            else {}
        )

        statuses: list[NxtTradingStatus] = []
        basis_by_code: dict[str, str] = {}
        for code in sorted(included_codes, key=lambda value: (candidates[value].snapshot_market, value)):
            master = candidates[code]
            quote = quote_by_code.get(code)
            reason = master_unavailable_reason(master)
            tradable_market = "거래불가" if reason else "전체"
            basis = "KIS NXT 시세"
            if code in restricted_previous_codes and code not in valid_codes:
                basis = "직전 NXT 대상 + KIS 시장조치"
            official = official_by_code.get(code)
            if official is not None:
                tradable_market = official.tradable_market or tradable_market
                reason = official.unavailable_reason or reason
                basis += " + NXT 대사 메타데이터"
            basis_by_code[code] = basis
            statuses.append(
                NxtTradingStatus(
                    status_date=status_date,
                    stock_code=code,
                    stock_name=(quote.name if quote and quote.name else master.name),
                    market=master.snapshot_market,
                    tradable_market=tradable_market,
                    unavailable_reason=reason,
                    isin=master.isin,
                    reference_price=(
                        quote.reference_price if quote else master.reference_price
                    ),
                    current_price=quote.current_price if quote else None,
                    cumulative_volume=quote.cumulative_volume if quote else 0,
                    cumulative_amount=quote.cumulative_amount if quote else 0,
                    open_price=quote.open_price if quote else None,
                    high_price=quote.high_price if quote else None,
                    low_price=quote.low_price if quote else None,
                    upper_limit_price=quote.upper_limit_price if quote else None,
                    lower_limit_price=quote.lower_limit_price if quote else None,
                )
            )

        self.store.save_universe(
            status_date,
            statuses,
            quote_valid_codes=valid_codes,
            basis_by_code=basis_by_code,
        )
        return statuses

    def reconcile_with_official(
        self,
        status_date: date,
        official_client: NxtClient,
    ) -> NxtUniverseReconciliation:
        kis_rows = self.refresh(status_date)
        official_rows = official_client.fetch_trading_status(
            status_date,
            force_refresh=True,
        )
        self.store.save_official_snapshot(status_date, official_rows)

        kis = {item.stock_code: item for item in kis_rows}
        official = {item.stock_code: item for item in official_rows}
        matched_codes = set(kis) & set(official)
        differences: list[dict[str, str]] = []
        for code in sorted(set(kis) | set(official)):
            kis_item = kis.get(code)
            official_item = official.get(code)
            if kis_item is None:
                difference_type = "NXT 공식만 존재"
            elif official_item is None:
                difference_type = "KIS 계산만 존재"
            elif (
                kis_item.tradable_market != official_item.tradable_market
                or kis_item.unavailable_reason != official_item.unavailable_reason
            ):
                difference_type = "거래상태 불일치"
            else:
                continue
            differences.append(
                {
                    "stock_code": code,
                    "stock_name": (
                        (kis_item.stock_name if kis_item else "")
                        or (official_item.stock_name if official_item else "")
                    ),
                    "difference_type": difference_type,
                    "kis_tradable_market": (
                        kis_item.tradable_market if kis_item else ""
                    ),
                    "kis_unavailable_reason": (
                        kis_item.unavailable_reason if kis_item else ""
                    ),
                    "official_tradable_market": (
                        official_item.tradable_market if official_item else ""
                    ),
                    "official_unavailable_reason": (
                        official_item.unavailable_reason if official_item else ""
                    ),
                }
            )

        run_at = datetime.now(KST)
        summary = NxtUniverseReconciliation(
            run_at=run_at,
            status_date=status_date,
            kis_count=len(kis),
            official_count=len(official),
            matched_count=len(matched_codes),
            kis_only_count=len(set(kis) - set(official)),
            official_only_count=len(set(official) - set(kis)),
            metadata_difference_count=sum(
                item["difference_type"] == "거래상태 불일치"
                for item in differences
            ),
        )
        self.store.save_reconciliation(summary, differences)
        self.store.apply_official_metadata(status_date, official_rows)
        return summary


class KisNxtUniverseSyncService:
    """Run the once-daily KIS universe build once per server process."""

    def __init__(self, resolver: KisNxtUniverseResolver) -> None:
        self.resolver = resolver
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = "대기"
        self._message = "KIS 종목 대상 계산 대기"
        self._snapshot_date: date | None = None
        self._universe_count = 0
        self._started_at: datetime | None = None
        self._completed_at: datetime | None = None

    def start_if_needed(self, status_date: date) -> bool:
        cached = self.resolver.store.load_universe(status_date)
        if cached:
            with self._lock:
                self._state = "완료"
                self._message = "당일 KIS 종목 대상 계산 완료"
                self._snapshot_date = status_date
                self._universe_count = len(cached)
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._state = "계산중"
            self._message = "KIS 마스터와 NXT 시세로 종목 대상을 계산하는 중"
            self._started_at = datetime.now(KST)
            self._thread = threading.Thread(
                target=self._run,
                args=(status_date,),
                name="kis-nxt-universe-sync",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, status_date: date) -> None:
        try:
            rows = self.resolver.refresh(status_date)
        except Exception as exc:
            with self._lock:
                self._state = "오류"
                self._message = str(exc)
            return
        with self._lock:
            self._state = "완료"
            self._message = "당일 KIS 종목 대상 계산 완료"
            self._snapshot_date = status_date
            self._universe_count = len(rows)
            self._completed_at = datetime.now(KST)

    def status(self) -> KisUniverseRefreshStatus:
        with self._lock:
            return KisUniverseRefreshStatus(
                state=self._state,
                message=self._message,
                snapshot_date=self._snapshot_date,
                universe_count=self._universe_count,
                started_at=self._started_at,
                completed_at=self._completed_at,
            )


def select_websocket_anchor_symbols(
    universe: Iterable[NxtTradingStatus],
    *,
    limit: int = 20,
) -> list[NxtTradingStatus]:
    """Pick stable, liquid symbols for the constrained KIS WebSocket channel."""

    return sorted(
        universe,
        key=lambda item: (
            item.cumulative_amount,
            item.cumulative_volume,
            item.stock_code,
        ),
        reverse=True,
    )[: max(0, min(20, limit))]
