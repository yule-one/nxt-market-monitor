from src.nxt_price_limits import (
    calculate_stock_price_limits,
    first_limit_proximity_time,
    first_limit_hit_time,
    format_limit_hit_time,
    limit_proximity_ticks,
    move_stock_price_ticks,
    reached_limit_proximity_points,
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


def test_move_stock_price_ticks_uses_each_crossed_price_band() -> None:
    assert move_stock_price_ticks(5_000, -3) == 4_985
    assert move_stock_price_ticks(5_000, 3) == 5_030
    assert move_stock_price_ticks(20_000, -3) == 19_970
    assert move_stock_price_ticks(20_000, 3) == 20_150


def test_limit_proximity_ticks_includes_zero_through_three_ticks() -> None:
    assert limit_proximity_ticks(5_000, 5_000, "상한가") == 0
    assert limit_proximity_ticks(4_997, 5_000, "상한가") == 1
    assert limit_proximity_ticks(4_986, 5_000, "상한가") == 3
    assert limit_proximity_ticks(4_984, 5_000, "상한가") is None
    assert limit_proximity_ticks(4_995, 4_995, "하한가") == 0
    assert limit_proximity_ticks(4_998, 4_995, "하한가") == 1
    assert limit_proximity_ticks(5_012, 4_995, "하한가") == 3
    assert limit_proximity_ticks(5_021, 4_995, "하한가") is None
    assert limit_proximity_ticks(float("nan"), 4_995, "하한가") is None
    assert limit_proximity_ticks(5_000, float("nan"), "상한가") is None


def test_reached_limit_proximity_points_checks_ohlc_range() -> None:
    assert reached_limit_proximity_points(
        open_price=4_970,
        high_price=4_995,
        low_price=4_960,
        close_price=4_990,
        limit_price=5_000,
        direction="상한가",
    ) == ("고가", "종가")


def test_first_limit_proximity_time_uses_earliest_matching_minute() -> None:
    minute_prices = [
        ("091500", 4_980, 4_995, 4_975, 4_990),
        ("081000", 4_970, 4_990, 4_960, 4_985),
        ("080000", 4_950, 4_970, 4_940, 4_960),
    ]

    assert (
        first_limit_proximity_time(minute_prices, 5_000, "상한가")
        == "081000"
    )


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
