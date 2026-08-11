from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from src.http import ResilientSession
from src.nxt_client import normalize_stock_code


KRX_INDEX_BASE_URL = "https://index.krx.co.kr"
KRX_INDEX_DATA_PATH = "/contents/IDX/99/IDX99000001.jspx"
KRX_INDEX_OTP_PATH = "/contents/COM/GenerateOTP.jspx"
KRX_INDEX_CONSTITUENT_BLD = "/IDX/03/0304/03040101/mkd03040101T3_01"
KRX_INDEX_CHANGE_BLD = "/IDX/03/0304/03040101/mkd03040101T3_03"
KRX_INDEX_PAGE_PATH = "/contents/MKD/03/0304/03040101/MKD03040101T3.jsp"


@dataclass(frozen=True)
class KrxIndexSpec:
    name: str
    market_code: str
    index_code: str
    index_id: str
    upper_menu_code: str

    @property
    def page_url(self) -> str:
        combined_code = f"{self.market_code}{self.index_code}"
        return (
            f"{KRX_INDEX_BASE_URL}{KRX_INDEX_PAGE_PATH}"
            f"?upmidCd={self.upper_menu_code}&idxCd={combined_code}"
            f"&idxId={self.index_id}"
        )


KRX_INDEX_SPECS: dict[str, KrxIndexSpec] = {
    "KOSPI200": KrxIndexSpec("KOSPI200", "1", "028", "K2G01P", "0102"),
    "KOSDAQ150": KrxIndexSpec("KOSDAQ150", "2", "203", "Q5G01P", "0103"),
}


@dataclass(frozen=True)
class KrxIndexConstituent:
    index_name: str
    stock_code: str
    stock_name: str


@dataclass(frozen=True)
class KrxIndexConstituentChange:
    index_name: str
    effective_date: date
    added_code: str
    added_name: str
    excluded_code: str
    excluded_name: str


class KrxIndexConstituentError(RuntimeError):
    pass


def _stock_code_from_isin(raw: object) -> str:
    value = str(raw or "").strip().upper()
    if len(value) >= 9 and value.startswith("KR7"):
        return normalize_stock_code(value[3:9])
    return normalize_stock_code(value)


def _date_chunks(start_date: date, end_date: date) -> Iterable[tuple[date, date]]:
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=364), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


class KrxIndexConstituentClient:
    """KRX 지수 사이트의 조회일자별 구성종목과 변경내역을 조회합니다."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        base_url: str = KRX_INDEX_BASE_URL,
    ) -> None:
        self.session = session or ResilientSession(referer=f"{base_url}/")
        self.base_url = base_url.rstrip("/")
        self._initialized_pages: set[str] = set()

    @property
    def ssl_fallback_used(self) -> bool:
        return bool(getattr(self.session, "ssl_fallback_used", False))

    def fetch_constituents(
        self,
        index_name: str,
        trading_date: date,
    ) -> list[KrxIndexConstituent]:
        spec = self._spec(index_name)
        rows = self._fetch_rows(
            spec,
            KRX_INDEX_CONSTITUENT_BLD,
            {
                "compst_isu_tp": "1",
                "schdate": trading_date.strftime("%Y%m%d"),
                "fromdate": trading_date.strftime("%Y%m%d"),
                "todate": trading_date.strftime("%Y%m%d"),
            },
        )
        result: list[KrxIndexConstituent] = []
        for raw in rows:
            stock_code = normalize_stock_code(raw.get("isu_cd"))
            if not stock_code:
                continue
            result.append(
                KrxIndexConstituent(
                    index_name=index_name,
                    stock_code=stock_code,
                    stock_name=str(raw.get("isu_nm") or stock_code).strip(),
                )
            )
        if not result:
            raise KrxIndexConstituentError(
                f"{trading_date:%Y-%m-%d} {index_name} 구성종목이 없습니다."
            )
        return result

    def fetch_changes(
        self,
        index_name: str,
        start_date: date,
        end_date: date,
    ) -> list[KrxIndexConstituentChange]:
        if end_date < start_date:
            return []
        spec = self._spec(index_name)
        changes: dict[
            tuple[date, str, str], KrxIndexConstituentChange
        ] = {}
        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            rows = self._fetch_rows(
                spec,
                KRX_INDEX_CHANGE_BLD,
                {
                    "compst_isu_tp": "2",
                    "schdate": chunk_end.strftime("%Y%m%d"),
                    "fromdate": chunk_start.strftime("%Y%m%d"),
                    "todate": chunk_end.strftime("%Y%m%d"),
                },
            )
            for raw in rows:
                raw_date = str(raw.get("appl_dd") or "").strip()
                try:
                    effective_date = date.fromisoformat(raw_date.replace("/", "-"))
                except ValueError:
                    continue
                added_code = _stock_code_from_isin(raw.get("transschl_isu_cd"))
                excluded_code = _stock_code_from_isin(raw.get("excld_isu_cd"))
                if not added_code and not excluded_code:
                    continue
                item = KrxIndexConstituentChange(
                    index_name=index_name,
                    effective_date=effective_date,
                    added_code=added_code,
                    added_name=str(raw.get("transschl_isu_nm") or "").strip(),
                    excluded_code=excluded_code,
                    excluded_name=str(raw.get("excld_isu_nm") or "").strip(),
                )
                changes[(effective_date, added_code, excluded_code)] = item
        return sorted(
            changes.values(),
            key=lambda item: (
                item.effective_date,
                item.added_code,
                item.excluded_code,
            ),
        )

    def build_daily_history(
        self,
        index_name: str,
        target_dates: Sequence[date],
        *,
        anchor_date: date | None = None,
    ) -> dict[date, tuple[KrxIndexConstituent, ...]]:
        dates = sorted(set(target_dates))
        if not dates:
            return {}
        anchor = anchor_date or dates[-1]
        if anchor < dates[-1]:
            raise ValueError("기준일은 저장 대상의 마지막 일자보다 빠를 수 없습니다.")
        anchor_members = self.fetch_constituents(index_name, anchor)
        changes = self.fetch_changes(index_name, dates[0], anchor)
        return reconstruct_daily_constituents(
            index_name,
            anchor,
            anchor_members,
            changes,
            dates,
        )

    def _fetch_rows(
        self,
        spec: KrxIndexSpec,
        bld: str,
        extra_params: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        self._initialize_page(spec)
        try:
            otp_response = self.session.get(
                f"{self.base_url}{KRX_INDEX_OTP_PATH}",
                params={"name": "form", "bld": bld},
                headers={"Referer": spec.page_url, "X-Requested-With": "XMLHttpRequest"},
            )
            code = str(otp_response.text or "").strip()
            if not code:
                raise KrxIndexConstituentError("KRX 지수 조회용 OTP가 비어 있습니다.")
            payload = {
                "ind_tp_cd": spec.market_code,
                "idx_ind_cd": spec.index_code,
                "idx_id": spec.index_id,
                "lang": "ko",
                "pagePath": KRX_INDEX_PAGE_PATH,
                "code": code,
                **extra_params,
            }
            response = self.session.post(
                f"{self.base_url}{KRX_INDEX_DATA_PATH}",
                data=payload,
                headers={"Referer": spec.page_url, "X-Requested-With": "XMLHttpRequest"},
            )
            body = response.json()
        except KrxIndexConstituentError:
            raise
        except Exception as exc:
            raise KrxIndexConstituentError(
                f"KRX {spec.name} 구성종목 조회 실패: {exc}"
            ) from exc
        rows = body.get("output") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise KrxIndexConstituentError("KRX 지수 구성종목 응답 형식이 올바르지 않습니다.")
        return [item for item in rows if isinstance(item, dict)]

    def _initialize_page(self, spec: KrxIndexSpec) -> None:
        if spec.name in self._initialized_pages:
            return
        try:
            self.session.get(spec.page_url)
        except Exception as exc:
            raise KrxIndexConstituentError(
                f"KRX {spec.name} 구성종목 화면 연결 실패: {exc}"
            ) from exc
        self._initialized_pages.add(spec.name)

    @staticmethod
    def _spec(index_name: str) -> KrxIndexSpec:
        try:
            return KRX_INDEX_SPECS[index_name]
        except KeyError as exc:
            raise ValueError(f"지원하지 않는 KRX 지수입니다: {index_name}") from exc


def reconstruct_daily_constituents(
    index_name: str,
    anchor_date: date,
    anchor_members: Sequence[KrxIndexConstituent],
    changes: Sequence[KrxIndexConstituentChange],
    target_dates: Sequence[date],
) -> dict[date, tuple[KrxIndexConstituent, ...]]:
    """기준일 구성종목에서 변경내역을 역적용해 일자별 구성종목을 복원합니다."""

    dates = sorted(set(target_dates), reverse=True)
    if any(item > anchor_date for item in dates):
        raise ValueError("기준일 이후의 구성종목은 복원할 수 없습니다.")
    members = {
        item.stock_code: item.stock_name
        for item in anchor_members
        if item.stock_code
    }
    ordered_changes = sorted(
        (item for item in changes if item.effective_date <= anchor_date),
        key=lambda item: item.effective_date,
        reverse=True,
    )
    change_position = 0
    result: dict[date, tuple[KrxIndexConstituent, ...]] = {}
    for trading_date in dates:
        while (
            change_position < len(ordered_changes)
            and ordered_changes[change_position].effective_date > trading_date
        ):
            effective_date = ordered_changes[change_position].effective_date
            same_day: list[KrxIndexConstituentChange] = []
            while (
                change_position < len(ordered_changes)
                and ordered_changes[change_position].effective_date == effective_date
            ):
                same_day.append(ordered_changes[change_position])
                change_position += 1
            for change in same_day:
                if change.added_code:
                    members.pop(change.added_code, None)
            for change in same_day:
                if change.excluded_code:
                    members[change.excluded_code] = (
                        change.excluded_name or change.excluded_code
                    )
        result[trading_date] = tuple(
            KrxIndexConstituent(index_name, stock_code, stock_name)
            for stock_code, stock_name in sorted(members.items())
        )
    return dict(sorted(result.items()))


def build_index_constituent_histories(
    client: KrxIndexConstituentClient,
    target_dates_by_index: Mapping[str, Sequence[date]],
    *,
    anchor_date: date | None = None,
) -> dict[date, dict[str, tuple[KrxIndexConstituent, ...]]]:
    """지수별 저장 대상 날짜를 일자 중심의 DB 저장 형태로 합칩니다."""

    result: dict[date, dict[str, tuple[KrxIndexConstituent, ...]]] = {}
    for index_name, target_dates in target_dates_by_index.items():
        history = client.build_daily_history(
            index_name,
            target_dates,
            anchor_date=anchor_date,
        )
        for trading_date, constituents in history.items():
            result.setdefault(trading_date, {})[index_name] = constituents
    return dict(sorted(result.items()))
