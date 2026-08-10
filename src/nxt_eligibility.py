from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from src.config import (
    TRADING_RESTRICTION_END_KEYWORDS,
    TRADING_RESTRICTION_START_KEYWORDS,
)
from src.models import NxtChange, NxtTradingStatus
from src.nxt_change_context import contextual_change_reason


CHANGE_GROUPS = ("편입", "편출", "거래불가", "거래불가 해제")

# 2025-03-21 NXT 3단계 확대 공지에서 파마리서치는 3월 24일부터
# 투자경고 해제 전까지 거래 제한이라고 명시됐고, 3월 31일 확대 공지에서는
# 제한 종목에서 빠졌습니다. 구형 일별 원본에 누락된 이 구간만 보정합니다.
KNOWN_HISTORICAL_RESTRICTIONS = (
    (
        "214450",
        "파마리서치",
        "KOSDAQ",
        "투자경고/위험",
        date(2025, 3, 24),
        date(2025, 3, 31),
        "NXT 2025-03-21 3단계 확대 공지",
    ),
)


@dataclass(frozen=True)
class NxtDailyEligibilitySummary:
    trade_date: date
    target_stock_count: int
    tradable_stock_count: int
    unavailable_stock_count: int
    target_kospi_count: int
    target_kosdaq_count: int
    tradable_kospi_count: int
    tradable_kosdaq_count: int
    inclusion_stock_count: int
    exclusion_stock_count: int
    restriction_start_stock_count: int
    restriction_end_stock_count: int


@dataclass(frozen=True)
class NxtDailyReasonCount:
    trade_date: date
    reason_group: str
    reason: str
    stock_count: int


@dataclass(frozen=True)
class NxtEligibilityAdjustment:
    trade_date: date
    stock_code: str
    stock_name: str
    market: str
    unavailable_reason: str
    restriction_start_date: date
    restriction_end_date: date | None
    basis: str


@dataclass(frozen=True)
class NxtDailyUnavailability:
    trade_date: date
    stock_code: str
    stock_name: str
    market: str
    tradable_market: str
    unavailable_reason: str
    source_type: str
    source_title: str
    source_url: str
    basis: str


@dataclass(frozen=True)
class NxtUnavailabilityEvent:
    event_date: date
    stock_code: str
    stock_name: str
    market: str
    event_type: str
    tradable_market: str
    unavailable_reason: str
    source_type: str
    source_title: str
    source_url: str
    basis: str


def classify_nxt_change_type(change_type: str, reason: str) -> str:
    """원본 편입·편출을 종목선정 변경과 일시 거래제한으로 구분합니다."""

    if change_type == "편출" and any(
        keyword in reason for keyword in TRADING_RESTRICTION_START_KEYWORDS
    ):
        return "거래불가"
    if change_type == "편입" and any(
        keyword in reason for keyword in TRADING_RESTRICTION_END_KEYWORDS
    ):
        return "거래불가 해제"
    return change_type


def classify_nxt_change(change: NxtChange) -> str:
    return classify_nxt_change_type(change.change_type, change.reason)


def restriction_reason_from_change(reason: str) -> str:
    if "투자경고" in reason or "투자위험" in reason:
        return "투자경고/위험"
    if "단기과열" in reason:
        return "단기과열"
    if "거래정지" in reason:
        return "거래정지"
    if "시가기준가" in reason:
        return "시가기준가"
    return reason.replace(" 지정", "").replace(" 해제", "").strip()


def infer_missing_restriction_statuses(
    trading_dates: Iterable[date],
    statuses_by_date: dict[date, list[NxtTradingStatus]],
    changes: Iterable[NxtChange],
) -> dict[date, list[NxtEligibilityAdjustment]]:
    """구형 NXT 원본에서 빠진 거래제한 종목을 변동 시작·해제로 복원합니다."""

    ordered_dates = sorted(set(trading_dates))
    if not ordered_dates:
        return {}
    date_positions = {trading_date: index for index, trading_date in enumerate(ordered_dates)}
    present_dates_by_code: dict[str, list[date]] = defaultdict(list)
    status_metadata: dict[str, NxtTradingStatus] = {}
    for trading_date in ordered_dates:
        for item in statuses_by_date.get(trading_date, []):
            present_dates_by_code[item.stock_code].append(trading_date)
            status_metadata[item.stock_code] = item

    changes_by_code: dict[str, list[NxtChange]] = defaultdict(list)
    for item in changes:
        changes_by_code[item.stock_code].append(item)

    inferred: dict[date, list[NxtEligibilityAdjustment]] = defaultdict(list)
    for stock_code, stock_changes in changes_by_code.items():
        ordered_changes = sorted(
            stock_changes,
            key=lambda item: (item.change_date, item.registered_at),
        )
        active: tuple[date, str, NxtChange] | None = None

        def add_period(
            start_date: date,
            end_date: date | None,
            reason: str,
            source_change: NxtChange,
            basis: str,
        ) -> None:
            for trading_date in ordered_dates:
                if trading_date < start_date:
                    continue
                if end_date is not None and trading_date >= end_date:
                    break
                if any(
                    item.stock_code == stock_code
                    for item in statuses_by_date.get(trading_date, [])
                ):
                    continue
                metadata = status_metadata.get(stock_code)
                inferred[trading_date].append(
                    NxtEligibilityAdjustment(
                        trade_date=trading_date,
                        stock_code=stock_code,
                        stock_name=(
                            metadata.stock_name if metadata else source_change.stock_name
                        ),
                        market=metadata.market if metadata else source_change.market,
                        unavailable_reason=reason,
                        restriction_start_date=start_date,
                        restriction_end_date=end_date,
                        basis=basis,
                    )
                )

        for item in ordered_changes:
            group = classify_nxt_change(item)
            if group == "거래불가":
                if active is not None:
                    add_period(
                        active[0],
                        item.change_date,
                        active[1],
                        active[2],
                        "거래불가 지정·해제 변동내역",
                    )
                active = (
                    item.change_date,
                    restriction_reason_from_change(item.reason),
                    item,
                )
                continue
            if group in {"편입", "편출"}:
                if active is not None:
                    add_period(
                        active[0],
                        item.change_date,
                        active[1],
                        active[2],
                        "거래불가 지정 후 실제 선정 변동",
                    )
                    active = None
                continue
            if group != "거래불가 해제":
                continue
            release_reason = restriction_reason_from_change(item.reason)
            if active is not None:
                add_period(
                    active[0],
                    item.change_date,
                    active[1],
                    active[2],
                    "거래불가 지정·해제 변동내역",
                )
                active = None
                continue

            release_position = date_positions.get(item.change_date)
            if release_position is None:
                continue
            previous_present = [
                trading_date
                for trading_date in present_dates_by_code.get(stock_code, [])
                if trading_date < item.change_date
            ]
            if not previous_present:
                continue
            previous_position = date_positions[previous_present[-1]]
            missing_dates = [
                trading_date
                for trading_date in ordered_dates[previous_position + 1 : release_position]
                if not any(
                    status.stock_code == stock_code
                    for status in statuses_by_date.get(trading_date, [])
                )
            ]
            if not missing_dates:
                continue
            add_period(
                missing_dates[0],
                item.change_date,
                release_reason,
                item,
                "해제 변동 전 원본 누락기간 역산",
            )
        if active is not None:
            add_period(
                active[0],
                None,
                active[1],
                active[2],
                "거래불가 지정 변동내역",
            )
    for (
        stock_code,
        stock_name,
        market,
        reason,
        start_date,
        end_date,
        basis,
    ) in KNOWN_HISTORICAL_RESTRICTIONS:
        for trading_date in ordered_dates:
            if not start_date <= trading_date < end_date:
                continue
            inferred[trading_date].append(
                NxtEligibilityAdjustment(
                    trade_date=trading_date,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    market=market,
                    unavailable_reason=reason,
                    restriction_start_date=start_date,
                    restriction_end_date=end_date,
                    basis=basis,
                )
            )

    deduplicated: dict[date, list[NxtEligibilityAdjustment]] = {}
    for trading_date, rows in inferred.items():
        by_code = {item.stock_code: item for item in rows}
        deduplicated[trading_date] = list(by_code.values())
    return deduplicated


def calculate_daily_eligibility(
    trading_date: date,
    statuses: Iterable[NxtTradingStatus],
    changes: Iterable[NxtChange],
) -> tuple[NxtDailyEligibilitySummary, list[NxtDailyReasonCount]]:
    status_by_code = {item.stock_code: item for item in statuses}
    status_rows = list(status_by_code.values())
    unavailable_rows = [item for item in status_rows if item.is_unavailable]
    tradable_rows = [item for item in status_rows if not item.is_unavailable]
    change_rows = list(changes)

    classified_codes: dict[str, set[str]] = defaultdict(set)
    reason_codes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in change_rows:
        group = classify_nxt_change(item)
        classified_codes[group].add(item.stock_code)
        reason = contextual_change_reason(item)
        reason_codes[(group, reason)].add(item.stock_code)

    for item in unavailable_rows:
        reason = item.unavailable_reason.strip()
        if not reason or reason == "-":
            reason = "사유 미제공"
        reason_codes[("거래불가사유", reason)].add(item.stock_code)

    summary = NxtDailyEligibilitySummary(
        trade_date=trading_date,
        target_stock_count=len(status_rows),
        tradable_stock_count=len(tradable_rows),
        unavailable_stock_count=len(unavailable_rows),
        target_kospi_count=sum(item.market == "KOSPI" for item in status_rows),
        target_kosdaq_count=sum(item.market == "KOSDAQ" for item in status_rows),
        tradable_kospi_count=sum(item.market == "KOSPI" for item in tradable_rows),
        tradable_kosdaq_count=sum(item.market == "KOSDAQ" for item in tradable_rows),
        inclusion_stock_count=len(classified_codes["편입"]),
        exclusion_stock_count=len(classified_codes["편출"]),
        restriction_start_stock_count=len(classified_codes["거래불가"]),
        restriction_end_stock_count=len(classified_codes["거래불가 해제"]),
    )
    counts = [
        NxtDailyReasonCount(
            trade_date=trading_date,
            reason_group=group,
            reason=reason,
            stock_count=len(stock_codes),
        )
        for (group, reason), stock_codes in sorted(reason_codes.items())
    ]
    return summary, counts
