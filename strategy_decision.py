from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import erf, isfinite, sqrt
from statistics import pstdev
from typing import Any

from binance_signal import BinanceDepth5SignalService
from clob_adapter import effective_price_after_fee
from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState, Strategy9SignalSample
from strategy import (
    get_side_for_round,
    strategy7_signal_gap_ok,
    strategy7_signals_agree,
    strategy7_strong_signal_allows_late_confirm,
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
    signal_probability: float | None = None
    signal_edge: float | None = None
    signal_locked: bool = False
    max_entry_price: float | None = None
    ofi_score: float | None = None
    order_cost_multiplier: float = 1.0


@dataclass(slots=True)
class Strategy7SignalCheck:
    decision: SideDecision
    ofi_score: float | None = None
    momentum_delta: float | None = None


@dataclass(slots=True)
class Strategy9QualityCheck:
    reason: str | None
    max_entry_price: float | None


@dataclass(slots=True)
class Strategy10FairValue:
    up_fair_value: float
    down_fair_value: float
    up_edge: float | None
    down_edge: float | None
    best_side: str | None
    best_price: float | None
    best_edge: float | None


@dataclass(slots=True)
class Strategy11Probability:
    up_probability: float
    down_probability: float
    up_edge: float | None
    down_edge: float | None
    best_side: str | None
    best_price: float | None
    best_edge: float | None
    diagnostic_best_side: str | None = None
    diagnostic_best_price: float | None = None
    diagnostic_best_edge: float | None = None


@dataclass(slots=True)
class Strategy13ProbabilityEdge:
    up_probability: float
    down_probability: float
    raw_up_probability: float
    raw_down_probability: float
    volatility_bps: float
    up_effective_price: float | None
    down_effective_price: float | None
    up_edge: float | None
    down_edge: float | None
    best_side: str | None
    best_price: float | None
    best_effective_price: float | None
    best_edge: float | None


def resolve_quote_price(side: str, quote: MarketQuote) -> float | None:
    if side == "UP":
        return quote.up_best_ask if quote.up_best_ask is not None else quote.up_price
    if side == "DOWN":
        return quote.down_best_ask if quote.down_best_ask is not None else quote.down_price
    raise ValueError(f"Unsupported side: {side}")


def strategy11_best_probability(probability: Strategy11Probability) -> float | None:
    if probability.best_side == "UP":
        return probability.up_probability
    if probability.best_side == "DOWN":
        return probability.down_probability
    return None


def strategy10_best_probability(fair_value: Strategy10FairValue) -> float | None:
    if fair_value.best_side == "UP":
        return fair_value.up_fair_value
    if fair_value.best_side == "DOWN":
        return fair_value.down_fair_value
    return None


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


def effective_decision_max_entry_price(cfg: AppConfig, decision: SideDecision) -> float | None:
    return decision.max_entry_price if decision.max_entry_price is not None else getattr(cfg, "max_entry_price", None)


def strategy7_order_cost_multiplier(
    *,
    cfg: AppConfig,
    decision: SideDecision,
    price: float | None,
    ofi_score: float | None = None,
) -> float:
    if getattr(cfg, "strategy_id", None) != 7:
        return 1.0
    decision_multiplier = max(0.0, float(decision.order_cost_multiplier or 0.0))
    if not getattr(cfg, "strategy7_dynamic_sizing_enabled", False):
        return decision_multiplier
    dynamic_multiplier = _dynamic_order_cost_multiplier(
        cfg=cfg,
        decision=decision,
        price=price,
        ofi_score=ofi_score,
        prefix="strategy7",
    )
    if decision_multiplier < 1.0:
        return min(decision_multiplier, dynamic_multiplier)
    return dynamic_multiplier


def _dynamic_order_cost_multiplier(
    *,
    cfg: AppConfig,
    decision: SideDecision,
    price: float | None,
    ofi_score: float | None,
    prefix: str,
) -> float:
    if price is None or price <= 0:
        return 1.0

    reference_price = float(getattr(cfg, f"{prefix}_sizing_reference_price", 0.50))
    price_step = float(getattr(cfg, f"{prefix}_sizing_price_step", 0.01))
    step_reduction = max(0.0, float(getattr(cfg, f"{prefix}_sizing_price_step_reduction", 0.0)))
    min_multiplier = max(0.0, float(getattr(cfg, f"{prefix}_sizing_min_multiplier", 1.0)))
    max_multiplier = max(min_multiplier, float(getattr(cfg, f"{prefix}_sizing_max_multiplier", 1.0)))
    strong_gap = max(0.0, float(getattr(cfg, f"{prefix}_sizing_strong_signal_gap", 0.0)))
    strong_boost = max(0.0, float(getattr(cfg, f"{prefix}_sizing_strong_signal_boost", 0.0)))

    multiplier = 1.0
    if price_step > 0 and price > reference_price:
        multiplier -= ((price - reference_price) / price_step) * step_reduction

    signal_delta = decision.signal_delta
    effective_ofi_score = ofi_score if ofi_score is not None else decision.ofi_score
    if signal_delta is not None and effective_ofi_score is not None:
        ofi_gap = max(0.0, abs(effective_ofi_score) - float(getattr(cfg, "strategy7_ofi_threshold", 0.0)))
        momentum_gap = max(0.0, abs(signal_delta) - float(getattr(cfg, "strategy7_momentum_threshold", 0.0)))
        signal_gap = min(ofi_gap, momentum_gap)
        if strong_gap <= 0 or signal_gap >= strong_gap:
            multiplier += strong_boost

    return max(min_multiplier, min(max_multiplier, multiplier))


def effective_decision_order_cost_multiplier(
    *,
    cfg: AppConfig,
    decision: SideDecision,
    price: float | None,
) -> float:
    if getattr(cfg, "strategy_id", None) == 9 and getattr(cfg, "strategy9_dynamic_sizing_enabled", False):
        return _dynamic_order_cost_multiplier(
            cfg=cfg,
            decision=decision,
            price=price,
            ofi_score=decision.ofi_score,
            prefix="strategy9",
        )
    return strategy7_order_cost_multiplier(
        cfg=cfg,
        decision=decision,
        price=price,
        ofi_score=decision.ofi_score,
    )


def resolve_signal_up_price(quote: MarketQuote) -> float | None:
    # For signal direction, prefer traded/last price to reduce orderbook ask spikes noise.
    return quote.up_price if quote.up_price is not None else quote.up_best_ask


def is_valid_signal_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def _clamp_probability(value: float, *, low: float = 0.01, high: float = 0.99) -> float:
    return max(low, min(high, value))


def _normalized_quote_probability(quote: MarketQuote) -> float | None:
    up_price = quote.up_price if is_valid_signal_price(quote.up_price) else quote.up_best_ask
    down_price = quote.down_price if is_valid_signal_price(quote.down_price) else quote.down_best_ask
    if not (is_valid_signal_price(up_price) and is_valid_signal_price(down_price)):
        return up_price if is_valid_signal_price(up_price) else None
    total = float(up_price) + float(down_price)
    if total <= 0:
        return None
    return float(up_price) / total


def estimate_strategy10_fair_value(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    ofi_score: float,
    momentum_delta: float,
) -> Strategy10FairValue:
    base_up = _normalized_quote_probability(quote)
    if base_up is None:
        base_up = resolve_signal_up_price(quote)
    if not is_valid_signal_price(base_up):
        base_up = 0.5

    fair_up = (
        float(base_up)
        + float(getattr(cfg, "strategy10_ofi_weight", 0.0)) * max(-1.0, min(1.0, ofi_score))
        + float(getattr(cfg, "strategy10_momentum_weight", 0.0)) * momentum_delta
    )
    max_fair = max(0.5, min(0.99, float(getattr(cfg, "strategy10_max_fair_value", 0.85))))
    fair_up = _clamp_probability(fair_up, low=1.0 - max_fair, high=max_fair)
    fair_down = 1.0 - fair_up

    edge_buffer = max(0.0, float(getattr(cfg, "strategy10_edge_buffer", 0.0)))
    up_price = resolve_quote_price("UP", quote)
    down_price = resolve_quote_price("DOWN", quote)
    up_effective_price = effective_price_after_fee(up_price) if is_valid_signal_price(up_price) else None
    down_effective_price = effective_price_after_fee(down_price) if is_valid_signal_price(down_price) else None
    up_edge = fair_up - up_effective_price - edge_buffer if up_effective_price is not None else None
    down_edge = fair_down - down_effective_price - edge_buffer if down_effective_price is not None else None

    best_side: str | None = None
    best_price: float | None = None
    best_edge: float | None = None
    for side, price, edge in (("UP", up_price, up_edge), ("DOWN", down_price, down_edge)):
        if edge is None:
            continue
        if best_edge is None or edge > best_edge:
            best_side = side
            best_price = price
            best_edge = edge

    return Strategy10FairValue(
        up_fair_value=fair_up,
        down_fair_value=fair_down,
        up_edge=up_edge,
        down_edge=down_edge,
        best_side=best_side,
        best_price=best_price,
        best_edge=best_edge,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def estimate_strategy11_probability(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    window: MarketWindow,
    now: datetime,
    round_start_btc_price: float,
) -> Strategy11Probability | None:
    current_btc_price = getattr(quote, "binance_mid_price", None)
    if current_btc_price is None or current_btc_price <= 0 or round_start_btc_price <= 0:
        return None

    remaining_seconds = max(1.0, (window.end_time - now).total_seconds())
    remaining_minutes = max(remaining_seconds / 60.0, 1.0 / 60.0)
    volatility_bps = max(0.1, float(getattr(cfg, "strategy11_volatility_bps_per_sqrt_minute", 18.0)))
    sigma_price = round_start_btc_price * (volatility_bps / 10_000.0) * sqrt(remaining_minutes)
    if sigma_price <= 0:
        return None

    distance = float(current_btc_price) - float(round_start_btc_price)
    raw_up_probability = _normal_cdf(distance / sigma_price)
    min_probability = max(0.5, min(0.99, float(getattr(cfg, "strategy11_min_probability", 0.55))))
    max_probability = max(min_probability, min(0.99, float(getattr(cfg, "strategy11_max_probability", 0.95))))
    up_probability = _clamp_probability(raw_up_probability, low=1.0 - max_probability, high=max_probability)
    down_probability = 1.0 - up_probability

    edge_buffer = max(0.0, float(getattr(cfg, "strategy11_edge_buffer", 0.0)))
    up_price = resolve_quote_price("UP", quote)
    down_price = resolve_quote_price("DOWN", quote)
    up_effective_price = effective_price_after_fee(up_price) if is_valid_signal_price(up_price) else None
    down_effective_price = effective_price_after_fee(down_price) if is_valid_signal_price(down_price) else None
    up_edge = up_probability - up_effective_price - edge_buffer if up_effective_price is not None else None
    down_edge = down_probability - down_effective_price - edge_buffer if down_effective_price is not None else None

    best_side: str | None = None
    best_price: float | None = None
    best_edge: float | None = None
    diagnostic_best_side: str | None = None
    diagnostic_best_price: float | None = None
    diagnostic_best_edge: float | None = None
    for side, price, edge in (("UP", up_price, up_edge), ("DOWN", down_price, down_edge)):
        if edge is None:
            continue
        if diagnostic_best_edge is None or edge > diagnostic_best_edge:
            diagnostic_best_side = side
            diagnostic_best_price = price
            diagnostic_best_edge = edge
        probability = up_probability if side == "UP" else down_probability
        if probability < min_probability:
            continue
        if best_edge is None or edge > best_edge:
            best_side = side
            best_price = price
            best_edge = edge

    return Strategy11Probability(
        up_probability=up_probability,
        down_probability=down_probability,
        up_edge=up_edge,
        down_edge=down_edge,
        best_side=best_side,
        best_price=best_price,
        best_edge=best_edge,
        diagnostic_best_side=diagnostic_best_side,
        diagnostic_best_price=diagnostic_best_price,
        diagnostic_best_edge=diagnostic_best_edge,
    )


def _strategy13_clamped_volatility_bps(cfg: AppConfig) -> float:
    min_bps = _strategy13_finite_float(getattr(cfg, "strategy13_vol_min_bps", 8.0), 8.0)
    min_bps = max(0.1, min_bps)
    max_bps = _strategy13_finite_float(getattr(cfg, "strategy13_vol_max_bps", 45.0), 45.0)
    max_bps = max(min_bps, max_bps)
    configured = _strategy13_finite_float(getattr(cfg, "strategy11_volatility_bps_per_sqrt_minute", min_bps), min_bps)
    return max(min_bps, min(max_bps, configured))


def _strategy13_finite_float(value: object, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if isfinite(numeric) else default


def _strategy13_shrink_probability(probability: float, shrink: float) -> float:
    bounded = _clamp_probability(probability, low=0.0, high=1.0)
    shrink_value = max(0.0, min(1.0, _strategy13_finite_float(shrink, 0.25)))
    return _clamp_probability(0.5 + (bounded - 0.5) * (1.0 - shrink_value), low=0.0, high=1.0)


def estimate_strategy13_probability_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    window: MarketWindow,
    now: datetime,
    round_start_btc_price: float,
) -> Strategy13ProbabilityEdge | None:
    current_btc_price = getattr(quote, "binance_mid_price", None)
    try:
        current_btc_price = float(current_btc_price)
        round_start_btc_price = float(round_start_btc_price)
    except (TypeError, ValueError):
        return None
    if (
        not isfinite(current_btc_price)
        or not isfinite(round_start_btc_price)
        or current_btc_price <= 0
        or round_start_btc_price <= 0
    ):
        return None

    remaining_seconds = max(1.0, (window.end_time - now).total_seconds())
    remaining_minutes = max(remaining_seconds / 60.0, 1.0 / 60.0)
    volatility_bps = _strategy13_clamped_volatility_bps(cfg)
    sigma_price = round_start_btc_price * (volatility_bps / 10_000.0) * sqrt(remaining_minutes)
    if not isfinite(sigma_price) or sigma_price <= 0:
        return None

    distance = current_btc_price - round_start_btc_price
    raw_up_probability = _normal_cdf(distance / sigma_price)
    raw_down_probability = 1.0 - raw_up_probability
    shrink = _strategy13_finite_float(getattr(cfg, "strategy13_probability_shrink", 0.25), 0.25)
    up_probability = _strategy13_shrink_probability(raw_up_probability, shrink)
    down_probability = 1.0 - up_probability

    edge_buffer = max(0.0, _strategy13_finite_float(getattr(cfg, "strategy13_edge_buffer", 0.0), 0.0))
    up_price = resolve_quote_price("UP", quote)
    down_price = resolve_quote_price("DOWN", quote)
    up_effective_price = effective_price_after_fee(up_price) if is_valid_signal_price(up_price) else None
    down_effective_price = effective_price_after_fee(down_price) if is_valid_signal_price(down_price) else None
    up_edge = up_probability - up_effective_price - edge_buffer if up_effective_price is not None else None
    down_edge = down_probability - down_effective_price - edge_buffer if down_effective_price is not None else None

    best_side: str | None = None
    best_price: float | None = None
    best_effective_price: float | None = None
    best_edge: float | None = None
    for side, price, effective_price, edge in (
        ("UP", up_price, up_effective_price, up_edge),
        ("DOWN", down_price, down_effective_price, down_edge),
    ):
        if edge is None:
            continue
        if best_edge is None or edge > best_edge:
            best_side = side
            best_price = price
            best_effective_price = effective_price
            best_edge = edge

    return Strategy13ProbabilityEdge(
        up_probability=up_probability,
        down_probability=down_probability,
        raw_up_probability=raw_up_probability,
        raw_down_probability=raw_down_probability,
        volatility_bps=volatility_bps,
        up_effective_price=up_effective_price,
        down_effective_price=down_effective_price,
        up_edge=up_edge,
        down_edge=down_edge,
        best_side=best_side,
        best_price=best_price,
        best_effective_price=best_effective_price,
        best_edge=best_edge,
    )


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


def is_binance_price_signal_stale(*, quote: MarketQuote, now: datetime, stale_seconds: float) -> bool:
    signal_at = getattr(quote, "binance_signal_at", None) or getattr(quote, "strategy6_signal_at", None) or quote.fetched_at
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
    if cfg.strategy_id not in {6, 7, 8, 9, 10, 11, 12} or binance_signal_service is None:
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
    if latest.bid_price is not None and latest.ask_price is not None:
        quote.binance_mid_price = (float(latest.bid_price) + float(latest.ask_price)) / 2.0
        quote.binance_signal_at = latest.signal_at


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


def effective_strategy10_confirm_before_entry_seconds(
    *,
    cfg: AppConfig,
    window: MarketWindow | None,
    entry_time: datetime | None,
) -> float:
    configured = max(0.0, float(getattr(cfg, "strategy10_confirm_before_entry_seconds", 0)))
    if window is None or entry_time is None:
        return configured
    available = max(0.0, (entry_time - window.start_time).total_seconds())
    return min(configured, available)


def evaluate_strategy7_consensus_signal(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    signal_open_up_price: float | None,
    signal_current_up_price: float | None,
) -> Strategy7SignalCheck:
    ofi_score = resolve_strategy6_ofi_score(quote)
    if ofi_score is None:
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_ofi_unavailable",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
        )
    if is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_ofi_stale",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            ),
            ofi_score=ofi_score,
        )
    if abs(ofi_score) < cfg.strategy7_ofi_threshold:
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_ofi_too_weak",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            ),
            ofi_score=ofi_score,
        )
    if not (is_valid_signal_price(signal_open_up_price) and is_valid_signal_price(signal_current_up_price)):
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_momentum_unavailable",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
            ),
            ofi_score=ofi_score,
        )

    momentum_delta = signal_current_up_price - signal_open_up_price
    if abs(momentum_delta) < cfg.strategy7_momentum_threshold:
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_momentum_too_weak",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            ),
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
        )
    max_momentum_delta = getattr(cfg, "strategy7_max_momentum_delta", None)
    hot_momentum_multiplier: float | None = None
    if max_momentum_delta is not None and max_momentum_delta > 0 and abs(momentum_delta) > max_momentum_delta:
        if abs(momentum_delta) <= max_momentum_delta + 0.04:
            hot_momentum_multiplier = 0.5
        else:
            return Strategy7SignalCheck(
                decision=SideDecision(
                    side=None,
                    reason="strategy7_momentum_too_hot",
                    signal_open_up_price=signal_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=max_momentum_delta,
                    signal_delta=momentum_delta,
                ),
                ofi_score=ofi_score,
                momentum_delta=momentum_delta,
            )
    if hot_momentum_multiplier is None:
        signal_threshold = cfg.strategy7_momentum_threshold
    else:
        signal_threshold = max_momentum_delta
    if not strategy7_signals_agree(ofi_score=ofi_score, momentum_delta=momentum_delta):
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_signal_conflict",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=signal_threshold,
                signal_delta=momentum_delta,
            ),
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
        )
    if not strategy7_signal_gap_ok(
        ofi_score=ofi_score,
        momentum_delta=momentum_delta,
        ofi_threshold=cfg.strategy7_ofi_threshold,
        momentum_threshold=cfg.strategy7_momentum_threshold,
        signal_min_gap=cfg.strategy7_min_signal_gap,
    ):
        return Strategy7SignalCheck(
            decision=SideDecision(
                side=None,
                reason="strategy7_confidence_too_low",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=signal_threshold,
                signal_delta=momentum_delta,
            ),
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
        )
    return Strategy7SignalCheck(
        decision=SideDecision(
            side="UP" if momentum_delta > 0 else "DOWN",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=signal_threshold,
            signal_delta=momentum_delta,
            order_cost_multiplier=hot_momentum_multiplier or 1.0,
        ),
        ofi_score=ofi_score,
        momentum_delta=momentum_delta,
    )


def _strategy9_sample_observed_at(sample: Strategy9SignalSample) -> datetime | None:
    try:
        return datetime.fromisoformat(sample.observed_at)
    except (TypeError, ValueError):
        return None


def _strategy9_sample_side(sample: Strategy9SignalSample) -> str | None:
    if sample.ofi_score * sample.momentum_delta <= 0:
        return None
    return "UP" if sample.momentum_delta > 0 else "DOWN"


def append_strategy9_signal_sample(
    *,
    cfg: AppConfig,
    state: SessionState,
    now: datetime,
    ofi_score: float,
    momentum_delta: float,
    signal_current_up_price: float,
) -> list[Strategy9SignalSample]:
    cutoff = now - timedelta(
        seconds=max(
            float(getattr(cfg, "strategy9_stability_window_seconds", 0.0)),
            float(getattr(cfg, "strategy9_reversal_lookback_seconds", 0.0)),
        )
    )
    samples: list[Strategy9SignalSample] = []
    for sample in state.strategy9_signal_samples:
        observed_at = _strategy9_sample_observed_at(sample)
        if observed_at is not None and observed_at >= cutoff:
            samples.append(sample)
    samples.append(
        Strategy9SignalSample(
            observed_at=now.isoformat(),
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
            current_up_price=signal_current_up_price,
        )
    )
    state.strategy9_signal_samples = samples
    return samples


def strategy9_dynamic_max_entry_price(
    *,
    cfg: AppConfig,
    ofi_score: float,
    momentum_delta: float,
) -> float:
    ofi_strength = abs(ofi_score)
    momentum_strength = abs(momentum_delta)
    ultra_gap = max(0.0, float(getattr(cfg, "strategy9_ultra_signal_gap", 0.0)))
    strong_gap = max(0.0, float(getattr(cfg, "strategy9_strong_signal_gap", 0.0)))
    if (
        ofi_strength >= cfg.strategy7_ofi_threshold + ultra_gap
        and momentum_strength >= cfg.strategy7_momentum_threshold + ultra_gap
    ):
        return float(getattr(cfg, "strategy9_ultra_max_entry_price", getattr(cfg, "max_entry_price", 0.54)))
    if (
        ofi_strength >= cfg.strategy7_ofi_threshold + strong_gap
        and momentum_strength >= cfg.strategy7_momentum_threshold + strong_gap
    ):
        return float(getattr(cfg, "strategy9_strong_max_entry_price", getattr(cfg, "max_entry_price", 0.53)))
    return float(getattr(cfg, "strategy9_base_max_entry_price", getattr(cfg, "max_entry_price", 0.52)))


def evaluate_strategy9_quality(
    *,
    cfg: AppConfig,
    state: SessionState,
    now: datetime,
    side: str,
    ofi_score: float,
    momentum_delta: float,
    signal_current_up_price: float,
) -> Strategy9QualityCheck:
    samples = append_strategy9_signal_sample(
        cfg=cfg,
        state=state,
        now=now,
        ofi_score=ofi_score,
        momentum_delta=momentum_delta,
        signal_current_up_price=signal_current_up_price,
    )
    stability_cutoff = now - timedelta(seconds=max(0.0, float(getattr(cfg, "strategy9_stability_window_seconds", 0.0))))
    stability_samples = [
        sample
        for sample in samples
        if (_strategy9_sample_observed_at(sample) or datetime.min.replace(tzinfo=timezone.utc)) >= stability_cutoff
    ]
    required_sample_count = max(1, int(getattr(cfg, "strategy9_stability_sample_count", 1)))
    required_agree_count = max(1, int(getattr(cfg, "strategy9_stability_required_count", 1)))
    agreeing_samples = [
        sample
        for sample in stability_samples
        if _strategy9_sample_side(sample) == side
        and abs(sample.ofi_score) >= cfg.strategy7_ofi_threshold
        and abs(sample.momentum_delta) >= cfg.strategy7_momentum_threshold
    ]
    if len(stability_samples) < required_sample_count or len(agreeing_samples) < required_agree_count:
        return Strategy9QualityCheck(reason="strategy9_signal_unstable", max_entry_price=None)

    lookback_cutoff = now - timedelta(seconds=max(0.0, float(getattr(cfg, "strategy9_reversal_lookback_seconds", 0.0))))
    lookback_samples = [
        sample
        for sample in samples
        if (_strategy9_sample_observed_at(sample) or datetime.min.replace(tzinfo=timezone.utc)) >= lookback_cutoff
        and _strategy9_sample_side(sample) == side
    ]
    current_strength = min(abs(ofi_score), abs(momentum_delta))
    peak_strength = max((min(abs(sample.ofi_score), abs(sample.momentum_delta)) for sample in lookback_samples), default=current_strength)
    max_decay = max(0.0, float(getattr(cfg, "strategy9_max_signal_decay", 0.0)))
    if peak_strength > 0 and current_strength < peak_strength * (1.0 - max_decay):
        return Strategy9QualityCheck(reason="strategy9_signal_decaying", max_entry_price=None)

    return Strategy9QualityCheck(
        reason=None,
        max_entry_price=strategy9_dynamic_max_entry_price(
            cfg=cfg,
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
        ),
    )


def evaluate_strategy10_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    signal_open_up_price: float | None,
    signal_current_up_price: float | None,
) -> SideDecision:
    ofi_score = resolve_strategy6_ofi_score(quote)
    if ofi_score is None:
        return SideDecision(
            side=None,
            reason="strategy10_ofi_unavailable",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
        )
    if is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return SideDecision(
            side=None,
            reason="strategy10_ofi_stale",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=cfg.binance_signal_stale_seconds,
            signal_delta=ofi_score,
            ofi_score=ofi_score,
        )
    if not (is_valid_signal_price(signal_open_up_price) and is_valid_signal_price(signal_current_up_price)):
        return SideDecision(
            side=None,
            reason="strategy10_momentum_unavailable",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=cfg.strategy10_min_edge,
            ofi_score=ofi_score,
        )

    momentum_delta = signal_current_up_price - signal_open_up_price
    min_momentum_delta = getattr(cfg, "strategy10_min_momentum_delta", None)
    if min_momentum_delta is not None and momentum_delta < min_momentum_delta:
        return SideDecision(
            side=None,
            reason="strategy10_momentum_too_cold",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=min_momentum_delta,
            signal_delta=momentum_delta,
            ofi_score=ofi_score,
        )
    max_momentum_delta = getattr(cfg, "strategy10_max_momentum_delta", None)
    if max_momentum_delta is not None and momentum_delta > max_momentum_delta:
        return SideDecision(
            side=None,
            reason="strategy10_momentum_too_hot",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=max_momentum_delta,
            signal_delta=momentum_delta,
            ofi_score=ofi_score,
        )

    fair_value = estimate_strategy10_fair_value(
        cfg=cfg,
        quote=quote,
        ofi_score=ofi_score,
        momentum_delta=momentum_delta,
    )
    best_probability = strategy10_best_probability(fair_value)
    min_edge = max(0.0, float(getattr(cfg, "strategy10_min_edge", 0.0)))
    side_min_edge = min_edge
    if fair_value.best_side == "DOWN":
        down_min_edge = getattr(cfg, "strategy10_down_min_edge", None)
        if down_min_edge is not None:
            side_min_edge = max(0.0, float(down_min_edge))
    if fair_value.best_side is None or fair_value.best_edge is None or fair_value.best_edge < side_min_edge:
        return SideDecision(
            side=None,
            reason="strategy10_edge_too_low",
            candidate_side=fair_value.best_side,
            candidate_price=fair_value.best_price,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=side_min_edge,
            signal_delta=momentum_delta,
            signal_probability=best_probability,
            signal_edge=fair_value.best_edge,
            ofi_score=ofi_score,
        )

    return SideDecision(
        side=fair_value.best_side,
        candidate_side=fair_value.best_side,
        candidate_price=fair_value.best_price,
        signal_open_up_price=signal_open_up_price,
        signal_current_up_price=signal_current_up_price,
        signal_threshold=side_min_edge,
        signal_delta=momentum_delta,
        signal_probability=best_probability,
        signal_edge=fair_value.best_edge,
        ofi_score=ofi_score,
    )


def evaluate_strategy11_probability_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow | None,
    state: SessionState,
) -> SideDecision:
    if window is None:
        return SideDecision(side=None, reason="strategy11_window_unavailable")
    if is_binance_price_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return SideDecision(
            side=None,
            reason="strategy11_btc_price_stale",
            signal_threshold=cfg.binance_signal_stale_seconds,
        )
    current_btc_price = getattr(quote, "binance_mid_price", None)
    if current_btc_price is None or current_btc_price <= 0:
        return SideDecision(side=None, reason="strategy11_btc_price_unavailable")
    if state.signal_round_slug != window.slug or state.strategy11_round_start_btc_price is None:
        price_to_beat = getattr(window, "price_to_beat", None)
        if price_to_beat is not None and price_to_beat > 0:
            state.strategy11_round_start_btc_price = float(price_to_beat)
        else:
            state.strategy11_round_start_btc_price = float(current_btc_price)

    probability = estimate_strategy11_probability(
        cfg=cfg,
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=state.strategy11_round_start_btc_price,
    )
    if probability is None:
        return SideDecision(side=None, reason="strategy11_probability_unavailable")

    min_edge = max(0.0, float(getattr(cfg, "strategy11_min_edge", 0.0)))
    distance = float(current_btc_price) - float(state.strategy11_round_start_btc_price)
    best_probability = strategy11_best_probability(probability)
    diagnostic_side = probability.best_side or probability.diagnostic_best_side
    diagnostic_price = probability.best_price
    diagnostic_edge = probability.best_edge
    if probability.best_side is None:
        diagnostic_price = probability.diagnostic_best_price
        diagnostic_edge = probability.diagnostic_best_edge
        if diagnostic_side == "UP":
            best_probability = probability.up_probability
        elif diagnostic_side == "DOWN":
            best_probability = probability.down_probability
    reason = (
        "strategy11_probability_too_low"
        if probability.best_side is None
        else "strategy11_edge_too_low"
    )
    if probability.best_side is None or probability.best_edge is None or probability.best_edge < min_edge:
        return SideDecision(
            side=None,
            reason=reason,
            candidate_side=diagnostic_side,
            candidate_price=diagnostic_price,
            signal_open_up_price=state.strategy11_round_start_btc_price,
            signal_current_up_price=float(current_btc_price),
            signal_threshold=min_edge,
            signal_delta=distance,
            signal_probability=best_probability,
            signal_edge=diagnostic_edge,
        )

    return SideDecision(
        side=probability.best_side,
        candidate_side=probability.best_side,
        candidate_price=probability.best_price,
        signal_open_up_price=state.strategy11_round_start_btc_price,
        signal_current_up_price=float(current_btc_price),
        signal_threshold=min_edge,
        signal_delta=distance,
        signal_probability=best_probability,
        signal_edge=probability.best_edge,
    )


def _strategy12_probability_skip(decision: SideDecision) -> SideDecision:
    reason = decision.reason
    if reason and reason.startswith("strategy11_"):
        reason = reason.replace("strategy11_", "strategy12_", 1)
    return SideDecision(
        side=None,
        reason=reason,
        candidate_side=decision.candidate_side,
        candidate_price=decision.candidate_price,
        signal_open_up_price=decision.signal_open_up_price,
        signal_current_up_price=decision.signal_current_up_price,
        signal_threshold=decision.signal_threshold,
        signal_delta=decision.signal_delta,
        signal_probability=decision.signal_probability,
        signal_edge=decision.signal_edge,
        signal_locked=decision.signal_locked,
    )


def evaluate_strategy12_hybrid_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow | None,
    state: SessionState,
    signal_open_up_price: float | None,
    signal_current_up_price: float | None,
) -> SideDecision:
    probability_decision = evaluate_strategy11_probability_edge(
        cfg=cfg,
        quote=quote,
        now=now,
        window=window,
        state=state,
    )
    if probability_decision.side is None:
        return _strategy12_probability_skip(probability_decision)

    micro_check = evaluate_strategy7_consensus_signal(
        cfg=cfg,
        quote=quote,
        now=now,
        signal_open_up_price=signal_open_up_price,
        signal_current_up_price=signal_current_up_price,
    )
    if micro_check.decision.side is None:
        micro_reason = micro_check.decision.reason
        if micro_reason == "strategy7_signal_conflict":
            reason = "strategy12_signal_conflict"
        elif micro_reason:
            reason = micro_reason.replace("strategy7_", "strategy12_micro_", 1)
        else:
            reason = "strategy12_micro_unavailable"
        return SideDecision(
            side=None,
            reason=reason,
            candidate_side=probability_decision.side,
            candidate_price=probability_decision.candidate_price,
            signal_open_up_price=probability_decision.signal_open_up_price,
            signal_current_up_price=probability_decision.signal_current_up_price,
            signal_threshold=probability_decision.signal_threshold,
            signal_delta=probability_decision.signal_delta,
            signal_probability=probability_decision.signal_probability,
            signal_edge=probability_decision.signal_edge,
            ofi_score=micro_check.ofi_score,
        )
    if micro_check.decision.side != probability_decision.side:
        return SideDecision(
            side=None,
            reason="strategy12_signal_conflict",
            candidate_side=probability_decision.side,
            candidate_price=probability_decision.candidate_price,
            signal_open_up_price=probability_decision.signal_open_up_price,
            signal_current_up_price=probability_decision.signal_current_up_price,
            signal_threshold=probability_decision.signal_threshold,
            signal_delta=probability_decision.signal_delta,
            signal_probability=probability_decision.signal_probability,
            signal_edge=probability_decision.signal_edge,
            ofi_score=micro_check.ofi_score,
        )

    if (
        micro_check.ofi_score is None
        or micro_check.momentum_delta is None
        or not is_valid_signal_price(signal_current_up_price)
    ):
        return SideDecision(
            side=None,
            reason="strategy12_micro_unavailable",
            candidate_side=probability_decision.side,
            candidate_price=probability_decision.candidate_price,
            signal_open_up_price=probability_decision.signal_open_up_price,
            signal_current_up_price=probability_decision.signal_current_up_price,
            signal_threshold=probability_decision.signal_threshold,
            signal_delta=probability_decision.signal_delta,
            signal_probability=probability_decision.signal_probability,
            signal_edge=probability_decision.signal_edge,
            ofi_score=micro_check.ofi_score,
        )
    quality_check = evaluate_strategy9_quality(
        cfg=cfg,
        state=state,
        now=now,
        side=probability_decision.side,
        ofi_score=micro_check.ofi_score,
        momentum_delta=micro_check.momentum_delta,
        signal_current_up_price=signal_current_up_price,
    )
    if quality_check.reason is not None:
        reason = quality_check.reason.replace("strategy9_signal_", "strategy12_micro_", 1)
        return SideDecision(
            side=None,
            reason=reason,
            candidate_side=probability_decision.side,
            candidate_price=probability_decision.candidate_price,
            signal_open_up_price=probability_decision.signal_open_up_price,
            signal_current_up_price=probability_decision.signal_current_up_price,
            signal_threshold=probability_decision.signal_threshold,
            signal_delta=probability_decision.signal_delta,
            signal_probability=probability_decision.signal_probability,
            signal_edge=probability_decision.signal_edge,
            ofi_score=micro_check.ofi_score,
        )

    return SideDecision(
        side=probability_decision.side,
        candidate_side=probability_decision.side,
        candidate_price=probability_decision.candidate_price,
        signal_open_up_price=probability_decision.signal_open_up_price,
        signal_current_up_price=probability_decision.signal_current_up_price,
        signal_threshold=probability_decision.signal_threshold,
        signal_delta=probability_decision.signal_delta,
        signal_probability=probability_decision.signal_probability,
        signal_edge=probability_decision.signal_edge,
        ofi_score=micro_check.ofi_score,
    )


def _strategy13_best_probability(edge: Strategy13ProbabilityEdge) -> float | None:
    if edge.best_side == "UP":
        return edge.up_probability
    if edge.best_side == "DOWN":
        return edge.down_probability
    return None


def _strategy13_side_probability(edge: Strategy13ProbabilityEdge, side: str | None) -> float | None:
    if side == "UP":
        return edge.up_probability
    if side == "DOWN":
        return edge.down_probability
    return None


def _strategy13_side_edge(edge: Strategy13ProbabilityEdge, side: str | None) -> float | None:
    if side == "UP":
        return edge.up_edge
    if side == "DOWN":
        return edge.down_edge
    return None


def _strategy13_resolve_finite_price(value: object) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price > 0 and isfinite(price):
        return price
    return None


def _strategy13_decision_from_edge(
    *,
    cfg: AppConfig,
    state: SessionState,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow,
    edge: Strategy13ProbabilityEdge,
    reason: str | None,
    ofi_score: float | None = None,
) -> SideDecision:
    current_btc_price = getattr(quote, "binance_mid_price", None)
    best_probability = _strategy13_best_probability(edge)
    distance = (
        float(current_btc_price) - float(state.strategy11_round_start_btc_price)
        if current_btc_price is not None and state.strategy11_round_start_btc_price is not None
        else None
    )
    return SideDecision(
        side=None if reason is not None else edge.best_side,
        reason=reason,
        candidate_side=edge.best_side,
        candidate_price=edge.best_price,
        signal_open_up_price=state.strategy11_round_start_btc_price,
        signal_current_up_price=float(current_btc_price) if current_btc_price is not None else None,
        signal_threshold=max(0.0, float(getattr(cfg, "strategy13_min_edge", 0.0))),
        signal_delta=distance,
        signal_probability=best_probability,
        signal_edge=edge.best_edge,
        max_entry_price=getattr(cfg, "max_entry_price", None),
        ofi_score=ofi_score,
    )


def _strategy13_selected_edge(edge: Strategy13ProbabilityEdge, quote: MarketQuote, side: str) -> Strategy13ProbabilityEdge:
    return Strategy13ProbabilityEdge(
        up_probability=edge.up_probability,
        down_probability=edge.down_probability,
        raw_up_probability=edge.raw_up_probability,
        raw_down_probability=edge.raw_down_probability,
        volatility_bps=edge.volatility_bps,
        up_effective_price=edge.up_effective_price,
        down_effective_price=edge.down_effective_price,
        up_edge=edge.up_edge,
        down_edge=edge.down_edge,
        best_side=side,
        best_price=resolve_quote_price(side, quote),
        best_effective_price=edge.up_effective_price if side == "UP" else edge.down_effective_price,
        best_edge=_strategy13_side_edge(edge, side),
    )


def _strategy13_select_eligible_edge(
    *,
    edge: Strategy13ProbabilityEdge,
    quote: MarketQuote,
    min_probability: float,
    min_edge: float,
) -> tuple[Strategy13ProbabilityEdge, str | None]:
    probability_eligible: list[str] = []
    fully_eligible: list[str] = []
    for side in ("UP", "DOWN"):
        probability = _strategy13_side_probability(edge, side)
        side_edge = _strategy13_side_edge(edge, side)
        if probability is None or probability < min_probability:
            continue
        probability_eligible.append(side)
        if side_edge is not None and side_edge >= min_edge:
            fully_eligible.append(side)

    if fully_eligible:
        selected_side = max(
            fully_eligible,
            key=lambda side: _strategy13_side_edge(edge, side) if _strategy13_side_edge(edge, side) is not None else float("-inf"),
        )
        return _strategy13_selected_edge(edge, quote, selected_side), None
    if probability_eligible:
        return edge, "strategy13_edge_too_low"
    return edge, "strategy13_probability_too_low"


def evaluate_strategy13_probability_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow | None,
    state: SessionState,
    signal_open_up_price: float | None,
    signal_current_up_price: float | None,
) -> SideDecision:
    if window is None:
        return SideDecision(side=None, reason="strategy13_btc_anchor_unavailable")
    if is_binance_price_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return SideDecision(
            side=None,
            reason="strategy13_btc_price_stale",
            signal_threshold=cfg.binance_signal_stale_seconds,
        )
    current_btc_price = _strategy13_resolve_finite_price(getattr(quote, "binance_mid_price", None))
    if current_btc_price is None:
        state.strategy11_round_start_btc_price = _strategy13_resolve_finite_price(state.strategy11_round_start_btc_price)
        return SideDecision(side=None, reason="strategy13_btc_price_unavailable")
    state.strategy11_round_start_btc_price = _strategy13_resolve_finite_price(state.strategy11_round_start_btc_price)
    if state.signal_round_slug != window.slug or state.strategy11_round_start_btc_price is None:
        price_to_beat = _strategy13_resolve_finite_price(getattr(window, "price_to_beat", None))
        if price_to_beat is not None:
            state.strategy11_round_start_btc_price = price_to_beat
        else:
            state.strategy11_round_start_btc_price = current_btc_price

    edge = estimate_strategy13_probability_edge(
        cfg=cfg,
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=state.strategy11_round_start_btc_price,
    )
    if edge is None:
        return SideDecision(side=None, reason="strategy13_volatility_unavailable")

    best_probability = _strategy13_best_probability(edge)
    min_probability = max(0.5, min(0.99, float(getattr(cfg, "strategy13_min_probability", 0.58))))
    min_edge = max(0.0, float(getattr(cfg, "strategy13_min_edge", 0.0)))
    selected_edge, rejection_reason = _strategy13_select_eligible_edge(
        edge=edge,
        quote=quote,
        min_probability=min_probability,
        min_edge=min_edge,
    )
    if rejection_reason is not None:
        return _strategy13_decision_from_edge(
            cfg=cfg,
            state=state,
            quote=quote,
            now=now,
            window=window,
            edge=edge,
            reason=rejection_reason,
        )
    edge = selected_edge
    best_probability = _strategy13_best_probability(edge)

    ofi_score = resolve_strategy6_ofi_score(quote)
    if getattr(cfg, "strategy13_confirm_micro", True):
        micro_open_up_price = signal_open_up_price
        micro_current_up_price = signal_current_up_price
        if not (is_valid_signal_price(micro_open_up_price) and is_valid_signal_price(micro_current_up_price)):
            micro_open_up_price = state.strategy11_round_start_btc_price
            micro_current_up_price = current_btc_price
        elif micro_open_up_price == micro_current_up_price:
            btc_distance = float(current_btc_price) - float(state.strategy11_round_start_btc_price)
            btc_direction = 1.0 if btc_distance >= 0 else -1.0
            synthetic_delta = btc_direction * max(
                abs(btc_distance / float(state.strategy11_round_start_btc_price)),
                float(getattr(cfg, "strategy7_momentum_threshold", 0.0)),
            )
            micro_open_up_price = 0.5
            micro_current_up_price = 0.5 + synthetic_delta
        micro_check = evaluate_strategy7_consensus_signal(
            cfg=cfg,
            quote=quote,
            now=now,
            signal_open_up_price=micro_open_up_price,
            signal_current_up_price=micro_current_up_price,
        )
        ofi_score = micro_check.ofi_score
        if micro_check.decision.side is None:
            return _strategy13_decision_from_edge(
                cfg=cfg,
                state=state,
                quote=quote,
                now=now,
                window=window,
                edge=edge,
                reason="strategy13_micro_unavailable"
                if micro_check.decision.reason in {None, "strategy7_ofi_unavailable", "strategy7_momentum_unavailable"}
                else "strategy13_micro_conflict",
                ofi_score=ofi_score,
            )
        if micro_check.decision.side != edge.best_side:
            return _strategy13_decision_from_edge(
                cfg=cfg,
                state=state,
                quote=quote,
                now=now,
                window=window,
                edge=edge,
                reason="strategy13_micro_conflict",
                ofi_score=ofi_score,
            )

    return _strategy13_decision_from_edge(
        cfg=cfg,
        state=state,
        quote=quote,
        now=now,
        window=window,
        edge=edge,
        reason=None,
        ofi_score=ofi_score,
    )


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

    if cfg.strategy_id not in {5, 7, 8, 9, 10, 11, 12, 13}:
        return SideDecision(side=fallback_side_resolver(cfg.strategy_id, state.round_index))

    fallback_strategy = cfg.signal_fallback_strategy_id
    if state.signal_round_slug != slug:
        state.signal_round_slug = slug
        state.signal_round_open_up_price = None
        state.signal_round_locked_side = None
        state.strategy11_round_start_btc_price = None
        state.strategy9_signal_samples = []

    signal_current_up_price = resolve_signal_up_price(quote)
    if state.signal_round_locked_side in {"UP", "DOWN"}:
        signal_delta = None
        locked_max_entry_price: float | None = None
        if is_valid_signal_price(state.signal_round_open_up_price) and is_valid_signal_price(signal_current_up_price):
            signal_delta = signal_current_up_price - state.signal_round_open_up_price
        if cfg.strategy_id == 12:
            now = now or datetime.now(timezone.utc)
            edge_decision = evaluate_strategy12_hybrid_edge(
                cfg=cfg,
                quote=quote,
                now=now,
                window=window,
                state=state,
                signal_open_up_price=state.signal_round_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
            state.strategy6_last_ofi_score = edge_decision.ofi_score
            edge_decision.signal_locked = True
            if edge_decision.side is None:
                return edge_decision
            if edge_decision.side != state.signal_round_locked_side:
                return SideDecision(
                    side=None,
                    reason="strategy12_signal_conflict",
                    candidate_side=edge_decision.side,
                    candidate_price=edge_decision.candidate_price,
                    signal_open_up_price=edge_decision.signal_open_up_price,
                    signal_current_up_price=edge_decision.signal_current_up_price,
                    signal_threshold=cfg.strategy11_min_edge,
                    signal_delta=edge_decision.signal_delta,
                    signal_probability=edge_decision.signal_probability,
                    signal_edge=edge_decision.signal_edge,
                    signal_locked=True,
                    ofi_score=edge_decision.ofi_score,
                )
            candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
            price_skip_reason = entry_price_skip_reason(
                strategy_prefix="strategy12",
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
                    signal_open_up_price=edge_decision.signal_open_up_price,
                    signal_current_up_price=edge_decision.signal_current_up_price,
                    signal_threshold=cfg.strategy11_min_edge,
                    signal_delta=edge_decision.signal_delta,
                    signal_probability=edge_decision.signal_probability,
                    signal_edge=edge_decision.signal_edge,
                    signal_locked=True,
                    ofi_score=edge_decision.ofi_score,
                )
            return SideDecision(
                side=state.signal_round_locked_side,
                candidate_side=state.signal_round_locked_side,
                candidate_price=candidate_price,
                signal_open_up_price=edge_decision.signal_open_up_price,
                signal_current_up_price=edge_decision.signal_current_up_price,
                signal_threshold=cfg.strategy11_min_edge,
                signal_delta=edge_decision.signal_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
                signal_locked=True,
                ofi_score=edge_decision.ofi_score,
            )
        if cfg.strategy_id == 13:
            now = now or datetime.now(timezone.utc)
            edge_decision = evaluate_strategy13_probability_edge(
                cfg=cfg,
                quote=quote,
                now=now,
                window=window,
                state=state,
                signal_open_up_price=state.signal_round_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
            state.strategy6_last_ofi_score = edge_decision.ofi_score
            edge_decision.signal_locked = True
            if edge_decision.side is None:
                return edge_decision
            if edge_decision.side != state.signal_round_locked_side:
                edge_decision.side = None
                edge_decision.reason = "strategy13_signal_conflict"
                return edge_decision
            candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
            price_skip_reason = entry_price_skip_reason(
                strategy_prefix="strategy13",
                price=candidate_price,
                min_entry_price=getattr(cfg, "min_entry_price", None),
                max_entry_price=getattr(cfg, "max_entry_price", None),
            )
            if price_skip_reason is not None:
                edge_decision.side = None
                edge_decision.reason = price_skip_reason
                edge_decision.candidate_side = state.signal_round_locked_side
                edge_decision.candidate_price = candidate_price
                return edge_decision
            edge_decision.side = state.signal_round_locked_side
            edge_decision.candidate_side = state.signal_round_locked_side
            edge_decision.candidate_price = candidate_price
            return edge_decision
        if cfg.strategy_id in {7, 9, 10, 11}:
            now = now or datetime.now(timezone.utc)
            if cfg.strategy_id == 11:
                edge_decision = evaluate_strategy11_probability_edge(
                    cfg=cfg,
                    quote=quote,
                    now=now,
                    window=window,
                    state=state,
                )
                edge_decision.signal_locked = True
                if edge_decision.side is None:
                    return edge_decision
                if edge_decision.side != state.signal_round_locked_side:
                    return SideDecision(
                        side=None,
                        reason="strategy11_signal_conflict",
                        candidate_side=edge_decision.side,
                        candidate_price=edge_decision.candidate_price,
                        signal_open_up_price=edge_decision.signal_open_up_price,
                        signal_current_up_price=edge_decision.signal_current_up_price,
                        signal_threshold=cfg.strategy11_min_edge,
                        signal_delta=edge_decision.signal_delta,
                        signal_locked=True,
                    )
                candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
                price_skip_reason = entry_price_skip_reason(
                    strategy_prefix="strategy11",
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
                        signal_open_up_price=edge_decision.signal_open_up_price,
                        signal_current_up_price=edge_decision.signal_current_up_price,
                        signal_threshold=cfg.strategy11_min_edge,
                        signal_delta=edge_decision.signal_delta,
                        signal_probability=edge_decision.signal_probability,
                        signal_edge=edge_decision.signal_edge,
                        signal_locked=True,
                    )
                return SideDecision(
                    side=state.signal_round_locked_side,
                    candidate_side=state.signal_round_locked_side,
                    candidate_price=candidate_price,
                    signal_open_up_price=edge_decision.signal_open_up_price,
                    signal_current_up_price=edge_decision.signal_current_up_price,
                    signal_threshold=cfg.strategy11_min_edge,
                    signal_delta=edge_decision.signal_delta,
                    signal_probability=edge_decision.signal_probability,
                    signal_edge=edge_decision.signal_edge,
                    signal_locked=True,
                )
            if cfg.strategy_id == 10:
                edge_decision = evaluate_strategy10_edge(
                    cfg=cfg,
                    quote=quote,
                    now=now,
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                )
                state.strategy6_last_ofi_score = edge_decision.ofi_score
                edge_decision.signal_locked = True
                if edge_decision.side is None:
                    return edge_decision
                if edge_decision.side != state.signal_round_locked_side:
                    return SideDecision(
                        side=None,
                        reason="strategy10_signal_conflict",
                        candidate_side=edge_decision.side,
                        candidate_price=edge_decision.candidate_price,
                        signal_open_up_price=state.signal_round_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=cfg.strategy10_min_edge,
                        signal_delta=edge_decision.signal_delta,
                        signal_probability=edge_decision.signal_probability,
                        signal_edge=edge_decision.signal_edge,
                        signal_locked=True,
                        ofi_score=edge_decision.ofi_score,
                    )
                signal_check = Strategy7SignalCheck(decision=edge_decision, ofi_score=edge_decision.ofi_score, momentum_delta=edge_decision.signal_delta)
            else:
                signal_check = evaluate_strategy7_consensus_signal(
                    cfg=cfg,
                    quote=quote,
                    now=now,
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                )
                state.strategy6_last_ofi_score = signal_check.ofi_score
                if signal_check.decision.side is None:
                    if cfg.strategy_id == 9 and signal_check.decision.reason:
                        signal_check.decision.reason = signal_check.decision.reason.replace("strategy7_", "strategy9_", 1)
                    signal_check.decision.signal_locked = True
                    return signal_check.decision
            if signal_check.decision.side != state.signal_round_locked_side:
                return SideDecision(
                    side=None,
                    reason="strategy9_signal_conflict" if cfg.strategy_id == 9 else "strategy7_signal_conflict",
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=cfg.strategy7_momentum_threshold,
                    signal_delta=signal_check.momentum_delta,
                    signal_locked=True,
                )
            if cfg.strategy_id == 9:
                signal_current_for_quality = signal_current_up_price
                if (
                    signal_check.ofi_score is None
                    or signal_check.momentum_delta is None
                    or not is_valid_signal_price(signal_current_for_quality)
                ):
                    return SideDecision(
                        side=None,
                        reason="strategy9_signal_unstable",
                        signal_open_up_price=state.signal_round_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=cfg.strategy7_momentum_threshold,
                        signal_delta=signal_check.momentum_delta,
                        signal_locked=True,
                    )
                quality_check = evaluate_strategy9_quality(
                    cfg=cfg,
                    state=state,
                    now=now,
                    side=state.signal_round_locked_side,
                    ofi_score=signal_check.ofi_score,
                    momentum_delta=signal_check.momentum_delta,
                    signal_current_up_price=signal_current_for_quality,
                )
                locked_max_entry_price = quality_check.max_entry_price
                if quality_check.reason is not None:
                    return SideDecision(
                        side=None,
                        reason=quality_check.reason,
                        signal_open_up_price=state.signal_round_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=cfg.strategy7_momentum_threshold,
                        signal_delta=signal_check.momentum_delta,
                        signal_locked=True,
                    )
                dynamic_price = resolve_quote_price(state.signal_round_locked_side, quote)
                if quality_check.max_entry_price is not None and dynamic_price is not None and dynamic_price > quality_check.max_entry_price:
                    return SideDecision(
                        side=None,
                        reason="strategy9_dynamic_price_too_high",
                        candidate_side=state.signal_round_locked_side,
                        candidate_price=dynamic_price,
                        signal_open_up_price=state.signal_round_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=cfg.strategy7_momentum_threshold,
                        signal_delta=signal_check.momentum_delta,
                        signal_locked=True,
                        max_entry_price=quality_check.max_entry_price,
                    )
        if cfg.strategy_id in {7, 8, 9, 10, 11}:
            strategy_prefix = (
                "strategy7"
                if cfg.strategy_id == 7
                else ("strategy8" if cfg.strategy_id == 8 else ("strategy9" if cfg.strategy_id == 9 else ("strategy10" if cfg.strategy_id == 10 else "strategy11")))
            )
            locked_signal_threshold = cfg.strategy10_min_edge if cfg.strategy_id == 10 else signal_check.decision.signal_threshold
            if locked_signal_threshold is None:
                locked_signal_threshold = cfg.strategy7_momentum_threshold
            if cfg.strategy_id == 11:
                locked_signal_threshold = cfg.strategy11_min_edge
                locked_signal_delta = edge_decision.signal_delta
            else:
                locked_signal_delta = signal_check.decision.signal_delta if cfg.strategy_id == 10 else signal_delta
            candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
            price_skip_reason = entry_price_skip_reason(
                strategy_prefix=strategy_prefix,
                price=candidate_price,
                min_entry_price=getattr(cfg, "min_entry_price", None),
                max_entry_price=locked_max_entry_price if locked_max_entry_price is not None else getattr(cfg, "max_entry_price", None),
            )
            if price_skip_reason is not None:
                return SideDecision(
                    side=None,
                    reason=price_skip_reason,
                    candidate_side=state.signal_round_locked_side,
                    candidate_price=candidate_price,
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=locked_signal_threshold,
                    signal_delta=locked_signal_delta,
                    signal_probability=edge_decision.signal_probability if cfg.strategy_id in {10, 11} else None,
                    signal_edge=edge_decision.signal_edge if cfg.strategy_id in {10, 11} else None,
                    signal_locked=True,
                    max_entry_price=locked_max_entry_price,
                )
        return SideDecision(
            side=state.signal_round_locked_side,
            signal_open_up_price=edge_decision.signal_open_up_price if cfg.strategy_id == 11 else state.signal_round_open_up_price,
            signal_current_up_price=edge_decision.signal_current_up_price if cfg.strategy_id == 11 else signal_current_up_price,
            signal_threshold=locked_signal_threshold if cfg.strategy_id in {7, 8, 9, 10, 11} else None,
            signal_delta=locked_signal_delta if cfg.strategy_id in {7, 8, 9, 10, 11} else signal_delta,
            signal_probability=edge_decision.signal_probability if cfg.strategy_id in {10, 11} else None,
            signal_edge=edge_decision.signal_edge if cfg.strategy_id in {10, 11} else None,
            signal_locked=True,
            max_entry_price=locked_max_entry_price,
            candidate_side=state.signal_round_locked_side if cfg.strategy_id in {10, 11} else None,
            candidate_price=edge_decision.candidate_price if cfg.strategy_id == 11 else (signal_check.decision.candidate_price if cfg.strategy_id == 10 else None),
            ofi_score=signal_check.ofi_score if cfg.strategy_id in {7, 9, 10} else None,
            order_cost_multiplier=signal_check.decision.order_cost_multiplier if cfg.strategy_id in {7, 9, 10} else 1.0,
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

    if cfg.strategy_id == 10:
        edge_decision = evaluate_strategy10_edge(
            cfg=cfg,
            quote=quote,
            now=now,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
        )
        state.strategy6_last_ofi_score = edge_decision.ofi_score
        if edge_decision.side is None:
            return edge_decision

        resolved_side = edge_decision.side
        momentum_delta = edge_decision.signal_delta
        effective_confirm_before_entry_seconds = effective_strategy10_confirm_before_entry_seconds(
            cfg=cfg,
            window=window,
            entry_time=entry_time,
        )
        if (
            entry_time is not None
            and effective_confirm_before_entry_seconds > 0
            and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
        ):
            return SideDecision(
                side=None,
                reason="strategy10_entry_too_late",
                candidate_side=resolved_side,
                candidate_price=edge_decision.candidate_price,
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy10_min_edge,
                signal_delta=momentum_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
                ofi_score=edge_decision.ofi_score,
            )

        candidate_price = resolve_quote_price(resolved_side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix="strategy10",
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
                signal_threshold=cfg.strategy10_min_edge,
                signal_delta=momentum_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
                ofi_score=edge_decision.ofi_score,
            )

        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            candidate_side=resolved_side,
            candidate_price=candidate_price,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=cfg.strategy10_min_edge,
            signal_delta=momentum_delta,
            signal_probability=edge_decision.signal_probability,
            signal_edge=edge_decision.signal_edge,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
            ofi_score=edge_decision.ofi_score,
        )

    if cfg.strategy_id == 11:
        edge_decision = evaluate_strategy11_probability_edge(
            cfg=cfg,
            quote=quote,
            now=now,
            window=window,
            state=state,
        )
        if edge_decision.side is None:
            return edge_decision

        effective_confirm_before_entry_seconds = max(
            0,
            int(getattr(cfg, "strategy11_confirm_before_entry_seconds", 0)),
        )
        if (
            entry_time is not None
            and effective_confirm_before_entry_seconds > 0
            and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
        ):
            return SideDecision(
                side=None,
                reason="strategy11_entry_too_late",
                candidate_side=edge_decision.side,
                candidate_price=edge_decision.candidate_price,
                signal_open_up_price=edge_decision.signal_open_up_price,
                signal_current_up_price=edge_decision.signal_current_up_price,
                signal_threshold=cfg.strategy11_min_edge,
                signal_delta=edge_decision.signal_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
            )

        candidate_price = resolve_quote_price(edge_decision.side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix="strategy11",
            price=candidate_price,
            min_entry_price=getattr(cfg, "min_entry_price", None),
            max_entry_price=getattr(cfg, "max_entry_price", None),
        )
        if price_skip_reason is not None:
            return SideDecision(
                side=None,
                reason=price_skip_reason,
                candidate_side=edge_decision.side,
                candidate_price=candidate_price,
                signal_open_up_price=edge_decision.signal_open_up_price,
                signal_current_up_price=edge_decision.signal_current_up_price,
                signal_threshold=cfg.strategy11_min_edge,
                signal_delta=edge_decision.signal_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
            )

        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = edge_decision.side
        return SideDecision(
            side=edge_decision.side,
            candidate_side=edge_decision.side,
            candidate_price=candidate_price,
            signal_open_up_price=edge_decision.signal_open_up_price,
            signal_current_up_price=edge_decision.signal_current_up_price,
            signal_threshold=cfg.strategy11_min_edge,
            signal_delta=edge_decision.signal_delta,
            signal_probability=edge_decision.signal_probability,
            signal_edge=edge_decision.signal_edge,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    if cfg.strategy_id == 12:
        edge_decision = evaluate_strategy12_hybrid_edge(
            cfg=cfg,
            quote=quote,
            now=now,
            window=window,
            state=state,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
        )
        state.strategy6_last_ofi_score = edge_decision.ofi_score
        if edge_decision.side is None:
            return edge_decision

        effective_confirm_before_entry_seconds = max(
            0,
            int(getattr(cfg, "strategy11_confirm_before_entry_seconds", 0)),
        )
        if (
            entry_time is not None
            and effective_confirm_before_entry_seconds > 0
            and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
        ):
            return SideDecision(
                side=None,
                reason="strategy12_entry_too_late",
                candidate_side=edge_decision.side,
                candidate_price=edge_decision.candidate_price,
                signal_open_up_price=edge_decision.signal_open_up_price,
                signal_current_up_price=edge_decision.signal_current_up_price,
                signal_threshold=cfg.strategy11_min_edge,
                signal_delta=edge_decision.signal_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
                ofi_score=edge_decision.ofi_score,
            )

        candidate_price = resolve_quote_price(edge_decision.side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix="strategy12",
            price=candidate_price,
            min_entry_price=getattr(cfg, "min_entry_price", None),
            max_entry_price=getattr(cfg, "max_entry_price", None),
        )
        if price_skip_reason is not None:
            return SideDecision(
                side=None,
                reason=price_skip_reason,
                candidate_side=edge_decision.side,
                candidate_price=candidate_price,
                signal_open_up_price=edge_decision.signal_open_up_price,
                signal_current_up_price=edge_decision.signal_current_up_price,
                signal_threshold=cfg.strategy11_min_edge,
                signal_delta=edge_decision.signal_delta,
                signal_probability=edge_decision.signal_probability,
                signal_edge=edge_decision.signal_edge,
                ofi_score=edge_decision.ofi_score,
            )

        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = edge_decision.side
        return SideDecision(
            side=edge_decision.side,
            candidate_side=edge_decision.side,
            candidate_price=candidate_price,
            signal_open_up_price=edge_decision.signal_open_up_price,
            signal_current_up_price=edge_decision.signal_current_up_price,
            signal_threshold=cfg.strategy11_min_edge,
            signal_delta=edge_decision.signal_delta,
            signal_probability=edge_decision.signal_probability,
            signal_edge=edge_decision.signal_edge,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
            ofi_score=edge_decision.ofi_score,
        )

    if cfg.strategy_id == 13:
        edge_decision = evaluate_strategy13_probability_edge(
            cfg=cfg,
            quote=quote,
            now=now,
            window=window,
            state=state,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
        )
        state.strategy6_last_ofi_score = edge_decision.ofi_score
        if edge_decision.side is None:
            return edge_decision

        effective_confirm_before_entry_seconds = max(
            0,
            int(getattr(cfg, "strategy13_confirm_before_entry_seconds", 0)),
        )
        if (
            entry_time is not None
            and effective_confirm_before_entry_seconds > 0
            and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
        ):
            edge_decision.side = None
            edge_decision.reason = "strategy13_entry_too_late"
            return edge_decision

        candidate_price = resolve_quote_price(edge_decision.side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix="strategy13",
            price=candidate_price,
            min_entry_price=getattr(cfg, "min_entry_price", None),
            max_entry_price=getattr(cfg, "max_entry_price", None),
        )
        if price_skip_reason is not None:
            edge_decision.side = None
            edge_decision.reason = price_skip_reason
            edge_decision.candidate_side = edge_decision.candidate_side or edge_decision.side
            edge_decision.candidate_price = candidate_price
            return edge_decision

        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = edge_decision.side
        edge_decision.signal_locked = state.signal_round_locked_side in {"UP", "DOWN"}
        edge_decision.candidate_side = edge_decision.side
        edge_decision.candidate_price = candidate_price
        return edge_decision

    if cfg.strategy_id in {7, 8, 9}:
        strategy_prefix = (
            "strategy7"
            if cfg.strategy_id == 7
            else ("strategy8" if cfg.strategy_id == 8 else "strategy9")
        )
        dynamic_max_entry_price: float | None = None
        if cfg.strategy_id in {7, 9}:
            signal_check = evaluate_strategy7_consensus_signal(
                cfg=cfg,
                quote=quote,
                now=now,
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
            state.strategy6_last_ofi_score = signal_check.ofi_score
            if signal_check.decision.side is None:
                if cfg.strategy_id == 9 and signal_check.decision.reason:
                    signal_check.decision.reason = signal_check.decision.reason.replace("strategy7_", "strategy9_", 1)
                return signal_check.decision
            ofi_score = signal_check.ofi_score
            momentum_delta = signal_check.momentum_delta
            strategy_signal_threshold = signal_check.decision.signal_threshold or cfg.strategy7_momentum_threshold
            strategy_order_multiplier = signal_check.decision.order_cost_multiplier
            signal_gap_ok = True
        else:
            strategy_signal_threshold = cfg.strategy7_momentum_threshold
            strategy_order_multiplier = 1.0
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
                    reason="strategy8_market_state_weak",
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
                    reason="strategy8_market_state_weak",
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
                signal_threshold=strategy_signal_threshold,
                signal_delta=momentum_delta,
                order_cost_multiplier=strategy_order_multiplier,
            )

        if cfg.strategy_id in {7, 9}:
            if not strategy7_signals_agree(ofi_score=ofi_score, momentum_delta=momentum_delta):
                return SideDecision(
                    side=None,
                    reason=f"{strategy_prefix}_signal_conflict",
                    signal_open_up_price=signal_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=strategy_signal_threshold,
                    signal_delta=momentum_delta,
                    order_cost_multiplier=strategy_order_multiplier,
                )
            resolved_side = "UP" if momentum_delta > 0 else "DOWN"
            decision_reason = None
            if cfg.strategy_id == 9:
                if not is_valid_signal_price(signal_current_up_price):
                    return SideDecision(
                        side=None,
                        reason="strategy9_signal_unstable",
                        signal_open_up_price=signal_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=strategy_signal_threshold,
                        signal_delta=momentum_delta,
                        order_cost_multiplier=strategy_order_multiplier,
                    )
                quality_check = evaluate_strategy9_quality(
                    cfg=cfg,
                    state=state,
                    now=now,
                    side=resolved_side,
                    ofi_score=ofi_score,
                    momentum_delta=momentum_delta,
                    signal_current_up_price=signal_current_up_price,
                )
                if quality_check.reason is not None:
                    return SideDecision(
                        side=None,
                        reason=quality_check.reason,
                        signal_open_up_price=signal_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=strategy_signal_threshold,
                        signal_delta=momentum_delta,
                        order_cost_multiplier=strategy_order_multiplier,
                    )
                dynamic_max_entry_price = quality_check.max_entry_price
        elif cfg.strategy_id == 8 and ofi_score * momentum_delta <= 0:
            resolved_side = "UP" if ofi_score > 0 else "DOWN"
            decision_reason = "strategy8_conflict_reversal"
        elif cfg.strategy_id == 8:
            resolved_side = "UP" if momentum_delta > 0 else "DOWN"
            decision_reason = None
        candidate_price = resolve_quote_price(resolved_side, quote)
        price_skip_reason = entry_price_skip_reason(
            strategy_prefix=strategy_prefix,
            price=candidate_price,
            min_entry_price=getattr(cfg, "min_entry_price", None),
            max_entry_price=dynamic_max_entry_price if dynamic_max_entry_price is not None else getattr(cfg, "max_entry_price", None),
        )
        if price_skip_reason is not None:
            if cfg.strategy_id == 9 and price_skip_reason == "strategy9_price_too_high":
                price_skip_reason = "strategy9_dynamic_price_too_high"
            return SideDecision(
                side=None,
                reason=price_skip_reason,
                candidate_side=resolved_side,
                candidate_price=candidate_price,
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=strategy_signal_threshold,
                signal_delta=momentum_delta,
                max_entry_price=dynamic_max_entry_price,
                order_cost_multiplier=strategy_order_multiplier,
            )
        if cfg.strategy_id in {7, 9} and not signal_gap_ok:
            return SideDecision(
                side=None,
                reason=f"{strategy_prefix}_confidence_too_low",
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=strategy_signal_threshold,
                signal_delta=momentum_delta,
                order_cost_multiplier=strategy_order_multiplier,
            )
        if cfg.strategy_id == 8:
            state.signal_round_locked_side = resolved_side
        elif entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            reason=decision_reason,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=strategy_signal_threshold,
            candidate_side=resolved_side if cfg.strategy_id == 10 else None,
            candidate_price=candidate_price if cfg.strategy_id == 10 else None,
            signal_delta=momentum_delta,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
            max_entry_price=dynamic_max_entry_price,
            ofi_score=ofi_score,
            order_cost_multiplier=strategy_order_multiplier,
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
