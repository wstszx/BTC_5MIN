from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any

from clob_adapter import build_verified_pending_live_trade_plan
from config import AppConfig
from models import LiveStrategyState, MarketWindow, PendingPaperTrade, SessionState, TradePlan, TradeRecord
from polymarket_api import normalize_outcome_label, parse_iso_datetime, parse_outcome_prices
from risk_and_sizing import apply_round_outcome, build_trade_plan
from trade_log import append_trade_log
from utils import _runtime_log


PROVISIONAL_LOSS_RESULT = "PROVISIONAL_LOSS"
_LIVE_BTC_ROUND_RE = re.compile(r"^btc-up(?:-or-)?down-(?:5m|15m)-\d+$")


def clear_pending_live_trade(strategy_state: LiveStrategyState) -> None:
    strategy_state.pending_live_slug = None
    strategy_state.pending_live_side = None
    strategy_state.pending_live_price = None
    strategy_state.pending_live_order_size = None
    strategy_state.pending_live_order_cost = None
    strategy_state.pending_live_expected_profit = None
    strategy_state.pending_live_order_id = None
    strategy_state.pending_live_end_time = None
    strategy_state.pending_live_tracks_recovery_loss = True


def timeframe_duration_seconds(timeframe: str | None) -> int:
    return 900 if str(timeframe or "").strip().lower() == "15m" else 300


def append_settled_live_trade_log(
    *,
    log_path: Path,
    cfg: AppConfig,
    strategy_id: int,
    prior_state: LiveStrategyState,
    updated_state: LiveStrategyState,
    settlement_status: dict[str, Any] | None,
) -> None:
    if not settlement_status or settlement_status.get("status") != "settled":
        return

    end_time = parse_iso_datetime(prior_state.pending_live_end_time) or datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=timeframe_duration_seconds(getattr(cfg, "market_timeframe", None)))
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=max(0, prior_state.round_index - 1),
            strategy=strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=str(prior_state.pending_live_slug or settlement_status.get("slug") or ""),
            start_time=start_time,
            end_time=end_time,
            side=str(settlement_status.get("side") or prior_state.pending_live_side or ""),
            price=settlement_status.get("price") if settlement_status.get("price") is not None else prior_state.pending_live_price,
            order_size=float(
                settlement_status.get("order_size")
                if settlement_status.get("order_size") is not None
                else (prior_state.pending_live_order_size or 0.0)
            ),
            order_cost=float(
                settlement_status.get("order_cost")
                if settlement_status.get("order_cost") is not None
                else (prior_state.pending_live_order_cost or 0.0)
            ),
            expected_profit=float(
                settlement_status.get("expected_profit")
                if settlement_status.get("expected_profit") is not None
                else (prior_state.pending_live_expected_profit or 0.0)
            ),
            result=str(settlement_status.get("result") or ""),
            trade_pnl=float(settlement_status.get("trade_pnl") or 0.0),
            cash_pnl=updated_state.cash_pnl,
            recovery_loss=updated_state.recovery_loss,
            consecutive_losses=updated_state.consecutive_losses,
            tracks_recovery_loss=prior_state.pending_live_tracks_recovery_loss,
        ),
    )


def append_provisional_live_loss_trade_log(
    *,
    log_path: Path,
    cfg: AppConfig,
    strategy_id: int,
    prior_state: LiveStrategyState,
    updated_state: LiveStrategyState,
    settlement_status: dict[str, Any] | None,
) -> None:
    if not settlement_status or settlement_status.get("status") != "provisional_loss":
        return

    end_time = parse_iso_datetime(prior_state.pending_live_end_time) or datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=timeframe_duration_seconds(getattr(cfg, "market_timeframe", None)))
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=max(0, prior_state.round_index - 1),
            strategy=strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=str(prior_state.pending_live_slug or settlement_status.get("slug") or ""),
            start_time=start_time,
            end_time=end_time,
            side=str(settlement_status.get("side") or prior_state.pending_live_side or ""),
            price=settlement_status.get("price") if settlement_status.get("price") is not None else prior_state.pending_live_price,
            order_size=float(
                settlement_status.get("order_size")
                if settlement_status.get("order_size") is not None
                else (prior_state.pending_live_order_size or 0.0)
            ),
            order_cost=float(
                settlement_status.get("order_cost")
                if settlement_status.get("order_cost") is not None
                else (prior_state.pending_live_order_cost or 0.0)
            ),
            expected_profit=float(
                settlement_status.get("expected_profit")
                if settlement_status.get("expected_profit") is not None
                else (prior_state.pending_live_expected_profit or 0.0)
            ),
            result=PROVISIONAL_LOSS_RESULT,
            trade_pnl=float(settlement_status.get("trade_pnl") or 0.0),
            cash_pnl=updated_state.cash_pnl,
            recovery_loss=updated_state.recovery_loss,
            consecutive_losses=updated_state.consecutive_losses,
            tracks_recovery_loss=prior_state.pending_live_tracks_recovery_loss,
        ),
    )


def build_pending_live_trade_plan(state: SessionState) -> TradePlan:
    if state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if state.pending_live_price is None:
        raise RuntimeError("Pending live trade is missing entry price.")
    if state.pending_live_order_size is None or state.pending_live_order_size <= 0:
        raise RuntimeError("Pending live trade is missing order size.")
    if state.pending_live_order_cost is None or state.pending_live_order_cost <= 0:
        raise RuntimeError("Pending live trade is missing order cost.")
    if state.pending_live_expected_profit is None:
        raise RuntimeError("Pending live trade is missing expected profit.")

    return TradePlan(
        True,
        side=state.pending_live_side,
        price=state.pending_live_price,
        order_size=state.pending_live_order_size,
        order_cost=state.pending_live_order_cost,
        expected_profit=state.pending_live_expected_profit,
        tracks_recovery_loss=state.pending_live_tracks_recovery_loss,
    )


def cached_ws_market_result(market_client: Any, market: dict[str, Any]) -> str:
    get_resolution = getattr(market_client, "get_ws_market_resolution", None)
    if not callable(get_resolution):
        return ""
    try:
        resolution = get_resolution(market)
    except Exception:
        return ""
    if not isinstance(resolution, dict):
        return ""
    outcome = normalize_outcome_label(str(resolution.get("winning_outcome") or ""))
    return outcome if outcome in {"UP", "DOWN"} else ""


def resolved_result_from_redeemable_positions(
    market_client: Any,
    *,
    funder: str | None,
    slug: str,
) -> str | None:
    target_user = str(funder or "").strip().lower()
    if not target_user:
        return None
    get_positions = getattr(market_client, "get_current_positions", None)
    if not callable(get_positions):
        return None
    positions = get_positions(user=target_user, redeemable=True)
    if not isinstance(positions, list):
        return None
    for row in positions:
        if not isinstance(row, dict):
            continue
        row_slug = str(row.get("eventSlug") or row.get("event_slug") or "").strip()
        if row_slug != slug:
            continue
        row_user = str(row.get("proxyWallet") or row.get("user") or row.get("owner") or "").strip().lower()
        if row_user and row_user != target_user:
            continue
        if not _truthy_position_flag(row.get("redeemable")):
            continue
        try:
            size = float(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        outcome = normalize_outcome_label(str(row.get("outcome") or ""))
        if outcome in {"UP", "DOWN"}:
            return outcome
    return None


def _truthy_position_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _is_officially_closed(*values: Any) -> bool:
    return any(value is True for value in values)


def _event_metadata(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("eventMetadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _market_event_metadata(market: dict[str, Any]) -> dict[str, Any]:
    metadata = market.get("eventMetadata") or {}
    if isinstance(metadata, dict) and metadata:
        return metadata
    events = market.get("events")
    if isinstance(events, list):
        for item in events:
            if not isinstance(item, dict):
                continue
            event_metadata = item.get("eventMetadata") or {}
            if isinstance(event_metadata, dict) and event_metadata:
                return event_metadata
    return {}


def _metadata_result(metadata: dict[str, Any]) -> str | None:
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        return None
    return "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"


def _metadata_waits_for_final_price(metadata: dict[str, Any]) -> bool:
    return metadata.get("priceToBeat") is not None and metadata.get("finalPrice") is None


def _metadata_has_final_price_pair(metadata: dict[str, Any]) -> bool:
    return metadata.get("priceToBeat") is not None and metadata.get("finalPrice") is not None


def _is_live_btc_round_slug(slug: str | None) -> bool:
    return bool(_LIVE_BTC_ROUND_RE.fullmatch(str(slug or "").strip()))


def _live_btc_round_missing_final_price(
    market_client: Any,
    event: dict[str, Any],
    market: dict[str, Any],
) -> bool:
    slug = str(event.get("slug") or market.get("slug") or "").strip()
    if not _is_live_btc_round_slug(slug):
        return False
    metadata = _event_metadata(event)
    if _metadata_has_final_price_pair(metadata):
        return False
    get_market = getattr(market_client, "get_market_by_slug", None)
    if callable(get_market) and slug:
        try:
            endpoint_market = get_market(slug)
        except Exception:
            endpoint_market = None
        if isinstance(endpoint_market, dict):
            endpoint_metadata = _market_event_metadata(endpoint_market)
            return not _metadata_has_final_price_pair(endpoint_metadata)
    return True


def _terminal_market_price_result(event: dict[str, Any], market: dict[str, Any]) -> str | None:
    prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
    up_price = prices.get("UP")
    down_price = prices.get("DOWN")
    is_closed = _is_officially_closed(event.get("closed"), market.get("closed"))
    if is_closed and up_price is not None and down_price is not None and {up_price, down_price} == {0.0, 1.0}:
        return "UP" if up_price > down_price else "DOWN"
    return None


def _live_event_is_closed(market_client: Any, event: dict[str, Any], market: dict[str, Any]) -> bool:
    if _is_officially_closed(event.get("closed"), market.get("closed")):
        return True
    get_market = getattr(market_client, "get_market_by_slug", None)
    slug = str(event.get("slug") or market.get("slug") or "").strip()
    if not callable(get_market) or not slug:
        return False
    try:
        endpoint_market = get_market(slug)
    except Exception:
        return False
    return isinstance(endpoint_market, dict) and _is_officially_closed(endpoint_market.get("closed"))


def live_market_waits_for_final_price(
    market_client: Any,
    event: dict[str, Any],
    market: dict[str, Any],
) -> bool:
    if _live_btc_round_missing_final_price(market_client, event, market):
        return True
    if _metadata_waits_for_final_price(_event_metadata(event)):
        return True
    get_market = getattr(market_client, "get_market_by_slug", None)
    slug = str(event.get("slug") or market.get("slug") or "").strip()
    if not callable(get_market) or not slug:
        return False
    try:
        endpoint_market = get_market(slug)
    except Exception:
        return False
    if not isinstance(endpoint_market, dict):
        return False
    return _metadata_waits_for_final_price(_market_event_metadata(endpoint_market))


def resolved_result_from_clob_token_winner(market_client: Any, market: dict[str, Any]) -> str | None:
    get_clob_market = getattr(market_client, "get_clob_market_by_condition_id", None)
    if not callable(get_clob_market):
        return None
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        return None
    try:
        clob_market = get_clob_market(condition_id)
    except Exception:
        return None
    if not isinstance(clob_market, dict) or clob_market.get("closed") is not True:
        return None
    tokens = clob_market.get("tokens")
    if not isinstance(tokens, list):
        return None
    winners = [token for token in tokens if isinstance(token, dict) and token.get("winner") is True]
    if len(winners) != 1:
        return None
    outcome = normalize_outcome_label(str(winners[0].get("outcome") or ""))
    return outcome if outcome in {"UP", "DOWN"} else None


def resolved_result_from_official_market(event: dict[str, Any], market: dict[str, Any]) -> str | None:
    return _metadata_result(_event_metadata(event)) or _terminal_market_price_result(event, market)


def resolved_result_from_official_market_endpoint(market_client: Any, slug: str) -> str | None:
    get_market = getattr(market_client, "get_market_by_slug", None)
    if not callable(get_market):
        return None
    try:
        market = get_market(slug)
    except Exception:
        return None
    if not isinstance(market, dict):
        return None
    event = {
        "closed": market.get("closed"),
        "eventMetadata": market.get("eventMetadata") or {},
    }
    if not event["eventMetadata"]:
        event["eventMetadata"] = _market_event_metadata(market)
    return resolved_result_from_official_market(event, market)


def resolved_live_result_from_official_sources(
    market_client: Any,
    event: dict[str, Any],
    market: dict[str, Any],
) -> str | None:
    metadata = _event_metadata(event)
    metadata_result = _metadata_result(metadata)
    if metadata_result:
        return metadata_result
    event_waits_for_final_price = _metadata_waits_for_final_price(metadata)
    terminal_result = _terminal_market_price_result(event, market)

    endpoint_market = None
    get_market = getattr(market_client, "get_market_by_slug", None)
    slug = str(event.get("slug") or market.get("slug") or "").strip()
    if callable(get_market) and slug:
        try:
            endpoint_market = get_market(slug)
        except Exception:
            endpoint_market = None
    endpoint_event = None
    endpoint_waits_for_final_price = False
    if isinstance(endpoint_market, dict):
        endpoint_event = {
            "slug": slug,
            "closed": endpoint_market.get("closed"),
            "eventMetadata": _market_event_metadata(endpoint_market),
        }
        endpoint_metadata_result = _metadata_result(endpoint_event["eventMetadata"])
        if endpoint_metadata_result:
            return endpoint_metadata_result
        endpoint_waits_for_final_price = _metadata_waits_for_final_price(endpoint_event["eventMetadata"])
    if _live_btc_round_missing_final_price(market_client, event, market):
        return None
    if event_waits_for_final_price or endpoint_waits_for_final_price:
        return None
    result = resolved_result_from_clob_token_winner(market_client, market)
    if result:
        return result
    if isinstance(endpoint_market, dict):
        result = resolved_result_from_clob_token_winner(market_client, endpoint_market)
        if result:
            return result
    return terminal_result


def resolve_pending_live_result(
    *,
    market_client: Any,
    funder: str | None,
    slug: str,
) -> tuple[str | None, dict[str, Any] | None]:
    event = market_client.get_event_by_slug(slug)
    market = (event.get("markets") or [{}])[0]
    official_market_result = resolved_live_result_from_official_sources(market_client, event, market)
    if official_market_result:
        return official_market_result, None
    if live_market_waits_for_final_price(market_client, event, market):
        if not _live_event_is_closed(market_client, event, market):
            return None, {
                "status": "pending_settlement",
                "slug": slug,
                "skip_reason": "round_unresolved",
            }
        return None, {
            "status": "awaiting_final_price",
            "slug": slug,
            "skip_reason": "round_unresolved",
        }

    official_position_result = resolved_result_from_redeemable_positions(
        market_client,
        funder=funder,
        slug=slug,
    )
    if official_position_result:
        return official_position_result, None
    return None, {
        "status": "pending_settlement",
        "slug": slug,
        "skip_reason": "round_unresolved",
    }


def build_frozen_pending_live_plan(strategy_state: LiveStrategyState) -> TradePlan | None:
    if strategy_state.pending_live_side not in {"UP", "DOWN"}:
        return None
    if strategy_state.pending_live_price is None:
        return None
    if strategy_state.pending_live_order_size is None or strategy_state.pending_live_order_size <= 0:
        return None
    if strategy_state.pending_live_order_cost is None or strategy_state.pending_live_order_cost <= 0:
        return None
    if strategy_state.pending_live_expected_profit is None:
        return None
    return TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=strategy_state.pending_live_price,
        order_size=strategy_state.pending_live_order_size,
        order_cost=strategy_state.pending_live_order_cost,
        expected_profit=strategy_state.pending_live_expected_profit,
        tracks_recovery_loss=strategy_state.pending_live_tracks_recovery_loss,
    )


def settle_pending_live_trade_if_needed(
    *,
    market_client: Any,
    clob_client: Any | None,
    strategy_state: LiveStrategyState,
    now: datetime,
    funder: str | None = None,
    pending_plan_resolver=build_verified_pending_live_trade_plan,
    final_price_wait_seconds: float = 0.0,
) -> tuple[LiveStrategyState, dict[str, Any] | None, bool]:
    if not strategy_state.pending_live_slug:
        return strategy_state, None, False

    end_time = parse_iso_datetime(strategy_state.pending_live_end_time)
    if end_time is None:
        raise RuntimeError("Pending live trade is missing round end time.")

    if now < end_time:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_in_progress",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )

    result, unresolved_status = resolve_pending_live_result(
        market_client=market_client,
        funder=funder,
        slug=strategy_state.pending_live_slug,
    )

    try:
        plan = pending_plan_resolver(
            strategy_state,
            clob_client=clob_client,
            require_confirmed_trades=True,
        )
    except TypeError:
        plan = pending_plan_resolver(strategy_state, clob_client=clob_client)
    if plan is None:
        if result and not strategy_state.pending_live_order_id:
            plan = build_frozen_pending_live_plan(strategy_state)
        if plan is None:
            skip_reason = "awaiting_fill_confirmation" if result else "round_unresolved"
            status_payload = unresolved_status or {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "skip_reason": skip_reason,
            }
            status_payload.update(
                {
                    "status": "pending_settlement",
                    "slug": strategy_state.pending_live_slug,
                    "side": strategy_state.pending_live_side,
                    "skip_reason": skip_reason,
                    "pending_end_time": strategy_state.pending_live_end_time,
                    "order_id": strategy_state.pending_live_order_id,
                }
            )
            return strategy_state, status_payload, False

    if not result and unresolved_status and unresolved_status.get("status") == "awaiting_final_price":
        final_price_deadline = end_time + timedelta(seconds=max(0.0, float(final_price_wait_seconds)))
        if now < final_price_deadline:
            return (
                strategy_state,
                {
                    "status": "pending_settlement",
                    "slug": strategy_state.pending_live_slug,
                    "side": strategy_state.pending_live_side,
                    "skip_reason": "awaiting_final_price",
                    "pending_end_time": strategy_state.pending_live_end_time,
                    "order_id": strategy_state.pending_live_order_id,
                    "final_price_deadline": final_price_deadline.isoformat(),
                },
                False,
            )
        updated_state = apply_round_outcome(strategy_state, plan, won=False)
        trade_pnl = updated_state.cash_pnl - strategy_state.cash_pnl
        clear_pending_live_trade(updated_state)
        return (
            updated_state,
            {
                "status": "provisional_loss",
                "slug": strategy_state.pending_live_slug,
                "side": plan.side,
                "price": plan.price,
                "order_size": plan.order_size,
                "order_cost": plan.order_cost,
                "expected_profit": plan.expected_profit,
                "result": PROVISIONAL_LOSS_RESULT,
                "trade_pnl": trade_pnl,
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            True,
        )
    if not result:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_unresolved",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )
    updated_state = apply_round_outcome(strategy_state, plan, won=(result == plan.side))
    trade_pnl = updated_state.cash_pnl - strategy_state.cash_pnl
    clear_pending_live_trade(updated_state)
    return (
        updated_state,
        {
            "status": "settled",
            "slug": strategy_state.pending_live_slug,
            "side": plan.side,
            "price": plan.price,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "result": result,
            "trade_pnl": trade_pnl,
        },
        True,
    )


def build_frozen_pending_paper_plan(item: PendingPaperTrade) -> TradePlan:
    return TradePlan(
        True,
        side=item.side,
        price=item.price,
        order_size=item.order_size,
        order_cost=item.order_cost,
        expected_profit=item.expected_profit,
        tracks_recovery_loss=item.tracks_recovery_loss,
    )


def settle_pending_paper_trade(
    *,
    client: Any,
    state: SessionState,
    item: PendingPaperTrade,
) -> tuple[SessionState, str, float]:
    event = client.get_event_by_slug(item.event_slug)
    market = (event.get("markets") or [{}])[0]
    result = resolved_result_from_official_market(event, market)
    if not result:
        result = resolved_result_from_official_market_endpoint(client, item.event_slug)
    if not result:
        raise RuntimeError(f"Round {item.event_slug} is not resolved yet.")
    plan = build_frozen_pending_paper_plan(item)
    updated_state = apply_round_outcome(state, plan, won=(result == item.side))
    trade_pnl = updated_state.cash_pnl - state.cash_pnl
    return updated_state, result, trade_pnl


def settle_pending_paper_trades(
    *,
    client: Any,
    state: SessionState,
    log_path: Path,
) -> tuple[SessionState, bool]:
    if not state.pending_paper_trades:
        return state, False

    updated_state = state
    changed = False
    remaining: list[PendingPaperTrade] = []
    for item in updated_state.pending_paper_trades:
        try:
            next_state, result, trade_pnl = settle_pending_paper_trade(
                client=client,
                state=updated_state,
                item=item,
            )
        except RuntimeError as exc:
            if "is not resolved yet" in str(exc):
                _runtime_log("round=" + item.event_slug + " pending resolution")
                remaining.append(item)
                continue
            _runtime_log("round=" + item.event_slug + " settlement error: " + str(exc))
            remaining.append(item)
            continue
        except Exception as exc:
            _runtime_log("round=" + item.event_slug + " settlement error: " + str(exc))
            remaining.append(item)
            continue

        updated_state = next_state
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="paper",
                experiment_id=item.experiment_id,
                round_index=item.round_index,
                strategy=item.strategy,
                entry_timing=item.entry_timing,
                event_slug=item.event_slug,
                start_time=parse_iso_datetime(item.start_time) or datetime.now(timezone.utc),
                end_time=parse_iso_datetime(item.end_time) or datetime.now(timezone.utc),
                side=item.side,
                price=item.price,
                order_size=item.order_size,
                order_cost=item.order_cost,
                expected_profit=item.expected_profit,
                result=result,
                trade_pnl=trade_pnl,
                cash_pnl=updated_state.cash_pnl,
                recovery_loss=updated_state.recovery_loss,
                consecutive_losses=updated_state.consecutive_losses,
                signal_open_up_price=item.signal_open_up_price,
                signal_current_up_price=item.signal_current_up_price,
                signal_threshold=item.signal_threshold,
                signal_delta=item.signal_delta,
                signal_locked=item.signal_locked,
                signal_reason=item.signal_reason,
                tracks_recovery_loss=item.tracks_recovery_loss,
            ),
        )
        _runtime_log(
            "round=" + item.event_slug
            + " settled result=" + result
            + " trade_pnl=" + f"{trade_pnl:.4f}"
            + " total_cash_pnl=" + f"{updated_state.cash_pnl:.4f}"
            + " consecutive_losses=" + str(updated_state.consecutive_losses)
        )
        changed = True

    updated_state.pending_paper_trades = remaining
    return updated_state, changed


def settle_paper_trade(
    client: Any,
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
        min_entry_price=getattr(cfg, "min_entry_price", getattr(cfg, "min_price_threshold", None)),
        max_entry_price=getattr(cfg, "max_entry_price", cfg.max_price_threshold),
        min_price_threshold=getattr(cfg, "min_price_threshold", None),
        max_price_threshold=cfg.max_price_threshold,
        min_stake=getattr(cfg, "min_stake", None),
        max_stake=cfg.max_stake,
        max_consecutive_losses=cfg.max_consecutive_losses,
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
    )
    updated_state = apply_round_outcome(state, plan, won=(result == side))
    return updated_state, result
