from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Sequence

import pandas as pd

from src.config import STATE_CATEGORIES, TRADING_RESTRICTION_START_KEYWORDS
from src.models import (
    Disclosure,
    NxtChange,
    NxtStockState,
    NxtTradingStatus,
    StatusPeriod,
)
from src.nxt_eligibility import classify_nxt_change


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", value or "").lower()


def is_temporary_exclusion(change: NxtChange) -> bool:
    return change.change_type == "편출" and any(
        keyword in change.reason for keyword in TRADING_RESTRICTION_START_KEYWORDS
    )


def is_preferred_share_notice(disclosure: Disclosure) -> bool:
    """회사 대표코드로 조회된 우선주 시장조치가 공통주와 잘못 결합되는 것을 막습니다."""

    trailing_groups = re.findall(r"\(([^()]*)\)", disclosure.title)
    if not trailing_groups:
        return False
    target = trailing_groups[-1].strip()
    return bool(re.search(r"(?:우|우선주|\d우[BC]?)$", target))


def build_nxt_state_as_of(
    changes: Sequence[NxtChange],
    as_of: date,
) -> dict[str, NxtStockState]:
    state: dict[str, NxtStockState] = {}
    for change in sorted(changes, key=lambda item: (item.change_date, item.registered_at)):
        if change.change_date > as_of:
            break
        state[change.stock_code] = NxtStockState(
            stock_code=change.stock_code,
            stock_name=change.stock_name,
            market=change.market,
            is_tradable=change.change_type == "편입",
            is_temporary_exclusion=is_temporary_exclusion(change),
            last_change_date=change.change_date,
            last_change_type=change.change_type,
            last_reason=change.reason,
        )
    return state


def build_nxt_states_by_date(
    changes: Sequence[NxtChange],
    dates: Iterable[date],
) -> dict[date, dict[str, NxtStockState]]:
    requested = sorted(set(dates))
    if not requested:
        return {}
    ordered_changes = sorted(changes, key=lambda item: (item.change_date, item.registered_at))
    cursor = 0
    current: dict[str, NxtStockState] = {}
    result: dict[date, dict[str, NxtStockState]] = {}
    for current_date in requested:
        while cursor < len(ordered_changes) and ordered_changes[cursor].change_date <= current_date:
            change = ordered_changes[cursor]
            current[change.stock_code] = NxtStockState(
                stock_code=change.stock_code,
                stock_name=change.stock_name,
                market=change.market,
                is_tradable=change.change_type == "편입",
                is_temporary_exclusion=is_temporary_exclusion(change),
                last_change_date=change.change_date,
                last_change_type=change.change_type,
                last_reason=change.reason,
            )
            cursor += 1
        result[current_date] = dict(current)
    return result


def state_is_nxt_related(state: NxtStockState | None) -> bool:
    """조회일 현재 NXT 매매체결 대상인 상태만 반환합니다."""

    return bool(state and (state.is_tradable or state.is_temporary_exclusion))


def _nxt_name_index(changes: Sequence[NxtChange]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for change in changes:
        index[(normalize_name(change.stock_name), change.market)] = change.stock_code
    return index


def match_disclosures_to_nxt(
    disclosures: Sequence[Disclosure],
    changes: Sequence[NxtChange],
) -> list[dict]:
    disclosure_dates = {item.disclosure_date for item in disclosures}
    lookup_dates = disclosure_dates | {day - timedelta(days=1) for day in disclosure_dates}
    states_by_date = build_nxt_states_by_date(changes, lookup_dates)
    name_index = _nxt_name_index(changes)
    result: list[dict] = []

    for disclosure in disclosures:
        code = disclosure.stock_code
        if code not in states_by_date.get(disclosure.disclosure_date, {}):
            code = name_index.get((normalize_name(disclosure.stock_name), disclosure.market), code)

        today_state = states_by_date.get(disclosure.disclosure_date, {}).get(code)
        previous_state = states_by_date.get(
            disclosure.disclosure_date - timedelta(days=1), {}
        ).get(code)
        related = state_is_nxt_related(today_state) or state_is_nxt_related(previous_state)
        if is_preferred_share_notice(disclosure):
            related = False

        if today_state and today_state.is_tradable:
            relation = "매매가능"
        elif today_state and today_state.is_temporary_exclusion:
            relation = today_state.membership_label
        elif previous_state and previous_state.is_tradable:
            relation = "직전일 매매가능"
        else:
            relation = "NXT 비대상"

        result.append(
            {
                "disclosure": disclosure,
                "matched_stock_code": code,
                "is_nxt_related": related,
                "nxt_relation": relation,
            }
        )
    return result


def match_disclosures_to_nxt_status(
    disclosures: Sequence[Disclosure],
    statuses_by_date: Mapping[date, Sequence[NxtTradingStatus]],
) -> list[dict]:
    """NXT 날짜별 공식 거래현황을 기준으로 공시의 NXT 포함 여부를 판정합니다."""

    code_indexes: dict[date, dict[str, NxtTradingStatus]] = {}
    name_indexes: dict[date, dict[tuple[str, str], NxtTradingStatus]] = {}
    for status_date, statuses in statuses_by_date.items():
        code_indexes[status_date] = {item.stock_code: item for item in statuses}
        name_indexes[status_date] = {
            (normalize_name(item.stock_name), item.market): item for item in statuses
        }

    result: list[dict] = []
    for disclosure in disclosures:
        status = code_indexes.get(disclosure.disclosure_date, {}).get(
            disclosure.stock_code
        )
        if status is None:
            status = name_indexes.get(disclosure.disclosure_date, {}).get(
                (normalize_name(disclosure.stock_name), disclosure.market)
            )
        related = status is not None and not is_preferred_share_notice(disclosure)
        result.append(
            {
                "disclosure": disclosure,
                "matched_stock_code": (
                    status.stock_code if status is not None else disclosure.stock_code
                ),
                "is_nxt_related": related,
                "nxt_tradable_market": (
                    status.tradable_market if status is not None else ""
                ),
                "nxt_unavailable_reason": (
                    status.unavailable_reason if status is not None else ""
                ),
            }
        )
    return result


_KOREAN_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
)


def _date_after_label(text: str, labels: Sequence[str]) -> date | None:
    for label in labels:
        match = re.search(
            re.escape(label) + r".{0,80}?" + _KOREAN_DATE_PATTERN.pattern,
            text,
        )
        if match:
            try:
                return date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
    return None


def extract_state_transitions(
    disclosure: Disclosure,
    document_text: str = "",
) -> list[tuple[date, str, str, bool]]:
    """(효력일, 분류, 종목코드, 활성여부) 형태의 상태 변동을 반환합니다."""

    if disclosure.category not in STATE_CATEGORIES:
        return []
    title = re.sub(r"\s+", "", disclosure.title)
    if "예고" in title or "우려" in title:
        return []

    code = disclosure.stock_code
    category = disclosure.category
    if category == "거래정지/재개":
        if document_text:
            start = _date_after_label(
                document_text,
                ("매매거래정지일", "매매거래정지 시작일", "거래정지일"),
            )
            end = _date_after_label(
                document_text,
                ("매매거래정지해제일", "매매거래정지 해제일", "거래재개일"),
            )
            transitions: list[tuple[date, str, str, bool]] = []
            if start:
                transitions.append((start, category, code, True))
            if end:
                transitions.append((end, category, code, False))
            if transitions:
                return transitions
        if re.search(r"정지(?:및|및정지)?해제", title) and not title.startswith("주권매매거래정지해제"):
            # 세부문서 없이 두 효력일을 안전하게 구분할 수 없습니다.
            return []
        if "해제" in title or "재개" in title:
            return [(disclosure.disclosure_date, category, code, False)]
        if "정지" in title:
            return [(disclosure.disclosure_date, category, code, True)]
        return []

    if "해제" in title or "취소" in title:
        return [(disclosure.disclosure_date, category, code, False)]
    if "지정" in title or "연장" in title:
        return [(disclosure.disclosure_date, category, code, True)]
    return []


def disclosures_to_frame(matched_rows: Sequence[dict]) -> pd.DataFrame:
    records = []
    for row in matched_rows:
        disclosure: Disclosure = row["disclosure"]
        records.append(
            {
                "공시일시": disclosure.disclosed_at,
                "구분": disclosure.category,
                "종목코드": row["matched_stock_code"] or disclosure.stock_code,
                "종목명": disclosure.stock_name,
                "상장시장": disclosure.market,
                "공시제목": disclosure.title,
                "제출인": disclosure.submitter,
                "KIND 원문": disclosure.viewer_url,
                "접수번호": disclosure.report_no,
                "NXT 거래가능시장": row.get("nxt_tradable_market", ""),
                "NXT 거래불가사유": row.get("nxt_unavailable_reason", ""),
            }
        )
    return pd.DataFrame.from_records(records)


def nxt_trading_status_to_frame(
    statuses: Sequence[NxtTradingStatus],
) -> pd.DataFrame:
    columns = ["종목코드", "종목명", "상장시장", "거래가능시장", "거래불가사유"]
    records = [
        {
            "종목코드": item.stock_code,
            "종목명": item.stock_name,
            "상장시장": item.market,
            "거래가능시장": item.tradable_market,
            "거래불가사유": item.unavailable_reason,
        }
        for item in statuses
    ]
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame.from_records(records).sort_values(
        ["거래가능시장", "상장시장", "종목코드"], ignore_index=True
    )


def nxt_changes_to_frame(changes: Sequence[NxtChange]) -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [
            {
                "일자": item.change_date,
                "종목코드": item.stock_code,
                "종목명": item.stock_name,
                "상장시장": item.market,
                "변동내역": classify_nxt_change(item),
                "원본변동내역": item.change_type,
                "변동사유": item.reason,
            }
            for item in changes
        ]
    )


def build_daily_nxt_metrics(
    changes: Sequence[NxtChange],
    statuses_by_date: Mapping[date, Sequence[NxtTradingStatus]],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """NXT 편입·편출 원천만으로 일별 상태와 당일 변동을 집계합니다."""

    if end_date < start_date:
        return pd.DataFrame()
    all_dates = sorted(
        current_date
        for current_date, statuses in statuses_by_date.items()
        if start_date <= current_date <= end_date and statuses
    )
    changes_by_date: dict[date, list[NxtChange]] = defaultdict(list)
    for change in changes:
        changes_by_date[change.change_date].append(change)

    records = []
    for current_date in all_dates:
        statuses = statuses_by_date.get(current_date, [])
        daily_changes = changes_by_date.get(current_date, [])
        records.append(
            {
                "일자": current_date,
                "NXT 종목수": len({item.stock_code for item in statuses}),
                "당일 편입 종목수": len(
                    {
                        item.stock_code
                        for item in daily_changes
                        if classify_nxt_change(item) == "편입"
                    }
                ),
                "당일 편출 종목수": len(
                    {
                        item.stock_code
                        for item in daily_changes
                        if classify_nxt_change(item) == "편출"
                    }
                ),
            }
        )
    return pd.DataFrame.from_records(records)


def build_daily_nxt_metrics_from_counts(
    changes: Sequence[NxtChange],
    stock_counts_by_date: Mapping[date, int],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """저장된 일별 NXT 종목수와 변동내역만으로 화면 집계를 만듭니다."""

    if end_date < start_date:
        return pd.DataFrame()
    changes_by_date: dict[date, list[NxtChange]] = defaultdict(list)
    for change in changes:
        if start_date <= change.change_date <= end_date:
            changes_by_date[change.change_date].append(change)

    records = []
    for current_date in sorted(stock_counts_by_date):
        if not start_date <= current_date <= end_date:
            continue
        daily_changes = changes_by_date.get(current_date, [])
        records.append(
            {
                "일자": current_date,
                "NXT 종목수": int(stock_counts_by_date[current_date]),
                "당일 편입 종목수": len(
                    {
                        item.stock_code
                        for item in daily_changes
                        if classify_nxt_change(item) == "편입"
                    }
                ),
                "당일 편출 종목수": len(
                    {
                        item.stock_code
                        for item in daily_changes
                        if classify_nxt_change(item) == "편출"
                    }
                ),
            }
        )
    return pd.DataFrame.from_records(records)


def build_daily_metrics(
    matched_disclosures: Sequence[dict],
    statuses_by_date: Mapping[date, Sequence[NxtTradingStatus]],
    start_date: date,
    end_date: date,
    status_periods: Sequence[StatusPeriod] | None = None,
) -> pd.DataFrame:
    if end_date < start_date:
        return pd.DataFrame()
    has_status_period_source = status_periods is not None
    status_periods = status_periods or []
    disclosure_counts: dict[tuple[date, str], set[str]] = defaultdict(set)
    for row in matched_disclosures:
        disclosure: Disclosure = row["disclosure"]
        matched_code = row["matched_stock_code"] or disclosure.stock_code
        if row["is_nxt_related"]:
            disclosure_counts[(disclosure.disclosure_date, disclosure.category)].add(
                matched_code
            )

    output_dates = sorted(
        current_date
        for current_date, statuses in statuses_by_date.items()
        if start_date <= current_date <= end_date and statuses
    )

    records = []
    for current_date in output_dates:
        status_rows = statuses_by_date.get(current_date, [])
        status_by_code = {item.stock_code: item for item in status_rows}
        related_codes = set(status_by_code)
        record = {
            "일자": current_date,
            "NXT 종목수": len(related_codes),
            "NXT 관련 종목수": len(related_codes),
        }
        for category in STATE_CATEGORIES:
            record[category] = 0
            record[f"{category} 당일공시"] = len(
                disclosure_counts.get((current_date, category), set())
            )

        # 날짜별 NXT 원본의 거래불가사유를 상태 수의 직접 근거로 사용합니다.
        for category, keyword in (
            ("거래정지/재개", "거래정지"),
            ("관리종목", "관리종목"),
            ("투자주의환기종목", "투자주의환기"),
            ("단기과열종목", "단기과열"),
        ):
            record[category] = sum(
                keyword in item.unavailable_reason for item in status_rows
            )
        for category in ("투자경고종목", "투자위험종목"):
            active_period_codes = {
                period.stock_code
                for period in status_periods
                if period.category == category
                and period.start_date <= current_date
                and (period.end_date is None or current_date < period.end_date)
            }
            if has_status_period_source:
                record[category] = len(active_period_codes & related_codes)
        records.append(record)
    return pd.DataFrame.from_records(records)


def collect_ambiguous_report_numbers(
    matched_rows: Sequence[dict],
    earliest_date: date | None = None,
    eligible_codes: set[str] | None = None,
) -> list[str]:
    eligible_codes = eligible_codes or set()
    result = []
    for row in matched_rows:
        disclosure: Disclosure = row["disclosure"]
        normalized = re.sub(r"\s+", "", disclosure.title)
        matched_code = row["matched_stock_code"] or disclosure.stock_code
        if (
            (row["is_nxt_related"] or matched_code in eligible_codes)
            and disclosure.category == "거래정지/재개"
            and (earliest_date is None or disclosure.disclosure_date >= earliest_date)
            and "정지" in normalized
            and "해제" in normalized
        ):
            result.append(disclosure.report_no)
    return sorted(set(result))
