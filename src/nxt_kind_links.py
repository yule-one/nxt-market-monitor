from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Sequence

from src.models import Disclosure
from src.nxt_eligibility import (
    NxtUnavailabilityEvent,
    NxtUnavailabilityKindLink,
)


KIND_LINK_CATEGORIES = (
    "거래정지/재개",
    "투자주의종목",
    "투자경고종목",
    "투자위험종목",
    "단기과열종목",
    "기타시장안내",
)
KIND_LINK_LOOKBACK_DAYS = 45

_REASON_CATEGORIES = {
    "투자경고/위험": {
        "투자주의종목",
        "투자경고종목",
        "투자위험종목",
    },
    "단기과열": {"단기과열종목"},
    "거래정지": {"거래정지/재개"},
    "시가기준가": {"기타시장안내"},
}


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def _direction(title: str, reason: str) -> str | None:
    compact = _compact(title)
    if not compact:
        return None

    if reason == "투자경고/위험":
        if "해제" in compact or "취소" in compact:
            return "거래불가 해제"
        if "예고" in compact or "우려" in compact:
            return None
        if "지정" in compact and "연장" not in compact:
            return "거래불가"
        return None

    if reason == "단기과열":
        if "해제" in compact or "취소" in compact:
            return "거래불가 해제"
        if "예고" in compact:
            return None
        if "지정" in compact and "연장" not in compact:
            return "거래불가"
        return None

    if reason == "거래정지":
        has_start = "정지" in compact
        has_end = "재개" in compact or "정지해제" in compact
        if has_start and has_end:
            # 한 공시에 정지와 재개가 모두 들어간 경우는 해제 이벤트에만 사용합니다.
            return "거래불가 해제"
        if has_end:
            return "거래불가 해제"
        if has_start:
            return "거래불가"
        return None

    if reason == "시가기준가":
        if "시가기준가" not in compact:
            return None
        if "해제" in compact or "종료" in compact:
            return "거래불가 해제"
        if "적용" in compact or "결정" in compact or "산출" in compact:
            return "거래불가"
        return None

    return None


def _matches_event_direction(
    event: NxtUnavailabilityEvent,
    disclosure: Disclosure,
) -> bool:
    direction = _direction(disclosure.title, event.unavailable_reason)
    if direction == event.event_type:
        return True
    if (
        event.unavailable_reason == "거래정지"
        and event.event_type == "거래불가"
        and direction == "거래불가 해제"
    ):
        compact = _compact(disclosure.title)
        # 한 공시에 정지와 해제가 함께 적힌 경우에도 해당 공시가 거래불가 구간의
        # 최초 원인이 됩니다. 해제 이벤트는 아래의 구간 승계 로직에서 연결합니다.
        return "정지" in compact and ("재개" in compact or "정지해제" in compact)
    if event.unavailable_reason != "단기과열" or event.event_type != "거래불가 해제":
        return False
    compact = _compact(disclosure.title)
    day_gap = (event.event_date - disclosure.disclosure_date).days
    # 단기과열은 별도 해제 공시 없이 공시된 3거래일 지정기간이 끝나는 경우가
    # 일반적이므로, 해당 지정 공시를 해제 이벤트의 원문으로도 연결합니다.
    return (
        "3거래일" in compact
        and "지정" in compact
        and "예고" not in compact
        and 3 <= day_gap
    )


def _candidate_score(
    event: NxtUnavailabilityEvent,
    disclosure: Disclosure,
) -> tuple[int, int, int]:
    day_gap = (event.event_date - disclosure.disclosure_date).days
    compact = _compact(disclosure.title)
    exact_reason = 0
    if event.unavailable_reason == "투자경고/위험":
        if disclosure.category == "투자위험종목" and "투자위험" in compact:
            exact_reason = 2
        elif disclosure.category == "투자경고종목" and "투자경고" in compact:
            exact_reason = 2
    elif event.unavailable_reason == "단기과열" and "단기과열" in compact:
        exact_reason = 2
    elif event.unavailable_reason == "거래정지" and "거래정지" in compact:
        exact_reason = 2
    elif event.unavailable_reason == "시가기준가" and "시가기준가" in compact:
        exact_reason = 2
    return (-day_gap, exact_reason, int(disclosure.disclosed_at.timestamp()))


def match_unavailability_event(
    event: NxtUnavailabilityEvent,
    disclosures: Sequence[Disclosure],
    *,
    lookback_days: int = KIND_LINK_LOOKBACK_DAYS,
) -> NxtUnavailabilityKindLink | None:
    """NXT 거래불가 이벤트에 방향과 날짜가 일치하는 KIND 공시를 연결합니다."""

    categories = _REASON_CATEGORIES.get(event.unavailable_reason)
    if not categories:
        return None
    window_start = event.event_date - timedelta(days=lookback_days)
    relevant = [
        item
        for item in disclosures
        if item.stock_code == event.stock_code
        and item.category in categories
        and window_start <= item.disclosure_date <= event.event_date
    ]
    candidates = [
        item
        for item in relevant
        if _matches_event_direction(event, item)
    ]
    if not candidates:
        return None

    selected = max(candidates, key=lambda item: _candidate_score(event, item))
    # 지정과 해제가 짧은 기간에 이어진 전환 구간에서는 선택 공시보다 나중의
    # 반대 방향 공시가 있으면 현재 이벤트의 직접 근거로 단정하지 않습니다.
    later_opposites = [
        item
        for item in relevant
        if item.disclosed_at > selected.disclosed_at
        and _direction(item.title, event.unavailable_reason) not in {
            None,
            event.event_type,
        }
    ]
    if later_opposites:
        return None

    gap = (event.event_date - selected.disclosure_date).days
    selected_direction = _direction(selected.title, event.unavailable_reason)
    is_combined_suspension_start = (
        event.unavailable_reason == "거래정지"
        and event.event_type == "거래불가"
        and selected_direction == "거래불가 해제"
    )
    is_short_period_end = (
        event.unavailable_reason == "단기과열"
        and event.event_type == "거래불가 해제"
        and selected_direction == "거래불가"
    )
    return NxtUnavailabilityKindLink(
        event_date=event.event_date,
        stock_code=event.stock_code,
        event_type=event.event_type,
        report_no=selected.report_no,
        category=selected.category,
        title=selected.title,
        disclosed_at=selected.disclosed_at,
        viewer_url=selected.viewer_url,
        match_basis=(
            (
                "종목코드·단기과열 지정기간 종료 일치, "
                if is_short_period_end
                else (
                    "종목코드·정지 및 해제 통합공시의 거래불가 구간 일치, "
                    if is_combined_suspension_start
                    else f"종목코드·거래불가사유·{event.event_type} 방향 일치, "
                )
            )
            + f"KIND 공시일과 NXT 이벤트일 차이 {gap}일"
        ),
    )


def match_unavailability_events(
    events: Iterable[NxtUnavailabilityEvent],
    disclosures: Sequence[Disclosure],
    *,
    lookback_days: int = KIND_LINK_LOOKBACK_DAYS,
) -> list[NxtUnavailabilityKindLink]:
    event_list = sorted(
        list(events),
        key=lambda item: (item.event_date, item.stock_code, item.event_type),
    )
    by_stock: dict[str, list[Disclosure]] = defaultdict(list)
    for disclosure in disclosures:
        by_stock[disclosure.stock_code].append(disclosure)

    matches_by_key: dict[tuple[date, str, str], NxtUnavailabilityKindLink] = {}
    for event in event_list:
        matched = match_unavailability_event(
            event,
            by_stock.get(event.stock_code, []),
            lookback_days=lookback_days,
        )
        if matched is not None:
            matches_by_key[(event.event_date, event.stock_code, event.event_type)] = matched

    # NXT는 동일한 거래불가 구간의 시작과 해제를 별도 이벤트로 기록하지만,
    # KIND는 시작 원문 하나만 공시하는 경우가 있습니다. 직접 매칭되지 않은 해제만
    # 직전 시작 이벤트와 같은 구간일 때 그 원문을 승계합니다.
    open_periods: dict[
        str, tuple[NxtUnavailabilityEvent, NxtUnavailabilityKindLink]
    ] = {}
    for event in event_list:
        key = (event.event_date, event.stock_code, event.event_type)
        matched = matches_by_key.get(key)
        if event.event_type == "거래불가":
            if matched is not None:
                open_periods[event.stock_code] = (event, matched)
            else:
                open_periods.pop(event.stock_code, None)
            continue
        if event.event_type != "거래불가 해제":
            continue

        prior = open_periods.pop(event.stock_code, None)
        if matched is not None or prior is None:
            continue
        start_event, start_link = prior
        same_reason = start_event.unavailable_reason == event.unavailable_reason
        base_price_after_suspension = (
            start_event.unavailable_reason == "거래정지"
            and event.unavailable_reason == "시가기준가"
        )
        gap = (event.event_date - start_event.event_date).days
        if (
            not (same_reason or base_price_after_suspension)
            or gap < 0
            or gap > lookback_days
        ):
            continue
        matches_by_key[key] = NxtUnavailabilityKindLink(
            event_date=event.event_date,
            stock_code=event.stock_code,
            event_type=event.event_type,
            report_no=start_link.report_no,
            category=start_link.category,
            title=start_link.title,
            disclosed_at=start_link.disclosed_at,
            viewer_url=start_link.viewer_url,
            match_basis=(
                "같은 종목의 연속된 거래불가 구간 원문 승계, "
                f"시작 이벤트와 해제 이벤트 차이 {gap}일"
            ),
        )

    return [matches_by_key[key] for key in sorted(matches_by_key)]
