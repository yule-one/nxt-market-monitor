from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.cache import ResponseCache
from src.config import (
    CATEGORY_CODES,
    KIND_SEARCH_MAIN_URL,
    KIND_SEARCH_URL,
    KIND_INVESTMENT_STATUS_MAIN_URL,
    KIND_INVESTMENT_STATUS_URL,
    KIND_VIEWER_URL,
    MARKET_LABELS,
)
from src.http import ResilientSession
from src.models import Disclosure, StatusPeriod


_DISCLOSURE_GROUPS = ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "13", "14", "20")


def _date_chunks(start_date: date, end_date: date, days: int = 90) -> Iterable[tuple[date, date]]:
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=days - 1), end_date)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def normalize_kind_code(internal_code: str) -> str:
    digits = "".join(character for character in internal_code if character.isdigit())
    if len(digits) == 5:
        # KIND 내부 issue code는 NXT 대상 공통주에서 마지막 0이 생략됩니다.
        return f"{digits}0"
    if digits:
        return digits[-6:].zfill(6)
    return ""


class KindClient:
    def __init__(self, cache: ResponseCache | None = None) -> None:
        self.cache = cache or ResponseCache()
        self.ssl_fallback_used = False

    def fetch_disclosures(
        self,
        start_date: date,
        end_date: date,
        categories: Sequence[str] | None = None,
        *,
        force_refresh: bool = False,
        max_workers: int = 4,
    ) -> list[Disclosure]:
        selected = list(categories or CATEGORY_CODES.keys())
        unknown = [category for category in selected if category not in CATEGORY_CODES]
        if unknown:
            raise ValueError(f"알 수 없는 KIND 분류: {', '.join(unknown)}")
        if end_date < start_date:
            return []

        disclosures: list[Disclosure] = []
        worker_count = max(1, min(max_workers, len(selected)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self._fetch_category,
                    category,
                    start_date,
                    end_date,
                    force_refresh,
                ): category
                for category in selected
            }
            for future in as_completed(futures):
                category_disclosures, ssl_fallback = future.result()
                self.ssl_fallback_used = self.ssl_fallback_used or ssl_fallback
                disclosures.extend(category_disclosures)

        unique: dict[tuple[str, str], Disclosure] = {}
        for item in disclosures:
            unique[(item.category, item.report_no)] = item
        return sorted(
            unique.values(),
            key=lambda item: (item.disclosed_at, item.category, item.stock_code),
            reverse=True,
        )

    def _fetch_category(
        self,
        category: str,
        start_date: date,
        end_date: date,
        force_refresh: bool,
    ) -> tuple[list[Disclosure], bool]:
        session = ResilientSession(referer=KIND_SEARCH_MAIN_URL)
        session.get(KIND_SEARCH_MAIN_URL)
        session.session.headers["X-Requested-With"] = "XMLHttpRequest"
        result: list[Disclosure] = []
        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            result.extend(
                self._fetch_chunk(
                    session,
                    category,
                    chunk_start,
                    chunk_end,
                    force_refresh=force_refresh,
                )
            )
        return result, session.ssl_fallback_used

    def _fetch_chunk(
        self,
        session: ResilientSession,
        category: str,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool,
    ) -> list[Disclosure]:
        cache_key = f"{category}:{start_date.isoformat()}:{end_date.isoformat()}"
        max_age = timedelta(minutes=15) if end_date >= date.today() else timedelta(hours=12)
        cached = None if force_refresh else self.cache.get("kind_disclosures", cache_key, max_age)
        if cached is not None:
            return [Disclosure.from_dict(item) for item in cached]

        codes = CATEGORY_CODES[category]
        form = self._build_form(start_date, end_date, codes, page=1)
        response = session.post(KIND_SEARCH_URL, data=form)
        parsed, total_pages = self._parse_results(response.text, category)
        for page in range(2, total_pages + 1):
            form["pageIndex"] = str(page)
            page_response = session.post(KIND_SEARCH_URL, data=form)
            page_items, _ = self._parse_results(page_response.text, category)
            parsed.extend(page_items)

        self.cache.set("kind_disclosures", cache_key, [item.to_dict() for item in parsed])
        return parsed

    @staticmethod
    def _build_form(
        start_date: date,
        end_date: date,
        codes: Sequence[str],
        *,
        page: int,
    ) -> dict[str, str]:
        code_value = "|".join(codes) + "|"
        form = {
            "method": "searchDetailsSub",
            "currentPageSize": "100",
            "pageIndex": str(page),
            "orderMode": "",
            "orderStat": "",
            "forward": "details_sub",
            "searchCodeType": "",
            "repIsuSrtCd": "",
            "allRepIsuSrtCd": "",
            "oldSearchCorpName": "",
            "disclosureType": "02",
            "disTypevalue": codes[0],
            "reportNm": "",
            "reportCd": "",
            "searchCorpName": "",
            "industry": "",
            "marketType": "",
            "settlementMonth": "",
            "securities": "",
            "submitOblgNm": "",
            "enterprise": "",
            "fromDate": start_date.isoformat(),
            "toDate": end_date.isoformat(),
            "reportNmTemp": "",
            "reportNmPop": "",
            "lastReport": "",
            "bfrDsclsType": "",
        }
        for group in _DISCLOSURE_GROUPS:
            selected = code_value if group == "02" else ""
            form[f"disclosureType{group}"] = selected
            form[f"pDisclosureType{group}"] = selected
        return form

    @staticmethod
    def _parse_results(html: str, category: str) -> tuple[list[Disclosure], int]:
        if "페이지 오류" in html:
            raise RuntimeError("KIND 상세검색이 오류 페이지를 반환했습니다.")
        soup = BeautifulSoup(html, "html.parser")
        results: list[Disclosure] = []
        for row in soup.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 5:
                continue
            timestamp_text = cells[1].get_text(" ", strip=True)
            try:
                disclosed_at = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            company_link = cells[2].find("a", onclick=re.compile(r"companysummary_open"))
            report_link = cells[3].find("a", onclick=re.compile(r"openDisclsViewer"))
            if company_link is None or report_link is None:
                continue
            company_match = re.search(r"companysummary_open\('([^']+)'", company_link.get("onclick", ""))
            report_match = re.search(r"openDisclsViewer\('([^']+)'", report_link.get("onclick", ""))
            if company_match is None or report_match is None:
                continue

            internal_code = company_match.group(1)
            report_no = report_match.group(1)
            market_image = cells[2].find("img")
            market_raw = market_image.get("alt", "") if market_image else ""
            market = MARKET_LABELS.get(market_raw, market_raw)
            title = report_link.get("title") or report_link.get_text(" ", strip=True)
            results.append(
                Disclosure(
                    disclosed_at=disclosed_at,
                    stock_code=normalize_kind_code(internal_code),
                    stock_name=company_link.get("title") or company_link.get_text(" ", strip=True),
                    market=market,
                    category=category,
                    title=str(title).strip(),
                    submitter=cells[4].get_text(" ", strip=True),
                    report_no=report_no,
                    viewer_url=KIND_VIEWER_URL.format(report_no=report_no),
                    internal_code=internal_code,
                )
            )

        text = soup.get_text(" ", strip=True)
        page_match = re.search(r"전체\s*[\d,]+\s*건\s*:\s*\d+\s*/\s*(\d+)", text)
        total_pages = int(page_match.group(1)) if page_match else 1
        return results, total_pages

    def fetch_document_text(self, report_no: str, *, force_refresh: bool = False) -> str:
        cached = None if force_refresh else self.cache.get(
            "kind_documents", report_no, timedelta(days=7)
        )
        if cached is not None:
            return str(cached.get("text") or "")

        session = ResilientSession(referer=KIND_SEARCH_MAIN_URL)
        viewer = session.get(KIND_VIEWER_URL.format(report_no=report_no))
        viewer_soup = BeautifulSoup(viewer.text, "html.parser")
        option = next(
            (
                item
                for item in viewer_soup.select("#mainDoc option")
                if (item.get("value") or "").strip()
            ),
            None,
        )
        if option is None:
            return ""
        doc_no = str(option.get("value")).split("|", 1)[0]
        contents = session.get(
            f"https://kind.krx.co.kr/common/disclsviewer.do?method=searchContents&docNo={doc_no}"
        )
        path_match = re.search(r"parent\.setPath\('[^']*','([^']+)'", contents.text)
        if path_match is None:
            return ""
        document_url = urljoin("https://kind.krx.co.kr", path_match.group(1))
        document = session.get(document_url)
        decoded = document.content.decode("euc-kr", errors="replace")
        text = BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
        self.ssl_fallback_used = self.ssl_fallback_used or session.ssl_fallback_used
        self.cache.set("kind_documents", report_no, {"text": text, "url": document_url})
        return text

    def fetch_investment_status_periods(
        self,
        start_date: date,
        end_date: date,
        categories: Sequence[str] = ("투자경고종목", "투자위험종목"),
        *,
        force_refresh: bool = False,
    ) -> list[StatusPeriod]:
        settings = {
            "투자경고종목": ("2", "invstwarnisu_sub"),
            "투자위험종목": ("3", "invstriskisu_sub"),
        }
        unknown = [item for item in categories if item not in settings]
        if unknown:
            raise ValueError(f"지원하지 않는 투자상태 분류: {', '.join(unknown)}")
        result: list[StatusPeriod] = []
        session: ResilientSession | None = None
        for category in categories:
            menu_index, forward = settings[category]
            cache_key = f"{category}:{start_date.isoformat()}:{end_date.isoformat()}"
            max_age = timedelta(minutes=15) if end_date >= date.today() else timedelta(hours=12)
            cached = None if force_refresh else self.cache.get(
                "kind_status_periods", cache_key, max_age
            )
            if cached is not None:
                result.extend(StatusPeriod.from_dict(item) for item in cached)
                continue

            if session is None:
                session = ResilientSession(referer=KIND_INVESTMENT_STATUS_MAIN_URL)
                session.get(KIND_INVESTMENT_STATUS_MAIN_URL)
                session.session.headers["X-Requested-With"] = "XMLHttpRequest"
            form = {
                "method": "investattentwarnriskySub",
                "currentPageSize": "100",
                "pageIndex": "1",
                "orderMode": "",
                "orderStat": "",
                "searchCodeType": "",
                "searchCorpName": "",
                "repIsuSrtCd": "",
                "menuIndex": menu_index,
                "forward": forward,
                "searchFromDate": end_date.isoformat(),
                "marketType": "",
                "searchCorpNameTmp": "",
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "etsIsuSrtCd": "",
            }
            response = session.post(KIND_INVESTMENT_STATUS_URL, data=form)
            periods, total_pages = self._parse_status_periods(response.text, category)
            for page in range(2, total_pages + 1):
                form["pageIndex"] = str(page)
                page_response = session.post(KIND_INVESTMENT_STATUS_URL, data=form)
                page_periods, _ = self._parse_status_periods(page_response.text, category)
                periods.extend(page_periods)
            self.cache.set(
                "kind_status_periods",
                cache_key,
                [item.to_dict() for item in periods],
            )
            result.extend(periods)
        if session is not None:
            self.ssl_fallback_used = self.ssl_fallback_used or session.ssl_fallback_used
        return result

    @staticmethod
    def _parse_status_periods(html: str, category: str) -> tuple[list[StatusPeriod], int]:
        if "페이지 오류" in html:
            raise RuntimeError("KIND 투자경고/위험 현황이 오류 페이지를 반환했습니다.")
        soup = BeautifulSoup(html, "html.parser")
        result: list[StatusPeriod] = []
        for row in soup.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 5:
                continue
            company_link = cells[1].find("a", onclick=re.compile(r"companysummary_open"))
            if company_link is None:
                continue
            company_match = re.search(
                r"companysummary_open\('([^']+)'", company_link.get("onclick", "")
            )
            if company_match is None:
                continue
            try:
                published_date = datetime.strptime(
                    cells[2].get_text(" ", strip=True), "%Y-%m-%d"
                ).date()
                status_start = datetime.strptime(
                    cells[3].get_text(" ", strip=True), "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            end_text = cells[4].get_text(" ", strip=True)
            try:
                status_end = (
                    datetime.strptime(end_text, "%Y-%m-%d").date()
                    if end_text and end_text != "-"
                    else None
                )
            except ValueError:
                status_end = None
            market_image = cells[1].find("img")
            market_raw = market_image.get("alt", "") if market_image else ""
            stock_name = company_link.get("title") or company_link.get_text(" ", strip=True)
            if re.search(r"(?:우|우선주|\d우[BC]?)$", str(stock_name).strip()):
                continue
            result.append(
                StatusPeriod(
                    category=category,
                    stock_code=normalize_kind_code(company_match.group(1)),
                    stock_name=str(stock_name).strip(),
                    market=MARKET_LABELS.get(market_raw, market_raw),
                    published_date=published_date,
                    start_date=status_start,
                    end_date=status_end,
                )
            )
        text = soup.get_text(" ", strip=True)
        page_match = re.search(r"전체\s*[\d,]+\s*건\s*:\s*\d+\s*/\s*(\d+)", text)
        return result, int(page_match.group(1)) if page_match else 1
