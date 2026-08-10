from __future__ import annotations

from datetime import date, datetime

from src.models import Disclosure
from src.nxt_eligibility import NxtUnavailabilityEvent
from src.nxt_kind_links import match_unavailability_event


def _event(
    event_type: str = "거래불가",
    reason: str = "투자경고/위험",
) -> NxtUnavailabilityEvent:
    return NxtUnavailabilityEvent(
        event_date=date(2026, 8, 4),
        stock_code="437730",
        stock_name="삼현",
        market="KOSDAQ",
        event_type=event_type,
        tradable_market="거래불가" if event_type == "거래불가" else "전체",
        unavailable_reason=reason,
        source_type="OFFICIAL_STATUS",
        source_title="NXT 거래현황",
        source_url="https://nextrade.example/status",
        basis="일별 상태 비교",
    )


def _disclosure(
    title: str,
    *,
    category: str = "투자경고종목",
    disclosed_at: datetime = datetime(2026, 8, 3, 20, 0),
    report_no: str = "20260803000883",
) -> Disclosure:
    return Disclosure(
        disclosed_at=disclosed_at,
        stock_code="437730",
        stock_name="삼현",
        market="KOSDAQ",
        category=category,
        title=title,
        submitter="코스닥시장본부",
        report_no=report_no,
        viewer_url=f"https://kind.example/{report_no}",
    )


def test_matches_previous_day_warning_designation() -> None:
    matched = match_unavailability_event(
        _event(),
        [_disclosure("투자경고종목 지정")],
    )

    assert matched is not None
    assert matched.report_no == "20260803000883"
    assert matched.viewer_url.endswith("20260803000883")


def test_ignores_warning_designation_notice() -> None:
    matched = match_unavailability_event(
        _event(),
        [_disclosure("투자경고종목 지정예고")],
    )

    assert matched is None


def test_matches_release_direction_only() -> None:
    matched = match_unavailability_event(
        _event(event_type="거래불가 해제"),
        [
            _disclosure("투자경고종목 지정", report_no="start"),
            _disclosure(
                "[투자주의]투자경고종목 지정해제 및 재지정 예고",
                category="투자주의종목",
                disclosed_at=datetime(2026, 8, 4, 7, 30),
                report_no="release",
            ),
        ],
    )

    assert matched is not None
    assert matched.report_no == "release"


def test_rejects_candidate_when_later_opposite_direction_exists() -> None:
    matched = match_unavailability_event(
        _event(event_type="거래불가 해제"),
        [
            _disclosure("투자경고종목 지정해제", report_no="release"),
            _disclosure(
                "투자위험종목 지정",
                category="투자위험종목",
                disclosed_at=datetime(2026, 8, 4, 7, 30),
                report_no="risk-start",
            ),
        ],
    )

    assert matched is None


def test_matches_short_overheat_and_suspension_categories() -> None:
    short = match_unavailability_event(
        _event(reason="단기과열"),
        [_disclosure("단기과열종목 지정", category="단기과열종목")],
    )
    suspension = match_unavailability_event(
        _event(event_type="거래불가 해제", reason="거래정지"),
        [
            _disclosure(
                "주권매매거래정지해제",
                category="거래정지/재개",
            )
        ],
    )

    assert short is not None
    assert suspension is not None


def test_short_overheat_release_uses_three_trading_day_designation() -> None:
    matched = match_unavailability_event(
        _event(event_type="거래불가 해제", reason="단기과열"),
        [
            _disclosure(
                "단기과열종목(3거래일 단일가매매) 지정",
                category="단기과열종목",
                disclosed_at=datetime(2026, 7, 31, 18, 0),
            )
        ],
    )

    assert matched is not None
    assert "지정기간 종료" in matched.match_basis
