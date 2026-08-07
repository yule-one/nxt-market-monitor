from __future__ import annotations

from io import BytesIO
from typing import Protocol
from zipfile import BadZipFile, ZipFile


KIS_MASTER_BASE_URL = "https://new.real.download.dws.co.kr/common/master"
LISTED_SHARES_UNIT = 1_000

# The widths and listed-share field positions follow the official KIS master-file
# parsers. The final field in each record is one character shorter than the
# official text-mode slice because splitlines() removes the newline.
KOSPI_FIELD_WIDTHS = (
    2, 1, 4, 4, 4,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 9, 5, 5, 1,
    1, 1, 2, 1, 1,
    1, 2, 2, 2, 3,
    1, 3, 12, 12, 8,
    15, 21, 2, 7, 1,
    1, 1, 1, 1, 9,
    9, 9, 5, 9, 8,
    9, 3, 1, 1, 1,
)
KOSDAQ_FIELD_WIDTHS = (
    2, 1, 4, 4, 4,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 9, 5, 5, 1,
    1, 1, 2, 1, 1,
    1, 2, 2, 2, 3,
    1, 3, 12, 12, 8,
    15, 21, 2, 7, 1,
    1, 1, 1, 9, 9,
    9, 5, 9, 8, 9,
    3, 1, 1, 1,
)
MASTER_SPECS = {
    "KOSPI": ("kospi_code.mst", KOSPI_FIELD_WIDTHS, 50),
    "KOSDAQ": ("kosdaq_code.mst", KOSDAQ_FIELD_WIDTHS, 45),
}


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: object) -> object: ...


class KisMasterError(RuntimeError):
    pass


def parse_listed_shares(archive: bytes, market: str) -> dict[str, int]:
    """Parse actual listed shares from an official KIS master-file archive."""
    try:
        member_name, widths, listed_shares_index = MASTER_SPECS[market]
    except KeyError as exc:
        raise ValueError(f"Unsupported KIS master market: {market}") from exc

    try:
        with ZipFile(BytesIO(archive)) as zipped:
            records = zipped.read(member_name).decode("cp949").splitlines()
    except (BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise KisMasterError(f"Invalid {market} KIS master file") from exc

    fixed_width = sum(widths)
    field_start = sum(widths[:listed_shares_index])
    field_end = field_start + widths[listed_shares_index]
    result: dict[str, int] = {}
    for record in records:
        if len(record) < fixed_width + 9:
            continue
        symbol = record[:-fixed_width][:9].strip()
        if len(symbol) != 6 or not symbol.isalnum():
            continue
        raw_shares = record[-fixed_width:][field_start:field_end].strip()
        try:
            shares_in_thousands = int(raw_shares or "0")
        except ValueError:
            continue
        if shares_in_thousands > 0:
            result[symbol] = shares_in_thousands * LISTED_SHARES_UNIT
    return result


def fetch_listed_shares(session: HttpSession) -> dict[str, int]:
    result: dict[str, int] = {}
    for market, (member_name, _widths, _index) in MASTER_SPECS.items():
        response = session.get(
            f"{KIS_MASTER_BASE_URL}/{member_name}.zip",
            timeout=30,
        )
        archive = getattr(response, "content", b"")
        if not isinstance(archive, bytes) or not archive:
            raise KisMasterError(f"Empty {market} KIS master response")
        result.update(parse_listed_shares(archive, market))
    return result
