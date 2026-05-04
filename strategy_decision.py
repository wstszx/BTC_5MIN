from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

from binance_signal import BinanceDepth5SignalService
from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState
from strategy import (
    get_side_for_round,
    strategy7_signal_gap_ok,
    strategy7_strong_signal_allows_late_confirm,
    strategy7_weighted_side_for_signals,
)


@dataclass(slots=True)
class SideDecision:
    side: str | None
    reason: str | None = None
    candidate_side: str | None = None
    candidate_price: float | None = None
    signal_open_up_price: float | None = None
    signal_current_up_price: float | None = None
    signal_threshold: float | None = None
    signal_delta: float | None = None
    signal_locked: bool = False


def resolve_quote_price(side: str, quote: MarketQuote) -> float | None:
    if side == "UP":
        return quote.up_best_ask if quote.up_best_ask is not None else quote.up_price
    if side == "DOWN":
        return quote.down_best_ask if quote.down_best_ask is not None else quote.down_price
    raise ValueError(f"Unsupported side: {side}")


def entry_price_skip_reason(
    *,
    strategy_prefix: str,
    price: float | None,
    min_entry_price: float | None,
    max_entry_price: float | None,
) -> str | None:
    if price is None:
        return None
    if min_entry_price is not None and price < min_entry_price:
        return f"{strategy_prefix}_price_too_low"
    if max_entry_price is not None and price > max_entry_price:
        return f"{strategy_prefix}_price_too_high"
    return None


def resolve_signal_up_price(quote: MarketQuote) -> float | None:
    # For signal direction, prefer traded/last price to reduce orderbook ask spikes noise.
    return quote.up_price if quote.up_price is not None else quote.up_best_ask


def is_valid_signal_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def resolve_signal_round_open_up_price(
    *,
    cfg: AppConfig,
    state: SessionState,
    market_client: Any | None,
    window: MarketWindow | None,
    current_up_price: float | None,
    now: datetime,
) -> float | None:
    if is_valid_signal_price(state.signal_round_open_up_price):
        return state.signal_round_open_up_price
    if (
        market_client is None
        or window is None
        or not window.up_token_id
        or not hasattr(market_client, "get_nearest_history_point")
    ):
        return current_up_price

    target_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    now_ts = int(now.timestamp())
    window_end_ts = int(window.end_time.timestamp())
    # Pull a tight history window around round open, then find the nearest point to the intended open anchor.
    start_ts = max(0, target_ts - max(30, cfg.signal_anchor_max_offset_seconds * 2))
    end_ts = max(start_ts + 1, min(window_end_ts, max(now_ts, target_ts + cfg.signal_anchor_max_offset_seconds)))
    try:
        anchor = market_client.get_nearest_history_point(
            window.up_token_id,
            target_ts=target_ts,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=max(1, cfg.signal_history_fidelity_seconds),
            max_offset_seconds=max(0, cfg.signal_anchor_max_offset_seconds),
        )
    except Exception:
        return current_up_price
    if anchor is None:
        return current_up_price
    return float(anchor["price"])


def compute_signal_threshold(
    *,
    cfg: AppConfig,
    market_client: Any | None,
    window: MarketWindow | None,
    now: datetime,
) -> float:
    base_threshold = max(0.0, cfg.signal_momentum_threshold)
    if (
        market_client is None
        or window is None
        or not window.up_token_id
        or not hasattr(market_client, "get_price_history")
    ):
        return base_threshold

    start_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    end_ts = min(int(window.end_time.timestamp()), int(now.timestamp()))
    if end_ts <= start_ts:
        return base_threshold

    try:
        history_payload = market_client.get_price_history(
            window.up_token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=max(1, cfg.signal_history_fidelity_seconds),
        )
    except Exception:
        return base_threshold
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


def resolve_strategy6_ofi_score(quote: MarketQuote) -> float | None:
    raw = quote.strategy6_ofi_score if hasattr(quote, "strategy6_ofi_score") else getattr(quote, "strategy6_ofi_score", None)
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def is_strategy6_signal_stale(*, quote: MarketQuote, now: datetime, stale_seconds: float) -> bool:
    signal_at = getattr(quote, "strategy6_signal_at", None) or quote.fetched_at
    if signal_at is None:
        return True
    return (now - signal_at).total_seconds() > max(0.0, stale_seconds)


def format_binance_signal_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    response = getattr(exc, "response", None)
    response_text = str(getattr(response, "text", "") or "").strip()
    if response_text:
        if len(response_text) > 500:
            response_text = response_text[:500] + "..."
        message += f" | response={response_text}"
    return message


def apply_strategy6_signal_to_quote(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    binance_signal_service: BinanceDepth5SignalService | None,
    now: datetime | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
) -> None:
    if cfg.strategy_id not in {6, 7} or binance_signal_service is None:
        return
    now = now or datetime.now(timezone.utc)
    latest = binance_signal_service.latest()
    if latest is None or (now - latest.signal_at).total_seconds() > max(0.0, cfg.binance_signal_stale_seconds):
        try:
            refreshed = binance_signal_service.refresh_from_rest(now=now)
        except Exception as exc:
            if diagnostic_log is not None:
                diagnostic_log(f"binance signal refresh failed: {format_binance_signal_error(exc)}")
            refreshed = None
        if refreshed is not None:
            latest = refreshed
    if latest is None:
        return
    quote.strategy6_ofi_score = latest.ofi_score
    quote.strategy6_signal_at = latest.signal_at


def effective_strategy7_confirm_before_entry_seconds(
    *,
    cfg: AppConfig,
    window: MarketWindow | None,
    entry_time: datetime | None,
) -> float:
    configured = max(0.0, float(cfg.strategy7_confirm_before_entry_seconds))
    if window is None or entry_time is None:
        return configured
    available = max(0.0, (entry_time - window.start_time).total_seconds())
    return min(configured, available)


def resolve_side_from_strategy(
    *,
    cfg: AppConfig,
    state: SessionState,
    slug: str,
    quote: MarketQuote,
    market_client: Any | None = None,
    window: MarketWindow | None = None,
    now: datetime | None = None,
    entry_time: datetime | None = None,
    fallback_side_resolver: Callable[[int, int], str] = get_side_for_round,
) -> SideDecision:
    if cfg.strategy_id == 6:
        now = now or datetime.now(timezone.utc)
        ofi_score = resolve_strategy6_ofi_score(quote)
        state.strategy6_last_ofi_score = ofi_score
        if ofi_score is None:
            return SideDecision(side=None, reason="ofi_unavailable")
        if is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
            return SideDecision(side=None, reason="ofi_stale", signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        if ofi_score >= cfg.ofi_threshold:
            return SideDecision(side="UP", signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        if ofi_score <= -cfg.ofi_threshold:
            return SideDecision(side="DOWN", signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        return SideDecision(side=None, reason="ofi_too_weak", signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)

    if cfg.strategy_id not in {5, 7, 8}:
        return SideDecision(side=fallback_side_resolver(cfg.strategy_id, state.round_index))

    fallback_strategy = cfg.signal_fallback_strategy_id
    if state.signal_round_slug != slug:
        state.signal_round_slug = slug
        state.signal_round_open_up_price = None
        state.signal_round_locked_side = None

    signal_current_up_price = resolve_signal_up_price(quote)
    if state.signal_round_locked_side in {"UP", "DOWN"}:
        signal_delta = None
        if is_valid_signal_price(state.signal_round_open_up_price) and is_valid_signal_price(signal_current_up_price):
            signal_delta = signal_current_up_price - state.signal_round_open_up_price
        if cfg.strategy_id in {7, 8}:
            strategy_prefix = "strategy7" if cfg.strategy_id == 7 else "strategy8"
            candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
            price_skip_reason = entry_price_skip_reason(
                strategy_prefix=strategy_prefix,
                price=candidate_price,
                min_entry_price=getattr(cfg, "min_entry_price", None),
                max_entry_price=getattr(cfg, "max_entry_price", None),
            )
            if price_skip_reason is not None:
                return SideDecision(
                    side=None,
                    reason=price_skip_reason,
                    candidate_side=state.signal_round_locked_side,
                    candidate_price=candidate_price,
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=cfg.strategy7_momentum_threshold,
                    signal_delta=signal_delta,
                    signal_locked=True,
                )
        return SideDecision(
            side=state.signal_round_locked_side,
            signal_open_up_price=state.signal_round_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_delta=signal_delta,
            signal_locked=True,
        )

    now = now or datetime.now(timezone.utc)
    signal_open_up_price = resolve_signal_round_open_up_price(
        cfg=cfg,
        state=state,
        market_client=market_client,
        window=window,
        current_up_price=signal_current_up_price,
        now=now,
    )
    state.signal_round_open_up_price = signal_open_up_price
    signal_threshold = compute_signal_threshold(
        cfg=cfg,
        market_client=market_client,
        window=window,
        now=now,
    )

    if cfg.strategy_id in {7, 8}:
        strategy_prefix = "strategy7" if cfg.strategy_id == 7 else "strategy8"
        ofi_score = resolve_strategy6_ofi_score(quote)
        state.strategy6_last_ofi_score = ofi_score
        if ofi_score is None:
            return SideDecision(
                side=None,
                reason=f"{strategy_prefix}_ofi_unavailable",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
        if is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
            return SideDecision(
                side=None,
                reason=f"{strategy_prefix}_ofi_stale",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            )
        if abs(ofi_score) < cfg.strategy7_ofi_threshold:
            return SideDecision(
                side=None,
                reason="strategy7_ofi_too_weak" if cfg.strategy_id == 7 else "strategy8_market_state_weak",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            )
        if not (is_valid_signal_price(signal_open_up_price) and is_valid_signal_price(signal_current_up_price)):
            return SideDecision(
                side=None,
                reason=f"{strategy_prefix}_momentum_unavailable",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
            )

        momentum_delta = signal_current_up_price - signal_open_up_price
        if abs(momentum_delta) < cfg.strategy7_momentum_threshold:
            return SideDecision(
                side=None,
                reason="strategy7_momentum_too_weak" if cfg.strategy_id == 7 else "strategy8_market_state_weak",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        signal_gap_ok = strategy7_signal_gap_ok(
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
            ofi_threshold=cfg.strategy7_ofi_threshold,
            momentum_threshold=cfg.strategy7_momentum_threshold,
            signal_min_gap=cfg.strategy7_min_signal_gap,
        )
        if cfg.strategy_id == 8 and not signal_gap_ok:
            return SideDecision(
                side=None,
                reason="strategy8_market_state_weak",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        effective_confirm_before_entry_seconds = effective_strategy7_confirm_before_entry_seconds(
            cfg=cfg,
            window=window,
            entry_time=entry_time,
        )
        if strategy7_strong_signal_allows_late_confirm(
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
            ofi_threshold=cfg.strategy7_ofi_threshold,
            momentum_threshold=cfg.strategy7_momentum_threshold,
            signal_min_gap=cfg.strategy7_min_signal_gap,
            strong_signal_gap=cfg.strategy7_late_confirm_strong_signal_gap,
        ):
            effective_confirm_before_entry_seconds = max(
                0.0,
                effective_confirm_before_entry_seconds - max(0.0, float(cfg.strategy7_late_confirm_relax_seconds)),
            )
        if (
            entry_time is not None
            and effective_confirm_before_entry_seconds > 0
            and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
        ):
            return SideDecision(
                side=None,
                reason=f"{strategy_prefix}_entry_too_late",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )

        if cfg.strategy_id == 7:
            resolved_side = strategy7_weighted_side_for_signals(
                ofi_score=ofi_score,
                momentum_delta=momentum_delta,
                ofi_threshold=cfg.strategy7_ofi_threshold,
                momentum_threshold=cfg.strategy7_momentum_threshold,
            )
            decision_reason = "strategy7_weighted_conflict" if ofi_score * momentum_delta <= 0 else None
        elif cfg.strategy_id == 8 and ofi_score * momentum_delta <= 0:
            resolved_side = "UP" if ofi_score > 0 else "DOWN"
            decision_reason = "strategy8_conflict_reversal"
        else:
            resolved_side = "UP" if momentum_delta > 0 else "DOWN"
            decision_reason = None
        candidate_price = resolve_quote_price(resolved_side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix=strategy_prefix,
            price=candidate_price,
            min_entry_price=getattr(cfg, "min_entry_price", None),
            max_entry_price=getattr(cfg, "max_entry_price", None),
        )
        if price_skip_reason is not None:
            return SideDecision(
                side=None,
                reason=price_skip_reason,
                candidate_side=resolved_side,
                candidate_price=candidate_price,
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        if cfg.strategy_id == 7 and not signal_gap_ok:
            return SideDecision(
                side=None,
                reason="strategy7_confidence_too_low",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            reason=decision_reason,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=cfg.strategy7_momentum_threshold,
            signal_delta=momentum_delta,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    weak_mode = cfg.signal_weak_signal_mode.upper()
    if weak_mode == "FALLBACK":
        weak_mode = "SKIP"

    if is_valid_signal_price(signal_open_up_price) and is_valid_signal_price(signal_current_up_price):
        signal_delta = signal_current_up_price - signal_open_up_price
        resolved_side: str | None = None
        reason: str | None = None

        if signal_delta >= signal_threshold:
            resolved_side = "UP"
        elif signal_delta <= -signal_threshold:
            resolved_side = "DOWN"
        elif weak_mode == "FALLBACK":
            resolved_side = fallback_side_resolver(fallback_strategy, state.round_index)
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
        resolved_side = fallback_side_resolver(fallback_strategy, state.round_index)
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
