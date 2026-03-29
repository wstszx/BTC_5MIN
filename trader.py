from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState, TradeRecord
from polymarket_api import PolymarketClient
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from strategy import get_side_for_round


def save_session_state(path: Path, state: SessionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def load_session_state(path: Path) -> SessionState:
    if not path.exists():
        return SessionState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SessionState(**payload)


def append_trade_log(path: Path, record: TradeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(record)
    row["timestamp"] = record.timestamp.isoformat()
    row["start_time"] = record.start_time.isoformat()
    row["end_time"] = record.end_time.isoformat()

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def resolve_quote_price(side: str, quote: MarketQuote) -> float | None:
    if side == "UP":
        return quote.up_best_ask if quote.up_best_ask is not None else quote.up_price
    if side == "DOWN":
        return quote.down_best_ask if quote.down_best_ask is not None else quote.down_price
    raise ValueError(f"Unsupported side: {side}")


def place_live_order(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("Live trading is disabled until credentials are configured.")


def _entry_time_for_round(cfg: AppConfig, window: MarketWindow) -> datetime:
    if cfg.entry_timing.upper() == "PRE_CLOSE":
        return window.end_time - timedelta(seconds=cfg.preclose_seconds)
    return window.start_time + timedelta(seconds=cfg.open_delay_seconds)


def _settle_paper_trade(
    client: PolymarketClient,
    state: SessionState,
    window: MarketWindow,
    price: float,
    *,
    side: str,
    cfg: AppConfig,
) -> tuple[SessionState, str]:
    event = client.get_event_by_slug(window.slug)
    metadata = event.get("eventMetadata") or {}
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        raise RuntimeError(f"Round {window.slug} is not resolved yet.")

    result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    plan = build_trade_plan(
        state=state,
        side=side,
        price=price,
        target_profit=cfg.target_profit,
        max_price_threshold=cfg.max_price_threshold,
        max_stake=cfg.max_stake,
        daily_loss_cap=cfg.daily_loss_cap,
        max_consecutive_losses=cfg.max_consecutive_losses,
    )
    updated_state = apply_round_outcome(state, plan, won=(result == side))
    return updated_state, result


def run_paper_trading(
    cfg: AppConfig | None = None,
    *,
    client: PolymarketClient | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run_once: bool = False,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    client = client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "session_state.json"
    log_path = log_path or cfg.logs_dir / "paper_trades.csv"
    state = load_session_state(state_path)

    while True:
        now = datetime.now(timezone.utc)
        current_round, next_round = client.find_current_and_next_rounds(now=now)
        target_round = current_round or next_round
        if target_round is None:
            if dry_run_once:
                return {"status": "no_market"}
            time.sleep(cfg.poll_interval_seconds)
            continue

        entry_time = _entry_time_for_round(cfg, target_round)
        side = get_side_for_round(cfg.strategy_id, state.round_index)
        market = client.get_market_by_slug(target_round.slug)
        quote = client.quote_from_market(market)
        price = resolve_quote_price(side, quote)
        plan = build_trade_plan(
            state=state,
            side=side,
            price=price,
            target_profit=cfg.target_profit,
            max_price_threshold=cfg.max_price_threshold,
            max_stake=cfg.max_stake,
            daily_loss_cap=cfg.daily_loss_cap,
            max_consecutive_losses=cfg.max_consecutive_losses,
        )

        if dry_run_once:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": side,
                "price": price,
                "should_trade": plan.should_trade,
                "skip_reason": plan.skip_reason,
                "entry_time": entry_time.isoformat(),
            }

        if not plan.should_trade:
            if plan.stop_loss_triggered:
                state = reset_after_stop_loss(state)
            state.round_index += 1
            save_session_state(state_path, state)
            time.sleep(cfg.poll_interval_seconds)
            continue

        if now < entry_time:
            sleep_seconds = min(cfg.poll_interval_seconds, max(1, int((entry_time - now).total_seconds())))
            time.sleep(sleep_seconds)
            continue

        while datetime.now(timezone.utc) < target_round.end_time:
            time.sleep(cfg.poll_interval_seconds)

        settled_state, result = _settle_paper_trade(client, state, target_round, plan.price or 0.0, side=side, cfg=cfg)
        settled_state.round_index = state.round_index + 1
        trade_pnl = settled_state.cash_pnl - state.cash_pnl
        state = settled_state

        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="paper",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=plan.price,
                order_size=plan.order_size,
                order_cost=plan.order_cost,
                expected_profit=plan.expected_profit,
                result=result,
                trade_pnl=trade_pnl,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
            ),
        )
        save_session_state(state_path, state)
