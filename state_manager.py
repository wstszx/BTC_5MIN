from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_text
from live_pending import normalize_pending_live_trades
from models import LiveStrategyState, PaperStrategyState, PendingLiveTrade, PendingPaperTrade, SessionState, Strategy9SignalSample


_LIVE_STRATEGY_FIELD_NAMES = (
    "round_index",
    "cash_pnl",
    "recovery_loss",
    "consecutive_losses",
    "consecutive_max_stake_skips",
    "signal_round_slug",
    "signal_round_open_up_price",
    "signal_round_locked_side",
    "strategy6_last_ofi_score",
    "strategy11_round_start_btc_price",
    "strategy9_signal_samples",
    "stop_loss_count",
    "daily_realized_pnl",
    "current_day",
    "pending_live_slug",
    "pending_live_side",
    "pending_live_price",
    "pending_live_order_size",
    "pending_live_order_cost",
    "pending_live_expected_profit",
    "pending_live_order_id",
    "pending_live_end_time",
    "pending_live_tracks_recovery_loss",
    "pending_live_trades",
    "last_processed_live_event_slug",
)


def hydrate_pending_paper_trades(items: list[dict[str, Any]] | list[PendingPaperTrade] | None) -> list[PendingPaperTrade]:
    trades: list[PendingPaperTrade] = []
    for item in items or []:
        if isinstance(item, PendingPaperTrade):
            item.tracks_recovery_loss = False
            trades.append(item)
            continue
        payload = dict(item)
        payload["tracks_recovery_loss"] = False
        trades.append(PendingPaperTrade(**payload))
    return trades


def hydrate_pending_live_trades(items: list[dict[str, Any]] | list[PendingLiveTrade] | None) -> list[PendingLiveTrade]:
    trades: list[PendingLiveTrade] = []
    for item in items or []:
        if isinstance(item, PendingLiveTrade):
            item.tracks_recovery_loss = False
            trades.append(item)
            continue
        payload = dict(item)
        payload["tracks_recovery_loss"] = False
        trades.append(PendingLiveTrade(**payload))
    return trades


def hydrate_strategy9_signal_samples(items: list[dict[str, Any]] | list[Strategy9SignalSample] | None) -> list[Strategy9SignalSample]:
    return [
        item if isinstance(item, Strategy9SignalSample) else Strategy9SignalSample(**item)
        for item in (items or [])
    ]


def hydrate_paper_strategy_state(payload: dict[str, Any]) -> PaperStrategyState:
    strategy_payload = dict(payload)
    strategy_payload["recovery_loss"] = 0.0
    pending_key = "pending_paper_trades"
    strategy_payload[pending_key] = hydrate_pending_paper_trades(strategy_payload.get(pending_key))
    strategy_payload["strategy9_signal_samples"] = hydrate_strategy9_signal_samples(
        strategy_payload.get("strategy9_signal_samples")
    )
    return PaperStrategyState(**strategy_payload)


def hydrate_live_strategy_state(payload: dict[str, Any], *, strategy_id: int = 0) -> LiveStrategyState:
    strategy_payload = dict(payload)
    strategy_payload["recovery_loss"] = 0.0
    strategy_payload["pending_live_tracks_recovery_loss"] = False
    strategy_payload["pending_live_trades"] = hydrate_pending_live_trades(
        strategy_payload.get("pending_live_trades")
    )
    strategy_payload["strategy9_signal_samples"] = hydrate_strategy9_signal_samples(
        strategy_payload.get("strategy9_signal_samples")
    )
    state = LiveStrategyState(**strategy_payload)
    normalize_pending_live_trades(state, strategy_id=strategy_id)
    return state


def apply_live_strategy_state_to_session_state(state: SessionState, strategy_state: LiveStrategyState) -> None:
    for field_name in _LIVE_STRATEGY_FIELD_NAMES:
        setattr(state, field_name, getattr(strategy_state, field_name))


def live_strategy_state_from_payload(payload: dict[str, Any]) -> LiveStrategyState:
    return LiveStrategyState(
        round_index=payload.get("round_index", 0),
        cash_pnl=payload.get("cash_pnl", 0.0),
        recovery_loss=0.0,
        consecutive_losses=payload.get("consecutive_losses", 0),
        consecutive_max_stake_skips=payload.get("consecutive_max_stake_skips", 0),
        signal_round_slug=payload.get("signal_round_slug"),
        signal_round_open_up_price=payload.get("signal_round_open_up_price"),
        signal_round_locked_side=payload.get("signal_round_locked_side"),
        strategy6_last_ofi_score=payload.get("strategy6_last_ofi_score"),
        strategy11_round_start_btc_price=payload.get("strategy11_round_start_btc_price"),
        strategy9_signal_samples=hydrate_strategy9_signal_samples(payload.get("strategy9_signal_samples")),
        stop_loss_count=payload.get("stop_loss_count", 0),
        daily_realized_pnl=payload.get("daily_realized_pnl", 0.0),
        current_day=payload.get("current_day"),
        pending_live_slug=payload.get("pending_live_slug"),
        pending_live_side=payload.get("pending_live_side"),
        pending_live_price=payload.get("pending_live_price"),
        pending_live_order_size=payload.get("pending_live_order_size"),
        pending_live_order_cost=payload.get("pending_live_order_cost"),
        pending_live_expected_profit=payload.get("pending_live_expected_profit"),
        pending_live_order_id=payload.get("pending_live_order_id"),
        pending_live_end_time=payload.get("pending_live_end_time"),
        pending_live_tracks_recovery_loss=False,
        pending_live_trades=hydrate_pending_live_trades(payload.get("pending_live_trades")),
        last_processed_live_event_slug=payload.get("last_processed_live_event_slug"),
    )


def trusted_legacy_live_strategy_id(payload: dict[str, Any], effective_live_strategy_ids: list[int]) -> int | None:
    if not effective_live_strategy_ids:
        return None
    for key in ("live_strategy_id", "strategy_id"):
        raw_value = payload.get(key)
        try:
            strategy_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if strategy_id in effective_live_strategy_ids:
            return strategy_id
    if len(effective_live_strategy_ids) == 1:
        return effective_live_strategy_ids[0]
    # Legacy single-strategy payloads do not encode strategy identity. Preserve the
    # configured runtime ordering instead of silently reassigning by numeric min().
    return effective_live_strategy_ids[0]


def hydrate_live_strategy_map(payload: dict[str, Any], effective_live_strategy_ids: list[int]) -> dict[int, LiveStrategyState]:
    raw_strategy_map = payload.get("live_strategies")
    if isinstance(raw_strategy_map, dict):
        hydrated_map = {
            int(raw_key): hydrate_live_strategy_state(raw_value, strategy_id=int(raw_key))
            for raw_key, raw_value in raw_strategy_map.items()
        }
        for strategy_id in effective_live_strategy_ids:
            hydrated_map.setdefault(strategy_id, LiveStrategyState())
        return hydrated_map

    if effective_live_strategy_ids:
        legacy_state = live_strategy_state_from_payload(payload)
        hydrated_map = {
            strategy_id: LiveStrategyState()
            for strategy_id in effective_live_strategy_ids
        }
        selected_strategy_id = trusted_legacy_live_strategy_id(payload, effective_live_strategy_ids)
        if selected_strategy_id is not None:
            normalize_pending_live_trades(legacy_state, strategy_id=selected_strategy_id)
            hydrated_map[selected_strategy_id] = legacy_state
        return hydrated_map

    return {}


def save_session_state(path: Path, state: SessionState) -> None:
    atomic_write_text(path, json.dumps(asdict(state), indent=2), encoding="utf-8")


def _load_session_state_legacy(path: Path) -> SessionState:
    if not path.exists():
        return SessionState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    pending_paper_trades = [
        item if isinstance(item, PendingPaperTrade) else PendingPaperTrade(**item)
        for item in payload.pop("pending_paper_trades", [])
    ]
    pending_live_trades = hydrate_pending_live_trades(payload.pop("pending_live_trades", []))
    strategy9_signal_samples = hydrate_strategy9_signal_samples(payload.pop("strategy9_signal_samples", []))
    raw_live_strategy_map = payload.pop("live_strategies", {})
    live_strategies = {}
    if isinstance(raw_live_strategy_map, dict):
        live_strategies = {
            int(raw_key): hydrate_live_strategy_state(raw_value, strategy_id=int(raw_key))
            for raw_key, raw_value in raw_live_strategy_map.items()
        }
    payload.pop("strategy_id", None)
    payload.pop("live_strategy_id", None)
    payload["recovery_loss"] = 0.0
    payload["pending_live_tracks_recovery_loss"] = False
    return SessionState(
        pending_paper_trades=pending_paper_trades,
        pending_live_trades=pending_live_trades,
        strategy9_signal_samples=strategy9_signal_samples,
        live_strategies=live_strategies,
        **payload,
    )


def load_session_state(
    path: Path,
    *,
    effective_paper_strategy_ids: list[int] | None = None,
    effective_live_strategy_ids: list[int] | None = None,
) -> SessionState:
    state = _load_session_state_legacy(path)
    selected_paper_strategy_ids = list(effective_paper_strategy_ids or [])
    selected_live_strategy_ids = list(effective_live_strategy_ids or [])
    if not path.exists():
        return state

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_paper_strategy_map = payload.get("paper_strategies")
    if selected_paper_strategy_ids:
        if not isinstance(raw_paper_strategy_map, dict):
            state.paper_strategies = {
                strategy_id: PaperStrategyState(
                    round_index=state.round_index,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    consecutive_max_stake_skips=state.consecutive_max_stake_skips,
                    signal_round_slug=state.signal_round_slug,
                    signal_round_open_up_price=state.signal_round_open_up_price,
                    signal_round_locked_side=state.signal_round_locked_side,
                    strategy6_last_ofi_score=state.strategy6_last_ofi_score,
                    strategy9_signal_samples=list(state.strategy9_signal_samples),
                    stop_loss_count=state.stop_loss_count,
                    daily_realized_pnl=state.daily_realized_pnl,
                    current_day=state.current_day,
                    pending_paper_trades=list(state.pending_paper_trades),
                    last_processed_paper_event_slug=state.last_processed_paper_event_slug,
                )
                for strategy_id in selected_paper_strategy_ids
            }
        else:
            hydrated_map: dict[int, PaperStrategyState] = {}
            for raw_key, raw_value in raw_paper_strategy_map.items():
                hydrated_map[int(raw_key)] = hydrate_paper_strategy_state(raw_value)
            for strategy_id in selected_paper_strategy_ids:
                hydrated_map.setdefault(strategy_id, PaperStrategyState())
            state.paper_strategies = hydrated_map

    if selected_live_strategy_ids:
        state.live_strategies = hydrate_live_strategy_map(payload, selected_live_strategy_ids)
        if not isinstance(payload.get("live_strategies"), dict):
            active_live_state = state.live_strategies.get(
                trusted_legacy_live_strategy_id(payload, selected_live_strategy_ids)
            )
        else:
            active_live_state = None
        if active_live_state is not None:
            apply_live_strategy_state_to_session_state(state, active_live_state)
    return state


def clone_session_state(state: SessionState) -> SessionState:
    payload = asdict(state)
    payload["pending_paper_trades"] = hydrate_pending_paper_trades(payload.get("pending_paper_trades"))
    payload["pending_live_trades"] = hydrate_pending_live_trades(payload.get("pending_live_trades"))
    payload["strategy9_signal_samples"] = hydrate_strategy9_signal_samples(payload.get("strategy9_signal_samples"))
    raw_strategy_map = payload.get("paper_strategies") or {}
    payload["paper_strategies"] = {
        int(raw_key): hydrate_paper_strategy_state(raw_value)
        for raw_key, raw_value in raw_strategy_map.items()
    }
    raw_live_strategy_map = payload.get("live_strategies") or {}
    payload["live_strategies"] = {
        int(raw_key): hydrate_live_strategy_state(raw_value, strategy_id=int(raw_key))
        for raw_key, raw_value in raw_live_strategy_map.items()
    }
    return SessionState(**payload)


def copy_session_state_into(target: SessionState, source: SessionState) -> None:
    payload = asdict(source)
    payload["pending_paper_trades"] = hydrate_pending_paper_trades(payload.get("pending_paper_trades"))
    payload["pending_live_trades"] = hydrate_pending_live_trades(payload.get("pending_live_trades"))
    payload["strategy9_signal_samples"] = hydrate_strategy9_signal_samples(payload.get("strategy9_signal_samples"))
    raw_strategy_map = payload.get("paper_strategies") or {}
    payload["paper_strategies"] = {
        int(raw_key): hydrate_paper_strategy_state(raw_value)
        for raw_key, raw_value in raw_strategy_map.items()
    }
    raw_live_strategy_map = payload.get("live_strategies") or {}
    payload["live_strategies"] = {
        int(raw_key): hydrate_live_strategy_state(raw_value, strategy_id=int(raw_key))
        for raw_key, raw_value in raw_live_strategy_map.items()
    }
    for field_name, value in payload.items():
        setattr(target, field_name, value)
