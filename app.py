from __future__ import annotations

import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from html import escape

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analytics import (
    build_daily_metrics,
    build_daily_nxt_metrics_from_counts,
    classify_nxt_change,
    disclosures_to_frame,
    match_disclosures_to_nxt_status,
    nxt_changes_display_frame,
    nxt_changes_to_frame,
    nxt_unavailability_events_to_frame,
    nxt_trading_status_to_frame,
    summarize_daily_nxt_unavailability_reasons,
    summarize_nxt_membership_flow,
)
from src.cache import ResponseCache
from src.config import (
    CATEGORY_CODES,
    NXT_LAUNCH_DATE,
    STATE_CATEGORIES,
)
from src.kind_client import KindClient
from src.historical_market import (
    DailyMarketMetric,
    HistoricalMarketStore,
    average_market_totals,
    monthly_six_month_volume_ratios,
    six_calendar_month_window_start,
)
from src.krx_openapi import KrxDailySnapshot, KrxOpenApiClient, KrxOpenApiError
from src.kis_rest import (
    IndexQuote,
    KisRestClient,
    KisRestUniverseCollector,
    RestQuote,
    build_comparison_rows,
    build_market_totals,
    build_nxt_weighted_change_rates,
    exclude_unavailable_nxt_quotes,
)
from src.kis_websocket import KisCredentials, KisRealtimeCollector
from src.market_realtime import NXT, MarketAggregator, MarketDataStore, WatchSymbol
from src.models import NxtTradingStatus
from src.nxt_client import NxtClient
from src.nxt_api_control import NxtMinuteLookupGate, is_nxt_morning_break
from src.nxt_price_limits import (
    first_limit_proximity_time,
    format_limit_hit_time,
    limit_proximity_ticks,
    reached_limit_proximity_points,
    reached_limit_points,
)
from src.nxt_change_store import (
    NxtChangeScheduler,
    NxtChangeStore,
    NxtChangeSyncService,
)


LOGGER = logging.getLogger(__name__)


SHOW_CHARTS = False
REST_UNIVERSE_REFRESH_SECONDS = 10
REST_UNIVERSE_RUNTIME_VERSION = 11
HISTORICAL_STORE_RUNTIME_VERSION = 15
NXT_CHANGE_STORE_RUNTIME_VERSION = 2
KIS_SHARED_CLIENT_RUNTIME_VERSION = 4
KIS_SHARED_MIN_REQUEST_INTERVAL_SECONDS = 0.20
KIS_MINUTE_REQUEST_INTERVAL_SECONDS = 1.0
NXT_LIMIT_PROXIMITY_TICKS = 3
DEFAULT_KIS_WATCHLIST = [
    WatchSymbol("005930", "삼성전자"),
    WatchSymbol("000660", "SK하이닉스"),
]


st.set_page_config(
    page_title="NEXTRADE 시장운영 DASHBOARD",
    page_icon="📡",
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
      .index-metric-card {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.28);
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 12px;
        box-sizing: border-box;
        min-height: 128px;
        padding: 14px 16px;
      }
      .index-metric-label {
        font-size: 0.875rem;
        line-height: 1.25;
        min-height: 2.2em;
        word-break: keep-all;
      }
      .index-metric-value {
        font-size: clamp(1.05rem, 1.45vw, 1.75rem);
        font-weight: 600;
        line-height: 1.3;
        overflow-wrap: anywhere;
      }
      .index-metric-delta {
        font-size: clamp(0.8rem, 1.05vw, 1.35rem);
        font-weight: 700;
        line-height: 1.3;
        overflow-wrap: anywhere;
      }
      .nxt-rate-comparison {
        font-size: 0.76rem;
        opacity: 0.78;
        word-break: keep-all;
      }
      .nxt-rate-values {
        display: grid;
        gap: 0.08rem;
        margin-top: 0.1rem;
      }
      .nxt-rate-value {
        font-size: clamp(1rem, 1.2vw, 1.48rem);
        font-weight: 700;
        line-height: 1.18;
        white-space: nowrap;
      }
      .market-summary-card {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.28);
        border-color: color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 10px;
        box-sizing: border-box;
        min-height: 74px;
        padding: 10px 12px;
      }
      .market-summary-label {
        font-size: 0.8rem;
        line-height: 1.3;
        margin-bottom: 0.3rem;
        opacity: 0.78;
      }
      .market-summary-value {
        font-size: clamp(1.05rem, 1.5vw, 1.75rem);
        font-weight: 600;
        line-height: 1.3;
        white-space: normal;
      }
      .market-summary-compact {
        font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif !important;
        font-size: 0.72em !important;
        font-weight: 600 !important;
        line-height: 1.3 !important;
        vertical-align: baseline;
        white-space: nowrap;
      }
      .market-summary-compact-number,
      .market-summary-compact-unit {
        font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif !important;
        font-weight: 600 !important;
        line-height: inherit !important;
        vertical-align: baseline;
      }
      .market-summary-compact-number { font-size: 1em !important; }
      .market-summary-compact-unit { font-size: 1em !important; }
      .market-positive { color: #d71920 !important; }
      .market-negative { color: #0ea5e9 !important; }
      .dashboard-section-gap { height: 1.25rem; }
      .dashboard-table-gap { height: 1rem; }
      .compact-universe-counts {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        font-size: 1.125rem;
        gap: 0.45rem 1.1rem;
        margin: -0.35rem 0 0.7rem;
        opacity: 0.82;
      }
      .compact-universe-counts strong {
        font-size: 1.125rem;
        margin-left: 0.2rem;
      }
      .compact-unavailable-reasons {
        font-size: 1.125rem;
        margin-left: 0.2rem;
        opacity: 0.86;
      }
      .dashboard-notice {
        font-size: 0.8rem;
        line-height: 1.5;
      }
      .dashboard-notice ul {
        margin: 0.2rem 0 0.2rem 1.1rem;
        padding: 0;
      }
      .dashboard-notice li { margin-bottom: 0.35rem; }
      .compact-status-strip {
        display: grid;
        gap: 0.5rem;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        margin: 0.1rem 0 0.65rem;
      }
      .compact-status-item {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.24);
        border-radius: 8px;
        min-width: 0;
        padding: 0.4rem 0.65rem;
      }
      .compact-status-label {
        font-size: 0.72rem;
        line-height: 1.15;
        opacity: 0.72;
      }
      .compact-status-value {
        font-size: 1rem;
        font-weight: 600;
        line-height: 1.3;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .limit-summary-strip {
        display: grid;
        gap: 0.45rem;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        margin: 0.2rem 0 0.8rem;
      }
      .limit-summary-strip-six {
        grid-template-columns: repeat(6, minmax(0, 1fr));
      }
      .limit-summary-item {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.24);
        border-radius: 8px;
        min-width: 0;
        padding: 0.45rem 0.65rem;
      }
      .limit-summary-label {
        font-size: 0.74rem;
        line-height: 1.2;
        opacity: 0.74;
      }
      .limit-summary-value {
        font-size: 1.05rem;
        font-weight: 650;
        line-height: 1.3;
        white-space: nowrap;
      }
      .limit-hit-table-wrap {
        border: 1px solid rgba(128, 128, 128, 0.24);
        border-radius: 10px;
        max-height: 650px;
        overflow: auto;
      }
      .limit-hit-table {
        border-collapse: separate;
        border-spacing: 0;
        font-size: 0.88rem;
        min-width: 1080px;
        width: 100%;
      }
      .limit-hit-table th {
        background: var(--secondary-background-color);
        border-bottom: 1px solid rgba(128, 128, 128, 0.28);
        padding: 0.55rem 0.65rem;
        position: sticky;
        text-align: center;
        top: 0;
        white-space: nowrap;
        z-index: 1;
      }
      .limit-hit-table td {
        border-bottom: 1px solid rgba(128, 128, 128, 0.16);
        padding: 0.48rem 0.65rem;
        white-space: nowrap;
      }
      .limit-hit-table td.center { text-align: center; }
      .limit-hit-table td.left { text-align: left; }
      .limit-hit-table td.right { text-align: right; }
      .limit-hit-table .limit-upper { color: #d71920; font-weight: 700; }
      .limit-hit-table .limit-lower { color: #0ea5e9; font-weight: 700; }
      .limit-hit-table .limit-rate-positive { color: #d71920; font-weight: 700; }
      .limit-hit-table .limit-rate-negative { color: #0ea5e9; font-weight: 700; }
      .limit-hit-table .limit-rate-neutral { opacity: 0.72; }
      .nextrade-dashboard-title {
        color: #d71920 !important;
        font-size: 2.25rem;
        font-weight: 700;
        line-height: 1.2;
        margin: 0 0 1rem;
      }
      div[data-testid="stDataFrame"] [role="columnheader"] {
        justify-content: center !important;
        text-align: center !important;
      }
      div[data-testid="stDataFrame"] [role="columnheader"] > div,
      div[data-testid="stDataFrame"] [role="columnheader"] span,
      div[data-testid="stDataFrame"] [role="columnheader"] p {
        justify-content: center !important;
        text-align: center !important;
        width: 100% !important;
      }
      @media (max-width: 1200px) {
        .limit-summary-strip-six { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      }
      @media (max-width: 900px) {
        .compact-status-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .limit-summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      }
      @media (max-width: 1400px) {
        .index-metric-card { padding: 12px 9px; }
        .index-metric-label { font-size: 0.78rem; }
        .nxt-rate-comparison { font-size: 0.64rem; }
      }
      button[aria-label="Download as CSV"] { display: none !important; }
      div[data-testid="stDataFrame"] { border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_response_cache() -> ResponseCache:
    return ResponseCache()


@st.cache_resource
def _get_historical_market_store(runtime_version: int) -> HistoricalMarketStore:
    # Include the persisted-model schema in Streamlit's cache key.  Otherwise a
    # hot reload can retain an instance created before a new snapshot field or
    # store method was added.
    _ = runtime_version
    return HistoricalMarketStore()


def get_historical_market_store() -> HistoricalMarketStore:
    store = _get_historical_market_store(HISTORICAL_STORE_RUNTIME_VERSION)
    if not all(
        hasattr(store, method_name)
        for method_name in (
            "nxt_limit_hit_coverage",
            "load_nxt_limit_proximity_times",
            "load_nxt_limit_proximity_hits",
            "list_nxt_eligibility_summaries",
            "list_nxt_unavailability_events",
            "list_nxt_index_member_counts",
        )
    ):
        _get_historical_market_store.clear()
        store = _get_historical_market_store(HISTORICAL_STORE_RUNTIME_VERSION)
    return store


@st.cache_resource
def _get_nxt_change_store(runtime_version: int) -> NxtChangeStore:
    _ = runtime_version
    return NxtChangeStore()


def get_nxt_change_store() -> NxtChangeStore:
    return _get_nxt_change_store(NXT_CHANGE_STORE_RUNTIME_VERSION)


@st.cache_resource
def _get_nxt_change_scheduler(runtime_version: int) -> NxtChangeScheduler:
    _ = runtime_version
    service = NxtChangeSyncService(
        get_nxt_change_store(),
        client_factory=lambda: NxtClient(get_response_cache()),
    )
    return NxtChangeScheduler(service)


def get_nxt_change_scheduler() -> NxtChangeScheduler:
    return _get_nxt_change_scheduler(NXT_CHANGE_STORE_RUNTIME_VERSION)


@st.cache_resource
def get_market_store() -> MarketDataStore:
    return MarketDataStore()


@st.cache_resource
def get_realtime_runtime(
    app_key: str,
    app_secret: str,
) -> tuple[MarketAggregator, KisRealtimeCollector]:
    store = get_market_store()
    aggregator = MarketAggregator(store)
    collector = KisRealtimeCollector(
        KisCredentials(app_key=app_key, app_secret=app_secret),
        aggregator,
    )
    return aggregator, collector


@st.cache_resource
def get_shared_kis_rest_client(
    app_key: str,
    app_secret: str,
    runtime_version: int,
) -> KisRestClient:
    _ = runtime_version
    return KisRestClient(
        KisCredentials(app_key=app_key, app_secret=app_secret),
        min_request_interval=KIS_SHARED_MIN_REQUEST_INTERVAL_SECONDS,
    )


@st.cache_resource
def get_nxt_minute_kis_rest_client(
    app_key: str,
    app_secret: str,
    runtime_version: int,
) -> KisRestClient:
    _ = runtime_version
    return KisRestClient(
        KisCredentials(app_key=app_key, app_secret=app_secret),
        min_request_interval=KIS_MINUTE_REQUEST_INTERVAL_SECONDS,
    )


@st.cache_resource
def get_nxt_minute_lookup_gate(runtime_version: int) -> NxtMinuteLookupGate:
    _ = runtime_version
    return NxtMinuteLookupGate(
        failure_base_seconds=60,
        failure_max_seconds=300,
        empty_retry_seconds=60,
        success_cooldown_seconds=5,
    )


@st.cache_resource
def get_nxt_minute_lookup_executor(runtime_version: int) -> ThreadPoolExecutor:
    _ = runtime_version
    return ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="nxt-minute-lookup",
    )


@st.cache_resource
def get_rest_universe_runtime(
    app_key: str,
    app_secret: str,
    runtime_version: int,
) -> KisRestUniverseCollector:
    # 데이터 단위나 수집 로직이 바뀌면 캐시된 이전 수집기를 교체합니다.
    _ = runtime_version
    client = get_shared_kis_rest_client(
        app_key,
        app_secret,
        KIS_SHARED_CLIENT_RUNTIME_VERSION,
    )
    return KisRestUniverseCollector(client)


def _secret_value(name: str) -> str:
    environment_value = os.getenv(name, "").strip()
    if environment_value:
        return environment_value
    try:
        return str(st.secrets.get(name, "")).strip()
    except (FileNotFoundError, KeyError):
        return ""


def _parse_watchlist(raw: str) -> tuple[list[WatchSymbol], list[str]]:
    symbols: list[WatchSymbol] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"(\d{6})(?:[\s,]+(.+))?", line)
        if not match:
            errors.append(f"{line_number}행: '6자리 종목코드 종목명' 형식으로 입력하세요.")
            continue
        symbol = match.group(1)
        if symbol in seen:
            continue
        seen.add(symbol)
        name = (match.group(2) or symbol).strip()
        symbols.append(WatchSymbol(symbol, name))
    if not symbols:
        errors.append("관심종목을 한 종목 이상 입력하세요.")
    if len(symbols) > 20:
        errors.append(f"현재 {len(symbols)}종목입니다. 최대 20종목까지 입력할 수 있습니다.")
    return symbols, errors


def _format_volume(value: object) -> str:
    return f"{int(value or 0):,}"


def _format_amount(value: object) -> str:
    return f"{int(value or 0):,}"


def _format_price(value: object) -> str:
    return "-" if value is None else f"{int(value):,}"


def _format_rate(value: object) -> str:
    return "-" if value is None else f"{float(value):.2%}"


def _format_volume_with_eok(value: object) -> str:
    volume = int(value or 0)
    return f"{volume:,} ({volume / 100_000_000:,.2f}억주)"


def _format_amount_with_trillion(value: object) -> str:
    amount = int(value or 0)
    return f"{amount:,} ({amount / 1_000_000_000_000:,.2f}조원)"


def _change_rate_text_color(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    rate = float(value)
    if rate > 0:
        return "color: #d71920; font-weight: 700"
    if rate < 0:
        return "color: #0ea5e9; font-weight: 700"
    return ""


def _market_direction_class(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    numeric_value = float(value)
    if numeric_value > 0:
        return "market-positive"
    if numeric_value < 0:
        return "market-negative"
    return ""


def _render_index_metric_card(
    column: object,
    label: str,
    value: str,
    *,
    direction: object = None,
    delta: str | None = None,
    tooltip: str | None = None,
    color_value: bool = False,
) -> None:
    direction_class = _market_direction_class(direction)
    label_html = escape(label).replace("\n", "<br>")
    title_attribute = (
        f' title="{escape(tooltip, quote=True)}"' if tooltip else ""
    )
    value_class = f" {direction_class}" if color_value and direction_class else ""
    delta_html = (
        f'<div class="index-metric-delta {direction_class}">{escape(delta)}</div>'
        if delta is not None
        else ""
    )
    aria_label = escape(
        f"{label.replace(chr(10), ' ')} {value} {delta or ''}. {tooltip or ''}",
        quote=True,
    )
    column.markdown(
        f"""
        <div class="index-metric-card"{title_attribute} tabindex="0" aria-label="{aria_label}">
          <div class="index-metric-label">{label_html}</div>
          <div class="index-metric-value{value_class}">{escape(value)}</div>
          {delta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_nxt_rate_metric_card(
    column: object,
    *,
    reference_rate: object,
    krx_rate: object,
    reference_count: int,
    krx_count: int,
    historical: bool,
) -> None:
    price_label = "종가" if historical else "현재가"
    reference_formula = (
        f"Σ(NXT {price_label}×상장주식수) ÷ Σ(기준가격×상장주식수) − 1"
    )
    krx_formula = (
        f"Σ(NXT {price_label}×상장주식수) ÷ Σ(KRX {price_label}×상장주식수) − 1"
    )
    tooltip = (
        f"기준가 대비: {reference_formula} · 포함 {reference_count:,}개 | "
        f"KRX 현재가 대비: {krx_formula} · 포함 {krx_count:,}개"
    )
    reference_class = _market_direction_class(reference_rate)
    krx_class = _market_direction_class(krx_rate)
    reference_text = _format_rate(reference_rate)
    krx_text = _format_rate(krx_rate)
    aria_label = escape(
        f"NXT 등락률 기준가 대비 {reference_text}, KRX 현재가 대비 {krx_text}. {tooltip}",
        quote=True,
    )
    column.markdown(
        f"""
        <div class="index-metric-card" title="{escape(tooltip, quote=True)}"
             tabindex="0" aria-label="{aria_label}">
          <div class="index-metric-label">
            NXT 등락률<br><span class="nxt-rate-comparison">(기준가 대비 | KRX 현재가 대비)</span>
          </div>
          <div class="nxt-rate-values">
            <div class="nxt-rate-value {reference_class}">{escape(reference_text)}</div>
            <div class="nxt-rate-value {krx_class}">{escape(krx_text)}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_market_summary_card(column: object, label: str, value: str) -> None:
    compact_match = re.fullmatch(
        r"(.*?)(\s+\()([+-]?[\d,.]+)(억주|조원)(\))",
        value,
    )
    if compact_match:
        value_html = (
            f"{escape(compact_match.group(1))}"
            f'<span class="market-summary-compact">'
            f"{escape(compact_match.group(2))}"
            f'<span class="market-summary-compact-number">'
            f"{escape(compact_match.group(3))}</span>"
            f'<span class="market-summary-compact-unit">'
            f"{escape(compact_match.group(4))}</span>"
            f"{escape(compact_match.group(5))}</span>"
        )
    else:
        value_html = escape(value)
    column.markdown(
        f"""
        <div class="market-summary-card">
          <div class="market-summary-label">{escape(label)}</div>
          <div class="market-summary-value">{value_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_market_totals(
    market_totals: dict[str, object],
    *,
    title: str = "시장별 거래량·거래대금 현황",
    value_label: str = "합계",
) -> None:
    st.markdown(f"#### {title}")
    volume_columns = st.columns(3)
    _render_market_summary_card(
        volume_columns[0],
        "거래량비율 · NXT/KRX",
        _format_rate(market_totals["volume_ratio"]),
    )
    _render_market_summary_card(
        volume_columns[1],
        f"NXT 거래량 {value_label} · 주",
        _format_volume_with_eok(market_totals["nxt_volume"]),
    )
    _render_market_summary_card(
        volume_columns[2],
        f"KRX 거래량 {value_label} · 주 · KOSPI+KOSDAQ",
        _format_volume_with_eok(market_totals["krx_volume"]),
    )
    amount_columns = st.columns(3)
    _render_market_summary_card(
        amount_columns[0],
        "거래대금비율 · NXT/KRX",
        _format_rate(market_totals["amount_ratio"]),
    )
    _render_market_summary_card(
        amount_columns[1],
        f"NXT 거래대금 {value_label} · 원",
        _format_amount_with_trillion(market_totals["nxt_amount"]),
    )
    _render_market_summary_card(
        amount_columns[2],
        f"KRX 거래대금 {value_label} · 원 · KOSPI+KOSDAQ",
        _format_amount_with_trillion(market_totals["krx_amount"]),
    )


@st.fragment(run_every="1s")
def _render_realtime_table(
    aggregator: MarketAggregator,
    collector: KisRealtimeCollector,
    watchlist: list[WatchSymbol],
) -> None:
    status = collector.status()
    rows = aggregator.snapshot(watchlist)
    quality_values = {str(row["집계상태"]) for row in rows}
    if "부분" in quality_values:
        quality_status = "부분"
    elif quality_values == {"정상"}:
        quality_status = "정상"
    elif "정상" in quality_values:
        quality_status = "수집 중"
    else:
        quality_status = "대기"

    status_columns = st.columns(4)
    status_columns[0].metric("연결 상태", status.state)
    status_columns[1].metric("집계 상태", quality_status)
    status_columns[2].metric("구독 종목", f"{len(watchlist)} / 20")
    status_columns[3].metric(
        "최종 수신",
        status.last_message_at.strftime("%H:%M:%S") if status.last_message_at else "대기",
    )

    if status.state in {"오류", "재연결"}:
        st.warning(status.message)

    display_rows: list[dict[str, str]] = []
    for row in rows:
        display_rows.append(
            {
                "종목코드": str(row["종목코드"]),
                "종목명": str(row["종목명"]),
                "NXT 현재가": _format_price(row.get("nxt_current_price")),
                "등락률": _format_rate(row.get("change_rate")),
                "괴리율": _format_rate(row.get("disparity_rate")),
                "NXT 거래량": _format_volume(row["nxt_total_volume"]),
                "NXT 거래대금": _format_amount(row["nxt_total_amount"]),
                "거래량비율": _format_rate(row.get("volume_ratio")),
                "거래대금비율": _format_rate(row.get("amount_ratio")),
                "KRX 거래량": _format_volume(row["krx_total_volume"]),
                "KRX 거래대금": _format_amount(row["krx_total_amount"]),
            }
        )

    st.dataframe(
        pd.DataFrame(display_rows),
        hide_index=True,
        use_container_width=True,
        height=min(760, 82 + len(display_rows) * 35),
    )

    st.caption(
        "거래대금 단위: 원 · 등락률 = NXT 현재가 ÷ 기준가격 − 1 · "
        "괴리율 = NXT 현재가 ÷ KRX 현재가 − 1 · 비율 = NXT ÷ KRX. "
        "집계 상태가 ‘부분’이면 앱이 장중에 시작됐거나 장 구간을 넘는 연결 공백이 있었던 경우입니다."
    )


def realtime_market_page() -> None:
    st.title("NXT · KRX 실시간 거래 현황")
    st.caption(
        "관심종목의 NXT·KRX 현재가, 거래량, 거래대금과 시장 간 비율을 실시간으로 보여줍니다."
    )

    store = get_market_store()
    watchlist = store.load_watchlist()
    if not watchlist:
        watchlist = list(DEFAULT_KIS_WATCHLIST)
        store.save_watchlist(watchlist)

    with st.expander("관심종목 설정", expanded=False):
        st.write("한 줄에 `6자리 종목코드 종목명` 형식으로 최대 20종목을 입력하세요.")
        with st.form("kis_watchlist_form"):
            raw_watchlist = st.text_area(
                "관심종목",
                value="\n".join(f"{item.symbol} {item.name}" for item in watchlist),
                height=220,
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("관심종목 적용", use_container_width=True)
        if submitted:
            parsed, errors = _parse_watchlist(raw_watchlist)
            if errors:
                for error in errors:
                    st.error(error)
            else:
                store.save_watchlist(parsed)
                st.success(f"{len(parsed)}종목을 저장했습니다.")
                st.rerun()

    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        st.warning("KIS App Key와 App Secret을 서버 설정에 등록하면 실시간 수신이 시작됩니다.")
        st.code(
            'KIS_APP_KEY = "발급받은 App Key"\n'
            'KIS_APP_SECRET = "발급받은 App Secret"',
            language="toml",
        )
        st.info("로컬에서는 `.streamlit/secrets.toml`에 위 두 값을 저장하세요. 키를 화면 입력란에 넣지는 않습니다.")
        return

    aggregator, collector = get_realtime_runtime(app_key, app_secret)
    probe_rows = aggregator.snapshot(watchlist[:1])
    if probe_rows and "nxt_current_price" not in probe_rows[0]:
        # Streamlit 핫 리로드가 이전 클래스의 cache_resource 객체를 유지할 수 있습니다.
        # 구 수집기를 먼저 정지한 뒤 저장소와 런타임 캐시를 새 스키마로 재생성합니다.
        collector.stop()
        get_realtime_runtime.clear()
        get_market_store.clear()
        st.rerun()
    collector.start(watchlist)
    _render_realtime_table(aggregator, collector, watchlist)


def _nxt_universe_for_date(status_date: date) -> tuple[
    list[WatchSymbol],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    list[NxtTradingStatus],
]:
    client = NxtClient(get_response_cache())
    statuses = client.fetch_trading_status(status_date)
    symbols, markets, tradable_markets, unavailable_reasons = _universe_metadata(
        statuses
    )
    return symbols, markets, tradable_markets, unavailable_reasons, statuses


def _universe_metadata(
    statuses: list[NxtTradingStatus],
) -> tuple[
    list[WatchSymbol],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    symbols = [
        WatchSymbol(item.stock_code, item.stock_name or item.stock_code)
        for item in statuses
    ]
    markets = {item.stock_code: item.market for item in statuses}
    tradable_markets = {
        item.stock_code: (item.tradable_market or "-") for item in statuses
    }
    unavailable_reasons = {
        item.stock_code: (item.unavailable_reason or "거래불가")
        if item.is_unavailable
        else ""
        for item in statuses
    }
    return symbols, markets, tradable_markets, unavailable_reasons


def _latest_nxt_universe() -> tuple[
    date | None,
    list[WatchSymbol],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    for days_ago in range(8):
        status_date = today - timedelta(days=days_ago)
        (
            symbols,
            markets,
            tradable_markets,
            unavailable_reasons,
            _statuses,
        ) = _nxt_universe_for_date(status_date)
        if symbols:
            return (
                status_date,
                symbols,
                markets,
                tradable_markets,
                unavailable_reasons,
            )
    return None, [], {}, {}, {}


def _render_universe_counts(
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    unavailable_reasons: dict[str, str],
) -> None:
    reason_counts = Counter(
        reason.strip()
        for item in symbols
        if (reason := unavailable_reasons.get(item.symbol, "")).strip()
    )
    unavailable_count = sum(reason_counts.values())
    reason_summary = ", ".join(
        f"{reason}: {count:,}"
        for reason, count in sorted(
            reason_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ) or "사유 없음"
    counts = [
        ("NXT 종목수", len(symbols)),
        ("KOSPI 종목수", sum(markets.get(item.symbol) == "KOSPI" for item in symbols)),
        (
            "KOSDAQ 종목수",
            sum(markets.get(item.symbol) == "KOSDAQ" for item in symbols),
        ),
    ]
    count_items = [
        f"<span>{escape(label)} <strong>{value:,}</strong></span>"
        for label, value in counts
    ]
    unavailable_item = (
        f"<span>거래불가 종목수 <strong>{unavailable_count:,}</strong>"
        f'<span class="compact-unavailable-reasons">'
        f"({escape(reason_summary)})</span></span>"
    )
    count_items.insert(1, unavailable_item)
    items = "".join(count_items)
    st.markdown(
        f'<div class="compact-universe-counts">{items}</div>',
        unsafe_allow_html=True,
    )


def _render_notice_items(notices: tuple[str, ...] | list[str]) -> None:
    notice_items = "".join(f"<li>{escape(item)}</li>" for item in notices)
    with st.expander("유의사항", expanded=False):
        st.markdown(
            f'<div class="dashboard-notice"><ul>{notice_items}</ul></div>',
            unsafe_allow_html=True,
        )


def _render_dashboard_notices(*, historical: bool) -> None:
    if historical:
        notices = [
            "과거 화면은 NXT 공식 일별 시세와 KRX OPEN API 확정값을 DB에서 조회합니다.",
            (
                "KRX 거래량·거래대금은 세션 구분이 없는 확정 일별 누적값입니다. "
                "정규장과 시간외 일반·대량·바스켓 매매가 합산되므로 G1만 분리할 수 없습니다."
            ),
            (
                "KRX 합계는 KOSPI·KOSDAQ 대표지수 누적값의 합으로 전종목 합계가 아니며, "
                "외국주권·DR은 포함하지 않습니다."
            ),
            "과거 시가총액은 KRX OPEN API가 제공하는 확정값입니다.",
            "지수·선물·환율은 제공처와 갱신 시각이 달라 같은 시점의 값이 아닐 수 있습니다.",
        ]
    else:
        notices = [
            (
                "당일 수치는 전 종목을 나누어 순차 조회한 누적값으로, 종목별 수신 시각이 "
                "다르며 최대 1분 정도 늦을 수 있습니다."
            ),
            (
                "KRX 거래량·거래대금 합계는 API가 천주·백만원 단위로 제공한 값을 "
                "주·원으로 환산한 참고값입니다. 하위 단위의 반올림·절사 방식은 공개되지 않았습니다."
            ),
            (
                "장중 KRX 값은 조회 시점 누적값입니다. 확정 일별 값에는 정규장과 시간외 "
                "일반·대량·바스켓 매매가 합산되며 외국주권·DR은 포함하지 않습니다."
            ),
            (
                "NXT 가중 등락률은 현재가·비교가격·상장주식수가 모두 있는 종목만 계산하며, "
                "기준가격이 없으면 전일 KRX 종가를 사용합니다."
            ),
            "시가총액은 KRX 현재가와 상장주식수를 곱한 값입니다.",
        ]
    _render_notice_items(notices)


@st.fragment(run_every="2s")
def _render_rest_index_cards(
    collector: KisRestUniverseCollector,
    unavailable_reasons: dict[str, str],
) -> None:
    collector.heartbeat()
    index_quotes = collector.index_snapshot()
    future_quotes = collector.futures_snapshot()
    available_quotes = exclude_unavailable_nxt_quotes(
        collector.snapshot(),
        (
            symbol
            for symbol, reason in unavailable_reasons.items()
            if str(reason).strip()
        ),
    )
    weighted_rates = build_nxt_weighted_change_rates(
        available_quotes,
        collector.listed_shares_snapshot(),
    )
    card_quotes = [
        (name, index_quotes.get(name))
        for name in ["KRX TMI", "KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
    ] + [
        (name, future_quotes.get(name))
        for name in ["KOSPI200 선물", "KOSPI200 야간선물"]
    ] + [("달러-원", index_quotes.get("달러-원"))]
    columns = st.columns(len(card_quotes) + 1)
    _render_nxt_rate_metric_card(
        columns[0],
        reference_rate=weighted_rates["reference_rate"],
        krx_rate=weighted_rates["krx_rate"],
        reference_count=int(weighted_rates["reference_count"]),
        krx_count=int(weighted_rates["krx_count"]),
        historical=False,
    )
    for column, (name, quote) in zip(columns[1:], card_quotes):
        if quote is None:
            _render_index_metric_card(column, name, "대기")
            continue
        _render_index_metric_card(
            column,
            name,
            f"{quote.current_value:,.2f}",
            direction=quote.change_value or quote.change_rate,
            delta=f"{quote.change_value:+,.2f} ({quote.change_rate:+.2f}%)",
        )


@st.fragment(run_every="2s")
def _render_rest_universe_table(
    collector: KisRestUniverseCollector,
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    tradable_markets: dict[str, str],
    unavailable_reasons: dict[str, str],
) -> None:
    collector.heartbeat()
    status = collector.status()
    status_items = [
        ("API 상태", status.state),
        ("갱신 진행", f"{status.completed_count:,} / {status.universe_count:,}"),
        ("수신 종목", f"{status.quote_count:,}"),
        (
            "전체 갱신 완료",
            status.last_completed_at.strftime("%H:%M:%S")
            if status.last_completed_at
            else "대기",
        ),
        (
            "실제 소요시간",
            f"{status.cycle_seconds:.1f}초" if status.cycle_seconds is not None else "대기",
        ),
    ]
    status_html = "".join(
        '<div class="compact-status-item">'
        f'<div class="compact-status-label">{escape(label)}</div>'
        f'<div class="compact-status-value">{escape(value)}</div>'
        "</div>"
        for label, value in status_items
    )
    market_snapshot = exclude_unavailable_nxt_quotes(
        collector.snapshot(),
        (
            symbol
            for symbol, reason in unavailable_reasons.items()
            if str(reason).strip()
        ),
    )
    market_totals = build_market_totals(
        market_snapshot,
        collector.index_snapshot(),
    )
    if (
        status.last_completed_at is not None
        and status.quote_count >= status.universe_count
        and market_totals["nxt_volume"] is not None
        and market_totals["krx_volume"] is not None
    ):
        get_historical_market_store().save_live_metric(
            pd.Timestamp.now(tz="Asia/Seoul").date(),
            nxt_volume=int(market_totals["nxt_volume"]),
            nxt_amount=int(market_totals["nxt_amount"]),
            krx_volume=int(market_totals["krx_volume"]),
            krx_amount=int(market_totals["krx_amount"]),
            nxt_stock_count=len(symbols),
        )
    st.markdown(
        '<div class="dashboard-section-gap" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    _render_market_totals(market_totals)
    if status.quote_count < status.universe_count:
        st.caption(
            "NXT 합계는 전종목 수집이 끝나기 전까지 부분 합계입니다. "
            "KRX 합계는 KIS KOSPI·KOSDAQ 시장 누적값입니다."
        )

    st.markdown(
        '<div class="dashboard-table-gap" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### NXT 정규시장 종목별 현황")
    _render_universe_counts(symbols, markets, unavailable_reasons)
    filter_columns = st.columns([1, 2])
    market_filter = filter_columns[0].selectbox(
        "상장시장",
        ["전체", "KOSPI", "KOSDAQ"],
        key="rest_universe_market_filter",
    )
    search_term = filter_columns[1].text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key="rest_universe_search",
    ).strip()

    filtered = [
        item
        for item in symbols
        if (market_filter == "전체" or markets.get(item.symbol) == market_filter)
        and (
            not search_term
            or search_term.lower() in item.name.lower()
            or search_term in item.symbol
        )
    ]
    rows = build_comparison_rows(
        filtered,
        market_snapshot,
        collector.listed_shares_snapshot(),
    )
    rows.sort(
        key=lambda row: (
            row["nxt_amount"] is not None,
            int(row["nxt_amount"] or 0),
        ),
        reverse=True,
    )
    display_rows = [
        {
            "종목코드": str(row["종목코드"]),
            "종목명": str(row["종목명"]),
            "NXT 현재가": row["nxt_current_price"],
            "등락률": row["change_rate"],
            "괴리율": row["disparity_rate"],
            "NXT 거래량": row["nxt_volume"],
            "거래량비율": row["volume_ratio"],
            "NXT 거래대금": row["nxt_amount"],
            "거래대금비율": row["amount_ratio"],
            "시가총액": row["market_cap"],
            "KRX 현재가": row["krx_current_price"],
            "KRX 거래량": row["krx_volume"],
            "KRX 거래대금": row["krx_amount"],
            "거래가능시장": tradable_markets.get(str(row["종목코드"])) or "-",
            "NXT 거래불가사유": unavailable_reasons.get(str(row["종목코드"])) or "-",
        }
        for row in rows
    ]
    display_columns = [
        "종목코드",
        "종목명",
        "NXT 현재가",
        "등락률",
        "괴리율",
        "NXT 거래량",
        "거래량비율",
        "NXT 거래대금",
        "거래대금비율",
        "시가총액",
        "KRX 현재가",
        "KRX 거래량",
        "KRX 거래대금",
        "거래가능시장",
        "NXT 거래불가사유",
    ]
    display_frame = pd.DataFrame(display_rows, columns=display_columns)
    price_columns = [
        "NXT 현재가",
        "NXT 거래량",
        "NXT 거래대금",
        "시가총액",
        "KRX 현재가",
        "KRX 거래량",
        "KRX 거래대금",
    ]
    rate_columns = ["등락률", "괴리율", "거래량비율", "거래대금비율"]
    right_aligned_columns = price_columns + rate_columns
    display_table = (
        display_frame.style.format(
            {
                **{column: _format_price for column in price_columns},
                **{column: _format_rate for column in rate_columns},
            },
            na_rep="-",
        )
        .set_properties(subset=["종목코드"], **{"text-align": "center"})
        .set_properties(subset=["거래가능시장"], **{"text-align": "center"})
        .set_properties(
            subset=["종목명", "NXT 거래불가사유"],
            **{"text-align": "left"},
        )
        .set_properties(subset=right_aligned_columns, **{"text-align": "right"})
        .map(_change_rate_text_color, subset=["등락률"])
        .set_table_styles(
            [
                {
                    "selector": "th.col_heading",
                    "props": [("text-align", "center")],
                }
            ],
            overwrite=False,
        )
    )
    st.dataframe(
        display_table,
        hide_index=True,
        use_container_width=True,
        height=700,
        column_config={
            "종목코드": st.column_config.TextColumn(width="small"),
            "종목명": st.column_config.TextColumn(width="medium"),
            "거래가능시장": st.column_config.TextColumn(width="small"),
            "NXT 거래불가사유": st.column_config.TextColumn(width="medium"),
        },
    )
    st.markdown(
        f'<div class="compact-status-strip" role="status">{status_html}</div>',
        unsafe_allow_html=True,
    )
    if status.state == "오류":
        st.warning(status.message)
    _render_dashboard_notices(historical=False)


def _build_historical_quote_snapshot(
    status_date: date,
    statuses: list[NxtTradingStatus],
    krx_snapshot: KrxDailySnapshot,
) -> dict[tuple[str, str], RestQuote]:
    quotes = dict(krx_snapshot.stock_quotes)
    updated_at = pd.Timestamp(status_date, tz="Asia/Seoul").to_pydatetime()
    for status in statuses:
        if status.current_price is None or status.current_price <= 0:
            continue
        quotes[(status.stock_code, "NXT")] = RestQuote(
            market="NXT",
            symbol=status.stock_code,
            name=status.stock_name,
            current_price=status.current_price,
            reference_price=status.reference_price,
            cumulative_volume=status.cumulative_volume,
            cumulative_amount=status.cumulative_amount,
            updated_at=updated_at,
        )
    return quotes


def _historical_market_totals(
    statuses: list[NxtTradingStatus],
    quotes: dict[tuple[str, str], RestQuote],
    krx_snapshot: KrxDailySnapshot,
) -> dict[str, object]:
    totals = build_market_totals(quotes, krx_snapshot.index_quotes)
    available_statuses = [item for item in statuses if not item.is_unavailable]
    nxt_volume = sum(item.cumulative_volume for item in available_statuses)
    nxt_amount = sum(item.cumulative_amount for item in available_statuses)
    krx_volume = totals["krx_volume"]
    krx_amount = totals["krx_amount"]
    totals.update(
        {
            "nxt_volume": nxt_volume,
            "volume_ratio": (
                nxt_volume / int(krx_volume)
                if krx_volume is not None and int(krx_volume) > 0
                else None
            ),
            "nxt_amount": nxt_amount,
            "amount_ratio": (
                nxt_amount / int(krx_amount)
                if krx_amount is not None and int(krx_amount) > 0
                else None
            ),
        }
    )
    return totals


def _render_historical_index_cards(
    quotes: dict[tuple[str, str], RestQuote],
    krx_snapshot: KrxDailySnapshot,
    fx_quote: IndexQuote | None = None,
) -> None:
    weighted_rates = build_nxt_weighted_change_rates(
        quotes,
        krx_snapshot.listed_shares,
    )
    index_cards = [
        (name, krx_snapshot.index_quotes.get(name))
        for name in ["KRX TMI", "KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
    ]
    columns = st.columns(9)
    _render_nxt_rate_metric_card(
        columns[0],
        reference_rate=weighted_rates["reference_rate"],
        krx_rate=weighted_rates["krx_rate"],
        reference_count=int(weighted_rates["reference_count"]),
        krx_count=int(weighted_rates["krx_count"]),
        historical=True,
    )
    for column, (name, quote) in zip(columns[1:6], index_cards):
        if quote is None:
            _render_index_metric_card(column, name, "미수신")
            continue
        _render_index_metric_card(
            column,
            name,
            f"{quote.current_value:,.2f}",
            direction=quote.change_value or quote.change_rate,
            delta=f"{quote.change_value:+,.2f} ({quote.change_rate:+.2f}%)",
        )
    # Snapshots that were cached before futures support do not have this field.
    # Treat those as "not received" so a past-date lookup remains usable while
    # Streamlit replaces the stale resource cache.
    future_quotes = getattr(krx_snapshot, "future_quotes", {})
    future_quote = future_quotes.get("KOSPI200 선물")
    if future_quote is None:
        _render_index_metric_card(
            columns[6],
            "KOSPI200 선물",
            "미수신",
            tooltip="KRX OPEN API 선물 일별매매정보가 아직 DB에 저장되지 않았습니다.",
        )
    else:
        _render_index_metric_card(
            columns[6],
            "KOSPI200 선물",
            f"{future_quote.current_value:,.2f}",
            direction=future_quote.change_value or future_quote.change_rate,
            delta=(
                f"{future_quote.change_value:+,.2f} "
                f"({future_quote.change_rate:+.2f}%)"
            ),
            tooltip=f"최근월물 {future_quote.code}",
        )
    _render_index_metric_card(
        columns[7],
        "KOSPI200 야간선물",
        "미제공",
        tooltip="KRX OPEN API 과거 화면의 제공 범위에 포함되지 않습니다.",
    )
    if fx_quote is None:
        _render_index_metric_card(
            columns[8],
            "달러-원",
            "미수신",
            tooltip="KIS 달러-원(KMB) 일별 환율이 DB에 저장되지 않았습니다.",
        )
    else:
        _render_index_metric_card(
            columns[8],
            "달러-원",
            f"{fx_quote.current_value:,.2f}",
            direction=fx_quote.change_value or fx_quote.change_rate,
            delta=f"{fx_quote.change_value:+,.2f} ({fx_quote.change_rate:+.2f}%)",
            tooltip="KIS 달러-원(KMB) 환율",
        )


def _render_historical_nxt_dashboard(
    status_date: date,
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    tradable_markets: dict[str, str],
    unavailable_reasons: dict[str, str],
    statuses: list[NxtTradingStatus],
    krx_snapshot: KrxDailySnapshot,
    fx_quote: IndexQuote | None = None,
) -> None:
    st.caption(f"{status_date:%Y-%m-%d} NXT 공식 종가 · KRX OPEN API 종가 기준")
    quotes = exclude_unavailable_nxt_quotes(
        _build_historical_quote_snapshot(status_date, statuses, krx_snapshot),
        (
            symbol
            for symbol, reason in unavailable_reasons.items()
            if str(reason).strip()
        ),
    )
    _render_historical_index_cards(quotes, krx_snapshot, fx_quote)
    st.markdown(
        '<div class="dashboard-section-gap" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    _render_market_totals(
        _historical_market_totals(statuses, quotes, krx_snapshot)
    )

    st.markdown(
        '<div class="dashboard-table-gap" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### NXT 정규시장 종목별 현황")
    _render_universe_counts(symbols, markets, unavailable_reasons)
    filter_columns = st.columns([1, 2])
    market_filter = filter_columns[0].selectbox(
        "상장시장",
        ["전체", "KOSPI", "KOSDAQ"],
        key="historical_nxt_market_filter",
    )
    search_term = filter_columns[1].text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key="historical_nxt_search",
    ).strip()
    filtered = [
        item
        for item in symbols
        if (market_filter == "전체" or markets.get(item.symbol) == market_filter)
        and (
            not search_term
            or search_term.lower() in item.name.lower()
            or search_term in item.symbol
        )
    ]
    rows = build_comparison_rows(filtered, quotes, krx_snapshot.listed_shares)
    rows.sort(
        key=lambda row: (
            row["nxt_amount"] is not None,
            int(row["nxt_amount"] or 0),
        ),
        reverse=True,
    )
    display_rows = [
        {
            "종목코드": str(row["종목코드"]),
            "종목명": str(row["종목명"]),
            "NXT 현재가": row["nxt_current_price"],
            "등락률": row["change_rate"],
            "괴리율": row["disparity_rate"],
            "NXT 거래량": row["nxt_volume"],
            "거래량비율": row["volume_ratio"],
            "NXT 거래대금": row["nxt_amount"],
            "거래대금비율": row["amount_ratio"],
            "시가총액": row["market_cap"],
            "KRX 현재가": row["krx_current_price"],
            "KRX 거래량": row["krx_volume"],
            "KRX 거래대금": row["krx_amount"],
            "거래가능시장": tradable_markets.get(str(row["종목코드"])) or "-",
            "NXT 거래불가사유": unavailable_reasons.get(str(row["종목코드"])) or "-",
        }
        for row in rows
    ]
    display_columns = [
        "종목코드",
        "종목명",
        "NXT 현재가",
        "등락률",
        "괴리율",
        "NXT 거래량",
        "거래량비율",
        "NXT 거래대금",
        "거래대금비율",
        "시가총액",
        "KRX 현재가",
        "KRX 거래량",
        "KRX 거래대금",
        "거래가능시장",
        "NXT 거래불가사유",
    ]
    display_frame = pd.DataFrame(display_rows, columns=display_columns)
    price_columns = [
        "NXT 현재가",
        "NXT 거래량",
        "NXT 거래대금",
        "시가총액",
        "KRX 현재가",
        "KRX 거래량",
        "KRX 거래대금",
    ]
    rate_columns = ["등락률", "괴리율", "거래량비율", "거래대금비율"]
    display_table = (
        display_frame.style.format(
            {
                **{column: _format_price for column in price_columns},
                **{column: _format_rate for column in rate_columns},
            },
            na_rep="-",
        )
        .set_properties(subset=["종목코드"], **{"text-align": "center"})
        .set_properties(subset=["거래가능시장"], **{"text-align": "center"})
        .set_properties(
            subset=["종목명", "NXT 거래불가사유"],
            **{"text-align": "left"},
        )
        .set_properties(
            subset=price_columns + rate_columns,
            **{"text-align": "right"},
        )
        .map(_change_rate_text_color, subset=["등락률"])
        .set_table_styles(
            [
                {
                    "selector": "th.col_heading",
                    "props": [("text-align", "center")],
                }
            ],
            overwrite=False,
        )
    )
    st.dataframe(
        display_table,
        hide_index=True,
        use_container_width=True,
        height=700,
        column_config={
            "종목코드": st.column_config.TextColumn(width="small"),
            "종목명": st.column_config.TextColumn(width="medium"),
            "거래가능시장": st.column_config.TextColumn(width="small"),
            "NXT 거래불가사유": st.column_config.TextColumn(width="medium"),
        },
    )
    _render_dashboard_notices(historical=True)


def rest_universe_page() -> None:
    st.markdown(
        '<h1 class="nextrade-dashboard-title">NEXTRADE 시장운영 DASHBOARD</h1>',
        unsafe_allow_html=True,
    )
    with st.spinner("최신 NXT 전체 종목 목록을 불러오고 있습니다..."):
        (
            universe_date,
            symbols,
            markets,
            tradable_markets,
            unavailable_reasons,
        ) = _latest_nxt_universe()
    if universe_date is None or not symbols:
        st.error("최근 NXT 전체 종목 목록을 불러오지 못했습니다.")
        return

    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    date_columns = st.columns([1, 4])
    selected_date = date_columns[0].date_input(
        "조회일자",
        value=universe_date,
        min_value=NXT_LAUNCH_DATE,
        max_value=today,
        format="YYYY-MM-DD",
        key="nxt_dashboard_date",
    )
    if selected_date != today or universe_date != today:
        history_store = get_historical_market_store()
        fx_quote = history_store.load_fx_quote(selected_date)
        stored_snapshot = history_store.load_historical_snapshot(selected_date)
        if stored_snapshot is not None:
            historical_statuses, krx_snapshot = stored_snapshot
            (
                historical_symbols,
                historical_markets,
                historical_tradable_markets,
                historical_unavailable_reasons,
            ) = _universe_metadata(historical_statuses)
        else:
            with st.spinner(
                f"{selected_date:%Y-%m-%d} 데이터를 최초 1회 DB에 저장하고 있습니다..."
            ):
                (
                    historical_symbols,
                    historical_markets,
                    historical_tradable_markets,
                    historical_unavailable_reasons,
                    historical_statuses,
                ) = _nxt_universe_for_date(selected_date)
            if not historical_symbols:
                st.warning("선택한 일자에는 NXT 정규시장 데이터가 없습니다.")
                return
            krx_key = _secret_value("KRX_KEY")
            if not krx_key:
                st.warning("과거 KRX 비교값 조회를 위해 secrets.toml에 KRX_KEY가 필요합니다.")
                return
            try:
                with st.spinner(
                    f"{selected_date:%Y-%m-%d} KRX 종목·지수 데이터를 불러오고 있습니다..."
                ):
                    krx_snapshot = KrxOpenApiClient(
                        krx_key,
                        get_response_cache(),
                        persist_raw_cache=False,
                    ).fetch_daily_snapshot(selected_date)
                    history_store.save_historical_snapshot(
                        selected_date,
                        historical_statuses,
                        krx_snapshot,
                    )
                    history_store.rebuild_derived_metrics()
            except KrxOpenApiError as exc:
                st.error(str(exc))
                return
        _render_historical_nxt_dashboard(
            selected_date,
            historical_symbols,
            historical_markets,
            historical_tradable_markets,
            historical_unavailable_reasons,
            historical_statuses,
            krx_snapshot,
            fx_quote,
        )
        return

    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        st.warning("KIS App Key와 App Secret을 서버 설정에 등록해야 조회할 수 있습니다.")
        return

    collector = get_rest_universe_runtime(
        app_key,
        app_secret,
        REST_UNIVERSE_RUNTIME_VERSION,
    )
    if (
        not hasattr(collector, "index_snapshot")
        or not hasattr(collector, "futures_snapshot")
        or not hasattr(collector, "listed_shares_snapshot")
    ):
        collector.stop()
        get_rest_universe_runtime.clear()
        st.rerun()
    collector.start(symbols, REST_UNIVERSE_REFRESH_SECONDS)
    _render_rest_index_cards(collector, unavailable_reasons)

    _render_rest_universe_table(
        collector,
        symbols,
        markets,
        tradable_markets,
        unavailable_reasons,
    )


def _history_metrics_frame(metrics: list[DailyMarketMetric]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "일자": item.trade_date,
                "NXT 지수": item.nxt_index_value,
                "NXT 등락률(기준가 대비)": item.nxt_change_rate,
                "TMI 지수": item.tmi_index_value,
                "TMI 등락률": item.tmi_change_rate,
                "NXT 거래량": item.nxt_volume,
                "NXT 거래대금": item.nxt_amount,
                "KRX 거래량": item.krx_volume,
                "KRX 거래대금": item.krx_amount,
                "거래량비율": item.volume_ratio,
                "거래대금비율": item.amount_ratio,
                "상태": "확정" if item.is_final else "당일 누적",
            }
            for item in metrics
        ]
    )


def _format_chart_date_axis(chart: object, *, title: str = "일자") -> None:
    """Plotly 날짜 축을 대시보드 공통 월 표기로 맞춥니다."""

    chart.update_xaxes(title=title, tickformat="'%y. %m")


def _render_history_market_charts(frame: pd.DataFrame) -> None:
    color_map = {"NXT": "#d71920", "KRX": "#475569"}
    volume = frame[["일자", "NXT 거래량", "KRX 거래량"]].melt(
        id_vars="일자",
        var_name="시장",
        value_name="거래량",
    )
    volume["시장"] = volume["시장"].str.replace(" 거래량", "", regex=False)
    volume["거래량(억주)"] = volume["거래량"] / 100_000_000
    amount = frame[["일자", "NXT 거래대금", "KRX 거래대금"]].melt(
        id_vars="일자",
        var_name="시장",
        value_name="거래대금",
    )
    amount["시장"] = amount["시장"].str.replace(" 거래대금", "", regex=False)
    amount["거래대금(조원)"] = amount["거래대금"] / 1_000_000_000_000
    chart_columns = st.columns(2)
    volume_chart = px.line(
        volume,
        x="일자",
        y="거래량(억주)",
        color="시장",
        color_discrete_map=color_map,
        title="시장별 거래량 추이",
    )
    volume_chart.update_traces(line_width=2, hovertemplate="%{y:,.2f}억주<extra></extra>")
    volume_chart.update_yaxes(title="억주", tickformat=",.2f")
    _format_chart_date_axis(volume_chart)
    volume_chart.update_layout(
        hovermode="x unified",
        legend_title_text="",
        margin=dict(l=10, r=10, t=50, b=10),
    )
    chart_columns[0].plotly_chart(volume_chart, use_container_width=True)

    amount_chart = px.line(
        amount,
        x="일자",
        y="거래대금(조원)",
        color="시장",
        color_discrete_map=color_map,
        title="시장별 거래대금 추이",
    )
    amount_chart.update_traces(line_width=2, hovertemplate="%{y:,.2f}조원<extra></extra>")
    amount_chart.update_yaxes(title="조원", tickformat=",.2f")
    _format_chart_date_axis(amount_chart)
    amount_chart.update_layout(
        hovermode="x unified",
        legend_title_text="",
        margin=dict(l=10, r=10, t=50, b=10),
    )
    chart_columns[1].plotly_chart(amount_chart, use_container_width=True)


def _ratio_line_chart(
    frame: pd.DataFrame,
    *,
    x: str,
    y: str,
    title: str,
    color: str,
    benchmark: float | None = None,
) -> object:
    chart = px.line(
        frame,
        x=x,
        y=y,
        title=title,
        color_discrete_sequence=[color],
    )
    chart.update_traces(line_width=2, hovertemplate="%{y:.2f}%<extra></extra>")
    if benchmark is not None:
        chart.add_hline(
            y=benchmark,
            line_width=1.5,
            line_dash="dash",
            line_color="#475569",
            annotation_text=f"기준 {benchmark:.0f}%",
            annotation_position="top left",
        )
    chart.update_yaxes(title="%", ticksuffix="%")
    _format_chart_date_axis(chart)
    chart.update_layout(
        hovermode="x unified",
        showlegend=False,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    return chart


def _ratio_bar_chart(
    frame: pd.DataFrame,
    *,
    x: str,
    y: str,
    title: str,
    color: str,
    benchmark: float | None = None,
) -> object:
    chart = px.bar(
        frame,
        x=x,
        y=y,
        title=title,
        color_discrete_sequence=[color],
    )
    chart.update_traces(
        marker_line_color="#1d4ed8",
        marker_line_width=0.7,
        hovertemplate="%{y:.2f}%<extra></extra>",
        texttemplate="%{y:.2f}%",
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="#ffffff", size=13),
    )
    if benchmark is not None:
        chart.add_hline(
            y=benchmark,
            line_width=1.5,
            line_dash="dash",
            line_color="#475569",
            annotation_text=f"기준 {benchmark:.0f}%",
            annotation_position="top left",
        )
    chart.update_yaxes(title="%", ticksuffix="%", rangemode="tozero")
    category_values = frame[x].astype(str).tolist()
    chart.update_xaxes(
        title="월말 기준",
        type="category",
        categoryorder="array",
        categoryarray=category_values,
    )
    chart.update_layout(
        hovermode="x unified",
        showlegend=False,
        bargap=0.25,
        margin=dict(l=10, r=10, t=50, b=10),
    )
    return chart


def _render_history_ratio_charts(
    frame: pd.DataFrame,
    all_final_metrics: list[DailyMarketMetric],
    start_date: date,
    end_date: date,
) -> None:
    st.markdown("#### NXT/KRX 거래 비율 추이")
    daily_tab, cumulative_tab = st.tabs(["거래량비율 · 일일", "거래량비율 · 6개월 누적"])
    daily_volume = frame[["일자", "거래량비율"]].dropna().copy()
    daily_volume["거래량비율"] = daily_volume["거래량비율"] * 100
    daily_tab.plotly_chart(
        _ratio_line_chart(
            daily_volume,
            x="일자",
            y="거래량비율",
            title="NXT/KRX 일일 거래량비율",
            color="#2563eb",
            benchmark=15,
        ),
        use_container_width=True,
    )

    monthly_rows = [
        item
        for item in monthly_six_month_volume_ratios(all_final_metrics)
        if start_date <= item["month_end"] <= end_date
    ]
    monthly_frame = pd.DataFrame(monthly_rows)
    if monthly_frame.empty:
        cumulative_tab.info("선택 기간에는 완전한 6개월 누적 구간이 없습니다.")
    else:
        monthly_frame["6개월 누적 거래량비율"] = monthly_frame["volume_ratio"] * 100
        monthly_frame["월말 기준"] = pd.to_datetime(
            monthly_frame["month_end"]
        ).dt.strftime("'%y. %m")
        cumulative_tab.plotly_chart(
            _ratio_bar_chart(
                monthly_frame,
                x="월말 기준",
                y="6개월 누적 거래량비율",
                title="월별 최근 6개월 누적 NXT/KRX 거래량비율",
                color="#2563eb",
                benchmark=15,
            ),
            use_container_width=True,
        )
        cumulative_tab.caption(
            "월말 기준 최근 6개월 NXT/KRX 누적 거래량비율입니다. "
            "최신 월은 조회 종료일까지 반영합니다."
        )

    amount_frame = frame[["일자", "거래대금비율"]].dropna().copy()
    amount_frame["거래대금비율"] = amount_frame["거래대금비율"] * 100
    st.plotly_chart(
        _ratio_line_chart(
            amount_frame,
            x="일자",
            y="거래대금비율",
            title="NXT/KRX 일일 거래대금비율",
            color="#f97316",
        ),
        use_container_width=True,
    )


def market_history_page() -> None:
    st.title("NXT·KRX 일별 거래 추이")
    store = get_historical_market_store()
    all_metrics = store.list_metrics(NXT_LAUNCH_DATE, date.today())
    if not all_metrics:
        st.warning(
            "저장된 일별 데이터가 없습니다. scripts/backfill_market_history.py를 먼저 실행하세요."
        )
        return

    final_metrics = [item for item in all_metrics if item.is_final]
    if not final_metrics:
        st.warning("확정된 직전 거래일 데이터가 없습니다.")
        return
    first_date = all_metrics[0].trade_date
    last_date = all_metrics[-1].trade_date
    last_final_date = final_metrics[-1].trade_date
    date_columns = st.columns(2)
    start_date = date_columns[0].date_input(
        "시작일",
        value=first_date,
        min_value=NXT_LAUNCH_DATE,
        max_value=last_date,
        format="YYYY-MM-DD",
        key="market_history_start",
    )
    end_date = date_columns[1].date_input(
        "종료일",
        value=last_final_date,
        min_value=NXT_LAUNCH_DATE,
        max_value=last_date,
        format="YYYY-MM-DD",
        key="market_history_end",
    )
    if end_date < start_date:
        st.warning("종료일은 시작일보다 빠를 수 없습니다.")
        return
    metrics = store.list_metrics(start_date, end_date)
    if not metrics:
        st.info("선택 기간에 저장된 거래일 데이터가 없습니다.")
        return
    frame = _history_metrics_frame(metrics)
    latest = metrics[-1]
    st.caption(
        f"DB 수록 {len(metrics):,}거래일 · 최근값 {latest.trade_date:%Y-%m-%d} "
        f"({'확정' if latest.is_final else '당일 누적'})"
    )
    average_window_start = max(
        NXT_LAUNCH_DATE,
        six_calendar_month_window_start(last_final_date),
    )
    average_metrics = [
        item
        for item in final_metrics
        if average_window_start <= item.trade_date <= last_final_date
    ]
    actual_average_start = average_metrics[0].trade_date
    _render_market_totals(
        average_market_totals(average_metrics),
        title="최근 6개월 시장별 일평균 거래량·거래대금 현황",
        value_label="일평균",
    )
    st.caption(
        f"평균 산정기간 {actual_average_start:%Y-%m-%d}~"
        f"{last_final_date:%Y-%m-%d} · 확정 거래일 {len(average_metrics):,}일"
    )
    _render_history_ratio_charts(
        frame,
        final_metrics,
        start_date,
        end_date,
    )

    st.markdown("#### 일별 데이터")
    table = (
        frame.sort_values("일자", ascending=False)
        .style.format(
            {
                "NXT 지수": lambda value: "-" if pd.isna(value) else f"{float(value):,.2f}",
                "NXT 등락률(기준가 대비)": _format_rate,
                "TMI 지수": lambda value: "-" if pd.isna(value) else f"{float(value):,.2f}",
                "TMI 등락률": _format_rate,
                "NXT 거래량": lambda value: "-" if pd.isna(value) else f"{int(value):,}",
                "NXT 거래대금": lambda value: "-" if pd.isna(value) else f"{int(value):,}",
                "KRX 거래량": lambda value: "-" if pd.isna(value) else f"{int(value):,}",
                "KRX 거래대금": lambda value: "-" if pd.isna(value) else f"{int(value):,}",
                "거래량비율": _format_rate,
                "거래대금비율": _format_rate,
            },
            na_rep="-",
        )
        .set_properties(
            subset=[
                "NXT 지수",
                "NXT 등락률(기준가 대비)",
                "TMI 지수",
                "TMI 등락률",
                "NXT 거래량",
                "NXT 거래대금",
                "KRX 거래량",
                "KRX 거래대금",
                "거래량비율",
                "거래대금비율",
            ],
            **{"text-align": "right"},
        )
        .set_properties(subset=["일자", "상태"], **{"text-align": "center"})
        .set_table_styles(
            [{"selector": "th.col_heading", "props": [("text-align", "center")]}],
            overwrite=False,
        )
    )
    st.dataframe(table, hide_index=True, use_container_width=True, height=700)
    _render_notice_items(
        [
            "이 화면은 외부 API를 호출하지 않고 SQLite에 저장된 값만 조회합니다.",
            (
                "NXT 지수는 2025-04-01을 100으로 두고 시가총액 가중 등락률을 "
                "거래일별로 연속 적용한 참고지수입니다."
            ),
            "KRX TMI는 KRX OPEN API 종가, 달러-원은 00증권의 KMB 환율을 사용합니다.",
            (
                "KRX 합계는 KOSPI·KOSDAQ 대표지수 누적값의 합으로 전종목 합계가 아니며, "
                "외국주권·DR은 포함하지 않습니다."
            ),
            (
                "당일 누적 행은 확정 전 참고값입니다. 확정값은 데이터 제공처 갱신 후 "
                "다음 저장 작업에서 반영됩니다."
            ),
        ]
    )
    _render_history_market_charts(frame)


NXT_LIMIT_COLUMNS = [
    "종목코드",
    "종목명",
    "상장시장",
    "거래가능시장",
    "기준가격",
    "상한가",
    "하한가",
    "시가",
    "고가",
    "저가",
    "종가",
    "거래량",
    "거래대금",
    "수신시각",
    "상한가 도달",
    "하한가 도달",
]


def _nxt_limit_observation(
    *,
    stock_code: str,
    stock_name: str,
    market: str,
    tradable_market: str,
    reference_price: int | None,
    upper_limit_price: int | None,
    lower_limit_price: int | None,
    open_price: int | None,
    high_price: int | None,
    low_price: int | None,
    close_price: int | None,
    volume: int,
    amount: int,
    received_at: str,
) -> dict[str, object]:
    return {
        "종목코드": stock_code,
        "종목명": stock_name,
        "상장시장": market,
        "거래가능시장": tradable_market or "-",
        "기준가격": reference_price,
        "상한가": upper_limit_price,
        "하한가": lower_limit_price,
        "시가": open_price,
        "고가": high_price,
        "저가": low_price,
        "종가": close_price,
        "거래량": volume,
        "거래대금": amount,
        "수신시각": received_at or "-",
        "상한가 도달": reached_limit_points(
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            limit_price=upper_limit_price,
        ),
        "하한가 도달": reached_limit_points(
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            limit_price=lower_limit_price,
        ),
    }


def _historical_nxt_limit_frame(
    statuses: list[NxtTradingStatus],
) -> pd.DataFrame:
    rows = [
        _nxt_limit_observation(
            stock_code=item.stock_code,
            stock_name=item.stock_name,
            market=item.market,
            tradable_market=item.tradable_market,
            reference_price=item.reference_price,
            upper_limit_price=item.upper_limit_price,
            lower_limit_price=item.lower_limit_price,
            open_price=item.open_price,
            high_price=item.high_price,
            low_price=item.low_price,
            close_price=item.current_price,
            volume=item.cumulative_volume,
            amount=item.cumulative_amount,
            received_at=item.quote_time,
        )
        for item in statuses
    ]
    return pd.DataFrame(rows, columns=NXT_LIMIT_COLUMNS)


def _live_nxt_limit_frame(
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    tradable_markets: dict[str, str],
    quotes: dict[tuple[str, str], RestQuote],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for item in symbols:
        quote = quotes.get((item.symbol, NXT))
        if quote is None:
            continue
        rows.append(
            _nxt_limit_observation(
                stock_code=item.symbol,
                stock_name=item.name,
                market=markets.get(item.symbol, ""),
                tradable_market=tradable_markets.get(item.symbol, ""),
                reference_price=quote.reference_price,
                upper_limit_price=getattr(quote, "upper_limit_price", None),
                lower_limit_price=getattr(quote, "lower_limit_price", None),
                open_price=getattr(quote, "open_price", None),
                high_price=getattr(quote, "high_price", None),
                low_price=getattr(quote, "low_price", None),
                close_price=quote.current_price,
                volume=quote.cumulative_volume,
                amount=quote.cumulative_amount,
                received_at=quote.updated_at.strftime("%H:%M:%S"),
            )
        )
    return pd.DataFrame(rows, columns=NXT_LIMIT_COLUMNS)


def _nxt_limit_hit_rows(
    frame: pd.DataFrame,
    *,
    hit_times: dict[tuple[str, str], str] | None = None,
) -> pd.DataFrame:
    hit_times = hit_times or {}
    rows: list[dict[str, object]] = []
    for item in frame.to_dict("records"):
        for direction in ("상한가", "하한가"):
            hits = tuple(item.get(f"{direction} 도달") or ())
            if not hits:
                continue
            row = dict(item)
            row["도달구분"] = direction
            stored_time = (
                "OPEN"
                if "시가" in hits
                else hit_times.get((str(item["종목코드"]), direction))
            )
            row["도달시점"] = format_limit_hit_time(stored_time)
            row["도달개수"] = len(hits)
            rows.append(row)
    return pd.DataFrame(
        rows,
        columns=[*NXT_LIMIT_COLUMNS, "도달구분", "도달시점", "도달개수"],
    )


def _nxt_limit_proximity_rows(
    frame: pd.DataFrame,
    *,
    hit_times: dict[tuple[str, str], str] | None = None,
) -> pd.DataFrame:
    hit_times = hit_times or {}
    rows: list[dict[str, object]] = []
    price_fields = (
        ("시가", "시가"),
        ("고가", "고가"),
        ("저가", "저가"),
        ("종가", "종가"),
    )
    for item in frame.to_dict("records"):
        market = str(item.get("상장시장") or "")
        for direction in ("상한가", "하한가"):
            limit_price = item.get(direction)
            proximity_points = reached_limit_proximity_points(
                open_price=item.get("시가"),
                high_price=item.get("고가"),
                low_price=item.get("저가"),
                close_price=item.get("종가"),
                limit_price=limit_price,
                direction=direction,
                max_ticks=NXT_LIMIT_PROXIMITY_TICKS,
                market=market,
            )
            if not proximity_points:
                continue
            candidates: list[tuple[int, int]] = []
            for label, field_name in price_fields:
                if label not in proximity_points:
                    continue
                raw_price = item.get(field_name)
                distance = limit_proximity_ticks(
                    None if raw_price is None or pd.isna(raw_price) else int(raw_price),
                    None
                    if limit_price is None or pd.isna(limit_price)
                    else int(limit_price),
                    direction,
                    max_ticks=NXT_LIMIT_PROXIMITY_TICKS,
                    market=market,
                )
                if distance is not None:
                    candidates.append((distance, int(raw_price)))
            if not candidates:
                continue
            distance, closest_price = min(
                candidates,
                key=lambda value: (
                    value[0],
                    -value[1] if direction == "상한가" else value[1],
                ),
            )
            row = dict(item)
            row["근접구분"] = direction
            row["근접시점"] = format_limit_hit_time(
                "OPEN"
                if "시가" in proximity_points
                else hit_times.get((str(item["종목코드"]), direction))
            )
            row["최근접가격"] = closest_price
            row["잔여틱"] = distance
            row["근접지점"] = proximity_points
            rows.append(row)
    return pd.DataFrame(
        rows,
        columns=[
            *NXT_LIMIT_COLUMNS,
            "근접구분",
            "근접시점",
            "최근접가격",
            "잔여틱",
            "근접지점",
        ],
    )


def _fetch_and_store_nxt_limit_hit_times(
    client: KisRestClient,
    lookup_gate: NxtMinuteLookupGate,
    store: HistoricalMarketStore,
    lookup_key: tuple[str, str],
    symbol: str,
    selected_date: date,
    targets: dict[str, int],
    end_time: str,
) -> None:
    try:
        found_times = client.fetch_nxt_limit_hit_times(
            symbol,
            selected_date,
            targets,
            end_time=end_time,
        )
        symbol_values = {
            (symbol, direction): hit_time
            for direction, hit_time in found_times.items()
        }
        if symbol_values:
            store.save_nxt_limit_hit_times(selected_date, symbol_values)
        else:
            LOGGER.warning(
                "NXT limit-hit minute lookup returned no match: date=%s symbol=%s",
                selected_date.isoformat(),
                symbol,
            )
    except Exception as exc:
        LOGGER.warning(
            "NXT limit-hit minute lookup failed: date=%s symbol=%s error=%s",
            selected_date.isoformat(),
            symbol,
            exc,
        )
        lookup_gate.fail(lookup_key, str(exc))
        return
    lookup_gate.complete(lookup_key, has_result=bool(symbol_values))


def _fetch_and_store_nxt_limit_proximity_times(
    client: KisRestClient,
    lookup_gate: NxtMinuteLookupGate,
    store: HistoricalMarketStore,
    lookup_key: tuple[str, str],
    symbol: str,
    selected_date: date,
    targets: tuple[tuple[str, int, str], ...],
    end_time: str,
) -> None:
    try:
        bars = client.fetch_nxt_minute_bars(
            symbol,
            selected_date,
            end_time=end_time,
        )
        minute_prices = [
            (
                bar.trade_time,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
            )
            for bar in bars
        ]
        symbol_values: dict[tuple[str, str], str] = {}
        for direction, limit_price, market in targets:
            hit_time = first_limit_proximity_time(
                minute_prices,
                limit_price,
                direction,
                max_ticks=NXT_LIMIT_PROXIMITY_TICKS,
                market=market,
            )
            if hit_time is not None:
                symbol_values[(symbol, direction)] = hit_time
        if symbol_values:
            store.save_nxt_limit_proximity_times(selected_date, symbol_values)
        else:
            LOGGER.warning(
                "NXT proximity minute lookup returned no match: date=%s symbol=%s",
                selected_date.isoformat(),
                symbol,
            )
    except Exception as exc:
        LOGGER.warning(
            "NXT proximity minute lookup failed: date=%s symbol=%s error=%s",
            selected_date.isoformat(),
            symbol,
            exc,
        )
        lookup_gate.fail(lookup_key, str(exc))
        return
    lookup_gate.complete(lookup_key, has_result=bool(symbol_values))


def _resolve_nxt_limit_hit_times(
    selected_date: date,
    hit_rows: pd.DataFrame,
) -> tuple[dict[tuple[str, str], str], str | None]:
    """후보 종목만 분봉 조회해 최초 도달시각을 DB에 보강합니다."""

    store = get_historical_market_store()
    resolved = store.load_nxt_limit_hit_times(selected_date)
    new_values: dict[tuple[str, str], str] = {}
    missing_by_symbol: dict[str, list[tuple[str, int]]] = {}
    for item in hit_rows.to_dict("records"):
        symbol = str(item["종목코드"])
        direction = str(item["도달구분"])
        key = (symbol, direction)
        hits = tuple(item.get(f"{direction} 도달") or ())
        if "시가" in hits:
            if resolved.get(key) != "OPEN":
                new_values[key] = "OPEN"
            continue
        if key in resolved:
            continue
        raw_limit_price = item.get(direction)
        if raw_limit_price is None or pd.isna(raw_limit_price):
            continue
        missing_by_symbol.setdefault(symbol, []).append(
            (direction, int(raw_limit_price))
        )

    if new_values:
        store.save_nxt_limit_hit_times(selected_date, new_values)
        resolved.update(new_values)
    if not missing_by_symbol:
        return resolved, None

    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    now = pd.Timestamp.now(tz="Asia/Seoul")
    if selected_date == today and is_nxt_morning_break(now.to_pydatetime()):
        return (
            resolved,
            "NXT 08:50~09:00 휴장으로 시세·분봉 조회를 일시 중지합니다.",
        )
    earliest_available = (pd.Timestamp(today) - pd.DateOffset(years=1)).date()
    if selected_date < earliest_available:
        return (
            resolved,
            "NXT 분봉 보관 범위를 지난 날짜는 시가 외 최초 기록시각을 확인할 수 없습니다.",
        )
    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        return resolved, "최초 기록시각 확인에는 KIS App Key와 App Secret이 필요합니다."

    end_time = "200000"
    if selected_date == today:
        if now.strftime("%H%M%S") < "080000":
            return resolved, None
        end_time = min(now.strftime("%H%M%S"), "200000")
    client = get_nxt_minute_kis_rest_client(
        app_key,
        app_secret,
        KIS_SHARED_CLIENT_RUNTIME_VERSION,
    )
    lookup_gate = get_nxt_minute_lookup_gate(KIS_SHARED_CLIENT_RUNTIME_VERSION)
    executor = get_nxt_minute_lookup_executor(KIS_SHARED_CLIENT_RUNTIME_VERSION)
    scheduled_count = 0
    for symbol, targets in missing_by_symbol.items():
        lookup_key = (selected_date.isoformat(), symbol)
        permit = lookup_gate.claim(lookup_key)
        if not permit.allowed:
            continue
        try:
            executor.submit(
                _fetch_and_store_nxt_limit_hit_times,
                client,
                lookup_gate,
                store,
                lookup_key,
                symbol,
                selected_date,
                dict(targets),
                end_time,
            )
        except Exception as exc:
            lookup_gate.fail(lookup_key, str(exc))
            continue
        scheduled_count += 1
    note = "최초 기록시각을 순차 확인 중입니다." if scheduled_count else None
    return resolved, note


def _resolve_nxt_limit_proximity_times(
    selected_date: date,
    proximity_rows: pd.DataFrame,
) -> tuple[dict[tuple[str, str], str], str | None]:
    """3호가 근접 후보의 최초 진입시각을 분봉으로 확인해 DB에 저장합니다."""

    store = get_historical_market_store()
    resolved = store.load_nxt_limit_proximity_times(selected_date)
    new_values: dict[tuple[str, str], str] = {}
    missing_by_symbol: dict[str, list[tuple[str, int, str]]] = {}
    for item in proximity_rows.to_dict("records"):
        symbol = str(item["종목코드"])
        direction = str(item["근접구분"])
        key = (symbol, direction)
        points = tuple(item.get("근접지점") or ())
        if "시가" in points:
            if resolved.get(key) != "OPEN":
                new_values[key] = "OPEN"
            continue
        if key in resolved:
            continue
        raw_limit_price = item.get(direction)
        if raw_limit_price is None or pd.isna(raw_limit_price):
            continue
        missing_by_symbol.setdefault(symbol, []).append(
            (
                direction,
                int(raw_limit_price),
                str(item.get("상장시장") or ""),
            )
        )

    if new_values:
        store.save_nxt_limit_proximity_times(selected_date, new_values)
        resolved.update(new_values)
    if not missing_by_symbol:
        return resolved, None

    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    now = pd.Timestamp.now(tz="Asia/Seoul")
    if selected_date == today and is_nxt_morning_break(now.to_pydatetime()):
        return (
            resolved,
            "NXT 08:50~09:00 휴장으로 시세·분봉 조회를 일시 중지합니다.",
        )
    earliest_available = (pd.Timestamp(today) - pd.DateOffset(years=1)).date()
    if selected_date < earliest_available:
        return (
            resolved,
            "NXT 분봉 보관 범위를 지난 날짜는 시가 외 최초 기록시각을 확인할 수 없습니다.",
        )
    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        return resolved, "최초 기록시각 확인에는 KIS App Key와 App Secret이 필요합니다."

    end_time = "200000"
    if selected_date == today:
        if now.strftime("%H%M%S") < "080000":
            return resolved, None
        end_time = min(now.strftime("%H%M%S"), "200000")
    client = get_nxt_minute_kis_rest_client(
        app_key,
        app_secret,
        KIS_SHARED_CLIENT_RUNTIME_VERSION,
    )
    lookup_gate = get_nxt_minute_lookup_gate(KIS_SHARED_CLIENT_RUNTIME_VERSION)
    executor = get_nxt_minute_lookup_executor(KIS_SHARED_CLIENT_RUNTIME_VERSION)
    scheduled_count = 0
    for symbol, targets in missing_by_symbol.items():
        lookup_key = (selected_date.isoformat(), symbol)
        permit = lookup_gate.claim(lookup_key)
        if not permit.allowed:
            continue
        try:
            executor.submit(
                _fetch_and_store_nxt_limit_proximity_times,
                client,
                lookup_gate,
                store,
                lookup_key,
                symbol,
                selected_date,
                tuple(targets),
                end_time,
            )
        except Exception as exc:
            lookup_gate.fail(lookup_key, str(exc))
            continue
        scheduled_count += 1
    note = "최초 기록시각을 순차 확인 중입니다." if scheduled_count else None
    return resolved, note


def _render_limit_summary(
    frame: pd.DataFrame,
    hit_rows: pd.DataFrame,
) -> None:
    ohlc_count = int(
        frame[["시가", "고가", "저가", "종가"]].notna().all(axis=1).sum()
    )

    def direction_count(direction: str) -> int:
        if hit_rows.empty:
            return 0
        return int(
            hit_rows.loc[
                hit_rows["도달구분"] == direction,
                "종목코드",
            ].nunique()
        )

    summary_items = [
        ("조회 종목수", f"{len(frame):,}", None),
        (
            "OHLC 수록 종목수 (?)",
            f"{ohlc_count:,}",
            "시가·고가·저가·종가 값이 모두 수록된 종목 수입니다.",
        ),
        ("상한가 종목수", f"{direction_count('상한가'):,}종목", None),
        ("하한가 종목수", f"{direction_count('하한가'):,}종목", None),
    ]
    summary_html = "".join(
        '<div class="limit-summary-item"'
        + (f' title="{escape(tooltip, quote=True)}" tabindex="0"' if tooltip else "")
        + ">"
        f'<div class="limit-summary-label">{escape(label)}</div>'
        f'<div class="limit-summary-value">{escape(value)}</div>'
        "</div>"
        for label, value, tooltip in summary_items
    )
    st.markdown(
        f'<div class="limit-summary-strip">{summary_html}</div>',
        unsafe_allow_html=True,
    )


def _limit_price_with_rate_html(
    price: object,
    reference_price: object,
    *,
    colorize_rate: bool = True,
) -> str:
    if price is None or pd.isna(price):
        return "-"
    price_value = int(price)
    price_text = f"{price_value:,}"
    if (
        reference_price is None
        or pd.isna(reference_price)
        or int(reference_price) <= 0
    ):
        return price_text
    rate = price_value / int(reference_price) - 1
    if rate > 0:
        css_class = "limit-rate-positive"
        rate_text = f"+{rate:.2%}"
    elif rate < 0:
        css_class = "limit-rate-negative"
        rate_text = f"{rate:.2%}"
    else:
        css_class = "limit-rate-neutral"
        rate_text = "0.00%"
    if not colorize_rate:
        return f"{price_text} ({escape(rate_text)})"
    return f'{price_text} <span class="{css_class}">({escape(rate_text)})</span>'


def _render_nxt_limit_table(hit_rows: pd.DataFrame) -> None:
    if hit_rows.empty:
        st.info("선택 조건에서 상·하한가에 도달한 종목이 없습니다.")
        return
    matched = hit_rows.copy()
    matched["도달구분순서"] = matched["도달구분"].map(
        {"상한가": 0, "하한가": 1}
    )
    matched["도달시점순서"] = matched["도달시점"].map(
        lambda value: (
            "0000"
            if value == "시가"
            else str(value).replace(":", "")
            if re.fullmatch(r"\d{2}:\d{2}", str(value))
            else "9999"
        )
    )
    matched = matched.sort_values(
        ["도달구분순서", "도달시점순서", "종목코드"],
        ascending=[True, True, True],
    )
    headers = [
        "종목코드", "종목명", "상장시장", "상·하한가 구분", "기록시점",
        "기준가격", "상한가", "하한가", "시가", "종가",
    ]
    body_rows: list[str] = []
    for item in matched.to_dict("records"):
        reference_price = item.get("기준가격")
        direction = str(item.get("도달구분") or "")
        direction_class = "limit-upper" if direction == "상한가" else "limit-lower"
        body_rows.append(
            "<tr>"
            f'<td class="center">{escape(str(item.get("종목코드") or "-"))}</td>'
            f'<td class="left">{escape(str(item.get("종목명") or "-"))}</td>'
            f'<td class="center">{escape(str(item.get("상장시장") or "-"))}</td>'
            f'<td class="center {direction_class}">{escape(direction)}</td>'
            f'<td class="center">{escape(str(item.get("도달시점") or "-"))}</td>'
            f'<td class="right">{escape(_format_price(reference_price))}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("상한가"), reference_price)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("하한가"), reference_price)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("시가"), reference_price)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("종가"), reference_price)}</td>'
            "</tr>"
        )
    header_html = "".join(f"<th>{escape(header)}</th>" for header in headers)
    st.markdown(
        '<div class="limit-hit-table-wrap">'
        '<table class="limit-hit-table">'
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def _render_nxt_limit_result(
    frame: pd.DataFrame,
    *,
    live: bool,
    selected_date: date,
) -> None:
    if frame.empty:
        st.info("조회된 NXT 종목 시세가 없습니다.")
        return
    hit_rows = _nxt_limit_hit_rows(frame)
    hit_times, resolution_note = _resolve_nxt_limit_hit_times(
        selected_date,
        hit_rows,
    )
    hit_rows = _nxt_limit_hit_rows(frame, hit_times=hit_times)
    _render_limit_summary(frame, hit_rows)
    if resolution_note:
        st.caption(resolution_note)

    filter_columns = st.columns([1, 2])
    market_filter = filter_columns[0].selectbox(
        "상장시장",
        ["전체", "KOSPI", "KOSDAQ"],
        key=f"nxt_limit_market_{'live' if live else 'daily'}",
    )
    search_term = filter_columns[1].text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key=f"nxt_limit_search_{'live' if live else 'daily'}",
    ).strip()
    filtered = frame
    if market_filter != "전체":
        filtered = filtered[filtered["상장시장"] == market_filter]
    if search_term:
        filtered = filtered[
            filtered["종목명"].str.contains(search_term, case=False, na=False)
            | filtered["종목코드"].astype(str).str.contains(search_term, na=False)
        ]

    del live
    filtered_symbols = set(filtered["종목코드"].astype(str))
    _render_nxt_limit_table(
        hit_rows[hit_rows["종목코드"].astype(str).isin(filtered_symbols)]
    )


def _render_nxt_limit_notice() -> None:
    _render_notice_items(
        [
            "장중 종가는 현재가를 뜻하며 확정 종가가 아닙니다.",
            "기록시점은 시가 또는 NXT 분봉에서 확인한 최초 상·하한가 기록시각입니다.",
            "분봉 보관범위를 지난 과거 일자는 시가 외 기록시각이 없을 수 있습니다.",
        ]
    )


@st.fragment(run_every="2s")
def _render_live_nxt_limit_page(
    collector: KisRestUniverseCollector,
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    tradable_markets: dict[str, str],
) -> None:
    collector.heartbeat()
    status = collector.status()
    frame = _live_nxt_limit_frame(
        symbols,
        markets,
        tradable_markets,
        collector.snapshot(),
    )
    completed_at = (
        status.last_completed_at.strftime("%H:%M:%S")
        if status.last_completed_at is not None
        else "전체 갱신 전"
    )
    st.caption(
        f"당일 장중 NXT 시세 · {status.state} · "
        f"수신 {status.quote_count:,}/{status.universe_count:,}종목 · "
        f"전체 갱신 {completed_at}"
    )
    _render_nxt_limit_result(
        frame,
        live=True,
        selected_date=pd.Timestamp.now(tz="Asia/Seoul").date(),
    )


def nxt_price_limits_page() -> None:
    st.title("NXT 상·하한가 종목 현황")
    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    selected_date = st.date_input(
        "조회일자",
        value=today,
        min_value=NXT_LAUNCH_DATE,
        max_value=today,
        format="YYYY-MM-DD",
        key="nxt_limit_date",
    )
    if selected_date < today:
        statuses = get_historical_market_store().load_nxt_statuses(selected_date)
        if not statuses:
            st.info("선택한 일자의 확정 NXT 종목 시세가 DB에 없습니다.")
            return
        _render_nxt_limit_result(
            _historical_nxt_limit_frame(statuses),
            live=False,
            selected_date=selected_date,
        )
        _render_nxt_limit_notice()
        return

    with st.spinner("당일 NXT 종목 목록을 불러오고 있습니다..."):
        (
            symbols,
            markets,
            tradable_markets,
            _unavailable_reasons,
            _current_statuses,
        ) = _nxt_universe_for_date(today)
        if not symbols:
            (
                _universe_date,
                symbols,
                markets,
                tradable_markets,
                _unavailable_reasons,
            ) = _latest_nxt_universe()
    if not symbols:
        st.error("당일 조회에 사용할 NXT 종목 목록이 없습니다.")
        return
    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        st.warning("당일 장중 조회를 위해 KIS App Key와 App Secret이 필요합니다.")
        return
    collector = get_rest_universe_runtime(
        app_key,
        app_secret,
        REST_UNIVERSE_RUNTIME_VERSION,
    )
    collector.start(symbols, REST_UNIVERSE_REFRESH_SECONDS)
    _render_live_nxt_limit_page(
        collector,
        symbols,
        markets,
        tradable_markets,
    )
    _render_nxt_limit_notice()


def _render_limit_proximity_summary(
    frame: pd.DataFrame,
    proximity_rows: pd.DataFrame,
) -> None:
    ohlc_count = int(
        frame[["시가", "고가", "저가", "종가"]].notna().all(axis=1).sum()
    )

    def direction_count(direction: str, *, exact: bool) -> int:
        if proximity_rows.empty:
            return 0
        distances = pd.to_numeric(proximity_rows["잔여틱"], errors="coerce")
        distance_mask = distances.eq(0) if exact else distances.between(1, 3)
        return int(
            proximity_rows.loc[
                (proximity_rows["근접구분"] == direction) & distance_mask,
                "종목코드",
            ].nunique()
        )

    summary_items = [
        ("조회 종목수", f"{len(frame):,}", None),
        (
            "OHLC 수록 종목수 (?)",
            f"{ohlc_count:,}",
            "시가·고가·저가·종가 값이 모두 수록된 종목 수입니다.",
        ),
        ("상한가 종목수", f"{direction_count('상한가', exact=True):,}종목", None),
        ("하한가 종목수", f"{direction_count('하한가', exact=True):,}종목", None),
        (
            "상한가 근접 종목수",
            f"{direction_count('상한가', exact=False):,}종목",
            None,
        ),
        (
            "하한가 근접 종목수",
            f"{direction_count('하한가', exact=False):,}종목",
            None,
        ),
    ]
    summary_html = "".join(
        '<div class="limit-summary-item"'
        + (f' title="{escape(tooltip, quote=True)}" tabindex="0"' if tooltip else "")
        + ">"
        f'<div class="limit-summary-label">{escape(label)}</div>'
        f'<div class="limit-summary-value">{escape(value)}</div>'
        "</div>"
        for label, value, tooltip in summary_items
    )
    st.markdown(
        f'<div class="limit-summary-strip limit-summary-strip-six">'
        f"{summary_html}</div>",
        unsafe_allow_html=True,
    )


def _render_nxt_limit_proximity_table(proximity_rows: pd.DataFrame) -> None:
    if proximity_rows.empty:
        st.info("선택 조건에서 상·하한가 3틱 이내에 진입한 종목이 없습니다.")
        return
    matched = proximity_rows.copy()
    matched["근접구분순서"] = matched["근접구분"].map(
        {"상한가": 0, "하한가": 1}
    )
    matched["근접시점순서"] = matched["근접시점"].map(
        lambda value: (
            "0000"
            if value == "시가"
            else str(value).replace(":", "")
            if re.fullmatch(r"\d{2}:\d{2}", str(value))
            else "9999"
        )
    )
    matched = matched.sort_values(
        ["근접구분순서", "잔여틱", "근접시점순서", "종목코드"],
        ascending=[True, True, True, True],
    )
    headers = [
        "종목코드",
        "종목명",
        "상장시장",
        "기준가격",
        "상한가",
        "하한가",
        "시가",
        "최근접가격",
        "상·하한가 근접 구분",
        "도달시점",
        "잔여틱",
        "종가",
    ]
    body_rows: list[str] = []
    for item in matched.to_dict("records"):
        reference_price = item.get("기준가격")
        direction = str(item.get("근접구분") or "")
        direction_class = "limit-upper" if direction == "상한가" else "limit-lower"
        distance = int(item.get("잔여틱") or 0)
        distance_text = "0틱" if distance == 0 else f"{distance}틱 이내"
        direction_text = direction if distance == 0 else f"{direction} 근접"
        body_rows.append(
            "<tr>"
            f'<td class="center">{escape(str(item.get("종목코드") or "-"))}</td>'
            f'<td class="left">{escape(str(item.get("종목명") or "-"))}</td>'
            f'<td class="center">{escape(str(item.get("상장시장") or "-"))}</td>'
            f'<td class="right">{escape(_format_price(reference_price))}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("상한가"), reference_price, colorize_rate=False)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("하한가"), reference_price, colorize_rate=False)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("시가"), reference_price)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("최근접가격"), reference_price)}</td>'
            f'<td class="center {direction_class}">{escape(direction_text)}</td>'
            f'<td class="center">{escape(str(item.get("근접시점") or "-"))}</td>'
            f'<td class="center {direction_class}">{escape(distance_text)}</td>'
            f'<td class="right">{_limit_price_with_rate_html(item.get("종가"), reference_price)}</td>'
            "</tr>"
        )
    header_html = "".join(f"<th>{escape(header)}</th>" for header in headers)
    st.markdown(
        '<div class="limit-hit-table-wrap">'
        '<table class="limit-hit-table">'
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def _render_nxt_limit_proximity_result(
    frame: pd.DataFrame,
    *,
    live: bool,
    selected_date: date,
) -> None:
    if frame.empty:
        st.info("조회된 NXT 종목 시세가 없습니다.")
        return
    proximity_rows = _nxt_limit_proximity_rows(frame)
    hit_times, resolution_note = _resolve_nxt_limit_proximity_times(
        selected_date,
        proximity_rows,
    )
    proximity_rows = _nxt_limit_proximity_rows(frame, hit_times=hit_times)
    _render_limit_proximity_summary(frame, proximity_rows)
    if resolution_note:
        st.caption(resolution_note)

    filter_columns = st.columns([1, 2])
    market_filter = filter_columns[0].selectbox(
        "상장시장",
        ["전체", "KOSPI", "KOSDAQ"],
        key=f"nxt_proximity_market_{'live' if live else 'daily'}",
    )
    search_term = filter_columns[1].text_input(
        "종목 검색",
        placeholder="종목명 또는 6자리 종목코드",
        key=f"nxt_proximity_search_{'live' if live else 'daily'}",
    ).strip()
    filtered = frame
    if market_filter != "전체":
        filtered = filtered[filtered["상장시장"] == market_filter]
    if search_term:
        filtered = filtered[
            filtered["종목명"].str.contains(search_term, case=False, na=False)
            | filtered["종목코드"].astype(str).str.contains(search_term, na=False)
        ]

    filtered_symbols = set(filtered["종목코드"].astype(str))
    _render_nxt_limit_proximity_table(
        proximity_rows[
            proximity_rows["종목코드"].astype(str).isin(filtered_symbols)
        ]
    )


def _render_nxt_limit_proximity_notice() -> None:
    _render_notice_items(
        [
            (
                "근접 종목은 상한가 아래 또는 하한가 위 0~3틱 범위에 진입한 종목입니다. "
                "잔여틱 0틱은 상·하한가 기록 종목입니다."
            ),
            (
                "가격대 경계를 넘으면 구간별 호가가격단위를 차례로 적용합니다. "
                "잔여틱은 최근접가격에서 상·하한가까지 남은 호가 수입니다."
            ),
            (
                "도달시점은 시가 또는 NXT 분봉에서 확인한 최초 진입시각입니다. "
                "분봉 보관범위를 지난 과거 일자는 시가 외 시각이 없을 수 있습니다."
            ),
            "장중 종가는 현재가를 뜻하며 확정 종가가 아닙니다.",
        ]
    )


@st.fragment(run_every="2s")
def _render_live_nxt_limit_proximity_page(
    collector: KisRestUniverseCollector,
    symbols: list[WatchSymbol],
    markets: dict[str, str],
    tradable_markets: dict[str, str],
) -> None:
    collector.heartbeat()
    status = collector.status()
    frame = _live_nxt_limit_frame(
        symbols,
        markets,
        tradable_markets,
        collector.snapshot(),
    )
    completed_at = (
        status.last_completed_at.strftime("%H:%M:%S")
        if status.last_completed_at is not None
        else "전체 갱신 전"
    )
    st.caption(
        f"당일 장중 NXT 시세 · {status.state} · "
        f"수신 {status.quote_count:,}/{status.universe_count:,}종목 · "
        f"전체 갱신 {completed_at}"
    )
    _render_nxt_limit_proximity_result(
        frame,
        live=True,
        selected_date=pd.Timestamp.now(tz="Asia/Seoul").date(),
    )


def nxt_price_limit_proximity_page() -> None:
    st.title("NXT 상·하한가 근접 종목 현황")
    today = pd.Timestamp.now(tz="Asia/Seoul").date()
    selected_date = st.date_input(
        "조회일자",
        value=today,
        min_value=NXT_LAUNCH_DATE,
        max_value=today,
        format="YYYY-MM-DD",
        key="nxt_limit_proximity_date",
    )
    if selected_date < today:
        statuses = get_historical_market_store().load_nxt_statuses(selected_date)
        if not statuses:
            st.info("선택한 일자의 확정 NXT 종목 시세가 DB에 없습니다.")
            return
        _render_nxt_limit_proximity_result(
            _historical_nxt_limit_frame(statuses),
            live=False,
            selected_date=selected_date,
        )
        _render_nxt_limit_proximity_notice()
        return

    with st.spinner("당일 NXT 종목 목록을 불러오고 있습니다..."):
        (
            symbols,
            markets,
            tradable_markets,
            _unavailable_reasons,
            _current_statuses,
        ) = _nxt_universe_for_date(today)
        if not symbols:
            (
                _universe_date,
                symbols,
                markets,
                tradable_markets,
                _unavailable_reasons,
            ) = _latest_nxt_universe()
    if not symbols:
        st.error("당일 조회에 사용할 NXT 종목 목록이 없습니다.")
        return
    app_key = _secret_value("KIS_APP_KEY")
    app_secret = _secret_value("KIS_APP_SECRET")
    if not app_key or not app_secret:
        st.warning("당일 장중 조회를 위해 KIS App Key와 App Secret이 필요합니다.")
        return
    collector = get_rest_universe_runtime(
        app_key,
        app_secret,
        REST_UNIVERSE_RUNTIME_VERSION,
    )
    collector.start(symbols, REST_UNIVERSE_REFRESH_SECONDS)
    _render_live_nxt_limit_proximity_page(
        collector,
        symbols,
        markets,
        tradable_markets,
    )
    _render_nxt_limit_proximity_notice()


def load_nxt_changes(
    start_date: date,
    end_date: date,
):
    return get_nxt_change_store().list_changes(start_date, end_date)


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


def _date_controls(
    key_prefix: str,
    default_days: int = 14,
    *,
    default_start: date | None = None,
) -> tuple[date, date]:
    today = date.today()
    default_start_date = (
        max(NXT_LAUNCH_DATE, min(default_start, today))
        if default_start is not None
        else max(NXT_LAUNCH_DATE, today - timedelta(days=default_days))
    )
    start_col, end_col = st.columns(2)
    with start_col:
        start_date = st.date_input(
            "시작일",
            value=default_start_date,
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
                "편입 종목수": format_addition,
                "편출 종목수": format_exclusion,
            }
        )
        .map(
            lambda value: (
                "color: #1e3a8a; background-color: #dbeafe; font-weight: 700;"
                if value > 0
                else ""
            ),
            subset=pd.IndexSlice[:, ["편입 종목수"]],
        )
        .map(
            lambda value: (
                "color: #991b1b; background-color: #fee2e2; font-weight: 700;"
                if value > 0
                else ""
            ),
            subset=pd.IndexSlice[:, ["편출 종목수"]],
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
    selected_specific = [
        category for category in CATEGORY_CODES if category in (selected_labels or [])
    ]
    return selected_specific or list(CATEGORY_CODES)


def _render_disclosure_notice() -> None:
    _render_notice_items(
        [
            "조회 결과는 선택 기간에 발생한 KIND 공시이며 현재 시장조치 상태를 뜻하지 않습니다.",
            "공시일의 NXT 매매체결대상 종목과 일치하는 공시만 표시합니다.",
            (
                "공시 구분은 공시 제목과 KIND 분류를 기준으로 정리합니다. "
                "복합 공시나 정정 공시는 원문을 함께 확인해야 합니다."
            ),
            "거래가능시장과 거래불가사유는 해당 공시일의 NXT 거래현황 값입니다.",
        ]
    )


def disclosure_page() -> None:
    st.title("NXT 정규시장 종목의 KRX 시장조치 현황")
    force_refresh = _refresh_control("refresh_disclosures")
    with st.form("disclosure_query_form"):
        start_date, end_date = _date_controls("disclosure", default_days=0)
        selected_labels = st.pills(
            "공시 구분",
            ["전체", *CATEGORY_CODES.keys()],
            default=["전체"],
            selection_mode="multi",
            key="disclosure_category",
        )
        query_clicked = st.form_submit_button(
            "조회",
            type="primary",
        )
    query_state_key = "disclosure_applied_query_v2"
    if query_clicked:
        categories = _selected_disclosure_categories(selected_labels)
        st.session_state[query_state_key] = {
            "start_date": start_date,
            "end_date": end_date,
            "categories": categories,
        }
    if query_state_key not in st.session_state:
        st.info("조회 조건을 선택한 후 조회 버튼을 눌러주세요.")
        _render_disclosure_notice()
        return
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

    _render_disclosure_notice()


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
        changes = load_nxt_changes(as_of, as_of)
        statuses = statuses_by_date.get(as_of, [])
        frame = nxt_trading_status_to_frame(statuses)
    _show_ssl_warning(status_ssl)

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

    day_changes = [item for item in changes if item.change_date == as_of]
    with st.expander(f"{as_of:%Y-%m-%d} 종목 변동내역 ({len(day_changes):,}건)", expanded=False):
        change_frame = nxt_changes_to_frame(day_changes)
        if change_frame.empty:
            st.info("해당 일자의 편입·편출 내역이 없습니다.")
        else:
            st.dataframe(change_frame, hide_index=True, use_container_width=True)


def nxt_changes_page() -> None:
    st.title("NXT 정규시장 종목 변동내역")
    start_date, end_date = _date_controls(
        "nxt_changes_v2",
        default_start=NXT_LAUNCH_DATE,
    )
    changes = load_nxt_changes(start_date, end_date)
    membership_changes = [
        item
        for item in changes
        if classify_nxt_change(item) in {"편입", "편출"}
    ]
    historical_store = get_historical_market_store()
    eligibility = historical_store.list_nxt_eligibility_summaries(
        start_date,
        end_date,
    )
    eligibility_reason_counts = historical_store.list_nxt_eligibility_reason_counts(
        start_date,
        end_date,
    )
    unavailability_events = historical_store.list_nxt_unavailability_events(
        start_date,
        end_date,
    )
    metrics = build_daily_nxt_metrics_from_counts(
        membership_changes,
        {item.trade_date: item.target_stock_count for item in eligibility},
        start_date,
        end_date,
    )
    index_member_counts = historical_store.list_nxt_index_member_counts(
        start_date,
        end_date,
    )
    index_coverage = {
        index_name: historical_store.index_constituent_dates(index_name)
        for index_name in ("KOSPI200", "KOSDAQ150")
    }
    sync_stats = get_nxt_change_store().stats()
    if sync_stats.coverage_start and sync_stats.coverage_end:
        last_success = (
            sync_stats.last_success_at.astimezone().strftime("%Y-%m-%d %H:%M")
            if sync_stats.last_success_at
            else "-"
        )
        st.caption(
            f"DB 적재 범위 {sync_stats.coverage_start:%Y-%m-%d}~"
            f"{sync_stats.coverage_end:%Y-%m-%d} · 변동내역 "
            f"{sync_stats.event_count:,}건 · 마지막 동기화 {last_success}"
        )
    if metrics.empty:
        st.info("선택한 기간의 NXT 변동 현황을 계산할 수 없습니다.")
        return

    latest = metrics.iloc[-1]
    period_additions, period_exclusions, period_net_change = (
        summarize_nxt_membership_flow(metrics)
    )
    columns = st.columns(4)
    columns[0].metric("NXT 종목수", f"{int(latest['NXT 종목수']):,}")
    columns[1].metric(
        "조회기간 편입종목수",
        f"{period_additions:,}",
        help="조회기간의 일별 편입 건수 합계입니다. 같은 종목의 재편입은 다시 집계합니다.",
    )
    columns[2].metric(
        "조회기간 편출종목수",
        f"{period_exclusions:,}",
        help="조회기간의 일별 편출 건수 합계입니다. 같은 종목의 재편출은 다시 집계합니다.",
    )
    columns[3].metric(
        "NXT 종목수 증감",
        f"{period_net_change:+,}",
        help="조회기간의 일별 편입 종목수 합계에서 편출 종목수 합계를 뺀 값입니다.",
    )

    summary_tab, detail_tab, unavailable_tab = st.tabs(
        ["일별 집계", "종목별 변동내역", "거래불가 현황"]
    )
    with summary_tab:
        eligibility_by_date = {item.trade_date: item for item in eligibility}
        reason_counts_by_date = {}
        for item in eligibility_reason_counts:
            reason_counts_by_date.setdefault(item.trade_date, []).append(item)
        summary_frame = metrics.sort_values("일자", ascending=False).rename(
            columns={
                "당일 편입 종목수": "편입 종목수",
                "당일 편출 종목수": "편출 종목수",
            }
        )
        summary_frame["거래가능 종목수"] = summary_frame["일자"].map(
            lambda current_date: eligibility_by_date[
                current_date
            ].tradable_stock_count
        )
        summary_frame["거래불가 종목수"] = summary_frame["일자"].map(
            lambda current_date: eligibility_by_date[
                current_date
            ].unavailable_stock_count
        )
        summary_frame["거래불가사유"] = summary_frame["일자"].map(
            lambda current_date: summarize_daily_nxt_unavailability_reasons(
                reason_counts_by_date.get(current_date, [])
            )
        )
        for index_name in ("KOSPI200", "KOSDAQ150"):
            summary_frame[f"{index_name} 종목수"] = pd.array(
                [
                    index_member_counts.get(current_date, {}).get(index_name, 0)
                    if current_date in index_coverage[index_name]
                    else pd.NA
                    for current_date in summary_frame["일자"]
                ],
                dtype="Int64",
            )
        summary_frame = summary_frame[
            [
                "일자",
                "NXT 종목수",
                "KOSPI200 종목수",
                "KOSDAQ150 종목수",
                "거래가능 종목수",
                "거래불가 종목수",
                "거래불가사유",
                "편입 종목수",
                "편출 종목수",
                "변경사유",
            ]
        ]
        st.dataframe(
            _style_daily_nxt_metrics(summary_frame),
            hide_index=True,
            use_container_width=True,
            height=650,
        )
        missing_index_dates = {
            current_date
            for current_date in summary_frame["일자"]
            if any(
                current_date not in index_coverage[index_name]
                for index_name in ("KOSPI200", "KOSDAQ150")
            )
        }
        if missing_index_dates:
            st.caption(
                f"KRX 지수 구성종목이 아직 DB에 적재되지 않은 거래일이 "
                f"{len(missing_index_dates):,}일 있어 해당 종목수는 빈칸으로 표시됩니다."
            )

    with detail_tab:
        change_frame = nxt_changes_display_frame(membership_changes)
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

    with unavailable_tab:
        unavailable_frame = nxt_unavailability_events_to_frame(
            unavailability_events
        )
        unavailable_start_symbols = {
            item.stock_code
            for item in unavailability_events
            if item.event_type == "거래불가"
        }
        unavailable_release_symbols = {
            item.stock_code
            for item in unavailability_events
            if item.event_type == "거래불가 해제"
        }
        unavailable_columns = st.columns(2)
        unavailable_columns[0].metric(
            "조회기간 거래불가 종목수",
            f"{len(unavailable_start_symbols):,}",
        )
        unavailable_columns[1].metric(
            "조회기간 거래불가 해제 종목수",
            f"{len(unavailable_release_symbols):,}",
        )
        if unavailable_frame.empty:
            st.info("선택한 기간의 거래불가 지정·해제 내역이 없습니다.")
        else:
            unavailable_frame = unavailable_frame.sort_values(
                ["일자", "구분", "종목코드"],
                ascending=[False, True, True],
            )
            st.dataframe(
                unavailable_frame,
                hide_index=True,
                use_container_width=True,
                height=650,
                column_config={
                    "원문": st.column_config.LinkColumn(
                        "원문",
                        display_text="원문 보기",
                    )
                },
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
        _format_chart_date_axis(flow_chart)
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
        _format_chart_date_axis(state_chart)
        st.plotly_chart(state_chart, use_container_width=True)

    _render_notice_items(
        [
            (
                "2025-03-04 최초 10종목은 '최초' 사유의 편입으로 집계합니다. "
                "NXT 종목수 증감은 조회기간 편입 합계에서 편출 합계를 뺀 값입니다."
            ),
            (
                "종목별 변동내역은 실제 매매체결대상 편입·편출만 표시합니다. "
                "거래불가 지정·해제는 거래불가 현황에서 별도로 확인합니다."
            ),
            (
                "거래불가 현황은 NXT 거래현황을 우선 사용하고, 사유 제공 전 구간은 "
                "종목 변동내역으로 보완합니다."
            ),
            (
                "거래불가 현황의 KIND 원문은 이벤트일 이전 45일 이내 공시를 확인합니다. "
                "종목코드·사유·지정/해제 방향을 우선 검증하고, 정지·해제가 한 원문에 "
                "함께 있거나 같은 거래불가 구간이면 해당 시작 원문을 연결합니다."
            ),
            (
                "공식 변동내역 없이 명단에서 사라지거나 추가된 종목은 일별 대상 명단과 "
                "공식 안내를 대사해 보정하며, 보정 여부와 근거를 함께 표시합니다."
            ),
            (
                "KOSPI200·KOSDAQ150 종목수는 각 일자의 NXT 매매체결대상 종목과 "
                "KRX 공식 지수 구성종목을 종목코드로 대조한 값입니다."
            ),
            "이 화면은 외부 API를 호출하지 않고 SQLite에 저장된 값만 조회합니다.",
        ]
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
        _format_chart_date_axis(status_chart)
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


def main() -> None:
    get_nxt_change_scheduler().start()
    pages = [
        st.Page(
            rest_universe_page,
            title="NXT DASHBOARD",
            icon="🔁",
            default=True,
        ),
        st.Page(
            nxt_price_limit_proximity_page,
            title="NXT 상·하한가 근접 종목 현황",
            icon="🎯",
        ),
        st.Page(nxt_changes_page, title="NXT 정규시장 종목 변동내역", icon="🔄"),
        st.Page(market_history_page, title="NXT·KRX 일별 거래 추이", icon="📈"),
        st.Page(disclosure_page, title="KRX 시장조치 조회", icon="📋"),
    ]
    navigation = st.navigation(pages)
    navigation.run()


if __name__ == "__main__":
    main()
