from __future__ import annotations

from datetime import date

from src.krx_index_constituents import (
    KrxIndexConstituent,
    KrxIndexConstituentChange,
    KrxIndexConstituentClient,
    reconstruct_daily_constituents,
)


class FakeResponse:
    def __init__(self, *, text: str = "", payload: dict | None = None) -> None:
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeSession:
    ssl_fallback_used = False

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        if url.endswith("GenerateOTP.jspx"):
            return FakeResponse(text="test-otp")
        return FakeResponse(text="page")

    def post(self, _url: str, **kwargs) -> FakeResponse:
        data = kwargs["data"]
        self.posts.append(data)
        if data["compst_isu_tp"] == "1":
            return FakeResponse(
                payload={
                    "output": [
                        {"isu_cd": "005930", "isu_nm": "삼성전자"},
                        {"isu_cd": "000660", "isu_nm": "SK하이닉스"},
                    ]
                }
            )
        return FakeResponse(
            payload={
                "output": [
                    {
                        "appl_dd": "2025/06/13",
                        "transschl_isu_cd": "KR7000660001",
                        "transschl_isu_nm": "신규",
                        "excld_isu_cd": "KR7000550004",
                        "excld_isu_nm": "제외",
                    }
                ]
            }
        )


def test_client_reads_constituents_and_isin_changes() -> None:
    session = FakeSession()
    client = KrxIndexConstituentClient(session=session)

    members = client.fetch_constituents("KOSPI200", date(2026, 8, 10))
    changes = client.fetch_changes(
        "KOSPI200",
        date(2025, 3, 4),
        date(2025, 12, 31),
    )

    assert [item.stock_code for item in members] == ["005930", "000660"]
    assert changes[0].added_code == "000660"
    assert changes[0].excluded_code == "000550"
    assert session.posts[0]["idx_ind_cd"] == "028"


def test_reconstruct_daily_constituents_reverses_changes_before_effective_date() -> None:
    anchor_members = [
        KrxIndexConstituent("KOSPI200", "000002", "신규종목"),
        KrxIndexConstituent("KOSPI200", "000003", "유지종목"),
    ]
    changes = [
        KrxIndexConstituentChange(
            "KOSPI200",
            date(2025, 6, 13),
            "000002",
            "신규종목",
            "000001",
            "제외종목",
        )
    ]

    history = reconstruct_daily_constituents(
        "KOSPI200",
        date(2025, 6, 13),
        anchor_members,
        changes,
        [date(2025, 6, 12), date(2025, 6, 13)],
    )

    assert {item.stock_code for item in history[date(2025, 6, 13)]} == {
        "000002",
        "000003",
    }
    assert {item.stock_code for item in history[date(2025, 6, 12)]} == {
        "000001",
        "000003",
    }
