from datetime import date, datetime

from src.analytics import (
    build_daily_metrics,
    build_daily_nxt_metrics,
    build_daily_nxt_metrics_from_counts,
    build_nxt_state_as_of,
    classify_nxt_change,
    disclosures_to_frame,
    extract_state_transitions,
    is_preferred_share_notice,
    match_disclosures_to_nxt_status,
    nxt_changes_display_frame,
    nxt_changes_to_frame,
    nxt_trading_status_to_frame,
    nxt_unavailability_events_to_frame,
    summarize_daily_nxt_unavailability_reasons,
    summarize_nxt_membership_flow,
)
from src.models import Disclosure, NxtChange, NxtTradingStatus, StatusPeriod
from src.nxt_eligibility import NxtDailyReasonCount, NxtUnavailabilityEvent


def nxt_change(
    day: date,
    code: str,
    change_type: str,
    reason: str,
    name: str = "테스트",
) -> NxtChange:
    return NxtChange(
        change_date=day,
        stock_code=code,
        stock_name=name,
        market="KOSDAQ",
        change_type=change_type,
        reason=reason,
    )


def disclosure(
    day: date,
    category: str,
    title: str,
    code: str = "123450",
    name: str = "테스트",
    report_no: str = "1",
) -> Disclosure:
    return Disclosure(
        disclosed_at=datetime.combine(day, datetime.min.time()),
        stock_code=code,
        stock_name=name,
        market="KOSDAQ",
        category=category,
        title=title,
        submitter="코스닥시장본부",
        report_no=report_no,
        viewer_url="https://example.test",
    )


def trading_status(
    day: date,
    code: str = "123450",
    name: str = "테스트",
    tradable_market: str = "전체",
    unavailable_reason: str = "",
) -> NxtTradingStatus:
    return NxtTradingStatus(
        status_date=day,
        stock_code=code,
        stock_name=name,
        market="KOSDAQ",
        tradable_market=tradable_market,
        unavailable_reason=unavailable_reason,
    )


def test_nxt_state_replays_add_exclude_readd() -> None:
    events = [
        nxt_change(date(2025, 3, 4), "123450", "편입", "특별변경"),
        nxt_change(date(2025, 3, 5), "123450", "편출", "투자경고/위험 지정"),
        nxt_change(date(2025, 3, 6), "123450", "편입", "투자경고/위험 해제"),
    ]
    excluded = build_nxt_state_as_of(events, date(2025, 3, 5))["123450"]
    assert not excluded.is_tradable
    assert excluded.is_temporary_exclusion
    assert excluded.membership_label == "거래불가 (2025-03-05)"
    readded = build_nxt_state_as_of(events, date(2025, 3, 6))["123450"]
    assert readded.is_tradable
    assert not readded.is_temporary_exclusion


def test_market_management_is_real_exclusion() -> None:
    event = nxt_change(date(2025, 3, 5), "123450", "편출", "시장관리")
    state = build_nxt_state_as_of([event], date(2025, 3, 5))["123450"]
    assert not state.is_tradable
    assert not state.is_temporary_exclusion
    assert state.membership_label == "편출 (2025-03-05)"


def test_disclosure_matches_official_unavailable_nxt_status() -> None:
    day = date(2025, 3, 5)
    item = disclosure(day, "관리종목", "관리종목지정")
    statuses = {
        day: [trading_status(day, tradable_market="거래불가", unavailable_reason="관리종목")]
    }
    matched = match_disclosures_to_nxt_status([item], statuses)[0]
    assert matched["is_nxt_related"]
    assert matched["nxt_tradable_market"] == "거래불가"
    assert matched["nxt_unavailable_reason"] == "관리종목"

    frame = disclosures_to_frame([matched])
    assert frame.iloc[0]["상장시장"] == "KOSDAQ"
    assert frame.iloc[0]["NXT 거래가능시장"] == "거래불가"
    assert frame.iloc[0]["NXT 거래불가사유"] == "관리종목"
    assert frame.columns[-2:].tolist() == [
        "NXT 거래가능시장",
        "NXT 거래불가사유",
    ]
    assert "시장" not in frame.columns


def test_disclosure_does_not_match_stock_excluded_before_query_period() -> None:
    day = date(2026, 7, 1)
    item = disclosure(day, "관리종목", "관리종목지정")
    statuses = {day: [trading_status(day, code="999999", name="다른종목")]}
    assert not match_disclosures_to_nxt_status([item], statuses)[0]["is_nxt_related"]


def test_trading_status_frame_uses_official_columns() -> None:
    frame = nxt_trading_status_to_frame(
        [
            trading_status(
                date(2025, 3, 5),
                tradable_market="거래불가",
                unavailable_reason="거래정지",
            )
        ]
    )
    assert frame.iloc[0]["거래가능시장"] == "거래불가"
    assert frame.iloc[0]["거래불가사유"] == "거래정지"
    assert frame.iloc[0]["상장시장"] == "KOSDAQ"
    assert "시장" not in frame.columns
    assert "NXT 상태" not in frame.columns


def test_nxt_change_frame_uses_user_facing_column_names() -> None:
    frame = nxt_changes_to_frame(
        [nxt_change(date(2025, 3, 5), "123450", "편출", "시장관리")]
    )
    assert frame.iloc[0]["상장시장"] == "KOSDAQ"
    assert frame.iloc[0]["변동내역"] == "편출"
    assert frame.iloc[0]["원본변동내역"] == "편출"
    assert frame.iloc[0]["변경사유"] == "거래량한도관리"
    assert frame.iloc[0]["원본사유"] == "시장관리"
    assert frame.iloc[0]["보정여부"] == "원본"
    assert not {"시장", "변동", "사유"}.intersection(frame.columns)


def test_nxt_change_display_frame_hides_audit_columns() -> None:
    frame = nxt_changes_display_frame(
        [nxt_change(date(2025, 3, 5), "123450", "편출", "시장관리")]
    )

    assert frame.columns.tolist() == [
        "일자",
        "종목코드",
        "종목명",
        "상장시장",
        "변동내역",
        "변경사유",
    ]
    assert not {
        "데이터근거",
        "보정여부",
        "원본변동내역",
        "원본사유",
    }.intersection(frame.columns)


def test_nxt_change_frame_accepts_cached_legacy_objects() -> None:
    item = nxt_change(date(2025, 3, 5), "123450", "편출", "시장관리")
    for field_name in (
        "display_reason",
        "source_title",
        "source_url",
        "basis",
        "is_inferred",
    ):
        object.__delattr__(item, field_name)

    frame = nxt_changes_to_frame([item])

    assert frame.iloc[0]["변경사유"] == "거래량한도관리"
    assert frame.iloc[0]["데이터근거"] == "NXT 종목 변동내역"
    assert frame.iloc[0]["보정여부"] == "원본"


def test_nxt_change_reclassifies_temporary_trading_restrictions() -> None:
    warning = nxt_change(
        date(2025, 4, 30),
        "123450",
        "편출",
        "투자경고/위험 지정",
    )
    released = nxt_change(
        date(2025, 5, 2),
        "123450",
        "편입",
        "투자경고/위험 해제",
    )
    assert classify_nxt_change(warning) == "거래불가"
    assert classify_nxt_change(released) == "거래불가 해제"
    frame = nxt_changes_to_frame([warning, released])
    assert frame["변동내역"].tolist() == ["거래불가", "거래불가 해제"]


def test_unavailability_frame_prefers_matched_kind_original() -> None:
    event = NxtUnavailabilityEvent(
        event_date=date(2026, 8, 4),
        stock_code="437730",
        stock_name="삼현",
        market="KOSDAQ",
        event_type="거래불가",
        tradable_market="거래불가",
        unavailable_reason="투자경고/위험",
        source_type="OFFICIAL_STATUS",
        source_title="NXT 거래현황",
        source_url="https://nextrade.example/status",
        basis="일별 상태 비교",
        kind_title="투자경고종목지정",
        kind_viewer_url="https://kind.example/viewer",
    )

    frame = nxt_unavailability_events_to_frame([event])

    assert frame.iloc[0]["KIND 공시"] == "https://kind.example/viewer"
    assert not {"데이터근거", "원문구분", "원문"}.intersection(frame.columns)


def test_unavailability_frame_leaves_unmatched_original_blank() -> None:
    event = NxtUnavailabilityEvent(
        event_date=date(2026, 8, 4),
        stock_code="437730",
        stock_name="삼현",
        market="KOSDAQ",
        event_type="거래불가",
        tradable_market="거래불가",
        unavailable_reason="거래정지",
        source_type="OFFICIAL_STATUS",
        source_title="NXT 거래현황",
        source_url="https://nextrade.example/status",
        basis="일별 상태 비교",
    )

    frame = nxt_unavailability_events_to_frame([event])

    assert frame.iloc[0]["KIND 공시"] == ""
    assert not {"데이터근거", "원문구분", "원문"}.intersection(frame.columns)


def test_preferred_share_notice_is_not_joined_to_common_share() -> None:
    item = disclosure(
        date(2025, 3, 5),
        "단기과열종목",
        "단기과열종목(가격괴리율) 지정(테스트우)",
    )
    assert is_preferred_share_notice(item)
    statuses = {date(2025, 3, 5): [trading_status(date(2025, 3, 5))]}
    assert not match_disclosures_to_nxt_status([item], statuses)[0]["is_nxt_related"]


def test_transaction_document_produces_start_and_release_transitions() -> None:
    item = disclosure(
        date(2026, 8, 3),
        "거래정지/재개",
        "매매거래정지및정지해제",
    )
    text = "매매거래정지일 2026년08월04일 부터 매매거래정지해제일 2026년08월05일 09:00 부터"
    transitions = extract_state_transitions(item, text)
    assert transitions == [
        (date(2026, 8, 4), "거래정지/재개", "123450", True),
        (date(2026, 8, 5), "거래정지/재개", "123450", False),
    ]


def test_daily_metrics_separates_status_and_daily_disclosure() -> None:
    status_day = date(2025, 3, 5)
    following_day = date(2025, 3, 6)
    statuses = {
        status_day: [
            trading_status(
                status_day,
                tradable_market="거래불가",
                unavailable_reason="관리종목",
            )
        ],
        following_day: [trading_status(following_day, code="999999", name="다른종목")],
    }
    item = disclosure(status_day, "관리종목", "관리종목지정")
    matched = match_disclosures_to_nxt_status([item], statuses)
    metrics = build_daily_metrics(
        matched,
        statuses,
        date(2025, 3, 4),
        date(2025, 3, 6),
    )
    day_row = metrics.loc[metrics["일자"] == status_day].iloc[0]
    following_row = metrics.loc[metrics["일자"] == following_day].iloc[0]
    assert day_row["NXT 종목수"] == 1
    assert day_row["관리종목"] == 1
    assert day_row["관리종목 당일공시"] == 1
    assert following_row["NXT 종목수"] == 1
    assert following_row["관리종목"] == 0


def test_daily_unavailable_reason_does_not_carry_to_later_date() -> None:
    notices = [
        disclosure(date(2025, 3, 5), "관리종목", "관리종목지정", report_no="1"),
    ]
    statuses = {
        date(2025, 3, 5): [trading_status(date(2025, 3, 5), unavailable_reason="관리종목")],
        date(2025, 3, 6): [trading_status(date(2025, 3, 6), code="999999", name="다른종목")],
        date(2025, 3, 7): [trading_status(date(2025, 3, 7))],
    }
    metrics = build_daily_metrics(
        match_disclosures_to_nxt_status(notices, statuses),
        statuses,
        date(2025, 3, 5),
        date(2025, 3, 7),
    )
    reentry_day = metrics.loc[metrics["일자"] == date(2025, 3, 7)].iloc[0]
    assert reentry_day["NXT 종목수"] == 1
    assert reentry_day["관리종목"] == 0


def test_daily_nxt_metrics_separates_state_and_daily_flow() -> None:
    events = [
        nxt_change(date(2025, 3, 4), "123450", "편입", "특별변경"),
        nxt_change(date(2025, 3, 5), "123450", "편출", "관리종목지정"),
    ]
    metrics = build_daily_nxt_metrics(
        events,
        {
            date(2025, 3, 4): [trading_status(date(2025, 3, 4))],
            date(2025, 3, 5): [
                trading_status(
                    date(2025, 3, 5),
                    tradable_market="거래불가",
                    unavailable_reason="관리종목",
                )
            ],
            date(2025, 3, 6): [trading_status(date(2025, 3, 6))],
        },
        date(2025, 3, 4),
        date(2025, 3, 6),
    )
    excluded_day = metrics.loc[metrics["일자"] == date(2025, 3, 5)].iloc[0]
    following_day = metrics.loc[metrics["일자"] == date(2025, 3, 6)].iloc[0]
    assert excluded_day["NXT 종목수"] == 1
    assert excluded_day["당일 편출 종목수"] == 1
    assert following_day["NXT 종목수"] == 1
    assert following_day["당일 편출 종목수"] == 0


def test_daily_nxt_metrics_uses_stored_stock_counts() -> None:
    events = [
        nxt_change(date(2025, 3, 5), "123450", "편출", "관리종목지정"),
    ]

    metrics = build_daily_nxt_metrics_from_counts(
        events,
        {
            date(2025, 3, 4): 590,
            date(2025, 3, 5): 589,
        },
        date(2025, 3, 4),
        date(2025, 3, 5),
    )

    first = metrics.loc[metrics["일자"] == date(2025, 3, 4)].iloc[0]
    second = metrics.loc[metrics["일자"] == date(2025, 3, 5)].iloc[0]
    assert first["NXT 종목수"] == 590
    assert first["당일 편출 종목수"] == 0
    assert second["NXT 종목수"] == 589
    assert second["당일 편출 종목수"] == 1


def test_membership_flow_includes_launch_day_additions_in_net_change() -> None:
    launch_date = date(2025, 3, 4)
    following_date = date(2025, 3, 5)
    events = [
        nxt_change(launch_date, f"{index:06d}", "편입", "특별변경")
        for index in range(10)
    ]
    events.extend(
        [
            nxt_change(following_date, "100001", "편입", "특별변경"),
            nxt_change(following_date, "000001", "편출", "정기변경"),
        ]
    )
    metrics = build_daily_nxt_metrics_from_counts(
        events,
        {launch_date: 10, following_date: 10},
        launch_date,
        following_date,
    )

    assert metrics.iloc[0]["당일 편입 종목수"] == 10
    assert summarize_nxt_membership_flow(metrics) == (11, 1, 10)


def test_daily_nxt_metrics_excludes_restrictions_from_membership_changes() -> None:
    events = [
        nxt_change(
            date(2025, 3, 5),
            "123450",
            "편출",
            "단기과열 지정",
        ),
        nxt_change(
            date(2025, 3, 6),
            "123450",
            "편입",
            "단기과열 해제",
        ),
    ]
    metrics = build_daily_nxt_metrics_from_counts(
        events,
        {
            date(2025, 3, 5): 590,
            date(2025, 3, 6): 590,
        },
        date(2025, 3, 5),
        date(2025, 3, 6),
    )
    assert metrics["당일 편출 종목수"].sum() == 0
    assert metrics["당일 편입 종목수"].sum() == 0


def test_summarize_daily_nxt_unavailability_reasons() -> None:
    trading_date = date(2026, 8, 10)
    reason_counts = [
        NxtDailyReasonCount(trading_date, "거래불가사유", "거래정지", 3),
        NxtDailyReasonCount(trading_date, "거래불가사유", "투자경고/위험", 2),
        NxtDailyReasonCount(trading_date, "편출", "정기변경", 10),
    ]

    label = summarize_daily_nxt_unavailability_reasons(reason_counts)

    assert label == "거래정지: 3종목 · 투자경고/위험: 2종목"
    assert summarize_daily_nxt_unavailability_reasons([]) == "-"


def test_daily_metrics_uses_official_warning_period() -> None:
    warning = disclosure(
        date(2025, 3, 5),
        "투자경고종목",
        "투자경고종목지정",
    )
    periods = [
        StatusPeriod(
            category="투자경고종목",
            stock_code="123450",
            stock_name="테스트",
            market="KOSDAQ",
            published_date=date(2025, 3, 5),
            start_date=date(2025, 3, 6),
            end_date=date(2025, 3, 8),
        )
    ]
    statuses = {
        day: [trading_status(day)]
        for day in (
            date(2025, 3, 5),
            date(2025, 3, 6),
            date(2025, 3, 7),
            date(2025, 3, 8),
        )
    }
    metrics = build_daily_metrics(
        match_disclosures_to_nxt_status([warning], statuses),
        statuses,
        date(2025, 3, 5),
        date(2025, 3, 8),
        status_periods=periods,
    )
    counts = dict(zip(metrics["일자"], metrics["투자경고종목"]))
    assert counts[date(2025, 3, 5)] == 0
    assert counts[date(2025, 3, 6)] == 1
    assert counts[date(2025, 3, 7)] == 1
    assert counts[date(2025, 3, 8)] == 0
