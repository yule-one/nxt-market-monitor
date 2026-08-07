from __future__ import annotations

from collections.abc import Iterable


PRICE_POINT_FIELDS = (
    ("시가", "open_price"),
    ("고가", "high_price"),
    ("저가", "low_price"),
    ("종가", "current_price"),
)


def stock_tick_size(price: int, market: str = "") -> int:
    """NXT 정규시장 주권에 적용되는 호가가격단위를 반환합니다."""

    del market  # NXT 정규시장에서는 원 상장시장과 무관하게 같은 구간을 적용합니다.
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def calculate_stock_price_limits(
    reference_price: int | None,
    market: str,
) -> tuple[int | None, int | None]:
    """기준가격의 ±30% 가격제한폭과 최종 상·하한가를 호가단위로 절사합니다."""

    if reference_price is None or reference_price <= 0:
        return None, None
    tick = stock_tick_size(reference_price, market)
    limit_width = ((reference_price * 30 // 100) // tick) * tick
    upper_price = reference_price + limit_width
    lower_price = reference_price - limit_width
    upper_tick = stock_tick_size(upper_price, market)
    lower_tick = stock_tick_size(lower_price, market)
    return (
        (upper_price // upper_tick) * upper_tick,
        (lower_price // lower_tick) * lower_tick,
    )


def reached_limit_points(
    *,
    open_price: int | None,
    high_price: int | None,
    low_price: int | None,
    close_price: int | None,
    limit_price: int | None,
) -> tuple[str, ...]:
    """OHLC 중 지정한 상·하한가와 정확히 일치한 가격 항목을 반환합니다."""

    if limit_price is None or limit_price <= 0:
        return ()
    values = (
        ("시가", open_price),
        ("고가", high_price),
        ("저가", low_price),
        ("종가", close_price),
    )
    return tuple(
        label for label, value in values if value is not None and value == limit_price
    )


def first_limit_hit_time(
    minute_prices: Iterable[
        tuple[
            str,
            int | None,
            int | None,
            int | None,
            int | None,
        ]
    ],
    limit_price: int | None,
) -> str | None:
    """분봉 OHLC에서 지정 가격에 최초 도달한 HHMMSS를 반환합니다."""

    if limit_price is None or limit_price <= 0:
        return None
    for trade_time, open_price, high_price, low_price, close_price in sorted(
        minute_prices,
        key=lambda item: item[0],
    ):
        if limit_price in (open_price, high_price, low_price, close_price):
            return trade_time
    return None


def format_limit_hit_time(trade_time: str | None) -> str:
    """저장된 최초 도달시각을 화면용 `HH:MM`으로 변환합니다."""

    if trade_time == "OPEN":
        return "시가"
    raw = str(trade_time or "").strip()
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[:2]}:{raw[2:4]}"
    return "-"
