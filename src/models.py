from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class NxtChange:
    change_date: date
    stock_code: str
    stock_name: str
    market: str
    change_type: str
    reason: str
    isin: str = ""
    registered_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["change_date"] = self.change_date.isoformat()
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NxtChange":
        values = dict(raw)
        values["change_date"] = date.fromisoformat(str(values["change_date"]))
        return cls(**values)


@dataclass(frozen=True)
class NxtTradingStatus:
    status_date: date
    stock_code: str
    stock_name: str
    market: str
    tradable_market: str
    unavailable_reason: str
    isin: str = ""
    reference_price: int | None = None
    current_price: int | None = None
    change_value: int | None = None
    change_rate: float | None = None
    cumulative_volume: int = 0
    cumulative_amount: int = 0
    quote_time: str = ""
    open_price: int | None = None
    high_price: int | None = None
    low_price: int | None = None
    upper_limit_price: int | None = None
    lower_limit_price: int | None = None

    @property
    def is_unavailable(self) -> bool:
        return self.tradable_market == "거래불가"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status_date"] = self.status_date.isoformat()
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NxtTradingStatus":
        values = dict(raw)
        values["status_date"] = date.fromisoformat(str(values["status_date"]))
        return cls(**values)


@dataclass(frozen=True)
class Disclosure:
    disclosed_at: datetime
    stock_code: str
    stock_name: str
    market: str
    category: str
    title: str
    submitter: str
    report_no: str
    viewer_url: str
    internal_code: str = ""

    @property
    def disclosure_date(self) -> date:
        return self.disclosed_at.date()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["disclosed_at"] = self.disclosed_at.isoformat()
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Disclosure":
        values = dict(raw)
        values["disclosed_at"] = datetime.fromisoformat(str(values["disclosed_at"]))
        return cls(**values)


@dataclass(frozen=True)
class NxtStockState:
    stock_code: str
    stock_name: str
    market: str
    is_tradable: bool
    is_temporary_exclusion: bool
    last_change_date: date
    last_change_type: str
    last_reason: str

    @property
    def membership_label(self) -> str:
        if self.is_tradable:
            return "매매가능"
        return f"편출 ({self.last_change_date:%Y-%m-%d})"


@dataclass(frozen=True)
class StatusPeriod:
    category: str
    stock_code: str
    stock_name: str
    market: str
    published_date: date
    start_date: date
    end_date: date | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["published_date"] = self.published_date.isoformat()
        result["start_date"] = self.start_date.isoformat()
        result["end_date"] = self.end_date.isoformat() if self.end_date else None
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StatusPeriod":
        values = dict(raw)
        values["published_date"] = date.fromisoformat(str(values["published_date"]))
        values["start_date"] = date.fromisoformat(str(values["start_date"]))
        values["end_date"] = (
            date.fromisoformat(str(values["end_date"])) if values.get("end_date") else None
        )
        return cls(**values)
