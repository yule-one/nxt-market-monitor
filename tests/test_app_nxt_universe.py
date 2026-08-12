from __future__ import annotations

from datetime import date

import requests

import app
from src.models import NxtTradingStatus


def test_latest_nxt_universe_falls_back_to_stored_statuses(
    monkeypatch: object,
) -> None:
    stored_date = date(2026, 8, 11)
    stored_statuses = [
        NxtTradingStatus(
            status_date=stored_date,
            stock_code="005930",
            stock_name="삼성전자",
            market="KOSPI",
            tradable_market="NXT",
            unavailable_reason="",
        )
    ]

    def fail_current_request(_status_date: date) -> object:
        raise requests.ConnectionError("temporary failure")

    class StoredHistory:
        def load_latest_nxt_statuses(
            self,
            _on_or_before: date,
        ) -> tuple[date, list[NxtTradingStatus]]:
            return stored_date, stored_statuses

    monkeypatch.setattr(app, "_nxt_universe_for_date", fail_current_request)  # type: ignore[attr-defined]
    monkeypatch.setattr(app, "get_historical_market_store", StoredHistory)  # type: ignore[attr-defined]

    (
        resolved_date,
        symbols,
        markets,
        tradable_markets,
        unavailable_reasons,
        warning,
    ) = app._latest_nxt_universe()

    assert resolved_date == stored_date
    assert [item.symbol for item in symbols] == ["005930"]
    assert markets == {"005930": "KOSPI"}
    assert tradable_markets == {"005930": "NXT"}
    assert unavailable_reasons == {"005930": ""}
    assert "저장된 종목 목록" in warning
