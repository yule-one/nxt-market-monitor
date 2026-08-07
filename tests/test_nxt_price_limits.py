from src.nxt_price_limits import (
    calculate_stock_price_limits,
    first_limit_hit_time,
    format_limit_hit_time,
    reached_limit_points,
    stock_tick_size,
)


def test_stock_tick_size_uses_nxt_price_bands() -> None:
    assert stock_tick_size(999, "KOSPI") == 1
    assert stock_tick_size(1_500, "KOSPI") == 1
    assert stock_tick_size(42_000, "KOSPI") == 50
    assert stock_tick_size(150_000, "KOSPI") == 100
    assert stock_tick_size(150_000, "KOSDAQ") == 100
    assert stock_tick_size(600_000, "KOSPI") == 1_000
    assert stock_tick_size(600_000, "KOSDAQ") == 1_000


def test_price_limit_calculation_matches_krx_rounding_example() -> None:
    assert calculate_stock_price_limits(9_940, "KOSDAQ") == (12_920, 6_960)
    assert calculate_stock_price_limits(42_000, "KOSDAQ") == (54_600, 29_400)
    assert calculate_stock_price_limits(165_100, "KOSDAQ") == (214_500, 115_600)


def test_reached_limit_points_checks_each_ohlc_value() -> None:
    assert reached_limit_points(
        open_price=13_000,
        high_price=13_000,
        low_price=12_000,
        close_price=13_000,
        limit_price=13_000,
    ) == ("시가", "고가", "종가")
    assert reached_limit_points(
        open_price=None,
        high_price=None,
        low_price=None,
        close_price=None,
        limit_price=13_000,
    ) == ()


def test_first_limit_hit_time_uses_earliest_matching_minute() -> None:
    minute_prices = [
        ("091500", 12_900, 13_000, 12_800, 12_950),
        ("081000", 12_800, 13_000, 12_700, 12_900),
        ("080000", 12_500, 12_900, 12_400, 12_800),
    ]

    assert first_limit_hit_time(minute_prices, 13_000) == "081000"
    assert first_limit_hit_time(minute_prices, 15_000) is None
    assert first_limit_hit_time(minute_prices, None) is None


def test_format_limit_hit_time_distinguishes_open_and_clock_time() -> None:
    assert format_limit_hit_time("OPEN") == "시가"
    assert format_limit_hit_time("081000") == "08:10"
    assert format_limit_hit_time(None) == "-"
