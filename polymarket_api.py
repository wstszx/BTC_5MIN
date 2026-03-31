from __future__ import annotations

import csv
import json
import random
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config import AppConfig
from models import MarketQuote, MarketWindow, ResolvedRound


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_outcome_label(label: str) -> str:
    lowered = label.strip().lower()
    if lowered in {"up", "yes"}:
        return "UP"
    if lowered in {"down", "no"}:
        return "DOWN"
    return label.strip().upper()


def parse_json_list_field(raw_value: str | list[Any] | None) -> list[Any]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []
    return []


def parse_outcome_prices(outcome_prices_raw: str | list[Any], outcomes_raw: str | list[Any]) -> dict[str, float]:
    prices = parse_json_list_field(outcome_prices_raw)
    outcomes = parse_json_list_field(outcomes_raw)
    parsed: dict[str, float] = {}

    for outcome, price in zip(outcomes, prices):
        try:
            parsed[normalize_outcome_label(str(outcome))] = float(price)
        except (TypeError, ValueError):
            continue

    return parsed


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_result(
    *,
    metadata: dict[str, Any],
    market: dict[str, Any],
) -> tuple[float | None, float | None, str]:
    price_to_beat = _optional_float(metadata.get("priceToBeat"))
    final_price = _optional_float(metadata.get("finalPrice"))

    if price_to_beat is not None and final_price is not None:
        return price_to_beat, final_price, "UP" if final_price >= price_to_beat else "DOWN"

    prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
    up_price = prices.get("UP")
    down_price = prices.get("DOWN")
    if up_price is None or down_price is None:
        return price_to_beat, final_price, ""
    if up_price > down_price:
        return price_to_beat, final_price, "UP"
    if down_price > up_price:
        return price_to_beat, final_price, "DOWN"
    return price_to_beat, final_price, ""


def extract_token_ids(clob_token_ids_raw: str | list[Any], outcomes_raw: str | list[Any]) -> dict[str, str]:
    token_ids = parse_json_list_field(clob_token_ids_raw)
    outcomes = parse_json_list_field(outcomes_raw)
    parsed: dict[str, str] = {}

    for outcome, token_id in zip(outcomes, token_ids):
        parsed[normalize_outcome_label(str(outcome))] = str(token_id)

    return parsed


def nearest_price_from_history(history_payload: dict[str, Any], target_ts: int) -> float | None:
    closest = nearest_history_point(history_payload, target_ts)
    if closest is None:
        return None
    return closest["price"]


def nearest_history_point(
    history_payload: dict[str, Any],
    target_ts: int,
    *,
    max_offset_seconds: int | None = None,
) -> dict[str, float | int] | None:
    history = history_payload.get("history", [])
    if not history:
        return None

    closest = min(history, key=lambda item: abs(int(item.get("t", 0)) - target_ts))
    point_ts = int(closest.get("t", 0))
    offset = abs(point_ts - target_ts)
    if max_offset_seconds is not None and max_offset_seconds >= 0 and offset > max_offset_seconds:
        return None
    price = _optional_float(closest.get("p"))
    if price is None:
        return None
    return {"timestamp": point_ts, "price": price, "offset_seconds": offset}


def build_resolved_round(event_payload: dict[str, Any]) -> ResolvedRound:
    market = (event_payload.get("markets") or [{}])[0]
    metadata = event_payload.get("eventMetadata") or {}
    token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
    price_to_beat, final_price, result = resolve_result(metadata=metadata, market=market)
    if price_to_beat is None or final_price is None:
        raise ValueError("Resolved round is missing priceToBeat/finalPrice metadata.")

    return ResolvedRound(
        event_id=str(event_payload.get("id", "")),
        market_id=str(market.get("id", "")),
        slug=str(event_payload.get("slug") or market.get("slug") or ""),
        title=str(event_payload.get("title") or market.get("question") or ""),
        start_time=parse_iso_datetime(event_payload.get("startTime") or market.get("eventStartTime") or market.get("startDate")) or datetime.now(timezone.utc),
        end_time=parse_iso_datetime(event_payload.get("endDate") or market.get("endDate")) or datetime.now(timezone.utc),
        price_to_beat=price_to_beat,
        final_price=final_price,
        result=result,
        up_token_id=token_ids.get("UP"),
        down_token_id=token_ids.get("DOWN"),
    )


class PolymarketClient:
    def __init__(self, config: AppConfig | None = None, session: requests.Session | None = None) -> None:
        self.config = config or AppConfig()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "BTC_5MIN/0.1"})

    def _get_json(
        self,
        path: str,
        *,
        base_url: str,
        params: dict[str, Any] | None = None,
        retries: int | None = None,
    ) -> Any:
        retries = retries if retries is not None else self.config.api_retry_count
        retries = max(1, retries)
        base_delay = max(0.0, self.config.api_retry_base_delay_seconds)
        max_delay = max(base_delay, self.config.api_retry_max_delay_seconds)
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self.session.get(f"{base_url}{path}", params=params, timeout=15)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                jitter = random.uniform(0.0, delay * 0.2) if delay > 0 else 0.0
                time.sleep(delay + jitter)
        if last_error is None:
            raise RuntimeError("Request failed without an exception.")
        raise RuntimeError(f"Unable to fetch {base_url}{path} after {retries} attempts: {last_error}") from last_error

    def list_series_events(
        self,
        *,
        limit: int = 200,
        offset: int | None = None,
        active: bool | None = None,
        closed: bool | None = None,
        archived: bool | None = False,
        start_time_min: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"series_id": self.config.series_id, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if archived is not None:
            params["archived"] = str(archived).lower()
        if start_time_min is not None:
            if isinstance(start_time_min, datetime):
                if start_time_min.tzinfo is None:
                    start_time_min = start_time_min.replace(tzinfo=timezone.utc)
                params["start_time_min"] = start_time_min.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                params["start_time_min"] = start_time_min

        payload = self._get_json("/events", base_url=self.config.gamma_api_base, params=params)
        events = payload.get("value", payload) if isinstance(payload, dict) else payload
        filtered: list[dict[str, Any]] = []
        for event in events or []:
            slug = str(event.get("slug", ""))
            series_slug = str(event.get("seriesSlug") or "")
            if series_slug == self.config.series_slug or slug.startswith("btc-updown-5m-"):
                filtered.append(event)
        return filtered

    def get_event_by_slug(self, slug: str) -> dict[str, Any]:
        return self._get_json(f"/events/slug/{slug}", base_url=self.config.gamma_api_base)

    def get_market_by_slug(self, slug: str) -> dict[str, Any]:
        payload = self._get_json("/markets", base_url=self.config.gamma_api_base, params={"slug": slug})
        if isinstance(payload, list):
            return payload[0] if payload else {}
        return payload

    def get_price_history(
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int = 60,
    ) -> dict[str, Any]:
        return self._get_json(
            "/prices-history",
            base_url=self.config.clob_api_base,
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": fidelity,
            },
        )

    def get_nearest_history_point(
        self,
        token_id: str,
        *,
        target_ts: int,
        start_ts: int,
        end_ts: int,
        fidelity: int = 60,
        max_offset_seconds: int | None = None,
    ) -> dict[str, float | int] | None:
        if end_ts <= start_ts:
            return None
        payload = self.get_price_history(token_id, start_ts=start_ts, end_ts=end_ts, fidelity=fidelity)
        return nearest_history_point(
            payload,
            target_ts,
            max_offset_seconds=max_offset_seconds,
        )

    def event_to_market_window(self, event: dict[str, Any]) -> MarketWindow:
        market = (event.get("markets") or [{}])[0]
        token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
        return MarketWindow(
            event_id=str(event.get("id", "")),
            market_id=str(market.get("id", "")),
            slug=str(event.get("slug") or market.get("slug") or ""),
            title=str(event.get("title") or market.get("question") or ""),
            start_time=parse_iso_datetime(event.get("startTime") or market.get("eventStartTime") or market.get("startDate")) or datetime.now(timezone.utc),
            end_time=parse_iso_datetime(event.get("endDate") or market.get("endDate")) or datetime.now(timezone.utc),
            up_token_id=token_ids.get("UP"),
            down_token_id=token_ids.get("DOWN"),
        )

    def quote_from_market(self, market: dict[str, Any]) -> MarketQuote:
        prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
        return MarketQuote(
            slug=str(market.get("slug", "")),
            up_price=prices.get("UP"),
            down_price=prices.get("DOWN"),
            up_best_bid=float(market["bestBid"]) if market.get("bestBid") is not None else None,
            up_best_ask=float(market["bestAsk"]) if market.get("bestAsk") is not None else None,
            down_best_bid=None,
            down_best_ask=None,
            accepting_orders=bool(market.get("acceptingOrders", False)),
            fetched_at=datetime.now(timezone.utc),
        )

    def find_current_and_next_rounds(
        self,
        *,
        now: datetime | None = None,
        limit: int = 200,
    ) -> tuple[MarketWindow | None, MarketWindow | None]:
        now = now or datetime.now(timezone.utc)
        events = sorted(
            self.list_series_events(limit=limit, active=True, closed=False, archived=False),
            key=lambda item: parse_iso_datetime(item.get("startTime") or item.get("endDate") or "") or datetime.max.replace(tzinfo=timezone.utc),
        )

        current_round: MarketWindow | None = None
        next_round: MarketWindow | None = None
        for event in events:
            window = self.event_to_market_window(event)
            if window.start_time <= now < window.end_time:
                current_round = window
            if window.start_time >= now and next_round is None:
                next_round = window
        return current_round, next_round

    def export_history(
        self,
        *,
        output_path: Path,
        limit: int = 100,
        active: bool | None = True,
        closed: bool | None = True,
    ) -> Path:
        now_utc = datetime.now(timezone.utc)
        recent_start = now_utc - timedelta(minutes=max(60, limit * 6))
        page_size = min(200, max(50, limit))
        paged_events: list[dict[str, Any]] = []
        offset = 0

        # Pull recent rounds first to avoid very old events that no longer have usable history snapshots.
        while True:
            batch = self.list_series_events(
                limit=page_size,
                offset=offset,
                active=active,
                closed=closed,
                archived=False,
                start_time_min=recent_start,
            )
            if not batch:
                break
            paged_events.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
            if offset >= page_size * 20:
                break

        events = paged_events or self.list_series_events(limit=limit, active=active, closed=closed, archived=False)
        ordered_events = sorted(
            events,
            key=lambda item: parse_iso_datetime(item.get("startTime") or item.get("startDate") or item.get("endDate") or "")
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        events = ordered_events[-limit:]
        rows: list[dict[str, Any]] = []

        for event in events:
            event_details = event if event.get("markets") else self.get_event_by_slug(str(event.get("slug")))
            market = (event_details.get("markets") or [{}])[0]
            metadata = event_details.get("eventMetadata") or {}
            prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
            token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
            price_to_beat, final_price, result = resolve_result(metadata=metadata, market=market)

            row = {
                "event_id": event_details.get("id", ""),
                "market_id": market.get("id", ""),
                "slug": event_details.get("slug") or market.get("slug") or "",
                "title": event_details.get("title") or market.get("question") or "",
                "series_id": self.config.series_id,
                "start_time": event_details.get("startTime") or market.get("eventStartTime") or "",
                "end_time": event_details.get("endDate") or market.get("endDate") or "",
                "price_to_beat": price_to_beat,
                "final_price": final_price,
                "result": result,
                "up_token_id": token_ids.get("UP"),
                "down_token_id": token_ids.get("DOWN"),
                "up_last_price": prices.get("UP"),
                "down_last_price": prices.get("DOWN"),
                "up_best_bid": market.get("bestBid"),
                "up_best_ask": market.get("bestAsk"),
                "down_best_bid": None,
                "down_best_ask": None,
                "entry_price_open_up": None,
                "entry_price_open_down": None,
                "entry_price_preclose_up": None,
                "entry_price_preclose_down": None,
            }

            start_time = parse_iso_datetime(row["start_time"])
            end_time = parse_iso_datetime(row["end_time"])
            if start_time and end_time:
                start_ts = int(start_time.timestamp())
                end_ts = int(end_time.timestamp())
                history_start_ts = max(0, start_ts - self.config.history_lookback_seconds)

                for side, token_id in (("UP", token_ids.get("UP")), ("DOWN", token_ids.get("DOWN"))):
                    if not token_id:
                        continue
                    history = self.get_price_history(
                        token_id,
                        start_ts=history_start_ts,
                        end_ts=end_ts,
                        fidelity=max(1, self.config.history_entry_fidelity_seconds),
                    )
                    open_point = nearest_history_point(
                        history,
                        start_ts + self.config.open_delay_seconds,
                        max_offset_seconds=max(0, self.config.history_entry_max_offset_seconds),
                    )
                    preclose_point = nearest_history_point(
                        history,
                        max(start_ts, end_ts - self.config.preclose_seconds),
                        max_offset_seconds=max(0, self.config.history_entry_max_offset_seconds),
                    )
                    if open_point is None:
                        open_point = nearest_history_point(history, start_ts + self.config.open_delay_seconds)
                    if preclose_point is None:
                        preclose_point = nearest_history_point(history, max(start_ts, end_ts - self.config.preclose_seconds))
                    row[f"entry_price_open_{side.lower()}"] = open_point["price"] if open_point is not None else None
                    row[f"entry_price_preclose_{side.lower()}"] = preclose_point["price"] if preclose_point is not None else None

            rows.append(row)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(rows[0].keys()) if rows else [
            "event_id",
            "market_id",
            "slug",
            "title",
            "series_id",
            "start_time",
            "end_time",
            "price_to_beat",
            "final_price",
            "result",
            "up_token_id",
            "down_token_id",
            "up_last_price",
            "down_last_price",
            "up_best_bid",
            "up_best_ask",
            "down_best_bid",
            "down_best_ask",
            "entry_price_open_up",
            "entry_price_open_down",
            "entry_price_preclose_up",
            "entry_price_preclose_down",
        ]

        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        return output_path
