from datetime import date

from src.models import NxtChange, NxtTradingStatus
from src.nxt_eligibility import (
    calculate_daily_eligibility,
    classify_nxt_change,
    infer_missing_restriction_statuses,
)


def _status(day: date, code: str = "123450") -> NxtTradingStatus:
    return NxtTradingStatus(
        status_date=day,
        stock_code=code,
        stock_name="테스트",
        market="KOSDAQ",
        tradable_market="전체",
        unavailable_reason="-",
    )


def _change(day: date, change_type: str, reason: str) -> NxtChange:
    return NxtChange(
        change_date=day,
        stock_code="123450",
        stock_name="테스트",
        market="KOSDAQ",
        change_type=change_type,
        reason=reason,
    )


def test_warning_and_short_term_heat_are_not_membership_changes() -> None:
    assert (
        classify_nxt_change(
            _change(date(2025, 4, 30), "편출", "투자경고/위험 지정")
        )
        == "거래불가"
    )
    assert (
        classify_nxt_change(
            _change(date(2025, 5, 2), "편입", "투자경고/위험 해제")
        )
        == "거래불가 해제"
    )
    assert (
        classify_nxt_change(_change(date(2025, 5, 3), "편출", "단기과열 지정"))
        == "거래불가"
    )


def test_infers_missing_rows_between_restriction_start_and_release() -> None:
    dates = [date(2025, 5, day) for day in (19, 20, 21, 22)]
    statuses = {
        dates[0]: [_status(dates[0])],
        dates[1]: [],
        dates[2]: [],
        dates[3]: [_status(dates[3])],
    }
    changes = [
        _change(dates[1], "편출", "투자경고/위험 지정"),
        _change(dates[3], "편입", "투자경고/위험 해제"),
    ]
    inferred = infer_missing_restriction_statuses(dates, statuses, changes)
    assert set(inferred) == {dates[1], dates[2]}
    assert inferred[dates[1]][0].unavailable_reason == "투자경고/위험"


def test_infers_release_only_gap_from_last_present_trading_day() -> None:
    dates = [date(2025, 5, day) for day in (19, 20, 21, 22)]
    statuses = {
        dates[0]: [_status(dates[0])],
        dates[1]: [],
        dates[2]: [],
        dates[3]: [_status(dates[3])],
    }
    changes = [_change(dates[3], "편입", "단기과열 해제")]
    inferred = infer_missing_restriction_statuses(dates, statuses, changes)
    assert set(inferred) == {dates[1], dates[2]}
    assert inferred[dates[2]][0].basis == "해제 변동 전 원본 누락기간 역산"


def test_daily_summary_counts_inferred_unavailable_as_target() -> None:
    day = date(2025, 5, 20)
    available = _status(day, "111111")
    unavailable = NxtTradingStatus(
        status_date=day,
        stock_code="222222",
        stock_name="거래제한",
        market="KOSPI",
        tradable_market="거래불가",
        unavailable_reason="단기과열",
    )
    summary, reason_counts = calculate_daily_eligibility(
        day,
        [available, unavailable],
        [],
    )
    assert summary.target_stock_count == 2
    assert summary.tradable_stock_count == 1
    assert summary.unavailable_stock_count == 1
    assert reason_counts[0].reason_group == "거래불가사유"


def test_known_launch_restriction_override_restores_paramaresearch() -> None:
    trading_date = date(2025, 3, 24)
    inferred = infer_missing_restriction_statuses(
        [trading_date],
        {trading_date: []},
        [],
    )
    item = inferred[trading_date][0]
    assert item.stock_code == "214450"
    assert item.unavailable_reason == "투자경고/위험"
    assert item.basis == "NXT 2025-03-21 3단계 확대 공지"
