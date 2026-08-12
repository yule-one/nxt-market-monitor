from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from src.kis_master import (
    KOSPI_FIELD_WIDTHS,
    parse_listed_shares,
    parse_master_securities,
)


def _master_archive(symbol: str, shares_in_thousands: int) -> bytes:
    listed_shares_index = 50
    fixed_width = sum(KOSPI_FIELD_WIDTHS)
    field_start = sum(KOSPI_FIELD_WIDTHS[:listed_shares_index])
    field_width = KOSPI_FIELD_WIDTHS[listed_shares_index]
    tail = [" "] * fixed_width
    encoded_shares = f"{shares_in_thousands:0{field_width}d}"
    tail[field_start : field_start + field_width] = encoded_shares
    record = f"{symbol:<9}KR7000000000테스트종목" + "".join(tail) + "\n"

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as zipped:
        zipped.writestr("kospi_code.mst", record.encode("cp949"))
    return buffer.getvalue()


def test_parse_listed_shares_converts_thousands_to_actual_shares() -> None:
    result = parse_listed_shares(_master_archive("005930", 5_846_278), "KOSPI")

    assert result == {"005930": 5_846_278_000}


def test_parse_master_security_reads_identity_and_market_flags() -> None:
    archive = _master_archive("005930", 5_846_278)
    rows = parse_master_securities(archive, "KOSPI")

    assert len(rows) == 1
    assert rows[0].symbol == "005930"
    assert rows[0].isin == "KR7000000000"
    assert rows[0].name == "테스트종목"
    assert rows[0].listed_shares == 5_846_278_000
