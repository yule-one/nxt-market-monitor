from __future__ import annotations

from collections import OrderedDict
from datetime import date


NXT_LAUNCH_DATE = date(2025, 3, 4)

# KIND 상세검색의 시장조치(disclosureType02) 코드입니다.
CATEGORY_CODES: OrderedDict[str, tuple[str, ...]] = OrderedDict(
    {
        # 0357/0345는 KIND 화면 라벨과 실제 조회 결과가 일치하지 않아 제외하고,
        # 일반 주권 매매거래정지/해제 코드(0311)를 기준으로 사용합니다.
        "거래정지/재개": ("0311",),
        "관리종목": ("0350",),
        "투자주의환기종목": ("0356",),
        "투자경고종목": ("0342",),
        "투자위험종목": ("0343",),
        "단기과열종목": ("0358",),
        "기타시장안내": ("0305",),
        "상장폐지": ("0328",),
    }
)

STATE_CATEGORIES = tuple(
    category
    for category in CATEGORY_CODES
    if category not in {"기타시장안내", "상장폐지"}
)

MARKET_LABELS = {
    "유가증권": "KOSPI",
    "코스닥": "KOSDAQ",
    "코넥스": "KONEX",
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
}

KIND_SEARCH_URL = "https://kind.krx.co.kr/disclosure/details.do"
KIND_SEARCH_MAIN_URL = (
    "https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain"
)
KIND_VIEWER_URL = (
    "https://kind.krx.co.kr/common/disclsviewer.do?method=search"
    "&acptno={report_no}&docno=&viewerhost=&viewerport="
)
KIND_INVESTMENT_STATUS_URL = (
    "https://kind.krx.co.kr/investwarn/investattentwarnrisky.do"
)
KIND_INVESTMENT_STATUS_MAIN_URL = (
    "https://kind.krx.co.kr/investwarn/investattentwarnrisky.do"
    "?method=investattentwarnriskyMain"
)

NXT_CHANGE_URL = "https://nextrade.co.kr/trdisuChg/trdisuChgList.do"
NXT_CHANGE_PAGE_URL = (
    "https://nextrade.co.kr/menu/transactionStatusConclusion/menuList.do"
)
NXT_TRADING_STATUS_URL = (
    "https://nextrade.co.kr/brdinfoTime/brdinfoTimeList.do"
)
NXT_TRADING_STATUS_PAGE_URL = (
    "https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do"
)

# NXT 변동 원본이 편출·편입으로 표시하더라도 종목 선정의 변경이 아니라
# 지정 기간의 거래제한 시작·해제로 분류해야 하는 사유입니다.
TRADING_RESTRICTION_START_KEYWORDS = (
    "투자경고",
    "투자위험",
    "단기과열",
    "거래정지",
    "시가기준가",
)

TRADING_RESTRICTION_END_KEYWORDS = (
    "투자경고/위험 해제",
    "투자경고 해제",
    "투자위험 해제",
    "단기과열 해제",
    "거래정지 해제",
    "시가기준가 해제",
)

# 기존 import 호환용 별칭입니다.
TEMPORARY_EXCLUSION_KEYWORDS = TRADING_RESTRICTION_START_KEYWORDS
