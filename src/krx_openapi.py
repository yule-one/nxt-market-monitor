from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from src.cache import ResponseCache
from src.http import ResilientSession
from src.kis_rest import FutureQuote, IndexQuote, RestQuote
from src.market_realtime import KRX
from src.nxt_client import normalize_stock_code


KRX_OPENAPI_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
KRX_STOCK_PATHS = {
    "KOSPI": "/sto/stk_bydd_trd",
    "KOSDAQ": "/sto/ksq_bydd_trd",
}
KRX_STOCK_BASIC_PATHS = {
    "KOSPI": "/sto/stk_isu_base_info",
    "KOSDAQ": "/sto/ksq_isu_base_info",
}
KRX_INDEX_PATHS = {
    "KRX TMI": ("/idx/krx_dd_trd", "KRX TMI"),
    "KOSPI": ("/idx/kospi_dd_trd", "코스피"),
    "KOSPI200": ("/idx/kospi_dd_trd", "코스피 200"),
    "KOSDAQ": ("/idx/kosdaq_dd_trd", "코스닥"),
    "KOSDAQ150": ("/idx/kosdaq_dd_trd", "코스닥 150"),
}
KRX_FUTURES_PATH = "/drv/fut_bydd_trd"
KST = ZoneInfo("Asia/Seoul")


class KrxOpenApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class KrxDailySnapshot:
    stock_quotes: dict[tuple[str, str], RestQuote]
    index_quotes: dict[str, IndexQuote]
    listed_shares: dict[str, int]
    future_quotes: dict[str, FutureQuote] = field(default_factory=dict)


@dataclass(frozen=True)
class KrxListedSecurityDaily:
    trade_date: date
    standard_code: str
    short_code: str
    stock_name: str
    market: str
    stock_type: str
    security_type: str
    listed_shares: int
    listing_date: date | None
    cumulative_volume: int
    cumulative_amount: int
    is_kospi200: bool = False
    is_kosdaq150: bool = False


def _integer(value: object) -> int:
    raw = str(value or "0").replace(",", "").strip()
    try:
        return int(float(raw or "0"))
    except ValueError:
        return 0


def _decimal(value: object) -> float:
    raw = str(value or "0").replace(",", "").strip()
    try:
        return float(raw or "0")
    except ValueError:
        return 0.0


def _date_value(value: object) -> date | None:
    raw = re.sub(r"[^0-9]", "", str(value or ""))
    if len(raw) != 8:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


class KrxOpenApiClient:
    def __init__(
        self,
        auth_key: str,
        cache: ResponseCache | None = None,
        *,
        session: Any | None = None,
        base_url: str = KRX_OPENAPI_BASE_URL,
        persist_raw_cache: bool = True,
    ) -> None:
        if not auth_key.strip():
            raise ValueError("KRX OPEN API 인증키가 필요합니다.")
        self.auth_key = auth_key.strip()
        self.cache = cache or ResponseCache()
        self.session = session or ResilientSession(referer="https://openapi.krx.co.kr/")
        self.base_url = base_url.rstrip("/")
        self.persist_raw_cache = persist_raw_cache

    def fetch_daily_snapshot(
        self,
        trading_date: date,
        *,
        force_refresh: bool = False,
        include_futures: bool = True,
        require_futures: bool = False,
    ) -> KrxDailySnapshot:
        stock_quotes, listed_shares = self.fetch_stock_quotes(
            trading_date,
            force_refresh=force_refresh,
        )
        index_quotes = self.fetch_index_quotes(
            trading_date,
            force_refresh=force_refresh,
        )
        future_quotes: dict[str, FutureQuote] = {}
        if include_futures:
            try:
                future_quotes = self.fetch_future_quotes(
                    trading_date,
                    force_refresh=force_refresh,
                )
            except KrxOpenApiError:
                if require_futures:
                    raise
        return KrxDailySnapshot(
            stock_quotes,
            index_quotes,
            listed_shares,
            future_quotes,
        )

    def fetch_stock_quotes(
        self,
        trading_date: date,
        *,
        force_refresh: bool = False,
    ) -> tuple[dict[tuple[str, str], RestQuote], dict[str, int]]:
        quotes: dict[tuple[str, str], RestQuote] = {}
        listed_shares: dict[str, int] = {}
        updated_at = datetime.combine(trading_date, time(18), tzinfo=KST)
        for market, path in KRX_STOCK_PATHS.items():
            rows = self._fetch_rows(path, trading_date, force_refresh=force_refresh)
            for raw in rows:
                symbol = normalize_stock_code(raw.get("ISU_CD"))
                if not symbol:
                    continue
                close_price = _integer(raw.get("TDD_CLSPRC"))
                change_value = _integer(raw.get("CMPPREVDD_PRC"))
                reference_price = close_price - change_value
                quotes[(symbol, KRX)] = RestQuote(
                    market=KRX,
                    symbol=symbol,
                    name=str(raw.get("ISU_NM") or symbol).strip(),
                    current_price=close_price,
                    reference_price=reference_price if reference_price > 0 else None,
                    cumulative_volume=_integer(raw.get("ACC_TRDVOL")),
                    cumulative_amount=_integer(raw.get("ACC_TRDVAL")),
                    updated_at=updated_at,
                    market_cap=_integer(raw.get("MKTCAP")) or None,
                )
                shares = _integer(raw.get("LIST_SHRS"))
                if shares > 0:
                    listed_shares[symbol] = shares
        return quotes, listed_shares

    def fetch_listed_securities(
        self,
        trading_date: date,
        *,
        force_refresh: bool = False,
    ) -> list[KrxListedSecurityDaily]:
        """KOSPI·KOSDAQ 전 주권의 일별 거래정보와 종목기본정보를 결합합니다."""

        daily_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for market, path in KRX_STOCK_PATHS.items():
            for raw in self._fetch_rows(
                path,
                trading_date,
                force_refresh=force_refresh,
            ):
                short_code = normalize_stock_code(raw.get("ISU_CD"))
                if short_code:
                    daily_rows[(market, short_code)] = raw

        records: list[KrxListedSecurityDaily] = []
        matched_keys: set[tuple[str, str]] = set()
        for market, path in KRX_STOCK_BASIC_PATHS.items():
            basic_rows = self._fetch_rows(
                path,
                trading_date,
                force_refresh=force_refresh,
            )
            for raw in basic_rows:
                short_code = normalize_stock_code(raw.get("ISU_SRT_CD"))
                standard_code = str(raw.get("ISU_CD") or "").strip()
                if not short_code or not standard_code:
                    continue
                daily = daily_rows.get((market, short_code))
                if daily is None:
                    continue
                matched_keys.add((market, short_code))
                records.append(
                    KrxListedSecurityDaily(
                        trade_date=trading_date,
                        standard_code=standard_code,
                        short_code=short_code,
                        stock_name=str(
                            raw.get("ISU_ABBRV")
                            or daily.get("ISU_NM")
                            or short_code
                        ).strip(),
                        market=str(raw.get("MKT_TP_NM") or market).strip(),
                        stock_type=str(
                            raw.get("KIND_STKCERT_TP_NM") or "미분류"
                        ).strip(),
                        security_type=str(
                            raw.get("SECUGRP_NM") or "미분류"
                        ).strip(),
                        listed_shares=(
                            _integer(daily.get("LIST_SHRS"))
                            or _integer(raw.get("LIST_SHRS"))
                        ),
                        listing_date=_date_value(raw.get("LIST_DD")),
                        cumulative_volume=_integer(daily.get("ACC_TRDVOL")),
                        cumulative_amount=_integer(daily.get("ACC_TRDVAL")),
                    )
                )

        unmatched = sorted(set(daily_rows) - matched_keys)
        if unmatched:
            preview = ", ".join(f"{market}:{code}" for market, code in unmatched[:5])
            raise KrxOpenApiError(
                f"{trading_date:%Y-%m-%d} KRX 종목기본정보와 결합하지 못한 "
                f"일별 종목이 {len(unmatched):,}개입니다: {preview}"
            )
        if not records:
            raise KrxOpenApiError(
                f"{trading_date:%Y-%m-%d} KRX KOSPI·KOSDAQ 종목 데이터가 없습니다."
            )
        return sorted(records, key=lambda item: (item.market, item.short_code))

    def fetch_index_quotes(
        self,
        trading_date: date,
        *,
        force_refresh: bool = False,
    ) -> dict[str, IndexQuote]:
        rows_by_path: dict[str, list[dict[str, Any]]] = {}
        for path, _index_name in KRX_INDEX_PATHS.values():
            if path not in rows_by_path:
                rows_by_path[path] = self._fetch_rows(
                    path,
                    trading_date,
                    force_refresh=force_refresh,
                )
        updated_at = datetime.combine(trading_date, time(18), tzinfo=KST)
        result: dict[str, IndexQuote] = {}
        for name, (path, krx_name) in KRX_INDEX_PATHS.items():
            raw = next(
                (
                    item
                    for item in rows_by_path[path]
                    if str(item.get("IDX_NM") or "").strip() == krx_name
                ),
                None,
            )
            if raw is None:
                continue
            result[name] = IndexQuote(
                name=name,
                code=str(raw.get("IDX_CLSS") or "").strip(),
                current_value=_decimal(raw.get("CLSPRC_IDX")),
                change_value=_decimal(raw.get("CMPPREVDD_IDX")),
                change_rate=_decimal(raw.get("FLUC_RT")),
                cumulative_volume=_integer(raw.get("ACC_TRDVOL")),
                cumulative_amount=_integer(raw.get("ACC_TRDVAL")),
                updated_at=updated_at,
            )
        return result

    def fetch_named_index_quote(
        self,
        name: str,
        trading_date: date,
        *,
        force_refresh: bool = False,
    ) -> IndexQuote | None:
        if name not in KRX_INDEX_PATHS:
            raise ValueError(f"지원하지 않는 KRX 지수입니다: {name}")
        path, krx_name = KRX_INDEX_PATHS[name]
        rows = self._fetch_rows(path, trading_date, force_refresh=force_refresh)
        raw = next(
            (
                item
                for item in rows
                if str(item.get("IDX_NM") or "").strip() == krx_name
            ),
            None,
        )
        if raw is None:
            return None
        return IndexQuote(
            name=name,
            code=str(raw.get("IDX_CLSS") or "").strip(),
            current_value=_decimal(raw.get("CLSPRC_IDX")),
            change_value=_decimal(raw.get("CMPPREVDD_IDX")),
            change_rate=_decimal(raw.get("FLUC_RT")),
            cumulative_volume=_integer(raw.get("ACC_TRDVOL")),
            cumulative_amount=_integer(raw.get("ACC_TRDVAL")),
            updated_at=datetime.combine(trading_date, time(18), tzinfo=KST),
        )

    def fetch_future_quotes(
        self,
        trading_date: date,
        *,
        force_refresh: bool = False,
    ) -> dict[str, FutureQuote]:
        rows = self._fetch_rows(
            KRX_FUTURES_PATH,
            trading_date,
            force_refresh=force_refresh,
        )
        candidates: list[tuple[int, dict[str, Any]]] = []
        current_month = trading_date.year * 100 + trading_date.month
        for raw in rows:
            if str(raw.get("PROD_NM") or "").strip() != "코스피200 선물":
                continue
            if str(raw.get("MKT_NM") or "").strip() != "정규":
                continue
            match = re.search(r"F\s+(\d{6})", str(raw.get("ISU_NM") or ""))
            if match is None:
                continue
            maturity = int(match.group(1))
            candidates.append((maturity, raw))
        if not candidates:
            return {}
        eligible = [item for item in candidates if item[0] >= current_month]
        _maturity, raw = min(eligible or candidates, key=lambda item: item[0])
        current_value = _decimal(raw.get("TDD_CLSPRC")) or _decimal(
            raw.get("SETL_PRC")
        )
        change_value = _decimal(raw.get("CMPPREVDD_PRC"))
        reference_value = current_value - change_value
        change_rate = (
            change_value / reference_value * 100 if reference_value else 0.0
        )
        updated_at = datetime.combine(trading_date, time(18, 10), tzinfo=KST)
        return {
            "KOSPI200 선물": FutureQuote(
                name="KOSPI200 선물",
                code=str(raw.get("ISU_CD") or "").strip(),
                current_value=current_value,
                change_value=change_value,
                change_rate=change_rate,
                updated_at=updated_at,
                cumulative_volume=_integer(raw.get("ACC_TRDVOL")),
                cumulative_amount=_integer(raw.get("ACC_TRDVAL")),
                open_interest=_integer(raw.get("ACC_OPNINT_QTY")) or None,
                settlement_price=_decimal(raw.get("SETL_PRC")) or None,
            )
        }

    def _fetch_rows(
        self,
        path: str,
        trading_date: date,
        *,
        force_refresh: bool,
    ) -> list[dict[str, Any]]:
        cache_key = f"{path}:{trading_date:%Y%m%d}"
        max_age = timedelta(minutes=30) if trading_date >= date.today() else None
        cached = (
            None
            if force_refresh or not self.persist_raw_cache
            else self.cache.get("krx_openapi_v1", cache_key, max_age)
        )
        if cached is not None:
            return list(cached)
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params={"basDd": trading_date.strftime("%Y%m%d")},
                headers={"AUTH_KEY": self.auth_key},
            )
            payload = response.json()
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {401, 403}:
                raise KrxOpenApiError(
                    "KRX OPEN API 활용 권한을 확인해 주세요."
                ) from exc
            raise KrxOpenApiError(f"KRX OPEN API 조회 실패: {exc}") from exc
        rows = payload.get("OutBlock_1") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise KrxOpenApiError("KRX OPEN API 응답 형식을 확인할 수 없습니다.")
        if self.persist_raw_cache:
            self.cache.set("krx_openapi_v1", cache_key, rows)
        return rows
