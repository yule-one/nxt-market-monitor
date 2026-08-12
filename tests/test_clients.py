from datetime import date

import requests
import pytest

from src.kind_client import KindClient, normalize_kind_code
from src.nxt_client import NxtClient, normalize_stock_code


class TrackingCache:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.max_age: object = "not-called"

    def get(self, _source: str, _key: str, max_age: object = None) -> object:
        self.max_age = max_age
        return self.payload

    def set(self, _source: str, _key: str, _payload: object) -> None:
        raise AssertionError("cached payload should be reused")


class StaleTrackingCache:
    def __init__(self, payload: object | None) -> None:
        self.payload = payload
        self.max_ages: list[object] = []

    def get(self, _source: str, _key: str, max_age: object = None) -> object:
        self.max_ages.append(max_age)
        return self.payload if max_age is None else None

    def set(self, _source: str, _key: str, _payload: object) -> None:
        raise AssertionError("failed response must not overwrite the stale cache")


def test_normalize_stock_codes() -> None:
    assert normalize_kind_code("40349") == "403490"
    assert normalize_kind_code("005930") == "005930"
    assert normalize_stock_code("A056080") == "056080"
    assert normalize_stock_code("A0008Z0") == "0008Z0"
    assert normalize_stock_code("") == ""


def test_parse_kind_result_html() -> None:
    html = """
    <table><tbody><tr>
      <td>1</td><td>2026-08-04 14:26</td>
      <td><img alt="코스닥"><a onclick="companysummary_open('18933');" title="씨이랩">씨이랩</a></td>
      <td><a onclick="openDisclsViewer('20260804000315','')" title="주권매매거래정지">공시</a></td>
      <td>코스닥시장본부</td><td>차트</td>
    </tr></tbody></table>
    <div>전체 1 건 : 1 /1</div>
    """
    rows, total_pages = KindClient._parse_results(html, "거래정지/재개")
    assert total_pages == 1
    assert len(rows) == 1
    assert rows[0].stock_code == "189330"
    assert rows[0].market == "KOSDAQ"
    assert rows[0].report_no == "20260804000315"


def test_parse_nxt_change() -> None:
    parsed = NxtClient._parse_change(
        {
            "aggDd": "20260804",
            "isuSrdCd": "A005930",
            "isuAbwdNm": "삼성전자",
            "mktNm": "KOSPI",
            "addExlCd": "편입",
            "rsnCd": "정기변경",
            "isuCd": "KR7005930003",
            "registDt": 1,
        }
    )
    assert parsed is not None
    assert parsed.change_date == date(2026, 8, 4)
    assert parsed.stock_code == "005930"
    assert parsed.change_type == "편입"


def test_parse_nxt_trading_status() -> None:
    parsed = NxtClient._parse_trading_status(
        {
            "aggDd": "20260803",
            "isuSrdCd": "A036800",
            "isuAbwdNm": "나이스정보통신",
            "mktNm": "KOSDAQ",
            "cptrTrdPmsnCdNm": "거래불가",
            "trdIpsbRsn": "거래정지",
            "isuCd": "KR7036800000",
            "basePrc": "42000",
            "curPrc": 44100,
            "contrastPrc": 2100,
            "upDownRate": 5.0,
            "accTdQty": 123456,
            "accTrval": 5432100000,
            "creTime": "200500",
            "oppr": 42_100,
            "hgpr": 54_600,
            "lwpr": 41_500,
        }
    )
    assert parsed is not None
    assert parsed.status_date == date(2026, 8, 3)
    assert parsed.stock_code == "036800"
    assert parsed.tradable_market == "거래불가"
    assert parsed.unavailable_reason == "거래정지"
    assert parsed.reference_price == 42_000
    assert parsed.current_price == 44_100
    assert parsed.change_value == 2_100
    assert parsed.change_rate == 0.05
    assert parsed.cumulative_volume == 123_456
    assert parsed.cumulative_amount == 5_432_100_000
    assert parsed.quote_time == "200500"
    assert parsed.open_price == 42_100
    assert parsed.high_price == 54_600
    assert parsed.low_price == 41_500
    assert parsed.upper_limit_price == 54_600
    assert parsed.lower_limit_price == 29_400


def test_past_nxt_trading_status_is_reused_without_expiration() -> None:
    cached_row = {
        "status_date": "2025-03-04",
        "stock_code": "005930",
        "stock_name": "삼성전자",
        "market": "KOSPI",
        "tradable_market": "NXT",
        "unavailable_reason": "",
    }
    cache = TrackingCache([cached_row])

    rows = NxtClient(cache).fetch_trading_status(date(2025, 3, 4))  # type: ignore[arg-type]

    assert cache.max_age is None
    assert rows[0].stock_code == "005930"


def test_current_nxt_trading_status_uses_stale_cache_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_date = date.today()
    cache_key = status_date.strftime("%Y%m%d")
    cached_row = {
        "status_date": status_date.isoformat(),
        "stock_code": "005930",
        "stock_name": "삼성전자",
        "market": "KOSPI",
        "tradable_market": "NXT",
        "unavailable_reason": "",
    }
    cache = StaleTrackingCache([cached_row])
    NxtClient._trading_status_retry_after.pop(cache_key, None)

    def fail_post(*_args: object, **_kwargs: object) -> object:
        raise requests.ConnectionError("temporary failure")

    monkeypatch.setattr("src.nxt_client.ResilientSession.post", fail_post)
    client = NxtClient(cache)  # type: ignore[arg-type]
    try:
        rows = client.fetch_trading_status(status_date)
    finally:
        NxtClient._trading_status_retry_after.pop(cache_key, None)

    assert rows[0].stock_code == "005930"
    assert cache.max_ages[-1] is None
    assert "최근 저장" in client.trading_status_fallback_warning


def test_current_nxt_trading_status_connection_failure_has_shared_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_date = date.today()
    cache_key = status_date.strftime("%Y%m%d")
    cache = StaleTrackingCache(None)
    call_count = 0
    NxtClient._trading_status_retry_after.pop(cache_key, None)

    def fail_post(*_args: object, **_kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        raise requests.ConnectionError("temporary failure")

    monkeypatch.setattr("src.nxt_client.ResilientSession.post", fail_post)
    try:
        with pytest.raises(requests.ConnectionError):
            NxtClient(cache).fetch_trading_status(status_date)  # type: ignore[arg-type]
        with pytest.raises(requests.ConnectionError):
            NxtClient(cache).fetch_trading_status(status_date)  # type: ignore[arg-type]
    finally:
        NxtClient._trading_status_retry_after.pop(cache_key, None)

    assert call_count == 1


def test_parse_investment_status_period() -> None:
    html = """
    <table><tbody><tr>
      <td>1</td>
      <td><img alt="유가증권"><a onclick="companysummary_open('00207');" title="비비안">비비안</a></td>
      <td>2026-07-16</td><td>2026-07-20</td><td>2026-08-03</td>
    </tr></tbody></table>
    <div>전체 1 건 : 1 /1</div>
    """
    periods, pages = KindClient._parse_status_periods(html, "투자경고종목")
    assert pages == 1
    assert len(periods) == 1
    assert periods[0].stock_code == "002070"
    assert periods[0].start_date == date(2026, 7, 20)
    assert periods[0].end_date == date(2026, 8, 3)
