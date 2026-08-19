from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from src.krx_openapi import KrxListedSecurityDaily
from src.seed_distribution import resolve_deployed_seed


@dataclass(frozen=True)
class KrxListedHistoryCoverage:
    first_date: date | None
    last_date: date | None
    trading_days: int
    rows: int


def apply_index_memberships(
    records: Iterable[KrxListedSecurityDaily],
    memberships: Mapping[str, set[str]],
) -> list[KrxListedSecurityDaily]:
    kospi200 = memberships.get("KOSPI200", set())
    kosdaq150 = memberships.get("KOSDAQ150", set())
    return [
        replace(
            item,
            is_kospi200=item.short_code in kospi200,
            is_kosdaq150=item.short_code in kosdaq150,
        )
        for item in records
    ]


class KrxListedHistoryStore:
    """KRX KOSPI·KOSDAQ 전 상장종목의 일별 거래정보를 저장합니다."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        seed_path: Path | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "krx_listed_history.db"
        if seed_path is not None:
            self.seed_path = seed_path
        elif path is None:
            bundled_seed = project_root / "data" / "krx_listed_history.db.gz"
            self.seed_path = resolve_deployed_seed(
                project_root,
                bundled_seed,
                "krx_listed_history",
            )
        else:
            self.seed_path = None
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
                prefix=".krx-listed-restore-",
                suffix=".db",
                delete=False,
            ) as target:
                temporary_path = Path(target.name)
                with gzip.open(self.seed_path, "rb") as source:
                    shutil.copyfileobj(source, target)
            seed_rows, seed_last = self._database_coverage(temporary_path)
            current_rows, current_last = self._database_coverage(self.path)
            if (
                seed_rows > 0
                and current_rows >= seed_rows
                and current_last is not None
                and seed_last is not None
                and current_last >= seed_last
            ):
                return
            if self.path.exists():
                source = sqlite3.connect(temporary_path, timeout=30)
                target = sqlite3.connect(self.path, timeout=30)
                try:
                    source.backup(target)
                    target.commit()
                finally:
                    target.close()
                    source.close()
            else:
                os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _database_coverage(path: Path) -> tuple[int, str | None]:
        if not path.exists():
            return 0, None
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=5,
            )
            try:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='krx_listed_daily'"
                ).fetchone()
                if exists is None:
                    return 0, None
                row = connection.execute(
                    "SELECT COUNT(*), MAX(trade_date) FROM krx_listed_daily"
                ).fetchone()
                return int(row[0]), str(row[1]) if row[1] else None
            finally:
                connection.close()
        except sqlite3.Error:
            return 0, None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS krx_listed_daily (
                    trade_date TEXT NOT NULL,
                    standard_code TEXT NOT NULL,
                    short_code TEXT NOT NULL,
                    stock_name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    stock_type TEXT NOT NULL,
                    security_type TEXT NOT NULL,
                    listed_shares INTEGER NOT NULL,
                    listing_date TEXT,
                    cumulative_volume INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL,
                    is_kospi200 INTEGER NOT NULL,
                    is_kosdaq150 INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, standard_code),
                    UNIQUE (trade_date, short_code)
                );

                CREATE INDEX IF NOT EXISTS idx_krx_listed_daily_date_market
                    ON krx_listed_daily(trade_date, market);
                CREATE INDEX IF NOT EXISTS idx_krx_listed_daily_short_code
                    ON krx_listed_daily(short_code, trade_date);
                CREATE INDEX IF NOT EXISTS idx_krx_listed_daily_indices
                    ON krx_listed_daily(trade_date, is_kospi200, is_kosdaq150);
                """
            )

    def replace_daily(self, records: Iterable[KrxListedSecurityDaily]) -> int:
        rows = list(records)
        if not rows:
            return 0
        trading_dates = {item.trade_date for item in rows}
        if len(trading_dates) != 1:
            raise ValueError("한 번에 하나의 거래일만 저장할 수 있습니다.")
        trading_date = next(iter(trading_dates))
        if len({item.standard_code for item in rows}) != len(rows):
            raise ValueError("동일 거래일에 중복된 표준코드가 있습니다.")
        if len({item.short_code for item in rows}) != len(rows):
            raise ValueError("동일 거래일에 중복된 단축코드가 있습니다.")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        date_key = trading_date.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM krx_listed_daily WHERE trade_date = ?",
                (date_key,),
            )
            connection.executemany(
                """
                INSERT INTO krx_listed_daily (
                    trade_date, standard_code, short_code, stock_name, market,
                    stock_type, security_type, listed_shares, listing_date,
                    cumulative_volume, cumulative_amount, is_kospi200,
                    is_kosdaq150, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        date_key,
                        item.standard_code,
                        item.short_code,
                        item.stock_name,
                        item.market,
                        item.stock_type,
                        item.security_type,
                        int(item.listed_shares),
                        item.listing_date.isoformat() if item.listing_date else None,
                        int(item.cumulative_volume),
                        int(item.cumulative_amount),
                        int(item.is_kospi200),
                        int(item.is_kosdaq150),
                        updated_at,
                    )
                    for item in rows
                ],
            )
        return len(rows)

    def dates(self) -> list[date]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM krx_listed_daily ORDER BY trade_date"
            ).fetchall()
        return [date.fromisoformat(str(row["trade_date"])) for row in rows]

    def load_daily(self, trading_date: date) -> list[KrxListedSecurityDaily]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM krx_listed_daily
                WHERE trade_date = ?
                ORDER BY market, short_code
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return [
            KrxListedSecurityDaily(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                standard_code=str(row["standard_code"]),
                short_code=str(row["short_code"]),
                stock_name=str(row["stock_name"]),
                market=str(row["market"]),
                stock_type=str(row["stock_type"]),
                security_type=str(row["security_type"]),
                listed_shares=int(row["listed_shares"]),
                listing_date=(
                    date.fromisoformat(str(row["listing_date"]))
                    if row["listing_date"]
                    else None
                ),
                cumulative_volume=int(row["cumulative_volume"]),
                cumulative_amount=int(row["cumulative_amount"]),
                is_kospi200=bool(row["is_kospi200"]),
                is_kosdaq150=bool(row["is_kosdaq150"]),
            )
            for row in rows
        ]

    def coverage(self) -> KrxListedHistoryCoverage:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(trade_date) AS first_date,
                       MAX(trade_date) AS last_date,
                       COUNT(DISTINCT trade_date) AS trading_days,
                       COUNT(*) AS rows
                FROM krx_listed_daily
                """
            ).fetchone()
        return KrxListedHistoryCoverage(
            first_date=(
                date.fromisoformat(str(row["first_date"])) if row["first_date"] else None
            ),
            last_date=(
                date.fromisoformat(str(row["last_date"])) if row["last_date"] else None
            ),
            trading_days=int(row["trading_days"] or 0),
            rows=int(row["rows"] or 0),
        )
