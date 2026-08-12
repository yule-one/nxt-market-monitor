from __future__ import annotations

from datetime import date

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

    class StoredHistory:
        def load_latest_nxt_statuses(
            self,
            _on_or_before: date,
        ) -> tuple[date, list[NxtTradingStatus]]:
            return stored_date, stored_statuses

    class EmptyKisStore:
        def load_universe(self, _status_date: date) -> list[NxtTradingStatus]:
            return []

        def load_latest_universe(
            self,
            _on_or_before: date,
        ) -> tuple[None, list[NxtTradingStatus]]:
            return None, []

    monkeypatch.setattr(app, "get_historical_market_store", StoredHistory)  # type: ignore[attr-defined]
    monkeypatch.setattr(app, "get_kis_nxt_universe_store", EmptyKisStore)  # type: ignore[attr-defined]
    monkeypatch.setattr(app, "_secret_value", lambda _name: "")  # type: ignore[attr-defined]

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
    assert "확정 일별 DB" in warning
