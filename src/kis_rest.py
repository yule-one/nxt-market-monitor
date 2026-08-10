from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import requests

from src.http import ResilientSession
from src.kis_master import fetch_listed_shares as fetch_kis_listed_shares
from src.kis_websocket import KisCredentials
from src.market_realtime import KRX, NXT, WatchSymbol
from src.nxt_api_control import (
    is_nxt_morning_break,
    seconds_until_nxt_morning_resume,
)


KIS_REST_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_TOKEN_PATH = "/oauth2/tokenP"
KIS_MULTI_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/intstock-multprice"
KIS_MULTI_PRICE_TR_ID = "FHKST11300006"
KIS_TIME_DAILY_CHART_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
)
KIS_TIME_DAILY_CHART_TR_ID = "FHKST03010230"
KIS_INDEX_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
KIS_INDEX_PRICE_TR_ID = "FHPUP02100000"
KIS_OVERSEAS_DAILY_CHART_PATH = (
    "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
)
KIS_OVERSEAS_DAILY_CHART_TR_ID = "FHKST03030100"
KIS_FUTURES_BOARD_PATH = "/uapi/domestic-futureoption/v1/quotations/display-board-futures"
KIS_FUTURES_BOARD_TR_ID = "FHPIF05030200"
KIS_FUTURES_PRICE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-price"
KIS_FUTURES_PRICE_TR_ID = "FHMIF10000000"
MARKET_DIVISION = {KRX: "J", NXT: "NX"}
INDEX_CODES = {
    "KRX TMI": "4448",
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
    "KOSDAQ150": "2203",
}
USD_KRW_CODE = "FX@KRW"
# KIS 업종지수 응답 단위: 누적거래량=천 주, 누적거래대금=백만원.
# 공식 문서는 하위 단위의 반올림/절사 규칙을 별도로 명시하지 않습니다.
INDEX_VOLUME_TO_SHARES = 1_000
INDEX_AMOUNT_TO_WON = 1_000_000
KST = ZoneInfo("Asia/Seoul")


class KisRestError(RuntimeError):
    pass


@dataclass(frozen=True)
class RestQuote:
    market: str
    symbol: str
    name: str
    current_price: int
    reference_price: int | None
    cumulative_volume: int
    cumulative_amount: int
    updated_at: datetime
    market_cap: int | None = None
    open_price: int | None = None
    high_price: int | None = None
    low_price: int | None = None
    upper_limit_price: int | None = None
    lower_limit_price: int | None = None


@dataclass(frozen=True)
class IndexQuote:
    name: str
    code: str
    current_value: float
    change_value: float
    change_rate: float
    cumulative_volume: int
    cumulative_amount: int
    updated_at: datetime


@dataclass(frozen=True)
class FutureQuote:
    name: str
    code: str
    current_value: float
    change_value: float
    change_rate: float
    updated_at: datetime
    cumulative_volume: int = 0
    cumulative_amount: int = 0
    open_interest: int | None = None
    settlement_price: float | None = None


@dataclass(frozen=True)
class NxtSessionQuote:
    trading_date: date
    session: str
    symbol: str
    name: str
    reference_price: int | None
    open_price: int | None
    high_price: int | None
    low_price: int | None
    close_price: int | None
    cumulative_volume: int
    cumulative_amount: int
    last_trade_time: str
    updated_at: datetime


@dataclass(frozen=True)
class NxtMinuteBar:
    trading_date: date
    symbol: str
    trade_time: str
    open_price: int | None
    high_price: int | None
    low_price: int | None
    close_price: int | None


@dataclass(frozen=True)
class RestCollectorStatus:
    state: str
    message: str
    universe_count: int
    completed_count: int
    quote_count: int
    last_completed_at: datetime | None
    cycle_seconds: float | None


def _integer(value: object) -> int:
    raw = str(value or "0").replace(",", "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _decimal(value: object) -> float:
    raw = str(value or "0").replace(",", "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def build_market_pairs(symbols: Iterable[WatchSymbol]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in symbols:
        pairs.extend(((NXT, item.symbol), (KRX, item.symbol)))
    return pairs


class KisRestClient:
    """KIS REST 접근토큰과 최대 30항목 멀티종목 조회를 관리합니다."""

    def __init__(
        self,
        credentials: KisCredentials,
        *,
        session: ResilientSession | None = None,
        base_url: str = KIS_REST_BASE_URL,
        min_request_interval: float = 0.09,
        token_cache_path: Path | None = None,
    ) -> None:
        self.credentials = credentials
        self.session = session or ResilientSession()
        self.base_url = base_url.rstrip("/")
        self.min_request_interval = min_request_interval
        self.token_cache_path = (
            token_cache_path
            if token_cache_path is not None
            else (
                Path(__file__).resolve().parents[1]
                / "data"
                / "kis_token_cache.json"
                if self.base_url == KIS_REST_BASE_URL
                else None
            )
        )
        self._token_cache_key = hashlib.sha256(
            f"{self.base_url}\0{credentials.app_key}".encode("utf-8")
        ).hexdigest()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.RLock()
        self._request_lock = threading.RLock()
        self._listed_shares_lock = threading.RLock()
        self._last_request_at = 0.0
        self._listed_shares: dict[str, int] = {}
        self._listed_shares_date: date | None = None

    def _load_cached_token(self) -> tuple[str, float] | None:
        if self.token_cache_path is None or not self.token_cache_path.exists():
            return None
        try:
            payload = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
            token = str(payload.get("access_token") or "").strip()
            expires_at = float(payload.get("expires_at") or 0)
            if (
                str(payload.get("cache_key") or "") == self._token_cache_key
                and token
                and expires_at > time.time() + 120
            ):
                return token, expires_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _save_cached_token(self, token: str, expires_at: float) -> None:
        if self.token_cache_path is None:
            return
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.token_cache_path.with_name(
            f"{self.token_cache_path.name}.{os.getpid()}.tmp"
        )
        temporary_path.write_text(
            json.dumps(
                {
                    "cache_key": self._token_cache_key,
                    "access_token": token,
                    "expires_at": expires_at,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.token_cache_path)

    def _access_token(self, *, force_refresh: bool = False) -> str:
        with self._token_lock:
            if (
                not force_refresh
                and self._token
                and time.monotonic() < self._token_expires_at
            ):
                return self._token
            if not force_refresh:
                cached = self._load_cached_token()
                if cached is not None:
                    token, expires_at = cached
                    self._token = token
                    self._token_expires_at = (
                        time.monotonic() + expires_at - time.time() - 120
                    )
                    return token
            response = self.session.post(
                f"{self.base_url}{KIS_TOKEN_PATH}",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.credentials.app_key,
                    "appsecret": self.credentials.app_secret,
                },
                timeout=15,
            )
            payload = response.json()
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise KisRestError(str(payload.get("error_description") or "KIS 접근토큰 발급 실패"))
            expires_in = max(300, _integer(payload.get("expires_in") or 86_400))
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in - 120
            self._save_cached_token(token, time.time() + expires_in)
            return token

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_request_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def fetch_nxt_minute_bars(
        self,
        symbol: str,
        trading_date: date,
        *,
        end_time: str = "200000",
    ) -> list[NxtMinuteBar]:
        """NXT 08:00~20:00 분봉을 페이지 단위로 이어서 조회합니다."""

        if len(symbol) != 6 or not symbol.isalnum():
            raise ValueError(f"6자리 종목코드가 필요합니다: {symbol}")
        if len(end_time) != 6 or not end_time.isdigit():
            raise ValueError(f"HHMMSS 형식의 조회 종료시각이 필요합니다: {end_time}")
        cursor = min(max(end_time, "080000"), "200000")
        date_key = trading_date.strftime("%Y%m%d")
        rows_by_time: dict[str, NxtMinuteBar] = {}

        # 한 번에 최대 120개 분봉이 반환되므로 가장 이른 수록 시각 직전으로
        # 커서를 옮겨 NXT의 하루 세션 전체를 역방향으로 조회합니다.
        for _ in range(10):
            params = {
                "FID_COND_MRKT_DIV_CODE": "NX",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": cursor,
                "FID_INPUT_DATE_1": date_key,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_FAKE_TICK_INCU_YN": "",
            }
            payload = self._request_nxt_time_daily_chart(params, retry_auth=True)
            raw_rows = payload.get("output2") or []
            page_rows = [
                row
                for row in raw_rows
                if isinstance(row, dict)
                and str(row.get("stck_bsop_date") or "") == date_key
                and "080000" <= str(row.get("stck_cntg_hour") or "") <= cursor
                and _integer(row.get("stck_prpr")) > 0
            ]
            if not page_rows:
                break
            for row in page_rows:
                trade_time = str(row.get("stck_cntg_hour") or "")
                rows_by_time[trade_time] = NxtMinuteBar(
                    trading_date=trading_date,
                    symbol=symbol,
                    trade_time=trade_time,
                    open_price=_integer(row.get("stck_oprc")) or None,
                    high_price=_integer(row.get("stck_hgpr")) or None,
                    low_price=_integer(row.get("stck_lwpr")) or None,
                    close_price=_integer(row.get("stck_prpr")) or None,
                )
            earliest = min(str(row.get("stck_cntg_hour") or "") for row in page_rows)
            if earliest <= "080000":
                break
            next_cursor = (
                datetime.strptime(earliest, "%H%M%S") - timedelta(seconds=1)
            ).strftime("%H%M%S")
            if next_cursor >= cursor:
                break
            cursor = next_cursor
        return [rows_by_time[key] for key in sorted(rows_by_time)]

    def fetch_nxt_limit_hit_times(
        self,
        symbol: str,
        trading_date: date,
        limit_prices: dict[str, int],
        *,
        end_time: str = "200000",
    ) -> dict[str, str]:
        """NXT 분봉을 시간순 구간으로 조회해 가격별 최초 도달시각을 찾습니다."""

        if len(symbol) != 6 or not symbol.isalnum():
            raise ValueError(f"6자리 종목코드가 필요합니다: {symbol}")
        if len(end_time) != 6 or not end_time.isdigit():
            raise ValueError(f"HHMMSS 형식의 조회 종료시각이 필요합니다: {end_time}")
        targets = {
            key: int(value)
            for key, value in limit_prices.items()
            if int(value) > 0
        }
        if not targets:
            return {}
        capped_end = min(max(end_time, "080000"), "200000")
        regular_cursors = ["100000", "120000", "140000", "160000", "180000", "200000"]
        cursors = [cursor for cursor in regular_cursors if cursor <= capped_end]
        if not cursors or cursors[-1] != capped_end:
            cursors.append(capped_end)
        date_key = trading_date.strftime("%Y%m%d")
        resolved: dict[str, str] = {}

        for cursor in cursors:
            params = {
                "FID_COND_MRKT_DIV_CODE": "NX",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": cursor,
                "FID_INPUT_DATE_1": date_key,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_FAKE_TICK_INCU_YN": "",
            }
            payload = self._request_nxt_time_daily_chart(params, retry_auth=True)
            rows = sorted(
                (
                    row
                    for row in (payload.get("output2") or [])
                    if isinstance(row, dict)
                    and str(row.get("stck_bsop_date") or "") == date_key
                    and "080000" <= str(row.get("stck_cntg_hour") or "") <= cursor
                    and _integer(row.get("stck_prpr")) > 0
                ),
                key=lambda row: str(row.get("stck_cntg_hour") or ""),
            )
            for key, limit_price in targets.items():
                if key in resolved:
                    continue
                for row in rows:
                    if limit_price in (
                        _integer(row.get("stck_oprc")),
                        _integer(row.get("stck_hgpr")),
                        _integer(row.get("stck_lwpr")),
                        _integer(row.get("stck_prpr")),
                    ):
                        resolved[key] = str(row.get("stck_cntg_hour") or "")
                        break
            if len(resolved) == len(targets):
                break

        # 과거 기업행사가 있으면 KIS 분봉은 수정주가로 환산되는 반면 NXT
        # 공식 일별 상·하한가는 당시 원가격으로 남아 숫자가 일치하지 않을
        # 수 있습니다. 일별 OHLC에서 도달 사실이 이미 확인된 후보에 한해
        # 수정 분봉의 당일 최고·최저가 최초 시각으로 보완합니다.
        unresolved = {key for key in targets if key not in resolved}
        if unresolved:
            bars = self.fetch_nxt_minute_bars(
                symbol,
                trading_date,
                end_time=capped_end,
            )
            high_prices = [
                bar.high_price for bar in bars if bar.high_price is not None
            ]
            low_prices = [bar.low_price for bar in bars if bar.low_price is not None]
            session_high = max(high_prices) if high_prices else None
            session_low = min(low_prices) if low_prices else None
            for key in unresolved:
                normalized_key = key.casefold()
                if (
                    "상" in key or "upper" in normalized_key
                ) and session_high is not None:
                    matching_bar = next(
                        (bar for bar in bars if bar.high_price == session_high),
                        None,
                    )
                elif (
                    "하" in key or "lower" in normalized_key
                ) and session_low is not None:
                    matching_bar = next(
                        (bar for bar in bars if bar.low_price == session_low),
                        None,
                    )
                else:
                    matching_bar = None
                if matching_bar is not None:
                    resolved[key] = matching_bar.trade_time
        return resolved

    def _request_nxt_time_daily_chart(
        self,
        params: dict[str, str],
        *,
        retry_auth: bool,
    ) -> dict[str, object]:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_TIME_DAILY_CHART_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_TIME_DAILY_CHART_PATH}",
                    headers=headers,
                    params=params,
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._request_nxt_time_daily_chart(
                    params,
                    retry_auth=False,
                )
            raise

        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(
                str(payload.get("msg1") or "KIS NXT 일별분봉 조회 실패")
            )
        return payload

    def fetch_nxt_pre_market_quote(
        self,
        symbol: str,
        trading_date: date,
    ) -> NxtSessionQuote:
        """NXT 08:00~08:49 분봉으로 프리마켓 OHLC를 집계합니다."""

        if len(symbol) != 6 or not symbol.isalnum():
            raise ValueError(f"6자리 종목코드가 필요합니다: {symbol}")
        params = {
            "FID_COND_MRKT_DIV_CODE": "NX",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": "085000",
            "FID_INPUT_DATE_1": trading_date.strftime("%Y%m%d"),
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "",
        }
        return self._fetch_nxt_pre_market_quote(
            symbol,
            trading_date,
            params,
            retry_auth=True,
        )

    def fetch_nxt_pre_market_quotes(
        self,
        symbols: Iterable[WatchSymbol],
        trading_date: date,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[NxtSessionQuote]:
        symbol_rows = list(symbols)
        quotes: list[NxtSessionQuote] = []
        total = len(symbol_rows)
        for index, item in enumerate(symbol_rows, start=1):
            quote = self.fetch_nxt_pre_market_quote(item.symbol, trading_date)
            quotes.append(replace(quote, name=item.name or quote.name))
            if progress is not None:
                progress(index, total)
        return quotes

    def _fetch_nxt_pre_market_quote(
        self,
        symbol: str,
        trading_date: date,
        params: dict[str, str],
        *,
        retry_auth: bool,
    ) -> NxtSessionQuote:
        payload = self._request_nxt_time_daily_chart(
            params,
            retry_auth=retry_auth,
        )
        raw_rows = payload.get("output2") or []
        rows = [
            row
            for row in raw_rows
            if isinstance(row, dict)
            and str(row.get("stck_bsop_date") or "")
            == trading_date.strftime("%Y%m%d")
            and "080000" <= str(row.get("stck_cntg_hour") or "") < "085000"
            and _integer(row.get("stck_prpr")) > 0
        ]
        rows.sort(key=lambda row: str(row.get("stck_cntg_hour") or ""))
        output1 = payload.get("output1") or {}
        name = str(output1.get("hts_kor_isnm") or "").strip()
        updated_at = datetime.now(KST)
        if not rows:
            return NxtSessionQuote(
                trading_date=trading_date,
                session="PRE",
                symbol=symbol,
                name=name,
                reference_price=_integer(output1.get("stck_prdy_clpr")) or None,
                open_price=None,
                high_price=None,
                low_price=None,
                close_price=None,
                cumulative_volume=0,
                cumulative_amount=0,
                last_trade_time="",
                updated_at=updated_at,
            )

        first = rows[0]
        last = rows[-1]
        highs = [_integer(row.get("stck_hgpr")) for row in rows]
        lows = [_integer(row.get("stck_lwpr")) for row in rows]
        return NxtSessionQuote(
            trading_date=trading_date,
            session="PRE",
            symbol=symbol,
            name=name,
            reference_price=_integer(output1.get("stck_prdy_clpr")) or None,
            open_price=_integer(first.get("stck_oprc")) or None,
            high_price=max((value for value in highs if value > 0), default=0)
            or None,
            low_price=min((value for value in lows if value > 0), default=0)
            or None,
            close_price=_integer(last.get("stck_prpr")) or None,
            cumulative_volume=sum(_integer(row.get("cntg_vol")) for row in rows),
            cumulative_amount=_integer(last.get("acml_tr_pbmn")),
            last_trade_time=str(last.get("stck_cntg_hour") or ""),
            updated_at=updated_at,
        )

    def fetch_quotes(self, pairs: list[tuple[str, str]]) -> list[RestQuote]:
        if not 1 <= len(pairs) <= 30:
            raise ValueError("멀티종목 조회는 한 번에 1~30개 항목을 지원합니다.")
        for market, symbol in pairs:
            if market not in MARKET_DIVISION or len(symbol) != 6:
                raise ValueError(f"지원하지 않는 시장 또는 종목코드입니다: {market} {symbol}")

        params: dict[str, str] = {}
        for index, (market, symbol) in enumerate(pairs, start=1):
            params[f"FID_COND_MRKT_DIV_CODE_{index}"] = MARKET_DIVISION[market]
            params[f"FID_INPUT_ISCD_{index}"] = symbol

        return self._fetch_quotes(params, pairs, retry_auth=True)

    def _fetch_quotes(
        self,
        params: dict[str, str],
        pairs: list[tuple[str, str]],
        *,
        retry_auth: bool,
    ) -> list[RestQuote]:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_MULTI_PRICE_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_MULTI_PRICE_PATH}",
                    headers=headers,
                    params=params,
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._fetch_quotes(params, pairs, retry_auth=False)
            raise

        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(str(payload.get("msg1") or "KIS 멀티종목 시세조회 실패"))
        raw_output = payload.get("output") or []
        rows = raw_output if isinstance(raw_output, list) else [raw_output]
        updated_at = datetime.now(KST)
        quotes: list[RestQuote] = []
        for (market, requested_symbol), raw in zip(pairs, rows):
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("inter_shrn_iscd") or requested_symbol).strip()[-6:]
            current_price = _integer(raw.get("inter2_prpr"))
            reference_price = _integer(
                raw.get("inter2_sdpr") or raw.get("inter2_prdy_clpr")
            )
            quotes.append(
                RestQuote(
                    market=market,
                    symbol=symbol,
                    name=str(raw.get("inter_kor_isnm") or "").strip(),
                    current_price=current_price,
                    reference_price=reference_price or None,
                    cumulative_volume=_integer(raw.get("acml_vol")),
                    cumulative_amount=_integer(raw.get("acml_tr_pbmn")),
                    updated_at=updated_at,
                    open_price=_integer(raw.get("inter2_oprc")) or None,
                    high_price=_integer(raw.get("inter2_hgpr")) or None,
                    low_price=_integer(raw.get("inter2_lwpr")) or None,
                    upper_limit_price=_integer(raw.get("inter2_mxpr")) or None,
                    lower_limit_price=_integer(raw.get("inter2_llam")) or None,
                )
            )
        return quotes

    def fetch_index_quotes(self) -> dict[str, IndexQuote]:
        quotes: dict[str, IndexQuote] = {}
        for name, code in INDEX_CODES.items():
            quotes[name] = self._fetch_index_quote(name, code, retry_auth=True)
        try:
            quotes["달러-원"] = self.fetch_usd_krw_quote()
        except (KisRestError, requests.RequestException):
            # 환율 제공이 일시 중단돼도 국내 지수 수집은 유지합니다.
            pass
        return quotes

    def fetch_usd_krw_quote(self) -> IndexQuote:
        today = datetime.now(KST).date()
        payload = self._fetch_usd_krw_payload(
            today - timedelta(days=7),
            today,
            retry_auth=True,
        )
        raw_output = payload.get("output1") or {}
        raw = raw_output[0] if isinstance(raw_output, list) and raw_output else raw_output
        if not isinstance(raw, dict):
            raise KisRestError("달러-원 환율 응답 형식 오류")
        current_value = _decimal(raw.get("ovrs_nmix_prpr"))
        if current_value <= 0:
            raise KisRestError("달러-원 환율 현재값이 없습니다.")
        return IndexQuote(
            name="달러-원",
            code=USD_KRW_CODE,
            current_value=current_value,
            change_value=_decimal(raw.get("ovrs_nmix_prdy_vrss")),
            change_rate=_decimal(raw.get("prdy_ctrt")),
            cumulative_volume=0,
            cumulative_amount=0,
            updated_at=datetime.now(KST),
        )

    def fetch_usd_krw_history(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, IndexQuote]:
        if end_date < start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        closes: dict[date, float] = {}
        cursor = start_date - timedelta(days=10)
        while cursor <= end_date:
            chunk_end = min(cursor + timedelta(days=89), end_date)
            payload = self._fetch_usd_krw_payload(
                cursor,
                chunk_end,
                retry_auth=True,
            )
            raw_output = payload.get("output2") or []
            rows = raw_output if isinstance(raw_output, list) else [raw_output]
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                raw_date = str(raw.get("stck_bsop_date") or "").strip()
                try:
                    trading_date = datetime.strptime(raw_date, "%Y%m%d").date()
                except ValueError:
                    continue
                close_value = _decimal(raw.get("ovrs_nmix_prpr"))
                if close_value > 0:
                    closes[trading_date] = close_value
            cursor = chunk_end + timedelta(days=1)

        result: dict[date, IndexQuote] = {}
        previous_close: float | None = None
        for trading_date, close_value in sorted(closes.items()):
            change_value = close_value - previous_close if previous_close else 0.0
            change_rate = (
                change_value / previous_close * 100 if previous_close else 0.0
            )
            if start_date <= trading_date <= end_date:
                result[trading_date] = IndexQuote(
                    name="달러-원",
                    code=USD_KRW_CODE,
                    current_value=close_value,
                    change_value=change_value,
                    change_rate=change_rate,
                    cumulative_volume=0,
                    cumulative_amount=0,
                    updated_at=datetime.combine(
                        trading_date,
                        datetime_time(18),
                        tzinfo=KST,
                    ),
                )
            previous_close = close_value
        return result

    def _fetch_usd_krw_payload(
        self,
        start_date: date,
        end_date: date,
        *,
        retry_auth: bool,
    ) -> dict[str, object]:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_OVERSEAS_DAILY_CHART_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_OVERSEAS_DAILY_CHART_PATH}",
                    headers=headers,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "X",
                        "FID_INPUT_ISCD": USD_KRW_CODE,
                        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
                        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
                        "FID_PERIOD_DIV_CODE": "D",
                    },
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._fetch_usd_krw_payload(
                    start_date,
                    end_date,
                    retry_auth=False,
                )
            raise
        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(str(payload.get("msg1") or "달러-원 환율 조회 실패"))
        return payload

    def fetch_listed_shares(self) -> dict[str, int]:
        today = datetime.now(KST).date()
        with self._listed_shares_lock:
            if self._listed_shares_date == today and self._listed_shares:
                return dict(self._listed_shares)

        shares = fetch_kis_listed_shares(self.session)
        with self._listed_shares_lock:
            self._listed_shares = shares
            self._listed_shares_date = today
            return dict(self._listed_shares)

    def _fetch_index_quote(
        self,
        name: str,
        code: str,
        *,
        retry_auth: bool,
    ) -> IndexQuote:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_INDEX_PRICE_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_INDEX_PRICE_PATH}",
                    headers=headers,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "U",
                        "FID_INPUT_ISCD": code,
                    },
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._fetch_index_quote(name, code, retry_auth=False)
            raise

        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(str(payload.get("msg1") or f"{name} 지수 조회 실패"))
        raw_output = payload.get("output") or {}
        raw = raw_output[0] if isinstance(raw_output, list) and raw_output else raw_output
        if not isinstance(raw, dict):
            raise KisRestError(f"{name} 지수 응답 형식 오류")
        return IndexQuote(
            name=name,
            code=code,
            current_value=_decimal(raw.get("bstp_nmix_prpr")),
            change_value=_decimal(raw.get("bstp_nmix_prdy_vrss")),
            change_rate=_decimal(raw.get("bstp_nmix_prdy_ctrt")),
            cumulative_volume=_integer(raw.get("acml_vol")) * INDEX_VOLUME_TO_SHARES,
            cumulative_amount=_integer(raw.get("acml_tr_pbmn")) * INDEX_AMOUNT_TO_WON,
            updated_at=datetime.now(KST),
        )

    def fetch_futures_quotes(self) -> dict[str, FutureQuote]:
        regular = self._fetch_nearest_kospi200_future(retry_auth=True)
        quotes = {regular.name: regular}
        try:
            night = self._fetch_future_quote(
                "KOSPI200 야간선물",
                "CM",
                regular.code,
                retry_auth=True,
            )
            quotes[night.name] = night
        except (KisRestError, requests.RequestException):
            # 야간 시세가 일시적으로 없더라도 정규 선물 카드는 유지합니다.
            pass
        return quotes

    def _fetch_nearest_kospi200_future(self, *, retry_auth: bool) -> FutureQuote:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_FUTURES_BOARD_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_FUTURES_BOARD_PATH}",
                    headers=headers,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "F",
                        "FID_COND_SCR_DIV_CODE": "20503",
                        # 공란은 KOSPI200 선물입니다.
                        "FID_COND_MRKT_CLS_CODE": "",
                    },
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._fetch_nearest_kospi200_future(retry_auth=False)
            raise

        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(str(payload.get("msg1") or "KOSPI200 선물 조회 실패"))
        raw_output = payload.get("output") or []
        rows = raw_output if isinstance(raw_output, list) else [raw_output]
        candidates = [
            raw
            for raw in rows
            if isinstance(raw, dict)
            and str(raw.get("futs_shrn_iscd") or "").strip()
            and _integer(raw.get("hts_rmnn_dynu")) >= 0
        ]
        if not candidates:
            raise KisRestError("KOSPI200 최근월물 응답이 없습니다.")
        raw = min(candidates, key=lambda item: _integer(item.get("hts_rmnn_dynu")))
        return FutureQuote(
            name="KOSPI200 선물",
            code=str(raw.get("futs_shrn_iscd") or "").strip(),
            current_value=_decimal(raw.get("futs_prpr")),
            change_value=_decimal(raw.get("futs_prdy_vrss")),
            change_rate=_decimal(raw.get("futs_prdy_ctrt")),
            updated_at=datetime.now(KST),
        )

    def _fetch_future_quote(
        self,
        name: str,
        market_code: str,
        code: str,
        *,
        retry_auth: bool,
    ) -> FutureQuote:
        token = self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": KIS_FUTURES_PRICE_TR_ID,
            "custtype": "P",
        }
        try:
            with self._request_lock:
                self._throttle()
                response = self.session.get(
                    f"{self.base_url}{KIS_FUTURES_PRICE_PATH}",
                    headers=headers,
                    params={
                        "FID_COND_MRKT_DIV_CODE": market_code,
                        "FID_INPUT_ISCD": code,
                    },
                    timeout=15,
                )
                self._last_request_at = time.monotonic()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if retry_auth and status_code in {401, 403}:
                self._access_token(force_refresh=True)
                return self._fetch_future_quote(
                    name,
                    market_code,
                    code,
                    retry_auth=False,
                )
            raise

        payload = response.json()
        if str(payload.get("rt_cd")) != "0":
            raise KisRestError(str(payload.get("msg1") or f"{name} 조회 실패"))
        raw_output = payload.get("output1") or {}
        raw = raw_output[0] if isinstance(raw_output, list) and raw_output else raw_output
        if not isinstance(raw, dict):
            raise KisRestError(f"{name} 응답 형식 오류")
        return FutureQuote(
            name=name,
            code=code,
            current_value=_decimal(raw.get("futs_prpr")),
            change_value=_decimal(raw.get("futs_prdy_vrss")),
            change_rate=_decimal(raw.get("futs_prdy_ctrt")),
            updated_at=datetime.now(KST),
        )


class KisRestUniverseCollector:
    """NXT 전체 종목을 KRX/NXT 15종목 단위로 반복 조회합니다."""

    def __init__(
        self,
        client: KisRestClient,
        *,
        symbols_per_request: int = 15,
        heartbeat_timeout: float = 20.0,
    ) -> None:
        if not 1 <= symbols_per_request <= 15:
            raise ValueError("KRX/NXT 동시 조회는 요청당 최대 15종목입니다.")
        self.client = client
        self.symbols_per_request = symbols_per_request
        self.heartbeat_timeout = heartbeat_timeout
        self._lock = threading.RLock()
        self._symbols: list[WatchSymbol] = []
        self._quotes: dict[tuple[str, str], RestQuote] = {}
        self._index_quotes: dict[str, IndexQuote] = {}
        self._future_quotes: dict[str, FutureQuote] = {}
        self._listed_shares: dict[str, int] = {}
        self._interval = 10.0
        self._revision = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_heartbeat = 0.0
        self._state = "대기"
        self._message = "수집을 시작하지 않음"
        self._completed_count = 0
        self._last_completed_at: datetime | None = None
        self._cycle_seconds: float | None = None

    def start(self, symbols: list[WatchSymbol], interval_seconds: float) -> None:
        normalized = list(symbols)
        with self._lock:
            changed = normalized != self._symbols or interval_seconds != self._interval
            self._symbols = normalized
            self._interval = max(5.0, float(interval_seconds))
            self._last_heartbeat = time.monotonic()
            if changed:
                self._revision += 1
                active = {item.symbol for item in normalized}
                self._quotes = {
                    key: quote for key, quote in self._quotes.items() if key[0] in active
                }
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kis-rest-universe-collector",
                daemon=True,
            )
            self._thread.start()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()
            if self._symbols and (not self._thread or not self._thread.is_alive()):
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name="kis-rest-universe-collector",
                    daemon=True,
                )
                self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> RestCollectorStatus:
        with self._lock:
            symbols_with_quotes = {
                symbol for symbol, _market in self._quotes
            }
            return RestCollectorStatus(
                state=self._state,
                message=self._message,
                universe_count=len(self._symbols),
                completed_count=self._completed_count,
                quote_count=len(symbols_with_quotes),
                last_completed_at=self._last_completed_at,
                cycle_seconds=self._cycle_seconds,
            )

    def snapshot(self) -> dict[tuple[str, str], RestQuote]:
        with self._lock:
            return dict(self._quotes)

    def index_snapshot(self) -> dict[str, IndexQuote]:
        with self._lock:
            return dict(self._index_quotes)

    def futures_snapshot(self) -> dict[str, FutureQuote]:
        with self._lock:
            return dict(self._future_quotes)

    def listed_shares_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._listed_shares)

    def _set_status(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _pause_for_nxt_morning_break(self) -> bool:
        now = datetime.now(KST)
        if not is_nxt_morning_break(now):
            return False
        self._set_status("휴장 대기", "NXT 08:50~09:00 휴장으로 REST 조회 중지")
        wait_seconds = seconds_until_nxt_morning_resume(now)
        self._stop_event.wait(min(max(wait_seconds, 1.0), 10.0))
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                if time.monotonic() - self._last_heartbeat > self.heartbeat_timeout:
                    self._state = "일시정지"
                    self._message = "페이지가 열려 있지 않아 REST 조회를 멈춤"
                    return
                symbols = list(self._symbols)
                interval = self._interval
                revision = self._revision
                self._completed_count = 0
            if not symbols:
                self._set_status("대기", "조회할 NXT 종목이 없음")
                self._stop_event.wait(1)
                continue
            if self._pause_for_nxt_morning_break():
                continue

            cycle_started = time.monotonic()
            try:
                self._set_status("갱신 중", "NXT 전종목 KRX/NXT 누적 시세 조회 중")
                if self._pause_for_nxt_morning_break():
                    continue
                try:
                    listed_shares = self.client.fetch_listed_shares()
                    active_symbols = {item.symbol for item in symbols}
                    with self._lock:
                        self._listed_shares = {
                            symbol: shares
                            for symbol, shares in listed_shares.items()
                            if symbol in active_symbols
                        }
                except Exception:
                    # 종목 마스터 갱신 실패 시 직전 상장주식수를 유지합니다.
                    pass
                if self._pause_for_nxt_morning_break():
                    continue
                try:
                    index_quotes = self.client.fetch_index_quotes()
                    with self._lock:
                        self._index_quotes = index_quotes
                except Exception:
                    # 지수 조회 실패가 전종목 누적 시세 갱신을 막지는 않게 합니다.
                    pass
                if self._pause_for_nxt_morning_break():
                    continue
                try:
                    future_quotes = self.client.fetch_futures_quotes()
                    with self._lock:
                        self._future_quotes = future_quotes
                except Exception:
                    # 선물 조회 실패가 전종목 누적 시세 갱신을 막지는 않게 합니다.
                    pass
                paused_for_break = False
                for start in range(0, len(symbols), self.symbols_per_request):
                    if self._stop_event.is_set():
                        return
                    if self._pause_for_nxt_morning_break():
                        paused_for_break = True
                        break
                    with self._lock:
                        if revision != self._revision:
                            break
                    chunk = symbols[start : start + self.symbols_per_request]
                    quotes = self.client.fetch_quotes(build_market_pairs(chunk))
                    with self._lock:
                        for quote in quotes:
                            self._quotes[(quote.symbol, quote.market)] = quote
                        self._completed_count = min(start + len(chunk), len(symbols))
                else:
                    elapsed = time.monotonic() - cycle_started
                    with self._lock:
                        self._last_completed_at = datetime.now(KST)
                        self._cycle_seconds = elapsed
                        self._state = "정상"
                        self._message = f"{len(symbols):,}종목 전체 갱신 완료"
                    remaining = max(0.0, interval - elapsed)
                    self._stop_event.wait(remaining)
                    continue
                if paused_for_break:
                    continue
            except Exception as exc:
                self._set_status("오류", str(exc))
                self._stop_event.wait(min(interval, 10.0))
        self._set_status("중지", "REST 수집 중지")


def build_comparison_rows(
    symbols: Iterable[WatchSymbol],
    quotes: dict[tuple[str, str], RestQuote],
    listed_shares: dict[str, int] | None = None,
) -> list[dict[str, object]]:
    listed_shares = listed_shares or {}
    rows: list[dict[str, object]] = []
    for item in symbols:
        nxt_quote = quotes.get((item.symbol, NXT))
        krx_quote = quotes.get((item.symbol, KRX))
        nxt_price = nxt_quote.current_price if nxt_quote else None
        krx_price = krx_quote.current_price if krx_quote else None
        reference_price = (
            nxt_quote.reference_price if nxt_quote and nxt_quote.reference_price else None
        ) or (
            krx_quote.reference_price if krx_quote and krx_quote.reference_price else None
        )
        nxt_volume = nxt_quote.cumulative_volume if nxt_quote else None
        nxt_amount = nxt_quote.cumulative_amount if nxt_quote else None
        krx_volume = krx_quote.cumulative_volume if krx_quote else None
        krx_amount = krx_quote.cumulative_amount if krx_quote else None
        direct_market_cap = getattr(krx_quote, "market_cap", None) if krx_quote else None
        shares = listed_shares.get(item.symbol)
        market_cap = direct_market_cap or (
            krx_price * shares
            if krx_price is not None and krx_price > 0 and shares and shares > 0
            else None
        )
        rows.append(
            {
                "종목코드": item.symbol,
                "종목명": item.name,
                "nxt_current_price": nxt_price,
                "change_rate": _change_rate(nxt_price, reference_price),
                "disparity_rate": _change_rate(nxt_price, krx_price),
                "market_cap": market_cap,
                "krx_current_price": krx_price,
                "nxt_volume": nxt_volume,
                "nxt_amount": nxt_amount,
                "volume_ratio": _ratio(nxt_volume, krx_volume),
                "amount_ratio": _ratio(nxt_amount, krx_amount),
                "krx_volume": krx_volume,
                "krx_amount": krx_amount,
            }
        )
    return rows


def exclude_unavailable_nxt_quotes(
    quotes: dict[tuple[str, str], RestQuote],
    unavailable_symbols: Iterable[str],
) -> dict[tuple[str, str], RestQuote]:
    """거래불가 종목의 NXT 시세만 제거하고 KRX 시세는 유지합니다."""

    excluded = {str(symbol) for symbol in unavailable_symbols}
    if not excluded:
        return dict(quotes)
    return {
        key: quote
        for key, quote in quotes.items()
        if not (quote.market == NXT and quote.symbol in excluded)
    }


def build_market_totals(
    quotes: dict[tuple[str, str], RestQuote],
    index_quotes: dict[str, IndexQuote],
) -> dict[str, object]:
    nxt_quotes = [quote for quote in quotes.values() if quote.market == NXT]
    nxt_volume = sum(quote.cumulative_volume for quote in nxt_quotes) if nxt_quotes else None
    nxt_amount = sum(quote.cumulative_amount for quote in nxt_quotes) if nxt_quotes else None
    krx_indices = [index_quotes.get("KOSPI"), index_quotes.get("KOSDAQ")]
    if all(index is not None for index in krx_indices):
        krx_volume = sum(index.cumulative_volume for index in krx_indices if index)
        krx_amount = sum(index.cumulative_amount for index in krx_indices if index)
    else:
        krx_volume = None
        krx_amount = None
    return {
        "nxt_volume": nxt_volume,
        "krx_volume": krx_volume,
        "volume_ratio": _ratio(nxt_volume, krx_volume),
        "nxt_amount": nxt_amount,
        "krx_amount": krx_amount,
        "amount_ratio": _ratio(nxt_amount, krx_amount),
    }


def build_nxt_weighted_change_rates(
    quotes: dict[tuple[str, str], RestQuote],
    listed_shares: dict[str, int],
) -> dict[str, object]:
    """Calculate NXT-universe market-cap-weighted price change rates."""
    reference_nxt_total = 0
    reference_base_total = 0
    reference_count = 0
    krx_nxt_total = 0
    krx_current_total = 0
    krx_count = 0

    for symbol, shares in listed_shares.items():
        if shares <= 0:
            continue
        nxt_quote = quotes.get((symbol, NXT))
        if nxt_quote is None or nxt_quote.current_price <= 0:
            continue
        krx_quote = quotes.get((symbol, KRX))
        reference_price = nxt_quote.reference_price or (
            krx_quote.reference_price if krx_quote else None
        )
        if reference_price and reference_price > 0:
            reference_nxt_total += nxt_quote.current_price * shares
            reference_base_total += reference_price * shares
            reference_count += 1
        if krx_quote is not None and krx_quote.current_price > 0:
            krx_nxt_total += nxt_quote.current_price * shares
            krx_current_total += krx_quote.current_price * shares
            krx_count += 1

    return {
        "reference_rate": _change_rate(reference_nxt_total, reference_base_total),
        "krx_rate": _change_rate(krx_nxt_total, krx_current_total),
        "reference_count": reference_count,
        "krx_count": krx_count,
    }


def _change_rate(current: int | None, base: int | None) -> float | None:
    if current is None or base is None or base <= 0:
        return None
    return current / base - 1


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator
