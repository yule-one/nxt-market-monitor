from __future__ import annotations

from datetime import date, timedelta
from html import escape
from io import BytesIO

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analytics import (
    build_daily_metrics,
    build_daily_nxt_metrics,
    disclosures_to_frame,
    match_disclosures_to_nxt_status,
    nxt_changes_to_frame,
    nxt_trading_status_to_frame,
)
from src.cache import ResponseCache
from src.config import (
    CATEGORY_CODES,
    KIND_SEARCH_MAIN_URL,
    NXT_CHANGE_PAGE_URL,
    NXT_LAUNCH_DATE,
    NXT_TRADING_STATUS_PAGE_URL,
    STATE_CATEGORIES,
)
from src.kind_client import KindClient
from src.nxt_client import NxtClient


SHOW_CHARTS = False


st.set_page_config(
    page_title="NXT 시장조치 모니터",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      [data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.28);
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 12px;
        padding: 14px 16px;
      }
      .source-note {
        color: var(--text-color);
        font-size: 0.85rem;
        opacity: 0.72;
      }
      .source-note a {
        color: var(--primary-color);
        font-weight: 600;
      }
      .hover-metric {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.28);
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 12px;
        box-sizing: border-box;
        color: var(--text-color);
        min-height: 108px;
        padding: 14px 16px;
      }
      .hover-metric[title] { cursor: help; }
      .hover-metric-label {
        font-size: 0.875rem;
        line-height: 1.4;
        margin-bottom: 0.35rem;
      }
      .hover-metric-value {
        font-size: 2.25rem;
        line-height: 1.2;
      }
      div[data-testid="stDataFrame"] { border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_response_cache() -> ResponseCache:
    return ResponseCache()


def load_nxt_changes(
    start_date: date,
    end_date: date,
    force_refresh: bool = False,
):
    client = NxtClient(get_response_cache())
    changes = client.fetch_changes(
        start_date,
        end_date,
        force_refresh=force_refresh,
    )
    return changes, client.ssl_fallback_used


def load_nxt_trading_statuses(
    start_date: date,
    end_date: date,
    force_refresh: bool = False,
):
    client = NxtClient(get_response_cache())
    statuses = client.fetch_trading_status_range(
        start_date,
        end_date,
        force_refresh=force_refresh,
    )
    return statuses, client.ssl_fallback_used


def load_kind_disclosures(
    start_date: date,
    end_date: date,
    categories: list[str] | tuple[str, ...],
    force_refresh: bool = False,
):
    client = KindClient(get_response_cache())
    disclosures = client.fetch_disclosures(
        start_date,
        end_date,
        categories,
        force_refresh=force_refresh,
    )
    return disclosures, client.ssl_fallback_used


def _date_controls(key_prefix: str, default_days: int = 14) -> tuple[date, date]:
    today = date.today()
    default_start = max(NXT_LAUNCH_DATE, today - timedelta(days=default_days))
    start_col, end_col = st.columns(2)
    with start_col:
        start_date = st.date_input(
            "시작일",
            value=default_start,
            min_value=NXT_LAUNCH_DATE,
            max_value=today,
            key=f"{key_prefix}_start",
        )
    with end_col:
        end_date = st.date_input(
            "종료일",
            value=today,
            min_value=NXT_LAUNCH_DATE,
            max_value=today,
            key=f"{key_prefix}_end",
        )
    if start_date > end_date:
        st.error("시작일은 종료일보다 늦을 수 없습니다.")
        st.stop()
    return start_date, end_date


def _refresh_control(key: str) -> bool:
    return st.sidebar.button(
        "원본 데이터 새로고침",
        key=key,
        help="로컬 캐시를 건너뛰고 선택한 조회의 공식 원본을 다시 요청합니다.",
        use_container_width=True,
    )


def _show_ssl_warning(used: bool) -> None:
    if used:
        st.warning(
            "이 PC의 인증서 저장소 문제로 공식 사이트 연결 시 SSL 검증 폴백이 사용되었습니다. "
            "사내 프록시 환경이 아니라면 Python 인증서 설정을 확인하세요.",
            icon="⚠️",
        )


def _download_buttons(frame: pd.DataFrame, filename_prefix: str) -> None:
    csv_bytes = frame.to_csv(index=False).encode("utf-8-sig")
    excel_buffer = BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="data")
    col1, col2, _ = st.columns([1, 1, 4])
    with col1:
        st.download_button(
            "CSV 다운로드",
            data=csv_bytes,
            file_name=f"{filename_prefix}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with col2:
        st.download_button(
            "Excel 다운로드",
            data=excel_buffer.getvalue(),
            file_name=f"{filename_prefix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


def _render_disclosure_table(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("선택한 조건에 해당하는 NXT 관련 공시가 없습니다.")
        return
    display = frame.copy()
    display["공시일시"] = pd.to_datetime(display["공시일시"]).dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=min(680, 42 + len(display) * 35),
        column_config={
            "KIND 원문": st.column_config.LinkColumn("KIND 원문", display_text="원문 보기"),
            "접수번호": None,
        },
    )


def _style_daily_nxt_metrics(frame: pd.DataFrame):
    def format_addition(value: int | float) -> str:
        return f"+{int(value):,}" if value > 0 else f"{int(value):,}"

    def format_exclusion(value: int | float) -> str:
        return f"-{int(value):,}" if value > 0 else f"{int(value):,}"

    return (
        frame.style.format(
            {
                "당일 편입 종목수": format_addition,
                "당일 편출 종목수": format_exclusion,
            }
        )
        .map(
            lambda value: (
                "color: #1e3a8a; background-color: #dbeafe; font-weight: 700;"
                if value > 0
                else ""
            ),
            subset=pd.IndexSlice[:, ["당일 편입 종목수"]],
        )
        .map(
            lambda value: (
                "color: #991b1b; background-color: #fee2e2; font-weight: 700;"
                if value > 0
                else ""
            ),
            subset=pd.IndexSlice[:, ["당일 편출 종목수"]],
        )
    )


def _filter_nxt_tradable_market(
    frame: pd.DataFrame,
    selection: str | None,
) -> pd.DataFrame:
    if selection == "거래불가":
        return frame[frame["거래가능시장"] == "거래불가"]
    if selection == "기타":
        return frame[~frame["거래가능시장"].isin(["전체", "거래불가"])]
    return frame


def _unavailable_reason_tooltip(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "거래불가 사유별 종목수\n해당 종목 없음"
    reasons = frame["거래불가사유"].fillna("").astype(str).str.strip()
    reasons = reasons.mask(reasons.eq(""), "사유 미제공")
    counts = reasons.value_counts()
    details = "\n".join(f"{reason}: {count:,}개" for reason, count in counts.items())
    return f"거래불가 사유별 종목수\n{details}"


def _render_metric_card(label: str, value: str, tooltip: str | None = None) -> None:
    title_attribute = (
        f' title="{escape(tooltip, quote=True).replace(chr(10), "&#10;")}"'
        if tooltip
        else ""
    )
    aria_label = escape(f"{label} {value}. {tooltip or ''}", quote=True)
    st.markdown(
        f"""
        <div class="hover-metric"{title_attribute} tabindex="0" aria-label="{aria_label}">
          <div class="hover-metric-label">{escape(label)}</div>
          <div class="hover-metric-value">{escape(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _selected_disclosure_categories(selected_labels: list[str] | None) -> list[str]:
    if not selected_labels or "전체" in selected_labels:
        return list(CATEGORY_CODES)
    return [category for category in CATEGORY_CODES if category in selected_labels]


def _normalize_disclosure_category_selection() -> None:
    current = list(st.session_state.get("disclosure_category", []))
    previous = list(
        st.session_state.get("_disclosure_category_previous", ["전체"])
    )
    if "전체" in current and len(current) > 1:
        current = (
            ["전체"]
            if "전체" not in previous
            else [item for item in current if item != "전체"]
        )
    if not current:
        current = ["전체"]
    st.session_state["disclosure_category"] = current
    st.session_state["_disclosure_category_previous"] = current


def disclosure_page() -> None:
    st.title("NXT 정규시장 종목의 KRX 시장조치 현황")
    st.caption("일자별 NXT 정규시장 종목의 KRX 시장조치 공시를 조회합니다.")
    force_refresh = _refresh_control("refresh_disclosures")
    start_date, end_date = _date_controls("disclosure", default_days=0)
    selected_labels = st.pills(
        "공시 구분",
        ["전체", *CATEGORY_CODES.keys()],
        default=["전체"],
        selection_mode="multi",
        key="disclosure_category",
        on_change=_normalize_disclosure_category_selection,
    )
    query_clicked = st.button(
        "조회",
        type="primary",
        key="disclosure_query_button",
    )
    query_state_key = "disclosure_applied_query"
    if query_clicked or query_state_key not in st.session_state:
        categories = _selected_disclosure_categories(selected_labels)
        st.session_state[query_state_key] = {
            "start_date": start_date,
            "end_date": end_date,
            "categories": categories,
        }
    applied_query = st.session_state[query_state_key]
    query_start_date = applied_query["start_date"]
    query_end_date = applied_query["end_date"]
    categories = applied_query["categories"]
    query_all_categories = set(categories) == set(CATEGORY_CODES)

    with st.spinner("KIND 공시와 NXT 일자별 유니버스를 조회하고 있습니다..."):
        statuses_by_date, nxt_ssl = load_nxt_trading_statuses(
            query_start_date,
            query_end_date,
            force_refresh,
        )
        disclosures, kind_ssl = load_kind_disclosures(
            query_start_date,
            query_end_date,
            categories,
            force_refresh,
        )
        matched = match_disclosures_to_nxt_status(disclosures, statuses_by_date)
        matched = [row for row in matched if row["is_nxt_related"]]
        frame = disclosures_to_frame(matched)

    _show_ssl_warning(nxt_ssl or kind_ssl)
    if not frame.empty:
        frame = frame.sort_values("공시일시", ascending=False, ignore_index=True)
    search_term = st.text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key="disclosure_search",
    ).strip()
    if search_term and not frame.empty:
        mask = frame["종목명"].str.contains(search_term, case=False, na=False) | frame[
            "종목코드"
        ].astype(str).str.contains(search_term, na=False)
        frame = frame.loc[mask].reset_index(drop=True)

    metric_cols = st.columns(4)
    metric_cols[0].metric("공시 건수", f"{len(frame):,}")
    metric_cols[1].metric(
        "공시 발생 종목수",
        f"{frame['종목코드'].nunique():,}" if not frame.empty else "0",
    )
    metric_cols[2].metric(
        "KOSPI 종목",
        f"{frame.loc[frame['상장시장'] == 'KOSPI', '종목코드'].nunique():,}"
        if not frame.empty
        else "0",
    )
    metric_cols[3].metric(
        "KOSDAQ 종목",
        f"{frame.loc[frame['상장시장'] == 'KOSDAQ', '종목코드'].nunique():,}"
        if not frame.empty
        else "0",
    )

    _render_disclosure_table(frame)
    if not frame.empty:
        _download_buttons(
            frame,
            f"nxt_kind_disclosures_{query_start_date}_{query_end_date}",
        )
    if SHOW_CHARTS and query_all_categories and not frame.empty:
        counts = (
            frame.groupby("구분")["종목코드"]
            .nunique()
            .reindex(CATEGORY_CODES.keys(), fill_value=0)
        )
        chart = px.bar(
            x=counts.index,
            y=counts.values,
            labels={"x": "공시 구분", "y": "공시 발생 종목수"},
            title="구분별 공시 발생 NXT 종목수",
        )
        chart.update_layout(
            showlegend=False, margin=dict(l=10, r=10, t=50, b=10)
        )
        st.plotly_chart(chart, use_container_width=True)
    st.markdown(
        f'<p class="source-note">출처: <a href="{KIND_SEARCH_MAIN_URL}">KIND 상세검색</a>, '
        f'<a href="{NXT_TRADING_STATUS_PAGE_URL}">NXT 정규시장 거래현황</a></p>',
        unsafe_allow_html=True,
    )


def nxt_stocks_page() -> None:
    st.title("NXT 정규시장 종목 현황")
    force_refresh = _refresh_control("refresh_nxt_stocks")
    as_of = st.date_input(
        "기준일",
        value=date.today(),
        min_value=NXT_LAUNCH_DATE,
        max_value=date.today(),
        key="nxt_as_of",
    )
    with st.spinner("NXT 정규시장 거래현황을 조회하고 있습니다..."):
        statuses_by_date, status_ssl = load_nxt_trading_statuses(
            as_of,
            as_of,
            force_refresh,
        )
        changes, change_ssl = load_nxt_changes(as_of, as_of, force_refresh)
        statuses = statuses_by_date.get(as_of, [])
        frame = nxt_trading_status_to_frame(statuses)
    _show_ssl_warning(status_ssl or change_ssl)

    unavailable = (
        frame[frame["거래가능시장"] == "거래불가"] if not frame.empty else frame
    )
    cols = st.columns(4)
    with cols[0]:
        _render_metric_card("NXT 종목수", f"{len(frame):,}")
    with cols[1]:
        _render_metric_card(
            "거래불가 종목수",
            f"{len(unavailable):,}",
            _unavailable_reason_tooltip(unavailable),
        )
    with cols[2]:
        _render_metric_card(
            "KOSPI 종목수",
            f"{(frame['상장시장'] == 'KOSPI').sum():,}" if not frame.empty else "0",
        )
    with cols[3]:
        _render_metric_card(
            "KOSDAQ 종목수",
            f"{(frame['상장시장'] == 'KOSDAQ').sum():,}" if not frame.empty else "0",
        )

    market_filter = st.pills(
        "거래가능시장",
        ["전체", "거래불가", "기타"],
        default="전체",
        selection_mode="single",
        key="nxt_market_filter",
    )
    search_term = st.text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key="nxt_stock_search",
    ).strip()
    display = _filter_nxt_tradable_market(frame, market_filter).copy()
    if search_term and not display.empty:
        mask = display["종목명"].str.contains(search_term, case=False, na=False) | display[
            "종목코드"
        ].astype(str).str.contains(search_term, na=False)
        display = display.loc[mask]

    st.dataframe(display, hide_index=True, use_container_width=True, height=650)
    if not display.empty:
        _download_buttons(display, f"nxt_stocks_{as_of}")

    day_changes = [item for item in changes if item.change_date == as_of]
    with st.expander(f"{as_of:%Y-%m-%d} 종목 변동내역 ({len(day_changes):,}건)", expanded=False):
        change_frame = nxt_changes_to_frame(day_changes)
        if change_frame.empty:
            st.info("해당 일자의 편입·편출 내역이 없습니다.")
        else:
            st.dataframe(change_frame, hide_index=True, use_container_width=True)
    st.markdown(
        f'<p class="source-note">출처: <a href="{NXT_TRADING_STATUS_PAGE_URL}">NXT 정규시장 거래현황</a>, '
        f'<a href="{NXT_CHANGE_PAGE_URL}">NXT 매매체결종목 변동내역</a></p>',
        unsafe_allow_html=True,
    )


def nxt_changes_page() -> None:
    st.title("NXT 정규시장 종목 변동내역")
    force_refresh = _refresh_control("refresh_nxt_changes")
    start_date, end_date = _date_controls("nxt_changes", default_days=30)

    with st.spinner("NXT 날짜별 종목수와 편입·편출을 조회하고 있습니다..."):
        changes, change_ssl = load_nxt_changes(
            start_date,
            end_date,
            force_refresh,
        )
        statuses_by_date, status_ssl = load_nxt_trading_statuses(
            start_date,
            end_date,
            force_refresh,
        )
        metrics = build_daily_nxt_metrics(
            changes,
            statuses_by_date,
            start_date,
            end_date,
        )

    _show_ssl_warning(change_ssl or status_ssl)
    if metrics.empty:
        st.info("선택한 기간의 NXT 변동 현황을 계산할 수 없습니다.")
        return

    latest = metrics.iloc[-1]
    first = metrics.iloc[0]
    period_additions = len(
        {
            item.stock_code
            for item in changes
            if start_date <= item.change_date <= end_date
            and item.change_type == "편입"
        }
    )
    period_exclusions = len(
        {
            item.stock_code
            for item in changes
            if start_date <= item.change_date <= end_date
            and item.change_type == "편출"
        }
    )
    columns = st.columns(4)
    columns[0].metric("NXT 종목수", f"{int(latest['NXT 종목수']):,}")
    columns[1].metric("조회기간 편입종목수", f"{period_additions:,}")
    columns[2].metric("조회기간 편출종목수", f"{period_exclusions:,}")
    columns[3].metric(
        "NXT 종목수 증감",
        f"{int(latest['NXT 종목수'] - first['NXT 종목수']):+,}",
    )

    detail_tab, summary_tab = st.tabs(["종목별 변동내역", "일별 집계"])
    with detail_tab:
        period_changes = [
            item for item in changes if start_date <= item.change_date <= end_date
        ]
        change_frame = nxt_changes_to_frame(period_changes)
        if change_frame.empty:
            st.info("선택한 기간의 NXT 편입·편출 내역이 없습니다.")
        else:
            change_frame = change_frame.sort_values("일자", ascending=False)
            st.dataframe(
                change_frame,
                hide_index=True,
                use_container_width=True,
                height=650,
            )
            _download_buttons(change_frame, f"nxt_changes_{start_date}_{end_date}")

    with summary_tab:
        summary_frame = metrics.sort_values("일자", ascending=False)
        st.dataframe(
            _style_daily_nxt_metrics(summary_frame),
            hide_index=True,
            use_container_width=True,
            height=650,
        )
        _download_buttons(metrics, f"nxt_daily_changes_{start_date}_{end_date}")

    with st.expander("지표 정의"):
        st.markdown(
            """
            - **NXT 종목수**: 해당 일자 NXT 정규시장 종목 수입니다. 거래불가 종목도 포함
            - **조회기간 편입·편출종목수**: 선택한 조회기간에 편입 또는 편출된 종목 수
            - **편입·편출 종목수**: 해당 일자에 발생한 편입·편출 종목 수
            - **NXT 종목수 증감**: 조회기간의 첫 거래일과 마지막 거래일의 NXT 종목수 차이
            """
        )

    if SHOW_CHARTS:
        flow_chart = px.bar(
            metrics,
            x="일자",
            y=["당일 편입 종목수", "당일 편출 종목수"],
            barmode="group",
            title="일별 편입·편출 변동",
            labels={"value": "종목수", "variable": "구분"},
        )
        flow_chart.update_layout(
            hovermode="x unified", margin=dict(l=10, r=10, t=50, b=10)
        )
        st.plotly_chart(flow_chart, use_container_width=True)

        state_chart = px.line(
            metrics,
            x="일자",
            y="NXT 종목수",
            markers=True,
            title="NXT 종목수 추이",
            labels={"NXT 종목수": "종목수"},
        )
        state_chart.update_layout(
            hovermode="x unified", margin=dict(l=10, r=10, t=50, b=10)
        )
        st.plotly_chart(state_chart, use_container_width=True)
    st.markdown(
        f'<p class="source-note">출처: <a href="{NXT_TRADING_STATUS_PAGE_URL}">NXT 정규시장 거래현황</a>, '
        f'<a href="{NXT_CHANGE_PAGE_URL}">NXT 매매체결종목 변동내역</a></p>',
        unsafe_allow_html=True,
    )


def krx_market_actions_page() -> None:
    st.title("KRX 시장조치")
    st.caption("각 일자의 NXT 공식 거래현황 종목에 한정해 KRX 시장조치 상태와 공시 수를 조회합니다.")
    force_refresh = _refresh_control("refresh_krx_market_actions")
    start_date, end_date = _date_controls("krx_market_actions", default_days=30)

    with st.spinner("KRX 시장조치 상태 이력을 재구성하고 있습니다. 첫 조회는 다소 시간이 걸릴 수 있습니다..."):
        statuses_by_date, nxt_ssl = load_nxt_trading_statuses(
            start_date,
            end_date,
            force_refresh,
        )
        disclosures, kind_ssl = load_kind_disclosures(
            start_date,
            end_date,
            list(STATE_CATEGORIES),
            force_refresh,
        )
        matched = match_disclosures_to_nxt_status(disclosures, statuses_by_date)
        status_client = KindClient(get_response_cache())
        status_periods = status_client.fetch_investment_status_periods(
            start_date,
            end_date,
            force_refresh=force_refresh,
        )
        metrics = build_daily_metrics(
            matched,
            statuses_by_date,
            start_date,
            end_date,
            status_periods,
        )

    _show_ssl_warning(
        nxt_ssl or kind_ssl or status_client.ssl_fallback_used
    )
    if metrics.empty:
        st.info("선택한 기간의 현황을 계산할 수 없습니다.")
        return

    latest = metrics.iloc[-1]
    for offset in range(0, len(STATE_CATEGORIES), 3):
        columns = st.columns(3)
        for column, metric_name in zip(columns, STATE_CATEGORIES[offset : offset + 3]):
            column.metric(metric_name, f"{int(latest[metric_name]):,}")

    selected_metrics = st.multiselect(
        "차트에 표시할 시장조치",
        list(STATE_CATEGORIES),
        default=list(STATE_CATEGORIES),
        key="krx_chart_metrics",
    )
    if SHOW_CHARTS and selected_metrics:
        status_chart = px.line(
            metrics,
            x="일자",
            y=selected_metrics,
            markers=True,
            title="시장조치 상태 종목수 추이",
            labels={"value": "종목수", "variable": "구분"},
        )
        status_chart.update_layout(hovermode="x unified", margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(status_chart, use_container_width=True)

    status_tab, disclosure_tab = st.tabs(["일별 시장조치", "KIND 당일 공시"])
    with status_tab:
        status_columns = ["일자", "NXT 종목수"]
        for category in STATE_CATEGORIES:
            status_columns.extend([category, f"{category} 당일공시"])
        status_frame = metrics[status_columns]
        st.dataframe(
            status_frame.sort_values("일자", ascending=False),
            hide_index=True,
            use_container_width=True,
            height=650,
        )
        _download_buttons(
            status_frame, f"krx_market_actions_{start_date}_{end_date}"
        )

    with disclosure_tab:
        period_matched = [
            row
            for row in matched
            if row["is_nxt_related"]
            and start_date <= row["disclosure"].disclosure_date <= end_date
        ]
        disclosure_frame = disclosures_to_frame(period_matched)
        if not disclosure_frame.empty:
            disclosure_frame = disclosure_frame.sort_values("공시일시", ascending=False)
        _render_disclosure_table(disclosure_frame)
        if not disclosure_frame.empty:
            _download_buttons(
                disclosure_frame, f"krx_action_disclosures_{start_date}_{end_date}"
            )

    with st.expander("지표 정의와 데이터 유의사항"):
        st.markdown(
            """
            - **NXT 대상 범위**: 각 일자 NXT 정규시장 거래현황에 실제로 포함된 종목입니다. 과거 편입·편출 이력으로 추정하지 않습니다.
            - **거래정지·관리·환기·단기과열 종목수**: 해당 일자 NXT 원본의 `거래불가사유`로 집계합니다. 과거 공시를 누적하지 않습니다.
            - **투자경고·투자위험 종목수**: KIND가 제공하는 공식 지정일·해제일 기간을 해당 일자 NXT 종목과 교차합니다.
            - **당일공시** 열: 상태 누계가 아니라 해당 일자에 공시가 발생한 고유 NXT 관련 종목 수입니다.
            - 공시 제목만으로 효력일을 구분하기 어려운 `정지및정지해제` 문서는 세부 본문의 정지일·해제일을 사용합니다. 그 밖의 공시는 제목상 지정·해제를 공시일 기준으로 반영합니다.
            - 우선주를 대상으로 한 시장조치가 대표 보통주 코드에 잘못 결합되지 않도록 별도 제외합니다.
            """
        )
    st.markdown(
        f'<p class="source-note">출처: <a href="{KIND_SEARCH_MAIN_URL}">KIND 상세검색</a>, '
        f'<a href="{NXT_TRADING_STATUS_PAGE_URL}">NXT 정규시장 거래현황</a></p>',
        unsafe_allow_html=True,
    )


def main() -> None:
    pages = [
        st.Page(disclosure_page, title="KRX 시장조치 조회", icon="📋", default=True),
        st.Page(nxt_stocks_page, title="NXT 정규시장 종목 현황", icon="🗓️"),
        st.Page(nxt_changes_page, title="NXT 정규시장 종목 변동내역", icon="🔄"),
    ]
    navigation = st.navigation(pages)
    navigation.run()


if __name__ == "__main__":
    main()
