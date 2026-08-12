from __future__ import annotations

from dataclasses import dataclass
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

# 필요한 필드의 위치는 KIS가 공개한 코스피·코스닥 종목 마스터 구조를
# 따른다. 시장별 레코드 구성이 달라 인덱스를 따로 관리한다.
MASTER_FIELD_INDEXES = {
    "KOSPI": {
        "security_group": 0,
        "short_term_overheat": 22,
        "reference_price": 31,
        "trading_halt": 34,
        "liquidation": 35,
        "management": 36,
        "market_warning": 37,
        "warning_preannouncement": 38,
        "listed_shares": 50,
    },
    "KOSDAQ": {
        "security_group": 0,
        "short_term_overheat": 17,
        "investment_caution": 20,
        "reference_price": 26,
        "trading_halt": 29,
        "liquidation": 30,
        "management": 31,
        "market_warning": 32,
        "warning_preannouncement": 33,
        "listed_shares": 45,
    },
}


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: object) -> object: ...


class KisMasterError(RuntimeError):
    pass


@dataclass(frozen=True)
class KisMasterSecurity:
    snapshot_market: str
    symbol: str
    isin: str
    name: str
    security_group: str
    reference_price: int | None
    listed_shares: int | None
    trading_halt: bool
    liquidation: bool
    management: bool
    market_warning: str
    warning_preannouncement: bool
    short_term_overheat: str
    investment_caution: bool

    @property
    def is_common_stock_candidate(self) -> bool:
        return (
            self.security_group == "ST"
            and len(self.symbol) == 6
            and self.symbol.isalnum()
        )


def _integer(value: str) -> int | None:
    try:
        parsed = int(value.strip() or "0")
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _yes(value: str) -> bool:
    return value.strip().upper() == "Y"


def _archive_records(archive: bytes, market: str) -> tuple[list[str], tuple[int, ...]]:
    try:
        member_name, widths, _listed_shares_index = MASTER_SPECS[market]
    except KeyError as exc:
        raise ValueError(f"Unsupported KIS master market: {market}") from exc

    try:
        with ZipFile(BytesIO(archive)) as zipped:
            records = zipped.read(member_name).decode("cp949").splitlines()
    except (BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise KisMasterError(f"Invalid {market} KIS master file") from exc
    return records, widths


def parse_master_securities(
    archive: bytes,
    market: str,
) -> list[KisMasterSecurity]:
    """Parse security identity, reference price, shares, and market flags."""

    records, widths = _archive_records(archive, market)
    indexes = MASTER_FIELD_INDEXES[market]
    fixed_width = sum(widths)
    parsed: list[KisMasterSecurity] = []
    for record in records:
        if len(record) < fixed_width + 21:
            continue
        head = record[:-fixed_width]
        symbol = head[:9].strip()
        isin = head[9:21].strip()
        name = head[21:].strip()
        tail = record[-fixed_width:]
        fields: list[str] = []
        position = 0
        for width in widths:
            fields.append(tail[position : position + width].strip())
            position += width

        listed_shares_thousands = _integer(fields[indexes["listed_shares"]])
        parsed.append(
            KisMasterSecurity(
                snapshot_market=market,
                symbol=symbol,
                isin=isin,
                name=name,
                security_group=fields[indexes["security_group"]],
                reference_price=_integer(fields[indexes["reference_price"]]),
                listed_shares=(
                    listed_shares_thousands * LISTED_SHARES_UNIT
                    if listed_shares_thousands is not None
                    else None
                ),
                trading_halt=_yes(fields[indexes["trading_halt"]]),
                liquidation=_yes(fields[indexes["liquidation"]]),
                management=_yes(fields[indexes["management"]]),
                market_warning=fields[indexes["market_warning"]],
                warning_preannouncement=_yes(
                    fields[indexes["warning_preannouncement"]]
                ),
                short_term_overheat=fields[indexes["short_term_overheat"]],
                investment_caution=(
                    _yes(fields[indexes["investment_caution"]])
                    if "investment_caution" in indexes
                    else False
                ),
            )
        )
    return parsed


def parse_listed_shares(archive: bytes, market: str) -> dict[str, int]:
    """Parse actual listed shares from an official KIS master-file archive."""
    return {
        item.symbol: item.listed_shares
        for item in parse_master_securities(archive, market)
        if len(item.symbol) == 6
        and item.symbol.isalnum()
        and item.listed_shares is not None
    }


def fetch_master_securities(session: HttpSession) -> list[KisMasterSecurity]:
    result: list[KisMasterSecurity] = []
    for market, (member_name, _widths, _index) in MASTER_SPECS.items():
        response = session.get(
            f"{KIS_MASTER_BASE_URL}/{member_name}.zip",
            timeout=30,
        )
        archive = getattr(response, "content", b"")
        if not isinstance(archive, bytes) or not archive:
            raise KisMasterError(f"Empty {market} KIS master response")
        result.extend(parse_master_securities(archive, market))
    return result


def fetch_listed_shares(session: HttpSession) -> dict[str, int]:
    return {
        item.symbol: item.listed_shares
        for item in fetch_master_securities(session)
        if len(item.symbol) == 6
        and item.symbol.isalnum()
        and item.listed_shares is not None
    }
