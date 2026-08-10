from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time
from typing import Hashable
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
NXT_MORNING_BREAK_START = datetime_time(8, 50)
NXT_MORNING_BREAK_END = datetime_time(9, 0)


def is_nxt_morning_break(moment: datetime | None = None) -> bool:
    """NXT의 08:50~09:00 거래 중단 구간인지 반환합니다."""

    current = moment or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    else:
        current = current.astimezone(KST)
    return NXT_MORNING_BREAK_START <= current.time() < NXT_MORNING_BREAK_END


def seconds_until_nxt_morning_resume(moment: datetime | None = None) -> float:
    current = moment or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    else:
        current = current.astimezone(KST)
    if not is_nxt_morning_break(current):
        return 0.0
    resume = current.replace(
        hour=NXT_MORNING_BREAK_END.hour,
        minute=NXT_MORNING_BREAK_END.minute,
        second=0,
        microsecond=0,
    )
    return max(0.0, (resume - current).total_seconds())


@dataclass(frozen=True)
class MinuteLookupPermit:
    allowed: bool
    retry_after_seconds: int = 0
    reason: str = ""


@dataclass
class _MinuteLookupState:
    in_flight: bool
    failure_count: int
    next_retry_at: float
    last_error: str
    updated_at: float


class NxtMinuteLookupGate:
    """여러 Streamlit 세션의 동일 종목 분봉 조회를 하나로 합칩니다."""

    def __init__(
        self,
        *,
        failure_base_seconds: float = 60.0,
        failure_max_seconds: float = 300.0,
        empty_retry_seconds: float = 60.0,
        success_cooldown_seconds: float = 5.0,
        in_flight_timeout_seconds: float = 30.0,
        state_ttl_seconds: float = 3_600.0,
    ) -> None:
        self.failure_base_seconds = max(1.0, float(failure_base_seconds))
        self.failure_max_seconds = max(
            self.failure_base_seconds,
            float(failure_max_seconds),
        )
        self.empty_retry_seconds = max(1.0, float(empty_retry_seconds))
        self.success_cooldown_seconds = max(
            1.0,
            float(success_cooldown_seconds),
        )
        self.in_flight_timeout_seconds = max(
            5.0,
            float(in_flight_timeout_seconds),
        )
        self.state_ttl_seconds = max(60.0, float(state_ttl_seconds))
        self._lock = threading.RLock()
        self._states: dict[Hashable, _MinuteLookupState] = {}

    def claim(
        self,
        key: Hashable,
        *,
        now: float | None = None,
    ) -> MinuteLookupPermit:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(timestamp)
            state = self._states.get(key)
            if state is None:
                self._states[key] = _MinuteLookupState(
                    in_flight=True,
                    failure_count=0,
                    next_retry_at=0.0,
                    last_error="",
                    updated_at=timestamp,
                )
                return MinuteLookupPermit(True)
            if state.in_flight:
                if timestamp - state.updated_at < self.in_flight_timeout_seconds:
                    return MinuteLookupPermit(False, reason="다른 접속자가 조회 중")
                # Streamlit 세션 이동·재실행으로 조회가 중단되면 완료 콜백이
                # 실행되지 않을 수 있으므로 오래된 잠금은 새 세션이 회수합니다.
                state.in_flight = True
                state.updated_at = timestamp
                return MinuteLookupPermit(True, reason="만료된 조회 잠금 회수")
            if timestamp < state.next_retry_at:
                return MinuteLookupPermit(
                    False,
                    retry_after_seconds=max(
                        1,
                        math.ceil(state.next_retry_at - timestamp),
                    ),
                    reason=state.last_error or "재조회 대기 중",
                )
            state.in_flight = True
            state.updated_at = timestamp
            return MinuteLookupPermit(True)

    def complete(
        self,
        key: Hashable,
        *,
        has_result: bool,
        now: float | None = None,
    ) -> int:
        timestamp = time.monotonic() if now is None else float(now)
        delay = (
            self.success_cooldown_seconds
            if has_result
            else self.empty_retry_seconds
        )
        with self._lock:
            state = self._states.setdefault(
                key,
                _MinuteLookupState(False, 0, 0.0, "", timestamp),
            )
            state.in_flight = False
            state.failure_count = 0
            state.next_retry_at = timestamp + delay
            state.last_error = "" if has_result else "분봉 결과 없음"
            state.updated_at = timestamp
        return math.ceil(delay)

    def fail(
        self,
        key: Hashable,
        error: str,
        *,
        now: float | None = None,
    ) -> int:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            state = self._states.setdefault(
                key,
                _MinuteLookupState(False, 0, 0.0, "", timestamp),
            )
            state.failure_count += 1
            delay = min(
                self.failure_base_seconds * (2 ** (state.failure_count - 1)),
                self.failure_max_seconds,
            )
            state.in_flight = False
            state.next_retry_at = timestamp + delay
            state.last_error = str(error)
            state.updated_at = timestamp
        return math.ceil(delay)

    def _prune(self, now: float) -> None:
        stale_keys = [
            key
            for key, state in self._states.items()
            if not state.in_flight and now - state.updated_at >= self.state_ttl_seconds
        ]
        for key in stale_keys:
            self._states.pop(key, None)
