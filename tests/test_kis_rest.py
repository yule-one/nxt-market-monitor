from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from src.kis_rest import (
    IndexQuote,
    KisRestClient,
    RestQuote,
    build_comparison_rows,
    build_market_pairs,
    build_market_totals,
    build_nxt_weighted_change_rates,
    exclude_unavailable_nxt_quotes,
)
from src.kis_websocket import KisCredentials
from src.market_realtime import KRX, NXT, WatchSymbol


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.last_params: dict[str, str] = {}
        self.post_count = 0

    def post(self, _url: str, **_kwargs: object) -> FakeResponse:
        self.post_count += 1
        return FakeResponse({"access_token": "test-token", "expires_in": 3600})

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        self.last_params = dict(kwargs["params"])  # type: ignore[arg-type]
        if "inquire-time-dailychartprice" in _url:
            return FakeResponse(
                {
                    "rt_cd": "0",
                    "output1": {
                        "hts_kor_isnm": "삼성전자",
                        "stck_prdy_clpr": "70000",
                    },
                    "output2": [
                        {
                            "stck_bsop_date": "20260806",
                            "stck_cntg_hour": "084900",
                            "stck_prpr": "71500",
                            "stck_oprc": "71400",
                            "stck_hgpr": "71600",
                            "stck_lwpr": "71300",
                            "cntg_vol": "20",
                            "acml_tr_pbmn": "2135000",
                        },
                        {
                            "stck_bsop_date": "20260806",
                            "stck_cntg_hour": "080000",
                            "stck_prpr": "70100",
                            "stck_oprc": "70000",
                            "stck_hgpr": "70200",
                            "stck_lwpr": "69900",
                            "cntg_vol": "10",
                            "acml_tr_pbmn": "700000",
                        },
                        {
                            "stck_bsop_date": "20260805",
                            "stck_cntg_hour": "084900",
                            "stck_prpr": "69000",
                            "stck_oprc": "69000",
                            "stck_hgpr": "69000",
                            "stck_lwpr": "69000",
                            "cntg_vol": "999",
                            "acml_tr_pbmn": "999999",
                        },
                    ],
                }
            )
        if "inquire-daily-chartprice" in _url:
            return FakeResponse(
                {
                    "rt_cd": "0",
                    "output1": {
                        "ovrs_nmix_prpr": "1425.40",
                        "ovrs_nmix_prdy_vrss": "1.30",
                        "prdy_ctrt": "0.09",
                    },
                    "output2": [
                        {"stck_bsop_date": "20260807", "ovrs_nmix_prpr": "1425.40"},
                        {"stck_bsop_date": "20260806", "ovrs_nmix_prpr": "1424.10"},
                    ],
                }
            )
        if "inquire-index-price" in _url:
            return FakeResponse(
                {
                    "rt_cd": "0",
                    "output": {
                        "bstp_nmix_prpr": "2800.12",
                        "bstp_nmix_prdy_vrss": "12.34",
                        "bstp_nmix_prdy_ctrt": "0.44",
                        "acml_vol": "123456",
                        "acml_tr_pbmn": "789012345",
                    },
                }
            )
        if "display-board-futures" in _url:
            return FakeResponse(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "futs_shrn_iscd": "A01612",
                            "futs_prpr": "1000.00",
                            "futs_prdy_vrss": "-51.15",
                            "futs_prdy_ctrt": "-4.87",
                            "hts_rmnn_dynu": "127",
                        },
                        {
                            "futs_shrn_iscd": "A01609",
                            "futs_prpr": "990.20",
                            "futs_prdy_vrss": "-51.85",
                            "futs_prdy_ctrt": "-4.98",
                            "hts_rmnn_dynu": "36",
                        },
                    ],
                }
            )
        if "domestic-futureoption" in _url and "inquire-price" in _url:
            return FakeResponse(
                {
                    "rt_cd": "0",
                    "output1": {
                        "futs_prpr": "1025.10",
                        "futs_prdy_vrss": "-16.95",
                        "futs_prdy_ctrt": "-1.63",
                    },
                }
            )
        return FakeResponse(
            {
                "rt_cd": "0",
                "output": [
                    {
                        "inter_shrn_iscd": "005930",
                        "inter_kor_isnm": "삼성전자",
                        "inter2_prpr": "71000",
                        "inter2_sdpr": "70000",
                        "inter2_oprc": "70200",
                        "inter2_hgpr": "91000",
                        "inter2_lwpr": "69500",
                        "inter2_mxpr": "91000",
                        "inter2_llam": "49000",
                        "acml_vol": "100",
                        "acml_tr_pbmn": "7100000",
                    },
                    {
                        "inter_shrn_iscd": "005930",
                        "inter_kor_isnm": "삼성전자",
                        "inter2_prpr": "70500",
                        "inter2_prdy_clpr": "70000",
                        "acml_vol": "200",
                        "acml_tr_pbmn": "14000000",
                    },
                ],
            }
        )


def test_rest_multprice_maps_nxt_and_krx_in_request_order() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )
    quotes = client.fetch_quotes([(NXT, "005930"), (KRX, "005930")])

    assert [(quote.market, quote.current_price) for quote in quotes] == [
        (NXT, 71_000),
        (KRX, 70_500),
    ]
    assert quotes[0].reference_price == 70_000
    assert quotes[0].open_price == 70_200
    assert quotes[0].high_price == 91_000
    assert quotes[0].low_price == 69_500
    assert quotes[0].upper_limit_price == 91_000
    assert quotes[0].lower_limit_price == 49_000
    assert quotes[1].cumulative_amount == 14_000_000
    assert session.last_params["FID_COND_MRKT_DIV_CODE_1"] == "NX"
    assert session.last_params["FID_COND_MRKT_DIV_CODE_2"] == "J"


def test_nxt_pre_market_quote_aggregates_0800_to_0849_minutes() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )

    quote = client.fetch_nxt_pre_market_quote("005930", date(2026, 8, 6))

    assert quote.reference_price == 70_000
    assert quote.open_price == 70_000
    assert quote.high_price == 71_600
    assert quote.low_price == 69_900
    assert quote.close_price == 71_500
    assert quote.cumulative_volume == 30
    assert quote.cumulative_amount == 2_135_000
    assert quote.last_trade_time == "084900"
    assert session.last_params["FID_COND_MRKT_DIV_CODE"] == "NX"
    assert session.last_params["FID_INPUT_HOUR_1"] == "085000"


def test_nxt_minute_bars_filters_date_and_sorts_full_session_rows() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )

    bars = client.fetch_nxt_minute_bars("005930", date(2026, 8, 6))

    assert [bar.trade_time for bar in bars] == ["080000", "084900"]
    assert bars[0].open_price == 70_000
    assert bars[1].high_price == 71_600
    assert bars[1].close_price == 71_500
    assert session.last_params["FID_COND_MRKT_DIV_CODE"] == "NX"


def test_nxt_limit_hit_times_find_first_matching_minute() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )

    hit_times = client.fetch_nxt_limit_hit_times(
        "005930",
        date(2026, 8, 6),
        {"upper": 71_600, "open": 70_000},
    )

    assert hit_times == {"upper": "084900", "open": "080000"}
    assert session.last_params["FID_COND_MRKT_DIV_CODE"] == "NX"


def test_nxt_limit_hit_times_use_adjusted_session_extreme_as_fallback() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )

    hit_times = client.fetch_nxt_limit_hit_times(
        "005930",
        date(2026, 8, 6),
        {"상한가": 80_000},
    )

    assert hit_times == {"상한가": "084900"}


def test_access_token_cache_is_reused_across_clients(tmp_path: Path) -> None:
    token_path = tmp_path / "kis-token.json"
    first_session = FakeSession()
    first = KisRestClient(
        KisCredentials("key", "secret"),
        session=first_session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
        token_cache_path=token_path,
    )
    assert first._access_token() == "test-token"
    assert first_session.post_count == 1

    second_session = FakeSession()
    second = KisRestClient(
        KisCredentials("key", "secret"),
        session=second_session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
        token_cache_path=token_path,
    )
    assert second._access_token() == "test-token"
    assert second_session.post_count == 0


def test_market_pairs_use_two_slots_per_symbol() -> None:
    symbols = [WatchSymbol("005930", "삼성전자"), WatchSymbol("000660", "SK하이닉스")]
    assert build_market_pairs(symbols) == [
        (NXT, "005930"),
        (KRX, "005930"),
        (NXT, "000660"),
        (KRX, "000660"),
    ]


def test_rest_index_quotes_include_value_change_and_market_totals() -> None:
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=FakeSession(),  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )
    indices = client.fetch_index_quotes()

    assert list(indices) == [
        "KRX TMI",
        "KOSPI",
        "KOSDAQ",
        "KOSPI200",
        "KOSDAQ150",
        "달러-원",
    ]
    assert indices["KOSPI"].current_value == 2800.12
    assert indices["KOSPI"].change_value == 12.34
    assert indices["KOSPI"].change_rate == 0.44
    assert indices["KOSPI"].cumulative_volume == 123_456_000
    assert indices["KOSPI"].cumulative_amount == 789_012_345_000_000
    assert indices["달러-원"].current_value == 1425.40
    assert indices["달러-원"].change_rate == 0.09


def test_rest_usd_krw_history_builds_daily_change_rates() -> None:
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=FakeSession(),  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )
    history = client.fetch_usd_krw_history(
        datetime(2026, 8, 6).date(),
        datetime(2026, 8, 7).date(),
    )

    assert history[datetime(2026, 8, 7).date()].current_value == 1425.40
    assert round(history[datetime(2026, 8, 7).date()].change_value, 2) == 1.30


def test_rest_futures_quotes_use_nearest_kospi200_contract_for_day_and_night() -> None:
    session = FakeSession()
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=session,  # type: ignore[arg-type]
        base_url="https://example.test",
        min_request_interval=0,
    )
    futures = client.fetch_futures_quotes()

    assert list(futures) == ["KOSPI200 선물", "KOSPI200 야간선물"]
    assert futures["KOSPI200 선물"].code == "A01609"
    assert futures["KOSPI200 선물"].current_value == 990.20
    assert futures["KOSPI200 선물"].change_rate == -4.98
    assert futures["KOSPI200 야간선물"].code == "A01609"
    assert futures["KOSPI200 야간선물"].current_value == 1025.10
    assert futures["KOSPI200 야간선물"].change_value == -16.95
    assert session.last_params == {
        "FID_COND_MRKT_DIV_CODE": "CM",
        "FID_INPUT_ISCD": "A01609",
    }


def test_rest_multprice_rejects_more_than_thirty_items() -> None:
    client = KisRestClient(
        KisCredentials("key", "secret"),
        session=FakeSession(),  # type: ignore[arg-type]
        base_url="https://example.test",
    )
    with pytest.raises(ValueError):
        client.fetch_quotes([(NXT, f"{index:06d}") for index in range(31)])


def test_comparison_rows_calculate_all_requested_rates() -> None:
    updated_at = datetime(2026, 8, 6, 10, 0)
    quotes = {
        ("005930", NXT): RestQuote(
            NXT, "005930", "삼성전자", 71_000, 70_000, 100, 7_100_000, updated_at
        ),
        ("005930", KRX): RestQuote(
            KRX, "005930", "삼성전자", 70_500, 70_000, 200, 14_000_000, updated_at
        ),
    }
    row = build_comparison_rows(
        [WatchSymbol("005930", "삼성전자")],
        quotes,
        {"005930": 5_000},
    )[0]

    assert row["change_rate"] == 71_000 / 70_000 - 1
    assert row["disparity_rate"] == 71_000 / 70_500 - 1
    assert row["volume_ratio"] == 0.5
    assert row["amount_ratio"] == 7_100_000 / 14_000_000
    assert row["krx_current_price"] == 70_500
    assert row["market_cap"] == 70_500 * 5_000


def test_unavailable_nxt_quote_is_removed_but_krx_quote_is_preserved() -> None:
    updated_at = datetime(2026, 8, 7, 10, 0)
    quotes = {
        ("005930", NXT): RestQuote(
            NXT, "005930", "삼성전자", 71_000, 70_000, 100, 7_100_000, updated_at
        ),
        ("005930", KRX): RestQuote(
            KRX, "005930", "삼성전자", 70_500, 70_000, 200, 14_000_000, updated_at
        ),
        ("000660", NXT): RestQuote(
            NXT, "000660", "SK하이닉스", 195_000, 190_000, 300, 58_500_000, updated_at
        ),
    }

    filtered = exclude_unavailable_nxt_quotes(quotes, ["005930"])
    row = build_comparison_rows(
        [WatchSymbol("005930", "삼성전자")],
        filtered,
        {"005930": 5_000},
    )[0]

    assert ("005930", NXT) not in filtered
    assert ("005930", KRX) in filtered
    assert ("000660", NXT) in filtered
    assert row["nxt_current_price"] is None
    assert row["change_rate"] is None
    assert row["disparity_rate"] is None
    assert row["nxt_volume"] is None
    assert row["volume_ratio"] is None
    assert row["nxt_amount"] is None
    assert row["amount_ratio"] is None
    assert row["krx_current_price"] == 70_500
    assert row["krx_volume"] == 200
    assert row["krx_amount"] == 14_000_000


def test_comparison_rows_prefer_direct_krx_market_cap() -> None:
    updated_at = datetime(2026, 8, 6, 10, 0)
    quotes = {
        ("005930", KRX): RestQuote(
            KRX,
            "005930",
            "삼성전자",
            70_500,
            70_000,
            200,
            14_000_000,
            updated_at,
            market_cap=420_000_000_000_000,
        ),
    }

    row = build_comparison_rows(
        [WatchSymbol("005930", "삼성전자")],
        quotes,
        {"005930": 5_000},
    )[0]

    assert row["market_cap"] == 420_000_000_000_000


def test_market_totals_compare_nxt_sum_with_krx_indices() -> None:
    updated_at = datetime(2026, 8, 6, 10, 0)
    quotes = {
        ("005930", NXT): RestQuote(
            NXT, "005930", "삼성전자", 71_000, 70_000, 100, 7_100_000, updated_at
        ),
        ("000660", NXT): RestQuote(
            NXT, "000660", "SK하이닉스", 500_000, 490_000, 300, 15_000_000, updated_at
        ),
    }
    indices = {
        "KOSPI": IndexQuote(
            "KOSPI", "0001", 2800, 10, 0.3, 1_000, 100_000_000, updated_at
        ),
        "KOSDAQ": IndexQuote(
            "KOSDAQ", "1001", 900, 5, 0.5, 3_000, 300_000_000, updated_at
        ),
    }
    totals = build_market_totals(quotes, indices)

    assert totals["nxt_volume"] == 400
    assert totals["krx_volume"] == 4_000
    assert totals["volume_ratio"] == 0.1
    assert totals["nxt_amount"] == 22_100_000
    assert totals["krx_amount"] == 400_000_000
    assert totals["amount_ratio"] == 22_100_000 / 400_000_000


def test_nxt_weighted_change_rates_use_listed_shares_and_krx_fallback() -> None:
    updated_at = datetime(2026, 8, 6, 10, 0)
    quotes = {
        ("111111", NXT): RestQuote(
            NXT, "111111", "첫째", 110, 100, 10, 1_100, updated_at
        ),
        ("111111", KRX): RestQuote(
            KRX, "111111", "첫째", 105, 100, 20, 2_100, updated_at
        ),
        ("222222", NXT): RestQuote(
            NXT, "222222", "둘째", 190, None, 30, 5_700, updated_at
        ),
        ("222222", KRX): RestQuote(
            KRX, "222222", "둘째", 195, 200, 40, 7_800, updated_at
        ),
    }
    rates = build_nxt_weighted_change_rates(
        quotes,
        {"111111": 100, "222222": 300, "333333": 500},
    )

    weighted_nxt = 110 * 100 + 190 * 300
    assert rates["reference_rate"] == weighted_nxt / (100 * 100 + 200 * 300) - 1
    assert rates["krx_rate"] == weighted_nxt / (105 * 100 + 195 * 300) - 1
    assert rates["reference_count"] == 2
    assert rates["krx_count"] == 2
