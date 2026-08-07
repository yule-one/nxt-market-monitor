from datetime import datetime
from pathlib import Path

from src.kis_websocket import KRX_TRADE_TR_ID, NXT_TRADE_TR_ID, parse_trade_message
from src.market_realtime import (
    KRX,
    NXT,
    MarketAggregator,
    MarketDataStore,
    TradeTick,
    WatchSymbol,
)


def tick(
    market: str,
    at: str,
    cumulative_volume: int,
    cumulative_amount: int,
    symbol: str = "005930",
    price: int = 70_000,
    reference_price: int | None = 69_000,
) -> TradeTick:
    return TradeTick(
        market=market,
        symbol=symbol,
        traded_at=datetime.strptime(f"20260806{at}", "%Y%m%d%H%M%S"),
        price=price,
        trade_volume=1,
        cumulative_volume=cumulative_volume,
        cumulative_amount=cumulative_amount,
        reference_price=reference_price,
    )


def aggregator(tmp_path: Path) -> MarketAggregator:
    return MarketAggregator(MarketDataStore(tmp_path / "market.db"))


def row(aggregator: MarketAggregator) -> dict[str, object]:
    return aggregator.snapshot([WatchSymbol("005930", "삼성전자")])[0]


def test_nxt_daily_cumulative_values_are_split_by_session(tmp_path: Path) -> None:
    agg = aggregator(tmp_path)
    agg.ingest(tick(NXT, "080001", 100, 1_000_000))
    agg.ingest(tick(NXT, "084959", 150, 1_500_000))
    agg.ingest(tick(NXT, "090030", 170, 1_700_000))
    agg.ingest(tick(NXT, "151959", 300, 3_000_000))
    agg.ingest(tick(NXT, "154000", 325, 3_250_000))
    agg.ingest(tick(NXT, "195959", 400, 4_000_000))

    result = row(agg)
    assert result["nxt_pre_volume"] == 150
    assert result["nxt_main_volume"] == 150
    assert result["nxt_after_volume"] == 100
    assert result["nxt_total_volume"] == 400
    assert result["nxt_total_amount"] == 4_000_000
    assert result["집계상태"] == "정상"


def test_nxt_session_cumulative_reset_is_supported(tmp_path: Path) -> None:
    agg = aggregator(tmp_path)
    agg.ingest(tick(NXT, "080001", 100, 1_000))
    agg.ingest(tick(NXT, "090030", 20, 200))
    agg.ingest(tick(NXT, "154000", 5, 50))

    result = row(agg)
    assert result["nxt_pre_volume"] == 100
    assert result["nxt_main_volume"] == 20
    assert result["nxt_after_volume"] == 5
    assert result["nxt_total_volume"] == 125


def test_krx_opening_continuous_and_closing_are_split(tmp_path: Path) -> None:
    agg = aggregator(tmp_path)
    agg.ingest(tick(KRX, "090000", 500, 5_000_000))
    agg.ingest(tick(KRX, "090000", 550, 5_500_000))
    agg.ingest(tick(KRX, "100000", 800, 8_000_000))
    agg.ingest(tick(KRX, "153000", 900, 9_000_000))

    result = row(agg)
    assert result["krx_opening_call_volume"] == 550
    assert result["krx_continuous_volume"] == 250
    assert result["krx_closing_call_volume"] == 100
    assert result["krx_total_volume"] == 900
    assert result["krx_total_amount"] == 9_000_000


def test_mid_session_start_is_marked_partial_without_misallocation(tmp_path: Path) -> None:
    agg = aggregator(tmp_path)
    agg.ingest(tick(KRX, "100000", 1_000, 10_000_000))
    agg.ingest(tick(KRX, "100001", 1_100, 11_000_000))

    result = row(agg)
    assert result["krx_opening_call_volume"] == 0
    assert result["krx_continuous_volume"] == 100
    assert result["krx_total_volume"] == 1_100
    assert result["집계상태"] == "부분"


def test_metrics_and_stream_state_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "market.db"
    store = MarketDataStore(path)
    first = MarketAggregator(store)
    first.ingest(tick(NXT, "080001", 100, 1_000))
    first.flush()

    second = MarketAggregator(MarketDataStore(path))
    second.ingest(tick(NXT, "080002", 125, 1_250))
    result = row(second)
    assert result["nxt_pre_volume"] == 125
    assert result["nxt_pre_amount"] == 1_250


def test_snapshot_calculates_prices_and_krx_ratios(tmp_path: Path) -> None:
    agg = aggregator(tmp_path)
    agg.ingest(tick(NXT, "080001", 100, 7_100_000, price=71_000, reference_price=None))
    agg.ingest(tick(KRX, "090000", 200, 14_000_000, price=70_500, reference_price=70_000))

    result = row(agg)
    assert result["nxt_current_price"] == 71_000
    assert result["krx_current_price"] == 70_500
    assert result["reference_price"] == 70_000
    assert result["change_rate"] == 71_000 / 70_000 - 1
    assert result["disparity_rate"] == 71_000 / 70_500 - 1
    assert result["volume_ratio"] == 0.5
    assert result["amount_ratio"] == 7_100_000 / 14_000_000


def test_parse_kis_batch_trade_message() -> None:
    def raw_row(
        symbol: str,
        at: str,
        volume: str,
        amount: str,
        sign: str = "2",
        difference: str = "1000",
    ) -> list[str]:
        values = [""] * 46
        values[0] = symbol
        values[1] = at
        values[2] = "70000"
        values[3] = sign
        values[4] = difference
        values[12] = "10"
        values[13] = volume
        values[14] = amount
        values[33] = "20260806"
        return values

    payload = "^".join(raw_row("005930", "090000", "100", "1000") + raw_row("000660", "090001", "200", "2000"))
    ticks = parse_trade_message(f"0|{KRX_TRADE_TR_ID}|002|{payload}")
    assert [item.symbol for item in ticks] == ["005930", "000660"]
    assert ticks[0].market == KRX
    assert ticks[0].reference_price == 69_000
    assert ticks[1].cumulative_amount == 2_000

    nxt = parse_trade_message(
        f"0|{NXT_TRADE_TR_ID}|001|{'^'.join(raw_row('005930', '080001', '10', '100'))}"
    )
    assert len(nxt) == 1
    assert nxt[0].market == NXT

    lower = parse_trade_message(
        f"0|{NXT_TRADE_TR_ID}|001|{'^'.join(raw_row('005930', '080002', '20', '200', '5', '500'))}"
    )
    assert lower[0].reference_price == 70_500
