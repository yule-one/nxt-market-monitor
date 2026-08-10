from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from src.models import NxtChange


NXT_CHANGE_LIST_URL = (
    "https://www.nextrade.co.kr/menu/transactionStatusConclusion/menuList.do"
)


@dataclass(frozen=True)
class ChangeContext:
    display_reason: str
    source_title: str
    source_url: str


DATE_CONTEXTS = {
    date(2025, 3, 4): ChangeContext(
        "최초",
        "NXT 개장·최초 매매체결대상종목",
        "https://www.nextrade.co.kr/menu/marketInfo/view.do?"
        "scBbsKndCode=marketInfo&scNttCl=general&scNttNo=4",
    ),
    date(2025, 3, 17): ChangeContext(
        "2단계 확대",
        "NXT 매매체결대상종목 2단계 확대 안내",
        "https://www.nextrade.co.kr/menu/marketInfo/view.do?"
        "scBbsKndCode=marketInfo&scNttCl=general&scNttNo=6",
    ),
    date(2025, 3, 24): ChangeContext(
        "3단계 확대",
        "NXT 매매체결대상종목 3단계 확대 안내",
        "https://www.nextrade.co.kr/menu/marketInfo/view.do?"
        "scBbsKndCode=marketInfo&scNttCl=general&scNttNo=11",
    ),
    date(2025, 3, 31): ChangeContext(
        "4단계 확대",
        "NXT 매매체결대상종목 4단계 확대 안내",
        "https://www.nextrade.co.kr/menu/marketInfo/view.do?"
        "scBbsKndCode=marketInfo&scNttCl=general&scNttNo=12",
    ),
    date(2025, 7, 1): ChangeContext(
        "2025년 3분기 정기변경",
        "2025년 3분기 정규시장 매매체결대상종목 안내",
        "https://www.nextrade.co.kr/menu/marketInfo/view.do?"
        "scBbsKndCode=marketInfo&scNttCl=general&scNttNo=61",
    ),
    date(2025, 8, 20): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 축소",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=24",
    ),
    date(2025, 9, 1): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 축소",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=24",
    ),
    date(2025, 9, 22): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 축소",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=25",
    ),
    date(2025, 11, 5): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 축소",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=27",
    ),
    date(2026, 1, 2): ChangeContext(
        "2026년 1분기 정기변경",
        "2026년 1분기 정규시장 매매체결대상종목 변경 안내",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=32",
    ),
    date(2026, 2, 12): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 조정",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=34",
    ),
    date(2026, 4, 21): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 조정",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=35",
    ),
    date(2026, 6, 15): ChangeContext(
        "거래량한도관리",
        "한도 관리를 위한 매매체결대상종목 조정",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=39",
    ),
    date(2026, 7, 1): ChangeContext(
        "2026년 3분기 정기변경",
        "2026년 3분기 정규시장 매매체결대상종목 변경 안내",
        "https://www.nextrade.co.kr/menu/notice/view.do?"
        "scBbsKndCode=notice&scNttCl=general&scNttNo=40",
    ),
}


NORMALIZED_REASONS = {
    "관리종목지정": "관리종목 지정",
    "관리종목": "관리종목 지정",
    "관리종목.투자주의환기지정": "관리종목·투자주의환기 지정",
    "관리종목.투자주의환기": "관리종목·투자주의환기 지정",
    "투자주의환기지정": "투자주의환기 지정",
    "투자주의환기": "투자주의환기 지정",
    "시장관리": "거래량한도관리",
    "시장관리해소": "거래량한도관리 해제",
    "수시변경 요건 충족": "수시편입 요건 충족",
}


Q1_2026_NEW_INFERRED_CODES = {
    "0007C0",
    "032820",
    "060720",
    "125490",
    "380550",
    "478340",
}


def contextual_change_reason(change: NxtChange) -> str:
    display_reason = getattr(change, "display_reason", "")
    if display_reason:
        return display_reason
    if change.change_date == date(2026, 1, 2):
        if change.reason == "시장관리해소":
            return "2026년 1분기 정기변경(재편입)"
        if change.reason == "정기변경" and change.change_type == "편입":
            return "2026년 1분기 정기변경(신규편입)"
        if change.reason == "정기변경" and change.change_type == "편출":
            return "2026년 1분기 정기변경(편출)"
    context = DATE_CONTEXTS.get(change.change_date)
    if context and change.reason in {"특별변경", "정기변경", "시장관리"}:
        return context.display_reason
    return NORMALIZED_REASONS.get(change.reason, change.reason.strip() or "사유 미제공")


def source_context_for_change(change: NxtChange) -> ChangeContext:
    source_title = getattr(change, "source_title", "")
    source_url = getattr(change, "source_url", "")
    if source_title or source_url:
        return ChangeContext(
            contextual_change_reason(change),
            source_title or "NXT 종목 변동내역",
            source_url or NXT_CHANGE_LIST_URL,
        )
    context = DATE_CONTEXTS.get(change.change_date)
    if change.change_date == date(2026, 1, 2) or (
        context and change.reason in {"특별변경", "정기변경", "시장관리"}
    ):
        return ChangeContext(
            contextual_change_reason(change),
            context.source_title if context else "NXT 종목 변동내역",
            context.source_url if context else NXT_CHANGE_LIST_URL,
        )
    return ChangeContext(
        contextual_change_reason(change),
        "NXT 종목 변동내역",
        NXT_CHANGE_LIST_URL,
    )


def enrich_raw_change(change: NxtChange) -> NxtChange:
    context = DATE_CONTEXTS.get(change.change_date)
    return replace(
        change,
        display_reason=contextual_change_reason(change),
        source_title=context.source_title if context else "NXT 종목 변동내역",
        source_url=context.source_url if context else NXT_CHANGE_LIST_URL,
        basis="NXT 종목 변동 원본",
        is_inferred=False,
    )


def inferred_addition_context(change_date: date, stock_code: str) -> tuple[str, ChangeContext] | None:
    context = DATE_CONTEXTS.get(change_date)
    if change_date in {date(2025, 3, 24), date(2025, 3, 31)} and context:
        return "특별변경", context
    if change_date == date(2026, 1, 2) and context:
        reason = "정기변경" if stock_code in Q1_2026_NEW_INFERRED_CODES else "시장관리해소"
        display = (
            "2026년 1분기 정기변경(신규편입)"
            if reason == "정기변경"
            else "2026년 1분기 정기변경(재편입)"
        )
        return reason, ChangeContext(display, context.source_title, context.source_url)
    return None


def inferred_exclusion_context(
    change_date: date,
    stock_code: str,
) -> tuple[str, ChangeContext, str] | None:
    if change_date == date(2025, 7, 31) and stock_code == "049770":
        return (
            "상장폐지",
            ChangeContext(
                "상장폐지(포괄적 주식교환)",
                "KRX 공시: 동원F&B 상장폐지",
                "https://kind.krx.co.kr/external/2025/08/22/000022/"
                "20250822000044/10601.htm",
            ),
            "전일·당일 NXT 대상 명단 차이와 KRX 상장폐지 공시",
        )
    return None
