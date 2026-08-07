from __future__ import annotations

from datetime import date

from src.krx_openapi import KrxOpenApiClient
from src.market_realtime import KRX


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def get(self, source: str, key: str, _max_age: object = None) -> object | None:
        return self.values.get((source, key))

    def set(self, source: str, key: str, value: object) -> None:
        self.values[(source, key)] = value


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if "fut_bydd_trd" in url:
            return FakeResponse(
                {
                    "OutBlock_1": [
                        {
                            "PROD_NM": "코스피200 선물",
                            "MKT_NM": "야간",
                            "ISU_CD": "101S9000",
                            "ISU_NM": "코스피200 F 202609 (야간)",
                            "TDD_CLSPRC": "990.00",
                            "CMPPREVDD_PRC": "-10.00",
                            "SETL_PRC": "990.00",
                            "ACC_TRDVOL": "10",
                            "ACC_TRDVAL": "20",
                            "ACC_OPNINT_QTY": "30",
                        },
                        {
                            "PROD_NM": "코스피200 선물",
                            "MKT_NM": "정규",
                            "ISU_CD": "101S9000",
                            "ISU_NM": "코스피200 F 202609 (주간)",
                            "TDD_CLSPRC": "1,025.10",
                            "CMPPREVDD_PRC": "-16.95",
                            "SETL_PRC": "1025.20",
                            "ACC_TRDVOL": "123456",
                            "ACC_TRDVAL": "789012345",
                            "ACC_OPNINT_QTY": "456789",
                        },
                        {
                            "PROD_NM": "코스피200 선물",
                            "MKT_NM": "정규",
                            "ISU_CD": "101SC000",
                            "ISU_NM": "코스피200 F 202612 (주간)",
                            "TDD_CLSPRC": "1,020.00",
                            "CMPPREVDD_PRC": "-17.00",
                            "SETL_PRC": "1020.00",
                            "ACC_TRDVOL": "100",
                            "ACC_TRDVAL": "200",
                            "ACC_OPNINT_QTY": "300",
                        },
                    ]
                }
            )
        if "stk_bydd_trd" in url:
            return FakeResponse(
                {
                    "OutBlock_1": [
                        {
                            "ISU_CD": "005930",
                            "ISU_NM": "삼성전자",
                            "TDD_CLSPRC": "71,000",
                            "CMPPREVDD_PRC": "1,000",
                            "ACC_TRDVOL": "123456",
                            "ACC_TRDVAL": "8765432100",
                            "MKTCAP": "420000000000000",
                            "LIST_SHRS": "5969782550",
                        }
                    ]
                }
            )
        if "ksq_bydd_trd" in url:
            return FakeResponse({"OutBlock_1": []})
        if "kospi_dd_trd" in url:
            return FakeResponse(
                {
                    "OutBlock_1": [
                        {
                            "IDX_CLSS": "KOSPI",
                            "IDX_NM": "코스피",
                            "CLSPRC_IDX": "2800.12",
                            "CMPPREVDD_IDX": "12.34",
                            "FLUC_RT": "0.44",
                            "ACC_TRDVOL": "123456789",
                            "ACC_TRDVAL": "9876543210000",
                        },
                        {
                            "IDX_CLSS": "KOSPI200",
                            "IDX_NM": "코스피 200",
                            "CLSPRC_IDX": "380.11",
                            "CMPPREVDD_IDX": "1.20",
                            "FLUC_RT": "0.32",
                            "ACC_TRDVOL": "100",
                            "ACC_TRDVAL": "200",
                        },
                    ]
                }
            )
        if "krx_dd_trd" in url:
            return FakeResponse(
                {
                    "OutBlock_1": [
                        {
                            "IDX_CLSS": "KRX",
                            "IDX_NM": "KRX TMI",
                            "CLSPRC_IDX": "3916.03",
                            "CMPPREVDD_IDX": "-199.10",
                            "FLUC_RT": "-4.84",
                            "ACC_TRDVOL": "0",
                            "ACC_TRDVAL": "0",
                        }
                    ]
                }
            )
        return FakeResponse(
            {
                "OutBlock_1": [
                    {
                        "IDX_CLSS": "KOSDAQ",
                        "IDX_NM": "코스닥",
                        "CLSPRC_IDX": "900.50",
                        "CMPPREVDD_IDX": "-2.50",
                        "FLUC_RT": "-0.28",
                        "ACC_TRDVOL": "333",
                        "ACC_TRDVAL": "444",
                    },
                    {
                        "IDX_CLSS": "KOSDAQ150",
                        "IDX_NM": "코스닥 150",
                        "CLSPRC_IDX": "1200.30",
                        "CMPPREVDD_IDX": "3.10",
                        "FLUC_RT": "0.26",
                        "ACC_TRDVOL": "555",
                        "ACC_TRDVAL": "666",
                    },
                ]
            }
        )


def test_krx_openapi_builds_stock_and_index_snapshot() -> None:
    session = FakeSession()
    client = KrxOpenApiClient(
        "secret-key",
        FakeCache(),  # type: ignore[arg-type]
        session=session,
        base_url="https://example.test",
    )
    snapshot = client.fetch_daily_snapshot(date(2026, 8, 5))

    stock = snapshot.stock_quotes[("005930", KRX)]
    assert stock.current_price == 71_000
    assert stock.reference_price == 70_000
    assert stock.cumulative_volume == 123_456
    assert stock.cumulative_amount == 8_765_432_100
    assert stock.market_cap == 420_000_000_000_000
    assert snapshot.listed_shares["005930"] == 5_969_782_550
    assert list(snapshot.index_quotes) == [
        "KRX TMI",
        "KOSPI",
        "KOSPI200",
        "KOSDAQ",
        "KOSDAQ150",
    ]
    assert snapshot.index_quotes["KRX TMI"].current_value == 3916.03
    assert snapshot.index_quotes["KOSPI"].current_value == 2800.12
    assert snapshot.index_quotes["KOSDAQ"].change_rate == -0.28
    future = snapshot.future_quotes["KOSPI200 선물"]
    assert future.code == "101S9000"
    assert future.current_value == 1025.10
    assert future.change_value == -16.95
    assert round(future.change_rate, 6) == round(-16.95 / 1042.05 * 100, 6)
    assert future.cumulative_volume == 123_456
    assert future.cumulative_amount == 789_012_345
    assert future.open_interest == 456_789
    assert future.settlement_price == 1025.20
    assert all(call[1]["headers"] == {"AUTH_KEY": "secret-key"} for call in session.calls)
    assert all(call[1]["params"] == {"basDd": "20260805"} for call in session.calls)


def test_krx_openapi_reuses_cached_rows() -> None:
    session = FakeSession()
    cache = FakeCache()
    client = KrxOpenApiClient(
        "secret-key",
        cache,  # type: ignore[arg-type]
        session=session,
        base_url="https://example.test",
    )
    client.fetch_index_quotes(date(2026, 8, 5))
    first_call_count = len(session.calls)
    client.fetch_index_quotes(date(2026, 8, 5))

    assert len(session.calls) == first_call_count
