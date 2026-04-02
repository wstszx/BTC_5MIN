from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any

from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState, TradeRecord
from polymarket_api import PolymarketClient, extract_token_ids
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from strategy import get_side_for_round


@dataclass(slots=True)
class SideDecision:
    side: str | None
    reason: str | None = None
    signal_open_up_price: float | None = None
    signal_current_up_price: float | None = None
    signal_threshold: float | None = None
    signal_delta: float | None = None
    signal_locked: bool = False


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
    fieldnames = list(row.keys())

    write_header = not path.exists()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_header = next(csv.reader(handle), [])
        if existing_header and existing_header != fieldnames:
            legacy_path = path.with_name(f"{path.stem}_legacy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{path.suffix}")
            path.replace(legacy_path)
            write_header = True

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def resolve_quote_price(side: str, quote: MarketQuote) -> float | None:
    if side == "UP":
        return quote.up_best_ask if quote.up_best_ask is not None else quote.up_price
    if side == "DOWN":
        return quote.down_best_ask if quote.down_best_ask is not None else quote.down_price
    raise ValueError(f"Unsupported side: {side}")


def _resolve_signal_up_price(quote: MarketQuote) -> float | None:
    # For signal direction, prefer traded/last price to reduce orderbook ask spikes noise.
    return quote.up_price if quote.up_price is not None else quote.up_best_ask


def _is_valid_signal_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def _resolve_signal_round_open_up_price(
    *,
    cfg: AppConfig,
    state: SessionState,
    market_client: PolymarketClient | None,
    window: MarketWindow | None,
    current_up_price: float | None,
    now: datetime,
) -> float | None:
    if _is_valid_signal_price(state.signal_round_open_up_price):
        return state.signal_round_open_up_price
    if market_client is None or window is None or not window.up_token_id:
        return current_up_price

    target_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    now_ts = int(now.timestamp())
    window_end_ts = int(window.end_time.timestamp())
    # Pull a tight history window around round open, then find the nearest point to the intended open anchor.
    start_ts = max(0, target_ts - max(30, cfg.signal_anchor_max_offset_seconds * 2))
    end_ts = max(start_ts + 1, min(window_end_ts, max(now_ts, target_ts + cfg.signal_anchor_max_offset_seconds)))
    anchor = market_client.get_nearest_history_point(
        window.up_token_id,
        target_ts=target_ts,
        start_ts=start_ts,
        end_ts=end_ts,
        fidelity=max(1, cfg.signal_history_fidelity_seconds),
        max_offset_seconds=max(0, cfg.signal_anchor_max_offset_seconds),
    )
    if anchor is None:
        return current_up_price
    return float(anchor["price"])


def _compute_signal_threshold(
    *,
    cfg: AppConfig,
    market_client: PolymarketClient | None,
    window: MarketWindow | None,
    now: datetime,
) -> float:
    base_threshold = max(0.0, cfg.signal_momentum_threshold)
    if market_client is None or window is None or not window.up_token_id:
        return base_threshold

    start_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    end_ts = min(int(window.end_time.timestamp()), int(now.timestamp()))
    if end_ts <= start_ts:
        return base_threshold

    history_payload = market_client.get_price_history(
        window.up_token_id,
        start_ts=start_ts,
        end_ts=end_ts,
        fidelity=max(1, cfg.signal_history_fidelity_seconds),
    )
    prices: list[float] = []
    for item in history_payload.get("history", []):
        raw = item.get("p")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value < 1:
            prices.append(value)

    if len(prices) < max(2, cfg.signal_dynamic_threshold_min_points):
        return base_threshold

    deltas = [curr - prev for prev, curr in zip(prices[:-1], prices[1:])]
    if not deltas:
        return base_threshold

    dynamic_threshold = max(0.0, cfg.signal_dynamic_threshold_k) * pstdev(deltas)
    return max(base_threshold, dynamic_threshold)


def _resolve_side_from_strategy(
    *,
    cfg: AppConfig,
    state: SessionState,
    slug: str,
    quote: MarketQuote,
    market_client: PolymarketClient | None = None,
    window: MarketWindow | None = None,
    now: datetime | None = None,
    entry_time: datetime | None = None,
) -> SideDecision:
    if cfg.strategy_id != 5:
        return SideDecision(side=get_side_for_round(cfg.strategy_id, state.round_index))

    if state.signal_round_slug != slug:
        state.signal_round_slug = slug
        state.signal_round_open_up_price = None
        state.signal_round_locked_side = None

    signal_current_up_price = _resolve_signal_up_price(quote)
    if state.signal_round_locked_side in {"UP", "DOWN"}:
        signal_delta = None
        if _is_valid_signal_price(state.signal_round_open_up_price) and _is_valid_signal_price(signal_current_up_price):
            signal_delta = signal_current_up_price - state.signal_round_open_up_price
        return SideDecision(
            side=state.signal_round_locked_side,
            signal_open_up_price=state.signal_round_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_delta=signal_delta,
            signal_locked=True,
        )

    now = now or datetime.now(timezone.utc)
    signal_open_up_price = _resolve_signal_round_open_up_price(
        cfg=cfg,
        state=state,
        market_client=market_client,
        window=window,
        current_up_price=signal_current_up_price,
        now=now,
    )
    state.signal_round_open_up_price = signal_open_up_price
    signal_threshold = _compute_signal_threshold(
        cfg=cfg,
        market_client=market_client,
        window=window,
        now=now,
    )

    weak_mode = cfg.signal_weak_signal_mode.upper()
    fallback_strategy = cfg.signal_fallback_strategy_id
    if fallback_strategy == 5:
        fallback_strategy = 2

    if _is_valid_signal_price(signal_open_up_price) and _is_valid_signal_price(signal_current_up_price):
        signal_delta = signal_current_up_price - signal_open_up_price
        resolved_side: str | None = None
        reason: str | None = None

        if signal_delta >= signal_threshold:
            resolved_side = "UP"
        elif signal_delta <= -signal_threshold:
            resolved_side = "DOWN"
        elif weak_mode == "FALLBACK":
            resolved_side = get_side_for_round(fallback_strategy, state.round_index)
            reason = "signal_too_weak_fallback"
        else:
            reason = "signal_too_weak_skip"

        if resolved_side in {"UP", "DOWN"} and entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side

        return SideDecision(
            side=resolved_side,
            reason=reason,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=signal_threshold,
            signal_delta=signal_delta,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    if weak_mode == "FALLBACK":
        resolved_side = get_side_for_round(fallback_strategy, state.round_index)
        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            reason="signal_price_unavailable_fallback",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=signal_threshold,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    return SideDecision(
        side=None,
        reason="signal_price_unavailable",
        signal_open_up_price=signal_open_up_price,
        signal_current_up_price=signal_current_up_price,
        signal_threshold=signal_threshold,
    )


def _update_max_stake_skip_streak(
    state: SessionState,
    *,
    skip_reason: str | None,
    threshold: int,
) -> bool:
    if skip_reason == "order_cost_above_max_stake":
        state.consecutive_max_stake_skips += 1
        return state.consecutive_max_stake_skips == max(1, threshold)

    state.consecutive_max_stake_skips = 0
    return False


def _emit_max_stake_skip_alert(
    *,
    slug: str,
    side: str,
    price: float | None,
    state: SessionState,
    cfg: AppConfig,
) -> None:
    printable_price = "N/A" if price is None else f"{price:.4f}"
    print(
        "[WARN] order_cost_above_max_stake 连续触发 "
        f"{state.consecutive_max_stake_skips} 次 | slug={slug} side={side} price={printable_price} "
        f"recovery_loss={state.recovery_loss:.4f} max_stake={cfg.max_stake:.4f} "
        "（仅告警，不会自动重置策略状态）"
    )


def _runtime_backoff_seconds(cfg: AppConfig, consecutive_errors: int) -> int:
    scaled = cfg.runtime_error_backoff_base_seconds * (2 ** max(0, consecutive_errors - 1))
    return max(1, min(cfg.runtime_error_backoff_max_seconds, scaled))


def _resolve_live_order_type(raw_order_type: str):
    from py_clob_client.clob_types import OrderType

    normalized = (raw_order_type or "FOK").upper()
    return getattr(OrderType, normalized, OrderType.FOK)


def _create_live_clob_client(cfg: AppConfig):
    if not cfg.live_private_key:
        raise RuntimeError("Missing PRIVATE_KEY/POLYMARKET_PRIVATE_KEY for live trading.")

    from py_clob_client.client import ClobClient

    clob_client = ClobClient(
        cfg.clob_api_base,
        chain_id=cfg.live_chain_id,
        key=cfg.live_private_key,
        signature_type=cfg.live_signature_type,
        funder=cfg.live_funder,
    )
    clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
    return clob_client


def place_live_order(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    market_client = market_client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "live_session_state.json"
    log_path = log_path or cfg.logs_dir / "live_orders.csv"
    state = load_session_state(state_path)

    now = datetime.now(timezone.utc)
    current_round, next_round = market_client.find_current_and_next_rounds(now=now)
    target_round = current_round or next_round
    if target_round is None:
        return {"status": "no_market"}

    entry_time = _entry_time_for_round(cfg, target_round)
    market = market_client.get_market_by_slug(target_round.slug)
    quote = market_client.quote_from_market(market)
    print('[live] quote {' + _describe_quote_source(quote) + '}', flush=True)
    print('[live] ws_runtime {' + _describe_ws_runtime(market_client) + '}', flush=True)
    side_decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug=target_round.slug,
        quote=quote,
        market_client=market_client,
        window=target_round,
        now=now,
        entry_time=entry_time,
    )
    if side_decision.side is None:
        if not dry_run:
            append_trade_log(
                log_path,
                TradeRecord(
                    timestamp=datetime.now(timezone.utc),
                    mode="live",
                    round_index=state.round_index,
                    strategy=cfg.strategy_id,
                    entry_timing=cfg.entry_timing,
                    event_slug=target_round.slug,
                    start_time=target_round.start_time,
                    end_time=target_round.end_time,
                    side="SKIP",
                    price=None,
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    skip_reason=side_decision.reason or "signal_unavailable",
                    **_signal_record_kwargs(side_decision),
                ),
            )
        save_session_state(state_path, state)
        return {
            "status": "dry_run" if dry_run else "skipped",
            "slug": target_round.slug,
            "side": None,
            "token_id": None,
            "price": None,
            "should_trade": False,
            "skip_reason": side_decision.reason or "signal_unavailable",
            "entry_time": entry_time.isoformat(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    side = side_decision.side
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
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
    )
    token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
    token_id = token_ids.get(side)

    if dry_run:
        projected_streak = (
            state.consecutive_max_stake_skips + 1
            if plan.skip_reason == "order_cost_above_max_stake"
            else 0
        )
        return {
            "status": "dry_run",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": plan.should_trade,
            "skip_reason": plan.skip_reason,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "projected_max_stake_skip_streak": projected_streak,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }

    if not cfg.live_trading_enabled:
        raise RuntimeError("Live trading is disabled. Set LIVE_TRADING_ENABLED=true (or config flag) to submit orders.")
    if not plan.should_trade:
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="live",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=price,
                order_size=0.0,
                order_cost=0.0,
                expected_profit=0.0,
                result=None,
                trade_pnl=0.0,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
                skip_reason=plan.skip_reason,
                stop_loss_triggered=plan.stop_loss_triggered,
                **_signal_record_kwargs(side_decision),
            ),
        )
        should_alert = _update_max_stake_skip_streak(
            state,
            skip_reason=plan.skip_reason,
            threshold=cfg.max_stake_skip_alert_threshold,
        )
        if should_alert:
            _emit_max_stake_skip_alert(
                slug=target_round.slug,
                side=side,
                price=price,
                state=state,
                cfg=cfg,
            )
        save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "price": price,
            "skip_reason": plan.skip_reason,
            "max_stake_skip_streak": state.consecutive_max_stake_skips,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    state.consecutive_max_stake_skips = 0
    if token_id is None:
        raise RuntimeError(f"Missing token id for side={side} on market={target_round.slug}")

    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import BUY

    live_client = clob_client or _create_live_clob_client(cfg)
    order_type = _resolve_live_order_type(cfg.live_order_type)
    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=plan.order_cost,
        side=BUY,
        order_type=order_type,
    )
    signed_order = live_client.create_market_order(order_args)
    response = live_client.post_order(signed_order, order_type)

    order_id = None
    if isinstance(response, dict):
        order_id = response.get("orderID") or response.get("orderId") or response.get("id")

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
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
            result=None,
            trade_pnl=0.0,
            cash_pnl=state.cash_pnl,
            recovery_loss=state.recovery_loss,
            consecutive_losses=state.consecutive_losses,
            **_signal_record_kwargs(side_decision),
        ),
    )
    state.round_index += 1
    save_session_state(state_path, state)

    return {
        "status": "submitted",
        "slug": target_round.slug,
        "side": side,
        "token_id": token_id,
        "price": price,
        "order_size": plan.order_size,
        "order_cost": plan.order_cost,
        "expected_profit": plan.expected_profit,
        "order_type": cfg.live_order_type.upper(),
        "order_id": order_id,
        "response": response,
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
    }


def _entry_time_for_round(cfg: AppConfig, window: MarketWindow) -> datetime:
    if cfg.entry_timing.upper() == "PRE_CLOSE":
        return window.end_time - timedelta(seconds=cfg.preclose_seconds)
    return window.start_time + timedelta(seconds=cfg.open_delay_seconds)


def _sleep_until_round_end(cfg: AppConfig, window: MarketWindow) -> None:
    while datetime.now(timezone.utc) < window.end_time:
        time.sleep(cfg.poll_interval_seconds)


def _runtime_log(message: str) -> None:
    print('[' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' UTC] ' + message, flush=True)


def _fmt_price(value: float | None) -> str:
    return 'N/A' if value is None else f'{value:.4f}'


def _signal_record_kwargs(side_decision: SideDecision) -> dict[str, Any]:
    return {
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
        "signal_reason": side_decision.reason,
    }


def _describe_side_decision(side_decision: SideDecision) -> str:
    signal_bits = []
    if side_decision.signal_open_up_price is not None:
        signal_bits.append('open_up=' + _fmt_price(side_decision.signal_open_up_price))
    if side_decision.signal_current_up_price is not None:
        signal_bits.append('current_up=' + _fmt_price(side_decision.signal_current_up_price))
    if side_decision.signal_threshold is not None:
        signal_bits.append('threshold=' + _fmt_price(side_decision.signal_threshold))
    if side_decision.signal_delta is not None:
        signal_bits.append('delta=' + _fmt_price(side_decision.signal_delta))
    signal_bits.append('locked=' + str(side_decision.signal_locked))
    if side_decision.reason:
        signal_bits.append('reason=' + side_decision.reason)
    return ', '.join(signal_bits)


def _describe_quote_source(quote: MarketQuote) -> str:
    source = quote.source or 'http'
    return (
        'source=' + source
        + ', up_best_ask=' + _fmt_price(quote.up_best_ask)
        + ', up_price=' + _fmt_price(quote.up_price)
        + ', down_best_ask=' + _fmt_price(quote.down_best_ask)
        + ', down_price=' + _fmt_price(quote.down_price)
    )


def _describe_ws_runtime(client: PolymarketClient | Any) -> str:
    get_stats = getattr(client, 'get_ws_runtime_stats', None)
    if not callable(get_stats):
        return 'ws_stats_unavailable'
    stats = get_stats()
    return (
        'ws_enabled=' + str(stats.get('ws_enabled'))
        + ', ws_available=' + str(stats.get('ws_available'))
        + ', ws_connected=' + str(stats.get('ws_connected'))
        + ', reconnects=' + str(stats.get('ws_reconnect_count'))
        + ', invalid_ops=' + str(stats.get('ws_invalid_operation_count'))
        + ', connect_attempts=' + str(stats.get('ws_connect_attempts'))
        + ', subscribed_assets=' + str(stats.get('ws_subscribed_asset_count'))
        + ', cached_assets=' + str(stats.get('ws_cached_asset_count'))
        + ', last_message_age_s=' + _fmt_price(stats.get('ws_last_message_age_seconds'))
        + ', last_error=' + str(stats.get('ws_last_error'))
    )


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
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
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
    consecutive_errors = 0
    _runtime_log(
        'paper-trade started | strategy=' + str(cfg.strategy_id)
        + ' entry_timing=' + cfg.entry_timing
        + ' poll=' + str(cfg.poll_interval_seconds)
        + 's dry_run_once=' + str(dry_run_once)
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            current_round, next_round = client.find_current_and_next_rounds(now=now)
            target_round = current_round or next_round
            if target_round is None:
                if dry_run_once:
                    return {"status": "no_market"}
                _runtime_log('no active round found; waiting ' + str(cfg.poll_interval_seconds) + 's')
                consecutive_errors = 0
                time.sleep(cfg.poll_interval_seconds)
                continue

            entry_time = _entry_time_for_round(cfg, target_round)
            market = client.get_market_by_slug(target_round.slug)
            quote = client.quote_from_market(market)
            _runtime_log('round=' + target_round.slug + ' quote {' + _describe_quote_source(quote) + '}')
            _runtime_log('round=' + target_round.slug + ' ws_runtime {' + _describe_ws_runtime(client) + '}')
            side_decision = _resolve_side_from_strategy(
                cfg=cfg,
                state=state,
                slug=target_round.slug,
                quote=quote,
                market_client=client,
                window=target_round,
                now=now,
                entry_time=entry_time,
            )
            _runtime_log(
                'round=' + target_round.slug
                + ' side=' + str(side_decision.side)
                + ' entry_at=' + entry_time.isoformat()
                + ' signal={' + _describe_side_decision(side_decision) + '}'
                + ' quote_source=' + str(quote.source)
            )
            if side_decision.side is None:
                if dry_run_once:
                    _runtime_log(
                        'dry-run round=' + target_round.slug
                        + ' skip due to signal; reason=' + str(side_decision.reason or 'signal_unavailable')
                    )
                    return {
                        "status": "dry_run",
                        "slug": target_round.slug,
                        "side": None,
                        "price": None,
                        "should_trade": False,
                        "skip_reason": side_decision.reason or "signal_unavailable",
                        "entry_time": entry_time.isoformat(),
                        "signal_open_up_price": side_decision.signal_open_up_price,
                        "signal_current_up_price": side_decision.signal_current_up_price,
                        "signal_threshold": side_decision.signal_threshold,
                        "signal_delta": side_decision.signal_delta,
                        "signal_locked": side_decision.signal_locked,
                    }
                if now < entry_time:
                    sleep_seconds = min(cfg.poll_interval_seconds, max(1, int((entry_time - now).total_seconds())))
                    _runtime_log(
                        'round=' + target_round.slug
                        + ' weak/no signal before entry; sleep ' + str(sleep_seconds) + 's then retry'
                    )
                    consecutive_errors = 0
                    time.sleep(sleep_seconds)
                    continue
                _runtime_log(
                    'round=' + target_round.slug
                    + ' skip trade due to signal; reason=' + str(side_decision.reason or 'signal_unavailable')
                )
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
                        side="SKIP",
                        price=None,
                        order_size=0.0,
                        order_cost=0.0,
                        expected_profit=0.0,
                        result=None,
                        trade_pnl=0.0,
                        cash_pnl=state.cash_pnl,
                        recovery_loss=state.recovery_loss,
                        consecutive_losses=state.consecutive_losses,
                        skip_reason=side_decision.reason or "signal_unavailable",
                        **_signal_record_kwargs(side_decision),
                    ),
                )
                state.round_index += 1
                save_session_state(state_path, state)
                consecutive_errors = 0
                _sleep_until_round_end(cfg, target_round)
                continue

            side = side_decision.side
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
                bet_sizing_mode=cfg.bet_sizing_mode,
                base_order_cost=cfg.base_order_cost,
            )
            _runtime_log(
                'round=' + target_round.slug
                + ' plan should_trade=' + str(plan.should_trade)
                + ' side=' + side
                + ' price=' + _fmt_price(price)
                + ' order_cost=' + f'{plan.order_cost:.4f}'
                + ' order_size=' + f'{plan.order_size:.4f}'
                + ' skip_reason=' + str(plan.skip_reason)
                + ' quote_source=' + str(quote.source)
            )

            if dry_run_once:
                projected_streak = (
                    state.consecutive_max_stake_skips + 1
                    if plan.skip_reason == "order_cost_above_max_stake"
                    else 0
                )
                _runtime_log(
                    'dry-run round=' + target_round.slug
                    + ' side=' + side
                    + ' should_trade=' + str(plan.should_trade)
                    + ' price=' + _fmt_price(price)
                    + ' skip_reason=' + str(plan.skip_reason)
                )
                return {
                    "status": "dry_run",
                    "slug": target_round.slug,
                    "side": side,
                    "price": price,
                    "should_trade": plan.should_trade,
                    "skip_reason": plan.skip_reason,
                    "entry_time": entry_time.isoformat(),
                    "projected_max_stake_skip_streak": projected_streak,
                    "signal_open_up_price": side_decision.signal_open_up_price,
                    "signal_current_up_price": side_decision.signal_current_up_price,
                    "signal_threshold": side_decision.signal_threshold,
                    "signal_delta": side_decision.signal_delta,
                    "signal_locked": side_decision.signal_locked,
                }

            if not plan.should_trade:
                if now < entry_time:
                    sleep_seconds = min(cfg.poll_interval_seconds, max(1, int((entry_time - now).total_seconds())))
                    _runtime_log(
                        'round=' + target_round.slug
                        + ' not tradable before entry; sleep ' + str(sleep_seconds) + 's then retry'
                    )
                    consecutive_errors = 0
                    time.sleep(sleep_seconds)
                    continue
                _runtime_log(
                    'round=' + target_round.slug
                    + ' skip trade due to risk gate; reason=' + str(plan.skip_reason)
                )
                should_alert = _update_max_stake_skip_streak(
                    state,
                    skip_reason=plan.skip_reason,
                    threshold=cfg.max_stake_skip_alert_threshold,
                )
                if should_alert:
                    _emit_max_stake_skip_alert(
                        slug=target_round.slug,
                        side=side,
                        price=price,
                        state=state,
                        cfg=cfg,
                    )
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
                        price=price,
                        order_size=0.0,
                        order_cost=0.0,
                        expected_profit=0.0,
                        result=None,
                        trade_pnl=0.0,
                        cash_pnl=state.cash_pnl,
                        recovery_loss=state.recovery_loss,
                        consecutive_losses=state.consecutive_losses,
                        stop_loss_triggered=plan.stop_loss_triggered,
                        skip_reason=plan.skip_reason,
                        **_signal_record_kwargs(side_decision),
                    ),
                )
                if plan.stop_loss_triggered:
                    state = reset_after_stop_loss(state)
                state.round_index += 1
                save_session_state(state_path, state)
                consecutive_errors = 0
                _sleep_until_round_end(cfg, target_round)
                continue
            state.consecutive_max_stake_skips = 0

            if now < entry_time:
                sleep_seconds = min(cfg.poll_interval_seconds, max(1, int((entry_time - now).total_seconds())))
                _runtime_log(
                    'round=' + target_round.slug
                    + ' waiting for entry; sleep ' + str(sleep_seconds) + 's'
                )
                consecutive_errors = 0
                time.sleep(sleep_seconds)
                continue

            _runtime_log('round=' + target_round.slug + ' entered trade; waiting for settlement')
            _sleep_until_round_end(cfg, target_round)

            while True:
                try:
                    settled_state, result = _settle_paper_trade(client, state, target_round, plan.price or 0.0, side=side, cfg=cfg)
                    break
                except RuntimeError as exc:
                    # Polymarket resolution metadata can lag a few polling cycles after round end.
                    if "is not resolved yet" in str(exc):
                        _runtime_log('round=' + target_round.slug + ' pending resolution')
                        time.sleep(cfg.poll_interval_seconds)
                        continue
                    raise
            settled_state.round_index = state.round_index + 1
            trade_pnl = settled_state.cash_pnl - state.cash_pnl
            state = settled_state
            _runtime_log(
                'round=' + target_round.slug
                + ' settled result=' + result
                + ' trade_pnl=' + f'{trade_pnl:.4f}'
                + ' total_cash_pnl=' + f'{state.cash_pnl:.4f}'
                + ' consecutive_losses=' + str(state.consecutive_losses)
            )

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
                    **_signal_record_kwargs(side_decision),
                ),
            )
            save_session_state(state_path, state)
            consecutive_errors = 0
        except Exception as exc:
            if dry_run_once:
                return {"status": "error", "error": str(exc)}
            consecutive_errors += 1
            backoff = _runtime_backoff_seconds(cfg, consecutive_errors)
            _runtime_log('runtime error #' + str(consecutive_errors) + ': ' + str(exc) + ' | backoff=' + str(backoff) + 's')
            time.sleep(backoff)
