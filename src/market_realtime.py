from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


NXT = "NXT"
KRX = "KRX"

NXT_PRE = "nxt_pre"
NXT_MAIN = "nxt_main"
NXT_AFTER = "nxt_after"
KRX_OPENING = "krx_opening_call"
KRX_CONTINUOUS = "krx_continuous"
KRX_CLOSING = "krx_closing_call"

NXT_PHASES = (NXT_PRE, NXT_MAIN, NXT_AFTER)
KRX_PHASES = (KRX_OPENING, KRX_CONTINUOUS, KRX_CLOSING)
ALL_PHASES = NXT_PHASES + KRX_PHASES


@dataclass(frozen=True)
class WatchSymbol:
    symbol: str
    name: str


@dataclass(frozen=True)
class TradeTick:
    market: str
    symbol: str
    traded_at: datetime
    price: int
    trade_volume: int
    cumulative_volume: int
    cumulative_amount: int
    reference_price: int | None = None


@dataclass
class PhaseMetric:
    volume: int = 0
    amount: int = 0
    complete: bool = True
    started: bool = False


@dataclass
class StreamState:
    last_volume: int | None = None
    last_amount: int | None = None
    last_traded_at: datetime | None = None
    last_phase: str | None = None
    opening_seen: bool = False
    gap_pending: bool = False
    last_price: int | None = None
    reference_price: int | None = None


class MarketDataStore:
    """관심종목과 당일 장 구간별 집계를 저장하는 SQLite 저장소입니다."""

    def __init__(self, path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.path = path or project_root / "data" / "kis_market.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_watchlist (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_phase_metrics (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    volume INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    started INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, symbol, phase)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_stream_state (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    last_volume INTEGER,
                    last_amount INTEGER,
                    last_traded_at TEXT,
                    last_phase TEXT,
                    opening_seen INTEGER NOT NULL,
                    gap_pending INTEGER NOT NULL,
                    last_price INTEGER,
                    reference_price INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, symbol, market)
                )
                """
            )
            state_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(market_stream_state)"
                ).fetchall()
            }
            if "last_price" not in state_columns:
                connection.execute(
                    "ALTER TABLE market_stream_state ADD COLUMN last_price INTEGER"
                )
            if "reference_price" not in state_columns:
                connection.execute(
                    "ALTER TABLE market_stream_state ADD COLUMN reference_price INTEGER"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_phase_metrics_date_symbol
                ON market_phase_metrics(trade_date, symbol)
                """
            )
            connection.execute("PRAGMA optimize")

    def load_watchlist(self) -> list[WatchSymbol]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, name
                FROM market_watchlist
                ORDER BY position
                """
            ).fetchall()
        return [WatchSymbol(str(row["symbol"]), str(row["name"])) for row in rows]

    def save_watchlist(self, symbols: Iterable[WatchSymbol]) -> None:
        normalized = list(symbols)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM market_watchlist")
            connection.executemany(
                """
                INSERT INTO market_watchlist(symbol, name, position)
                VALUES (?, ?, ?)
                """,
                [(item.symbol, item.name, index) for index, item in enumerate(normalized)],
            )

    def load_day(
        self,
        trade_date: date,
    ) -> tuple[dict[tuple[str, str], PhaseMetric], dict[tuple[str, str], StreamState]]:
        metrics: dict[tuple[str, str], PhaseMetric] = {}
        states: dict[tuple[str, str], StreamState] = {}
        with self._lock, self._connect() as connection:
            metric_rows = connection.execute(
                """
                SELECT symbol, phase, volume, amount, complete, started
                FROM market_phase_metrics
                WHERE trade_date = ?
                """,
                (trade_date.isoformat(),),
            ).fetchall()
            state_rows = connection.execute(
                """
                SELECT symbol, market, last_volume, last_amount, last_traded_at,
                       last_phase, opening_seen, gap_pending, last_price,
                       reference_price
                FROM market_stream_state
                WHERE trade_date = ?
                """,
                (trade_date.isoformat(),),
            ).fetchall()

        for row in metric_rows:
            metrics[(str(row["symbol"]), str(row["phase"]))] = PhaseMetric(
                volume=int(row["volume"]),
                amount=int(row["amount"]),
                complete=bool(row["complete"]),
                started=bool(row["started"]),
            )
        for row in state_rows:
            traded_at = (
                datetime.fromisoformat(str(row["last_traded_at"]))
                if row["last_traded_at"]
                else None
            )
            states[(str(row["symbol"]), str(row["market"]))] = StreamState(
                last_volume=(int(row["last_volume"]) if row["last_volume"] is not None else None),
                last_amount=(int(row["last_amount"]) if row["last_amount"] is not None else None),
                last_traded_at=traded_at,
                last_phase=(str(row["last_phase"]) if row["last_phase"] else None),
                opening_seen=bool(row["opening_seen"]),
                gap_pending=bool(row["gap_pending"]),
                last_price=(int(row["last_price"]) if row["last_price"] is not None else None),
                reference_price=(
                    int(row["reference_price"])
                    if row["reference_price"] is not None
                    else None
                ),
            )
        return metrics, states

    def save_day(
        self,
        trade_date: date,
        metrics: dict[tuple[str, str], PhaseMetric],
        states: dict[tuple[str, str], StreamState],
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        metric_values = [
            (
                trade_date.isoformat(),
                symbol,
                phase,
                metric.volume,
                metric.amount,
                int(metric.complete),
                int(metric.started),
                updated_at,
            )
            for (symbol, phase), metric in metrics.items()
        ]
        state_values = [
            (
                trade_date.isoformat(),
                symbol,
                market,
                state.last_volume,
                state.last_amount,
                state.last_traded_at.isoformat() if state.last_traded_at else None,
                state.last_phase,
                int(state.opening_seen),
                int(state.gap_pending),
                state.last_price,
                state.reference_price,
                updated_at,
            )
            for (symbol, market), state in states.items()
        ]
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_phase_metrics(
                    trade_date, symbol, phase, volume, amount,
                    complete, started, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, symbol, phase) DO UPDATE SET
                    volume = excluded.volume,
                    amount = excluded.amount,
                    complete = excluded.complete,
                    started = excluded.started,
                    updated_at = excluded.updated_at
                """,
                metric_values,
            )
            connection.executemany(
                """
                INSERT INTO market_stream_state(
                    trade_date, symbol, market, last_volume, last_amount,
                    last_traded_at, last_phase, opening_seen, gap_pending,
                    last_price, reference_price, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, symbol, market) DO UPDATE SET
                    last_volume = excluded.last_volume,
                    last_amount = excluded.last_amount,
                    last_traded_at = excluded.last_traded_at,
                    last_phase = excluded.last_phase,
                    opening_seen = excluded.opening_seen,
                    gap_pending = excluded.gap_pending,
                    last_price = excluded.last_price,
                    reference_price = excluded.reference_price,
                    updated_at = excluded.updated_at
                """,
                state_values,
            )


class MarketAggregator:
    """KRX/NXT 누적 체결값을 요청한 장 구간별 증분으로 변환합니다."""

    def __init__(self, store: MarketDataStore, trade_date: date | None = None) -> None:
        self.store = store
        self.trade_date = trade_date or datetime.now(ZoneInfo("Asia/Seoul")).date()
        self._lock = threading.RLock()
        self._metrics, self._states = self.store.load_day(self.trade_date)
        self._dirty = False

    def _ensure_date(self, trade_date: date) -> None:
        if trade_date == self.trade_date:
            return
        if self._dirty:
            self.flush()
        self.trade_date = trade_date
        self._metrics, self._states = self.store.load_day(trade_date)
        self._dirty = False

    @staticmethod
    def _nxt_phase(at: time) -> str | None:
        if time(8, 0) <= at <= time(8, 50):
            return NXT_PRE
        if time(9, 0, 30) <= at <= time(15, 20):
            return NXT_MAIN
        if time(15, 40) <= at <= time(20, 0):
            return NXT_AFTER
        return None

    @staticmethod
    def _krx_phase(at: time, state: StreamState) -> str | None:
        if at < time(9, 0) or at > time(15, 30):
            return None
        # 시가 단일가 체결은 같은 09:00:00 타임스탬프로 여러 건 올 수 있어
        # 첫 건만이 아니라 해당 초의 체결을 모두 시가 구간에 포함합니다.
        if at == time(9, 0):
            return KRX_OPENING
        if at < time(15, 20):
            return KRX_CONTINUOUS
        return KRX_CLOSING

    @staticmethod
    def _is_first_market_phase(market: str, phase: str, traded_at: datetime) -> bool:
        if market == NXT:
            return phase == NXT_PRE and traded_at.time() <= time(8, 1)
        return phase == KRX_OPENING and traded_at.time() == time(9, 0)

    def ingest(self, tick: TradeTick) -> None:
        if tick.market not in {NXT, KRX}:
            raise ValueError(f"지원하지 않는 시장입니다: {tick.market}")
        with self._lock:
            self._ensure_date(tick.traded_at.date())
            state_key = (tick.symbol, tick.market)
            state = self._states.setdefault(state_key, StreamState())

            if state.last_traded_at and tick.traded_at < state.last_traded_at:
                return

            phase = (
                self._nxt_phase(tick.traded_at.time())
                if tick.market == NXT
                else self._krx_phase(tick.traded_at.time(), state)
            )
            if phase is None:
                return

            metric = self._metrics.setdefault((tick.symbol, phase), PhaseMetric())
            first_observation = state.last_volume is None or state.last_amount is None

            if first_observation:
                if self._is_first_market_phase(tick.market, phase, tick.traded_at):
                    delta_volume = max(0, tick.cumulative_volume)
                    delta_amount = max(0, tick.cumulative_amount)
                else:
                    delta_volume = 0
                    delta_amount = 0
                    metric.complete = False
                    for previous_phase in self._previous_phases(tick.market, phase):
                        previous = self._metrics.setdefault(
                            (tick.symbol, previous_phase), PhaseMetric()
                        )
                        previous.complete = False
            elif state.gap_pending and state.last_phase != phase:
                delta_volume = 0
                delta_amount = 0
                metric.complete = False
                if state.last_phase:
                    previous = self._metrics.setdefault(
                        (tick.symbol, state.last_phase), PhaseMetric()
                    )
                    previous.complete = False
            else:
                delta_volume = self._cumulative_delta(
                    state.last_volume,
                    tick.cumulative_volume,
                )
                delta_amount = self._cumulative_delta(
                    state.last_amount,
                    tick.cumulative_amount,
                )

            metric.volume += delta_volume
            metric.amount += delta_amount
            metric.started = True

            if phase == KRX_OPENING:
                state.opening_seen = True
            state.last_volume = tick.cumulative_volume
            state.last_amount = tick.cumulative_amount
            state.last_traded_at = tick.traded_at
            state.last_phase = phase
            state.gap_pending = False
            state.last_price = tick.price if tick.price > 0 else state.last_price
            if tick.reference_price and tick.reference_price > 0:
                state.reference_price = tick.reference_price
            self._dirty = True

    @staticmethod
    def _cumulative_delta(previous: int | None, current: int) -> int:
        if previous is None:
            return 0
        if current >= previous:
            return current - previous
        return max(0, current)

    @staticmethod
    def _previous_phases(market: str, phase: str) -> tuple[str, ...]:
        phases = NXT_PHASES if market == NXT else KRX_PHASES
        position = phases.index(phase)
        return phases[:position]

    def mark_gap(self) -> None:
        with self._lock:
            for state in self._states.values():
                state.gap_pending = True
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.store.save_day(self.trade_date, self._metrics, self._states)
            self._dirty = False

    def snapshot(self, watchlist: Iterable[WatchSymbol]) -> list[dict[str, object]]:
        with self._lock:
            rows: list[dict[str, object]] = []
            for item in watchlist:
                phase_metrics = {
                    phase: self._metrics.get((item.symbol, phase), PhaseMetric())
                    for phase in ALL_PHASES
                }
                started = [metric for metric in phase_metrics.values() if metric.started]
                quality = (
                    "부분"
                    if any(not metric.complete for metric in started)
                    else ("정상" if started else "대기")
                )
                last_times = [
                    state.last_traded_at
                    for (symbol, _), state in self._states.items()
                    if symbol == item.symbol and state.last_traded_at is not None
                ]
                row: dict[str, object] = {
                    "종목코드": item.symbol,
                    "종목명": item.name,
                    "집계상태": quality,
                    "최종수신": max(last_times).strftime("%H:%M:%S") if last_times else "-",
                }
                for phase, metric in phase_metrics.items():
                    row[f"{phase}_volume"] = metric.volume
                    row[f"{phase}_amount"] = metric.amount
                nxt_state = self._states.get((item.symbol, NXT), StreamState())
                krx_state = self._states.get((item.symbol, KRX), StreamState())
                row["nxt_total_volume"] = max(
                    sum(phase_metrics[phase].volume for phase in NXT_PHASES),
                    nxt_state.last_volume or 0,
                )
                row["nxt_total_amount"] = max(
                    sum(phase_metrics[phase].amount for phase in NXT_PHASES),
                    nxt_state.last_amount or 0,
                )
                row["krx_total_volume"] = max(
                    sum(phase_metrics[phase].volume for phase in KRX_PHASES),
                    krx_state.last_volume or 0,
                )
                row["krx_total_amount"] = max(
                    sum(phase_metrics[phase].amount for phase in KRX_PHASES),
                    krx_state.last_amount or 0,
                )
                nxt_price = nxt_state.last_price
                krx_price = krx_state.last_price
                reference_price = nxt_state.reference_price or krx_state.reference_price
                row["nxt_current_price"] = nxt_price
                row["krx_current_price"] = krx_price
                row["reference_price"] = reference_price
                row["change_rate"] = self._price_change(nxt_price, reference_price)
                row["disparity_rate"] = self._price_change(nxt_price, krx_price)
                row["volume_ratio"] = self._ratio(
                    int(row["nxt_total_volume"]),
                    int(row["krx_total_volume"]),
                )
                row["amount_ratio"] = self._ratio(
                    int(row["nxt_total_amount"]),
                    int(row["krx_total_amount"]),
                )
                rows.append(row)
            return rows

    @staticmethod
    def _price_change(current: int | None, base: int | None) -> float | None:
        if current is None or base is None or base <= 0:
            return None
        return current / base - 1

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return numerator / denominator
