import os
from datetime import date, timedelta

import pytest

from src.config import CATEGORY_CODES
from src.kind_client import KindClient
from src.nxt_client import NxtClient


pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_SOURCE_TESTS") != "1",
    reason="RUN_LIVE_SOURCE_TESTS=1일 때만 공식 사이트를 호출합니다.",
)
def test_official_sources_return_valid_shapes() -> None:
    end_date = date.today()
    start_date = end_date - timedelta(days=7)
    changes = NxtClient().fetch_changes(start_date, end_date, force_refresh=True)
    assert all(len(item.stock_code) == 6 for item in changes)
    assert all(item.change_type in {"편입", "편출"} for item in changes)

    statuses = NxtClient().fetch_trading_status(end_date, force_refresh=True)
    assert all(len(item.stock_code) == 6 for item in statuses)
    assert all(item.stock_code.isalnum() for item in statuses)
    assert all(item.tradable_market for item in statuses)
    assert len(statuses) == len({item.stock_code for item in statuses})

    first_category = next(iter(CATEGORY_CODES))
    disclosures = KindClient().fetch_disclosures(
        start_date,
        end_date,
        [first_category],
        force_refresh=True,
        max_workers=1,
    )
    assert all(item.report_no.isdigit() for item in disclosures)
    assert all(item.market in {"KOSPI", "KOSDAQ", "KONEX"} for item in disclosures)

    periods = KindClient().fetch_investment_status_periods(
        end_date - timedelta(days=30),
        end_date,
        force_refresh=True,
    )
    assert all(item.start_date <= (item.end_date or end_date + timedelta(days=1)) for item in periods)
    assert all(item.category in {"투자경고종목", "투자위험종목"} for item in periods)
