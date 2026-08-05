from datetime import date

from src.kind_client import KindClient, normalize_kind_code
from src.nxt_client import NxtClient, normalize_stock_code


def test_normalize_stock_codes() -> None:
    assert normalize_kind_code("40349") == "403490"
    assert normalize_kind_code("005930") == "005930"
    assert normalize_stock_code("A056080") == "056080"
    assert normalize_stock_code("A0008Z0") == "0008Z0"
    assert normalize_stock_code("") == ""


def test_parse_kind_result_html() -> None:
    html = """
    <table><tbody><tr>
      <td>1</td><td>2026-08-04 14:26</td>
      <td><img alt="코스닥"><a onclick="companysummary_open('18933');" title="씨이랩">씨이랩</a></td>
      <td><a onclick="openDisclsViewer('20260804000315','')" title="주권매매거래정지">공시</a></td>
      <td>코스닥시장본부</td><td>차트</td>
    </tr></tbody></table>
    <div>전체 1 건 : 1 /1</div>
    """
    rows, total_pages = KindClient._parse_results(html, "거래정지/재개")
    assert total_pages == 1
    assert len(rows) == 1
    assert rows[0].stock_code == "189330"
    assert rows[0].market == "KOSDAQ"
    assert rows[0].report_no == "20260804000315"


def test_parse_nxt_change() -> None:
    parsed = NxtClient._parse_change(
        {
            "aggDd": "20260804",
            "isuSrdCd": "A005930",
            "isuAbwdNm": "삼성전자",
            "mktNm": "KOSPI",
            "addExlCd": "편입",
            "rsnCd": "정기변경",
            "isuCd": "KR7005930003",
            "registDt": 1,
        }
    )
    assert parsed is not None
    assert parsed.change_date == date(2026, 8, 4)
    assert parsed.stock_code == "005930"
    assert parsed.change_type == "편입"


def test_parse_nxt_trading_status() -> None:
    parsed = NxtClient._parse_trading_status(
        {
            "aggDd": "20260803",
            "isuSrdCd": "A036800",
            "isuAbwdNm": "나이스정보통신",
            "mktNm": "KOSDAQ",
            "cptrTrdPmsnCdNm": "거래불가",
            "trdIpsbRsn": "거래정지",
            "isuCd": "KR7036800000",
        }
    )
    assert parsed is not None
    assert parsed.status_date == date(2026, 8, 3)
    assert parsed.stock_code == "036800"
    assert parsed.tradable_market == "거래불가"
    assert parsed.unavailable_reason == "거래정지"


def test_parse_investment_status_period() -> None:
    html = """
    <table><tbody><tr>
      <td>1</td>
      <td><img alt="유가증권"><a onclick="companysummary_open('00207');" title="비비안">비비안</a></td>
      <td>2026-07-16</td><td>2026-07-20</td><td>2026-08-03</td>
    </tr></tbody></table>
    <div>전체 1 건 : 1 /1</div>
    """
    periods, pages = KindClient._parse_status_periods(html, "투자경고종목")
    assert pages == 1
    assert len(periods) == 1
    assert periods[0].stock_code == "002070"
    assert periods[0].start_date == date(2026, 7, 20)
    assert periods[0].end_date == date(2026, 8, 3)
