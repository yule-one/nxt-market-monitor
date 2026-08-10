from __future__ import annotations

import logging
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cache import ResponseCache
from src.config import NXT_LAUNCH_DATE
from src.historical_market import HistoricalMarketStore
from src.kind_client import KindClient
from src.kis_rest import KisRestClient
from src.kis_websocket import KisCredentials
from src.krx_openapi import KrxOpenApiClient
from src.nxt_client import NxtClient
from src.nxt_change_store import NxtChangeStore
from src.nxt_kind_links import KIND_LINK_CATEGORIES, match_unavailability_events
from src.nxt_price_limits import reached_limit_points


KST = ZoneInfo("Asia/Seoul")


def _logger() -> logging.Logger:
    logger = logging.getLogger("daily-market-sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path = PROJECT_ROOT / "data" / "daily_market_sync.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _krx_key() -> str:
    path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if not path.exists():
        raise RuntimeError(f"secrets 파일이 없습니다: {path}")
    secrets = tomllib.loads(path.read_text(encoding="utf-8"))
    key = str(secrets.get("KRX_KEY") or "").strip()
    if not key:
        raise RuntimeError("KRX_KEY가 필요합니다.")
    return key


def _kis_client() -> KisRestClient:
    path = PROJECT_ROOT / ".streamlit" / "secrets.toml"
    secrets = tomllib.loads(path.read_text(encoding="utf-8"))
    app_key = str(secrets.get("KIS_APP_KEY") or "").strip()
    app_secret = str(secrets.get("KIS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET이 필요합니다.")
    return KisRestClient(KisCredentials(app_key, app_secret))


def _sync_usd_krw(
    store: HistoricalMarketStore,
    target_date: date,
    kis_client: KisRestClient,
) -> None:
    existing_dates = store.fx_dates()
    start_date = (
        max(NXT_LAUNCH_DATE, max(existing_dates) + timedelta(days=1))
        if existing_dates
        else NXT_LAUNCH_DATE
    )
    if start_date > target_date:
        return
    quotes = kis_client.fetch_usd_krw_history(start_date, target_date)
    store.save_fx_quotes(quotes)


def _sync_nxt_limit_hit_times(
    store: HistoricalMarketStore,
    trading_date: date,
    statuses: list,
    kis_client: KisRestClient,
    logger: logging.Logger,
) -> None:
    if not statuses:
        return
    stored = store.load_nxt_limit_hit_times(trading_date)
    hit_times: dict[tuple[str, str], str] = {}
    missing_by_symbol: dict[str, list[tuple[str, int]]] = {}
    for item in statuses:
        for direction, limit_price in (
            ("상한가", item.upper_limit_price),
            ("하한가", item.lower_limit_price),
        ):
            hits = reached_limit_points(
                open_price=item.open_price,
                high_price=item.high_price,
                low_price=item.low_price,
                close_price=item.current_price,
                limit_price=limit_price,
            )
            if not hits:
                continue
            key = (item.stock_code, direction)
            if "시가" in hits:
                hit_times[key] = "OPEN"
            elif key not in stored and limit_price is not None:
                missing_by_symbol.setdefault(item.stock_code, []).append(
                    (direction, limit_price)
                )

    for symbol, targets in missing_by_symbol.items():
        found_times = kis_client.fetch_nxt_limit_hit_times(
            symbol,
            trading_date,
            dict(targets),
        )
        for direction, hit_time in found_times.items():
            hit_times[(symbol, direction)] = hit_time

    store.save_nxt_limit_hit_times(trading_date, hit_times)
    logger.info(
        "%s NXT 상·하한가 최초 도달시각 저장 완료: %s건",
        trading_date,
        len(hit_times),
    )


def _sync_nxt_unavailability_kind_links(
    store: HistoricalMarketStore,
    start_date: date,
    end_date: date,
    logger: logging.Logger,
) -> None:
    if end_date < start_date:
        return
    events = store.list_nxt_unavailability_events(start_date, end_date)
    disclosures = KindClient(ResponseCache()).fetch_disclosures(
        max(NXT_LAUNCH_DATE, start_date - timedelta(days=10)),
        end_date,
        categories=KIND_LINK_CATEGORIES,
        force_refresh=True,
        max_workers=3,
    )
    links = match_unavailability_events(events, disclosures)
    stored = store.replace_nxt_unavailability_kind_links(
        start_date,
        end_date,
        links,
    )
    logger.info(
        "NXT 거래불가 KIND 원문 연결 완료: 범위=%s~%s 이벤트=%s 연결=%s",
        start_date,
        end_date,
        len(events),
        stored,
    )


def _target_date(now: datetime) -> date:
    """Return the previous Korean calendar day for the 08:00 daily job."""
    local_now = now.astimezone(KST)
    return local_now.date() - timedelta(days=1)


def main() -> int:
    logger = _logger()
    now = datetime.now(KST)
    target_date = _target_date(now)
    store = HistoricalMarketStore()
    latest_final = store.latest_final_date()
    start_date = (
        max(NXT_LAUNCH_DATE, latest_final + timedelta(days=1))
        if latest_final
        else NXT_LAUNCH_DATE
    )
    try:
        kis_client = _kis_client()
        _sync_usd_krw(store, target_date, kis_client)
    except Exception:
        logger.exception("달러-원 환율 동기화 실패: 목표=%s", target_date)
        return 1
    if start_date > target_date:
        try:
            _sync_nxt_limit_hit_times(
                store,
                target_date,
                store.load_nxt_statuses(target_date),
                kis_client,
                logger,
            )
        except Exception:
            logger.exception("NXT 상·하한가 도달시각 동기화 실패: 목표=%s", target_date)
            return 1
        logger.info(
            "확정 일별 데이터 누락 없음: 최근=%s 목표=%s",
            latest_final,
            target_date,
        )
        inferred_count = NxtChangeStore(store.path).rebuild_inferred_changes()
        store.rebuild_nxt_eligibility_history(NXT_LAUNCH_DATE, target_date)
        try:
            _sync_nxt_unavailability_kind_links(
                store,
                max(NXT_LAUNCH_DATE, target_date - timedelta(days=10)),
                target_date,
                logger,
            )
        except Exception:
            logger.exception("NXT 거래불가 KIND 원문 연결 실패: 목표=%s", target_date)
        logger.info("원본 누락 종목 변동 보정=%s건", inferred_count)
        return 0

    try:
        auth_key = _krx_key()
        statuses_by_date = NxtClient(ResponseCache()).fetch_trading_status_range(
            start_date,
            target_date,
            force_refresh=True,
            max_workers=8,
        )
        krx_client = KrxOpenApiClient(
            auth_key,
            ResponseCache(),
            persist_raw_cache=False,
        )
        completed = 0
        for trading_date, statuses in sorted(statuses_by_date.items()):
            snapshot = krx_client.fetch_daily_snapshot(
                trading_date,
                force_refresh=True,
            )
            if "KOSPI" not in snapshot.index_quotes or "KOSDAQ" not in snapshot.index_quotes:
                raise RuntimeError(
                    f"{trading_date:%Y-%m-%d} KRX 대표지수 확정 데이터가 없습니다."
                )
            store.save_historical_snapshot(trading_date, statuses, snapshot)
            _sync_nxt_limit_hit_times(
                store,
                trading_date,
                statuses,
                kis_client,
                logger,
            )
            completed += 1
            if not getattr(snapshot, "future_quotes", {}):
                logger.warning(
                    "%s KOSPI200 선물 데이터 미저장: KRX API 권한 또는 제공 시각 확인 필요",
                    trading_date,
                )
        store.rebuild_derived_metrics()
        inferred_count = NxtChangeStore(store.path).rebuild_inferred_changes()
        store.rebuild_nxt_eligibility_history(NXT_LAUNCH_DATE, target_date)
        try:
            _sync_nxt_unavailability_kind_links(
                store,
                start_date,
                target_date,
                logger,
            )
        except Exception:
            logger.exception(
                "NXT 거래불가 KIND 원문 연결 실패: 범위=%s~%s",
                start_date,
                target_date,
            )
        logger.info(
            "당일 확정 데이터 동기화 완료: 범위=%s~%s 거래일=%s 변동보정=%s",
            start_date,
            target_date,
            completed,
            inferred_count,
        )
        return 0
    except Exception:
        logger.exception(
            "당일 확정 데이터 동기화 실패: 범위=%s~%s",
            start_date,
            target_date,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
