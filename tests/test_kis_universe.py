from __future__ import annotations

from datetime import date, datetime

from src.kis_master import KisMasterSecurity
from src.kis_rest import RestQuote
from src.kis_universe import (
    KisNxtUniverseResolver,
    KisNxtUniverseStore,
    master_unavailable_reason,
    select_websocket_anchor_symbols,
)
from src.models import NxtTradingStatus


def _master(
    symbol: str,
    *,
    market: str = "KOSPI",
    warning: str = "00",
) -> KisMasterSecurity:
    return KisMasterSecurity(
        snapshot_market=market,
        symbol=symbol,
        isin=f"KR7{symbol}0000",
        name=f"종목{symbol}",
        security_group="ST",
        reference_price=10_000,
        listed_shares=1_000_000,
        trading_halt=False,
        liquidation=False,
        management=False,
        market_warning=warning,
        warning_preannouncement=False,
        short_term_overheat="0",
        investment_caution=False,
    )


def _quote(symbol: str, price: int) -> RestQuote:
    return RestQuote(
        market="NXT",
        symbol=symbol,
        name=f"종목{symbol}",
        current_price=price,
        reference_price=price or None,
        cumulative_volume=100 if price else 0,
        cumulative_amount=1_000_000 if price else 0,
        updated_at=datetime(2026, 8, 12),
        upper_limit_price=13_000 if price else None,
        lower_limit_price=7_000 if price else None,
    )


class FakeClient:
    session = object()

    def fetch_quotes(self, pairs: list[tuple[str, str]]) -> list[RestQuote]:
        return [
            _quote(symbol, 10_000 if symbol == "005930" else 0)
            for _market, symbol in pairs
        ]


def test_resolver_combines_valid_kis_quotes_with_restricted_previous_symbol(
    tmp_path: object,
    monkeypatch: object,
) -> None:
    master_rows = [
        _master("005930"),
        _master("437730", market="KOSDAQ", warning="02"),
        _master("111111"),
    ]
    monkeypatch.setattr(
        "src.kis_universe.fetch_master_securities",
        lambda _session: master_rows,
    )
    previous = [
        NxtTradingStatus(
            status_date=date(2026, 8, 11),
            stock_code=code,
            stock_name=code,
            market="KOSDAQ",
            tradable_market="전체",
            unavailable_reason="",
        )
        for code in ("437730", "111111")
    ]
    store = KisNxtUniverseStore(tmp_path / "universe.db")
    resolver = KisNxtUniverseResolver(
        FakeClient(),
        store,
        previous_status_loader=lambda _date: (date(2026, 8, 11), previous),
    )

    rows = resolver.refresh(date(2026, 8, 12))

    assert {item.stock_code for item in rows} == {"005930", "437730"}
    restricted = next(item for item in rows if item.stock_code == "437730")
    assert restricted.tradable_market == "거래불가"
    assert restricted.unavailable_reason == "투자경고/위험"
    assert len(store.load_universe(date(2026, 8, 12))) == 2


def test_master_reason_does_not_treat_warning_notice_as_unavailable() -> None:
    item = _master("005930", warning="01")

    assert master_unavailable_reason(item) == ""


def test_websocket_anchor_selection_is_limited_to_twenty() -> None:
    rows = [
        NxtTradingStatus(
            status_date=date(2026, 8, 12),
            stock_code=f"{index:06d}",
            stock_name=str(index),
            market="KOSPI",
            tradable_market="전체",
            unavailable_reason="",
            cumulative_amount=index,
        )
        for index in range(30)
    ]

    anchors = select_websocket_anchor_symbols(rows)

    assert len(anchors) == 20
    assert anchors[0].stock_code == "000029"
