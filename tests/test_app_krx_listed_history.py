from __future__ import annotations

from datetime import date
from io import BytesIO

from openpyxl import load_workbook

import app
from src.krx_openapi import KrxListedSecurityDaily


def test_krx_listed_frame_and_excel_include_requested_columns() -> None:
    trading_date = date(2026, 8, 11)
    frame = app._krx_listed_daily_frame(
        [
            KrxListedSecurityDaily(
                trade_date=trading_date,
                standard_code="KR7005930003",
                short_code="005930",
                stock_name="삼성전자",
                market="KOSPI",
                stock_type="보통주",
                security_type="주권",
                listed_shares=5_969_782_550,
                listing_date=date(1975, 6, 11),
                cumulative_volume=12_345_678,
                cumulative_amount=876_543_210_000,
                is_kospi200=True,
            )
        ]
    )

    assert frame.columns.tolist() == [
        "표준코드",
        "단축코드",
        "종목명",
        "시장구분",
        "주식종류",
        "증권구분",
        "상장주식수",
        "상장일",
        "KRX 거래량",
        "KRX 거래대금",
        "K200",
        "Q150",
    ]
    assert frame.loc[0, "K200"] == "Y"
    assert frame.loc[0, "Q150"] == "-"

    workbook = load_workbook(
        BytesIO(app._krx_listed_excel_bytes(frame, trading_date)),
    )
    worksheet = workbook[f"KRX_{trading_date:%Y%m%d}"]
    assert [cell.value for cell in next(worksheet.iter_rows(max_row=1))] == list(
        frame.columns
    )
    assert worksheet.freeze_panes == "A2"
