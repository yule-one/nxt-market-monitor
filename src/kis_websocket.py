from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests
import websocket

from src.market_realtime import KRX, NXT, MarketAggregator, TradeTick, WatchSymbol


LOGGER = logging.getLogger(__name__)

KIS_APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
KIS_WEBSOCKET_URL = "ws://ops.koreainvestment.com:21000/tryitout"
KRX_TRADE_TR_ID = "H0STCNT0"
NXT_TRADE_TR_ID = "H0NXCNT0"

TRADE_FIELD_COUNT = 46


@dataclass(frozen=True)
class KisCredentials:
    app_key: str
    app_secret: str


@dataclass(frozen=True)
class CollectorStatus:
    state: str
    message: str
    connected_at: datetime | None
    last_message_at: datetime | None


def _reference_price(price: int, sign_code: str, raw_difference: str) -> int | None:
    """KIS 전일대비 부호코드를 적용해 비교 기준가격을 복원합니다."""

    if not raw_difference.strip():
        return None
    try:
        magnitude = abs(int(raw_difference))
    except ValueError:
        return None
    if sign_code in {"4", "5"}:
        signed_difference = -magnitude
    elif sign_code in {"1", "2"}:
        signed_difference = magnitude
    elif sign_code == "3":
        signed_difference = 0
    else:
        signed_difference = int(raw_difference)
    reference = price - signed_difference
    return reference if reference > 0 else None


def parse_trade_message(message: str) -> list[TradeTick]:
    """KIS 실시간 체결 문자열을 시장별 TradeTick 목록으로 변환합니다."""

    parts = message.split("|", 3)
    if len(parts) != 4 or parts[1] not in {KRX_TRADE_TR_ID, NXT_TRADE_TR_ID}:
        return []
    try:
        count = int(parts[2])
    except ValueError:
        return []
    values = parts[3].split("^")
    market = KRX if parts[1] == KRX_TRADE_TR_ID else NXT
    ticks: list[TradeTick] = []
    for index in range(count):
        start = index * TRADE_FIELD_COUNT
        row = values[start : start + TRADE_FIELD_COUNT]
        if len(row) < TRADE_FIELD_COUNT:
            break
        try:
            symbol = row[0].strip()
            traded_at = datetime.strptime(row[33] + row[1], "%Y%m%d%H%M%S")
            price = int(row[2] or 0)
            ticks.append(
                TradeTick(
                    market=market,
                    symbol=symbol,
                    traded_at=traded_at,
                    price=price,
                    trade_volume=abs(int(row[12] or 0)),
                    cumulative_volume=int(row[13] or 0),
                    cumulative_amount=int(row[14] or 0),
                    reference_price=_reference_price(price, row[3].strip(), row[4]),
                )
            )
        except (TypeError, ValueError):
            continue
    return ticks


class KisRealtimeCollector:
    """KIS KRX/NXT 체결 채널을 한 세션에서 관리하는 백그라운드 수집기입니다."""

    def __init__(
        self,
        credentials: KisCredentials,
        aggregator: MarketAggregator,
        websocket_url: str = KIS_WEBSOCKET_URL,
        approval_url: str = KIS_APPROVAL_URL,
        request_post: Callable[..., requests.Response] = requests.post,
    ) -> None:
        self.credentials = credentials
        self.aggregator = aggregator
        self.websocket_url = websocket_url
        self.approval_url = approval_url
        self._request_post = request_post
        self._lock = threading.RLock()
        self._symbols: list[WatchSymbol] = []
        self._stop_event = threading.Event()
        self._reconnect_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: websocket.WebSocketApp | None = None
        self._state = "중지"
        self._message = "연결되지 않음"
        self._connected_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_flush_at = 0.0

    def start(self, symbols: list[WatchSymbol]) -> None:
        if len(symbols) > 20:
            raise ValueError("KRX/NXT 동시 구독은 최대 20종목까지 지원합니다.")
        with self._lock:
            if self._thread and self._thread.is_alive() and symbols == self._symbols:
                return
            self._symbols = list(symbols)
            if self._thread and self._thread.is_alive():
                self._reconnect_event.set()
                if self._ws:
                    self._ws.close()
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kis-realtime-collector",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._ws:
                self._ws.close()
        self.aggregator.flush()

    def status(self) -> CollectorStatus:
        with self._lock:
            return CollectorStatus(
                state=self._state,
                message=self._message,
                connected_at=self._connected_at,
                last_message_at=self._last_message_at,
            )

    def _set_status(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _approval_key(self) -> str:
        response = self._request_post(
            self.approval_url,
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": self.credentials.app_key,
                "secretkey": self.credentials.app_secret,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        approval_key = str(payload.get("approval_key", "")).strip()
        if not approval_key:
            raise RuntimeError(payload.get("msg1") or "KIS 웹소켓 접속키 발급 실패")
        return approval_key

    @staticmethod
    def _subscription_message(approval_key: str, tr_id: str, symbol: str) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": tr_id, "tr_key": symbol}},
            },
            ensure_ascii=False,
        )

    def _run(self) -> None:
        retry_seconds = 1
        first_connection = True
        while not self._stop_event.is_set():
            try:
                self._set_status("연결중", "KIS 실시간 시세에 연결하는 중")
                approval_key = self._approval_key()
                if not first_connection:
                    self.aggregator.mark_gap()
                first_connection = False

                def on_open(ws: websocket.WebSocketApp) -> None:
                    with self._lock:
                        symbols = list(self._symbols)
                        self._connected_at = datetime.now()
                    for item in symbols:
                        ws.send(self._subscription_message(approval_key, KRX_TRADE_TR_ID, item.symbol))
                        time.sleep(0.08)
                        ws.send(self._subscription_message(approval_key, NXT_TRADE_TR_ID, item.symbol))
                        time.sleep(0.08)
                    self._set_status("연결됨", f"{len(symbols)}종목 실시간 수신 중")

                def on_message(ws: websocket.WebSocketApp, raw: str | bytes) -> None:
                    message = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    now = datetime.now()
                    with self._lock:
                        self._last_message_at = now
                    if not message:
                        return
                    if message[0] in {"0", "1"}:
                        for tick in parse_trade_message(message):
                            self.aggregator.ingest(tick)
                        monotonic = time.monotonic()
                        if monotonic - self._last_flush_at >= 1:
                            self.aggregator.flush()
                            self._last_flush_at = monotonic
                        return
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        return
                    header = payload.get("header") or {}
                    if header.get("tr_id") == "PINGPONG":
                        ws.send(message, websocket.ABNF.OPCODE_PING)
                        return
                    body = payload.get("body") or {}
                    if body.get("rt_cd") not in {None, "0"}:
                        self._set_status("오류", str(body.get("msg1") or "KIS 구독 오류"))

                def on_error(_ws: websocket.WebSocketApp, error: object) -> None:
                    LOGGER.warning("KIS WebSocket error: %s", error)
                    self._set_status("재연결", "실시간 연결이 끊겨 재연결하는 중")

                def on_close(
                    _ws: websocket.WebSocketApp,
                    _status_code: int | None,
                    _close_message: str | None,
                ) -> None:
                    if not self._stop_event.is_set():
                        self._set_status("재연결", "KIS 실시간 연결 재시도 중")

                ws = websocket.WebSocketApp(
                    self.websocket_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                with self._lock:
                    self._ws = ws
                ws.run_forever()
                self.aggregator.flush()
                if self._stop_event.is_set():
                    break
                retry_seconds = 1 if self._reconnect_event.is_set() else min(retry_seconds * 2, 30)
                self._reconnect_event.clear()
            except Exception as exc:  # 연결 루프는 화면을 중단시키지 않고 상태로 전달합니다.
                LOGGER.exception("KIS real-time collector failed")
                self._set_status("오류", str(exc))
                retry_seconds = min(retry_seconds * 2, 30)
            self._stop_event.wait(retry_seconds)
        self._set_status("중지", "실시간 수신 중지")
