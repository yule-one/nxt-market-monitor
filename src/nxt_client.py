from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import re
from typing import Iterable

from src.cache import ResponseCache
from src.config import NXT_CHANGE_URL, NXT_TRADING_STATUS_URL
from src.http import ResilientSession
from src.models import NxtChange, NxtTradingStatus
from src.nxt_price_limits import calculate_stock_price_limits


def _date_chunks(start_date: date, end_date: date, days: int = 90) -> Iterable[tuple[date, date]]:
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=days - 1), end_date)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def normalize_stock_code(raw: str | None) -> str:
    value = re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()
    if len(value) == 7 and value.startswith("A"):
        value = value[1:]
    return value[-6:].zfill(6) if value else ""


def _integer(raw: object) -> int:
    value = str(raw or "0").replace(",", "").strip()
    try:
        return int(float(value or "0"))
    except ValueError:
        return 0


def _decimal(raw: object) -> float:
    value = str(raw or "0").replace(",", "").strip()
    try:
        return float(value or "0")
    except ValueError:
        return 0.0


class NxtClient:
    def __init__(self, cache: ResponseCache | None = None) -> None:
        self.cache = cache or ResponseCache()
        self.ssl_fallback_used = False

    def fetch_changes(
        self,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool = False,
    ) -> list[NxtChange]:
        if end_date < start_date:
            return []

        changes: list[NxtChange] = []
        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            changes.extend(
                self._fetch_chunk(
                    chunk_start,
                    chunk_end,
                    force_refresh=force_refresh,
                )
            )

        unique: dict[tuple[date, str, str, str], NxtChange] = {}
        for change in changes:
            key = (
                change.change_date,
                change.stock_code,
                change.change_type,
                change.reason,
            )
            unique[key] = change
        return sorted(
            unique.values(),
            key=lambda item: (item.change_date, item.registered_at, item.stock_code),
        )

    def fetch_trading_status(
        self,
        status_date: date,
        *,
        force_refresh: bool = False,
    ) -> list[NxtTradingStatus]:
        cache_key = status_date.strftime("%Y%m%d")
        # 확정된 과거 일별 데이터는 SQLite에 영구 보관해 재호출하지 않습니다.
        # 당일 값만 장중 변경을 반영하기 위해 짧은 만료시간을 둡니다.
        max_age = timedelta(minutes=15) if status_date >= date.today() else None
        cached = (
            None
            if force_refresh
            else self.cache.get("nxt_trading_status_v4", cache_key, max_age)
        )
        if cached is not None:
            return [NxtTradingStatus.from_dict(item) for item in cached]

        session = ResilientSession(referer="https://www.nextrade.co.kr/")
        payload = {
            "pageIndex": "1",
            "pageUnit": "5000",
            "scAggDd": cache_key,
            "scMktId": "",
            "searchKeyword": "",
        }
        response = session.post(
            NXT_TRADING_STATUS_URL,
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.ssl_fallback_used = self.ssl_fallback_used or session.ssl_fallback_used
        raw = response.json()
        pages = max(1, int(raw.get("total") or 1))
        rows = list(raw.get("brdinfoTimeList") or [])
        for page in range(2, pages + 1):
            payload["pageIndex"] = str(page)
            page_response = session.post(
                NXT_TRADING_STATUS_URL,
                data=payload,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            rows.extend(page_response.json().get("brdinfoTimeList") or [])
        self.ssl_fallback_used = self.ssl_fallback_used or session.ssl_fallback_used

        parsed = [self._parse_trading_status(item) for item in rows]
        unique = {
            item.stock_code: item for item in parsed if item is not None
        }
        result = sorted(unique.values(), key=lambda item: (item.market, item.stock_code))
        self.cache.set(
            "nxt_trading_status_v4",
            cache_key,
            [item.to_dict() for item in result],
        )
        return result

    def fetch_trading_status_range(
        self,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool = False,
        max_workers: int = 8,
    ) -> dict[date, list[NxtTradingStatus]]:
        if end_date < start_date:
            return {}
        dates: list[date] = []
        cursor = start_date
        while cursor <= end_date:
            dates.append(cursor)
            cursor += timedelta(days=1)
        result: dict[date, list[NxtTradingStatus]] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(dates))) as executor:
            futures = {
                executor.submit(
                    self.fetch_trading_status,
                    current_date,
                    force_refresh=force_refresh,
                ): current_date
                for current_date in dates
            }
            for future in as_completed(futures):
                current_date = futures[future]
                rows = future.result()
                if rows:
                    result[current_date] = rows
        return dict(sorted(result.items()))

    def _fetch_chunk(
        self,
        start_date: date,
        end_date: date,
        *,
        force_refresh: bool,
    ) -> list[NxtChange]:
        cache_key = f"{start_date:%Y%m%d}:{end_date:%Y%m%d}"
        # 전 구간이 과거이면 DB 값을 계속 재사용합니다.
        max_age = timedelta(minutes=15) if end_date >= date.today() else None
        cached = (
            None
            if force_refresh
            else self.cache.get("nxt_changes_v2", cache_key, max_age)
        )
        if cached is not None:
            return [NxtChange.from_dict(item) for item in cached]

        session = ResilientSession(referer="https://nextrade.co.kr/")
        payload = {
            "pageIndex": "1",
            "pageUnit": "5000",
            "scBeginDe": start_date.strftime("%Y%m%d"),
            "scEndDe": end_date.strftime("%Y%m%d"),
            "scMktId": "",
            "searchKeyword": "",
        }
        response = session.post(
            NXT_CHANGE_URL,
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.ssl_fallback_used = self.ssl_fallback_used or session.ssl_fallback_used
        raw = response.json()
        pages = max(1, int(raw.get("total") or 1))
        rows = list(raw.get("trdisuChgList") or [])
        for page in range(2, pages + 1):
            payload["pageIndex"] = str(page)
            page_response = session.post(
                NXT_CHANGE_URL,
                data=payload,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            rows.extend(page_response.json().get("trdisuChgList") or [])

        parsed = [self._parse_change(item) for item in rows]
        parsed = [item for item in parsed if item is not None]
        self.cache.set("nxt_changes_v2", cache_key, [item.to_dict() for item in parsed])
        return parsed

    @staticmethod
    def _parse_change(raw: dict) -> NxtChange | None:
        raw_date = str(raw.get("aggDd") or "")
        stock_code = normalize_stock_code(raw.get("isuSrdCd"))
        if len(raw_date) != 8 or not stock_code:
            return None
        try:
            change_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            return None
        return NxtChange(
            change_date=change_date,
            stock_code=stock_code,
            stock_name=str(raw.get("isuAbwdNm") or "").strip(),
            market=str(raw.get("mktNm") or "").strip(),
            change_type=str(raw.get("addExlCd") or "").strip(),
            reason=str(raw.get("rsnCd") or "").strip(),
            isin=str(raw.get("isuCd") or "").strip(),
            registered_at=int(raw.get("registDt") or 0),
        )

    @staticmethod
    def _parse_trading_status(raw: dict) -> NxtTradingStatus | None:
        raw_date = str(raw.get("aggDd") or "")
        stock_code = normalize_stock_code(raw.get("isuSrdCd"))
        if len(raw_date) != 8 or not stock_code:
            return None
        try:
            status_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            return None
        market = str(raw.get("mktNm") or "").strip()
        reference_price = _integer(raw.get("basePrc")) or None
        upper_limit_price, lower_limit_price = calculate_stock_price_limits(
            reference_price,
            market,
        )
        return NxtTradingStatus(
            status_date=status_date,
            stock_code=stock_code,
            stock_name=str(raw.get("isuAbwdNm") or "").strip(),
            market=market,
            tradable_market=str(raw.get("cptrTrdPmsnCdNm") or "").strip(),
            unavailable_reason=str(raw.get("trdIpsbRsn") or "").strip(),
            isin=str(raw.get("isuCd") or "").strip(),
            reference_price=reference_price,
            current_price=_integer(raw.get("curPrc")) or None,
            change_value=(
                _integer(raw.get("contrastPrc"))
                if raw.get("contrastPrc") not in {None, ""}
                else None
            ),
            change_rate=(
                _decimal(raw.get("upDownRate")) / 100
                if raw.get("upDownRate") not in {None, ""}
                else None
            ),
            cumulative_volume=_integer(raw.get("accTdQty")),
            cumulative_amount=_integer(raw.get("accTrval")),
            quote_time=str(raw.get("creTime") or "").strip(),
            open_price=_integer(raw.get("oppr")) or None,
            high_price=_integer(raw.get("hgpr")) or None,
            low_price=_integer(raw.get("lwpr")) or None,
            upper_limit_price=upper_limit_price,
            lower_limit_price=lower_limit_price,
        )
