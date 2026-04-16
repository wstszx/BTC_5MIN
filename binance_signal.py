from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

try:
    import websocket
except ModuleNotFoundError:  # pragma: no cover
    websocket = None

from strategy import compute_ofi_score


@dataclass(slots=True)
class BinanceTopOfBookSignal:
    ofi_score: float | None
    bid_price: float | None
    bid_qty: float | None
    ask_price: float | None
    ask_qty: float | None
    signal_at: datetime


class BinanceDepth5SignalService:
    def __init__(
        self,
        *,
        ws_url: str,
        stream: str,
        rest_url: str | None = None,
        rest_timeout_seconds: float = 3.0,
    ) -> None:
        self.ws_url = ws_url.rstrip('/') + '/' + stream.lstrip('/')
        self.stream = stream
        self.rest_url = rest_url or _default_rest_url_for_stream(stream)
        self.rest_timeout_seconds = max(0.1, float(rest_timeout_seconds))
        self._lock = threading.Lock()
        self._latest: BinanceTopOfBookSignal | None = None
        self._app: websocket.WebSocketApp | None = None if websocket is not None else None
        self._thread: threading.Thread | None = None
        self._rest_session: Any = requests

    def close(self) -> None:
        with self._lock:
            app = self._app
            thread = self._thread
            self._app = None
            self._thread = None
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def latest(self) -> BinanceTopOfBookSignal | None:
        with self._lock:
            return self._latest

    def push_payload(self, payload: dict[str, Any], *, now: datetime | None = None) -> BinanceTopOfBookSignal:
        signal = parse_depth5_message(payload, now=now)
        with self._lock:
            self._latest = signal
        return signal

    def refresh_from_rest(
        self,
        *,
        session: Any | None = None,
        now: datetime | None = None,
    ) -> BinanceTopOfBookSignal | None:
        if not self.rest_url:
            return None
        client = session or self._rest_session
        response = client.get(self.rest_url, timeout=self.rest_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return self.push_payload(payload, now=now)

    def start(self) -> None:
        if websocket is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            def on_message(_ws: websocket.WebSocketApp, message: str) -> None:
                try:
                    payload = json.loads(message)
                except (TypeError, ValueError):
                    return
                if isinstance(payload, dict):
                    self.push_payload(payload)

            self._app = websocket.WebSocketApp(self.ws_url, on_message=on_message)

            def run() -> None:
                app = self._app
                if app is None:
                    return
                app.run_forever(ping_interval=20, ping_timeout=10)

            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()


def _default_rest_url_for_stream(stream: str) -> str | None:
    raw = str(stream or '').strip().lower()
    if not raw:
        return None
    symbol = raw.split('@', 1)[0].strip()
    if not symbol:
        return None
    return 'https://api.binance.com/api/v3/depth?symbol=' + symbol.upper() + '&limit=5'


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_depth5_message(payload: dict[str, Any], *, now: datetime | None = None) -> BinanceTopOfBookSignal:
    bids = payload.get('bids') or payload.get('b') or []
    asks = payload.get('asks') or payload.get('a') or []

    bid_level = bids[0] if bids else None
    ask_level = asks[0] if asks else None

    if isinstance(bid_level, (list, tuple)):
        bid_price = _as_float(bid_level[0] if len(bid_level) > 0 else None)
        bid_qty = _as_float(bid_level[1] if len(bid_level) > 1 else None)
    else:
        bid_price = _as_float((bid_level or {}).get('price') if isinstance(bid_level, dict) else None)
        bid_qty = _as_float((bid_level or {}).get('quantity') if isinstance(bid_level, dict) else None)

    if isinstance(ask_level, (list, tuple)):
        ask_price = _as_float(ask_level[0] if len(ask_level) > 0 else None)
        ask_qty = _as_float(ask_level[1] if len(ask_level) > 1 else None)
    else:
        ask_price = _as_float((ask_level or {}).get('price') if isinstance(ask_level, dict) else None)
        ask_qty = _as_float((ask_level or {}).get('quantity') if isinstance(ask_level, dict) else None)

    return BinanceTopOfBookSignal(
        ofi_score=compute_ofi_score(bid_price, bid_qty, ask_price, ask_qty),
        bid_price=bid_price,
        bid_qty=bid_qty,
        ask_price=ask_price,
        ask_qty=ask_qty,
        signal_at=now or datetime.now(timezone.utc),
    )
