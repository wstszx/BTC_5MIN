from __future__ import annotations

import csv
from pathlib import Path

from config import AppConfig
from polymarket_api import PolymarketClient


class _StubHistoryClient(PolymarketClient):
    def __init__(self, cfg: AppConfig, event: dict) -> None:
        super().__init__(config=cfg)
        self._event = event
        self.price_history_calls: list[tuple[str, int, int]] = []

    def list_series_events(self, **kwargs):  # type: ignore[override]
        return [self._event]

    def get_event_by_slug(self, slug: str):  # type: ignore[override]
        return self._event

    def get_price_history(  # type: ignore[override]
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int = 60,
    ):
        self.price_history_calls.append((token_id, start_ts, end_ts))
        event_start_ts = 1_700_000_000
        if start_ts <= event_start_ts - 900:
            price = 0.51 if token_id == "up-token" else 0.49
            return {
                "history": [
                    {"t": event_start_ts + 5, "p": price},
                    {"t": event_start_ts + 270, "p": price},
                ]
            }
        return {"history": []}


def _build_event(*, with_metadata: bool) -> dict:
    event = {
        "id": "evt-1",
        "slug": "btc-updown-5m-1",
        "title": "BTC Up or Down",
        "startTime": "2023-11-14T22:13:20Z",
        "endDate": "2023-11-14T22:18:20Z",
        "markets": [
            {
                "id": "mkt-1",
                "slug": "btc-updown-5m-1",
                "question": "BTC Up or Down",
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
                "clobTokenIds": '["up-token", "down-token"]',
                "bestBid": "0.45",
                "bestAsk": "0.55",
            }
        ],
    }
    event["eventMetadata"] = {"priceToBeat": 100.0, "finalPrice": 90.0} if with_metadata else None
    return event


def _read_single_row(csv_path: Path) -> dict[str, str]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    return rows[0]


def test_export_history_uses_lookback_window_for_entry_prices(tmp_path: Path):
    cfg = AppConfig()
    client = _StubHistoryClient(cfg, _build_event(with_metadata=True))
    output = tmp_path / "history.csv"

    client.export_history(output_path=output, limit=1)
    row = _read_single_row(output)

    assert row["entry_price_open_up"] == "0.51"
    assert row["entry_price_open_down"] == "0.49"
    assert row["entry_price_preclose_up"] == "0.51"
    assert row["entry_price_preclose_down"] == "0.49"


def test_export_history_falls_back_to_result_from_outcome_prices(tmp_path: Path):
    cfg = AppConfig()
    client = _StubHistoryClient(cfg, _build_event(with_metadata=False))
    output = tmp_path / "history.csv"

    client.export_history(output_path=output, limit=1)
    row = _read_single_row(output)

    assert row["result"] == "DOWN"
