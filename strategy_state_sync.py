from __future__ import annotations

from dataclasses import asdict

from live_pending import normalize_pending_live_trades
from models import LiveStrategyState, PaperStrategyState, SessionState
from state_manager import apply_live_strategy_state_to_session_state, live_strategy_state_from_payload


def strategy_has_pending_live_trade(strategy_state: LiveStrategyState | None) -> bool:
    return bool(
        strategy_state is not None
        and (
            strategy_state.pending_live_slug
            or strategy_state.pending_live_trades
        )
    )


def managed_live_strategy_ids(
    configured_strategy_ids: list[int],
    state: SessionState,
) -> list[int]:
    managed_strategy_ids = list(configured_strategy_ids)
    for strategy_id, strategy_state in state.live_strategies.items():
        if strategy_id in managed_strategy_ids:
            continue
        if strategy_has_pending_live_trade(strategy_state):
            managed_strategy_ids.append(strategy_id)
    return managed_strategy_ids


def paper_strategy_state_to_session_state(state: PaperStrategyState, base_state: SessionState) -> SessionState:
    return SessionState(
        round_index=state.round_index,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        consecutive_max_stake_skips=state.consecutive_max_stake_skips,
        signal_round_slug=state.signal_round_slug,
        signal_round_open_up_price=state.signal_round_open_up_price,
        signal_round_locked_side=state.signal_round_locked_side,
        strategy6_last_ofi_score=state.strategy6_last_ofi_score,
        strategy11_round_start_btc_price=state.strategy11_round_start_btc_price,
        strategy9_signal_samples=list(state.strategy9_signal_samples),
        stop_loss_count=state.stop_loss_count,
        daily_realized_pnl=state.daily_realized_pnl,
        current_day=state.current_day,
        last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        pending_live_slug=base_state.pending_live_slug,
        pending_live_side=base_state.pending_live_side,
        pending_live_price=base_state.pending_live_price,
        pending_live_order_size=base_state.pending_live_order_size,
        pending_live_order_cost=base_state.pending_live_order_cost,
        pending_live_expected_profit=base_state.pending_live_expected_profit,
        pending_live_order_id=base_state.pending_live_order_id,
        pending_live_end_time=base_state.pending_live_end_time,
        pending_live_tracks_recovery_loss=base_state.pending_live_tracks_recovery_loss,
        pending_live_trades=list(base_state.pending_live_trades),
        pending_paper_trades=list(state.pending_paper_trades),
        paper_strategies=dict(base_state.paper_strategies),
        live_strategies=dict(base_state.live_strategies),
    )


def session_state_to_paper_strategy_state(state: SessionState) -> PaperStrategyState:
    return PaperStrategyState(
        round_index=state.round_index,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        consecutive_max_stake_skips=state.consecutive_max_stake_skips,
        signal_round_slug=state.signal_round_slug,
        signal_round_open_up_price=state.signal_round_open_up_price,
        signal_round_locked_side=state.signal_round_locked_side,
        strategy6_last_ofi_score=state.strategy6_last_ofi_score,
        strategy11_round_start_btc_price=state.strategy11_round_start_btc_price,
        strategy9_signal_samples=list(state.strategy9_signal_samples),
        stop_loss_count=state.stop_loss_count,
        daily_realized_pnl=state.daily_realized_pnl,
        current_day=state.current_day,
        pending_paper_trades=list(state.pending_paper_trades),
        last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        experiment_id=getattr(state, "experiment_id", None),
    )


def ensure_paper_strategy_state_map(state: SessionState, strategy_ids: list[int]) -> None:
    if state.paper_strategies:
        for strategy_id in strategy_ids:
            state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
        return
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
            strategy11_round_start_btc_price=state.strategy11_round_start_btc_price,
            strategy9_signal_samples=list(state.strategy9_signal_samples),
            stop_loss_count=state.stop_loss_count,
            daily_realized_pnl=state.daily_realized_pnl,
            current_day=state.current_day,
            pending_paper_trades=list(state.pending_paper_trades),
            last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        )
        for strategy_id in strategy_ids
    }


def ensure_live_strategy_state_map(state: SessionState, strategy_ids: list[int]) -> None:
    if state.live_strategies:
        for strategy_id in strategy_ids:
            state.live_strategies.setdefault(strategy_id, LiveStrategyState())
        return
    legacy_state = live_strategy_state_from_payload(asdict(state))
    state.live_strategies = {
        strategy_id: LiveStrategyState()
        for strategy_id in strategy_ids
    }
    if strategy_ids:
        normalize_pending_live_trades(legacy_state, strategy_id=strategy_ids[0])
        state.live_strategies[strategy_ids[0]] = legacy_state


def sync_current_live_strategy_state(state: SessionState, strategy_id: int) -> None:
    if not state.live_strategies and strategy_id not in state.live_strategies:
        return
    strategy_state = live_strategy_state_from_payload(asdict(state))
    normalize_pending_live_trades(strategy_state, strategy_id=strategy_id)
    state.live_strategies[strategy_id] = strategy_state


def sync_legacy_paper_state_fields(state: SessionState, strategy_ids: list[int]) -> None:
    if not strategy_ids:
        return
    strategy_state = state.paper_strategies.get(strategy_ids[0])
    if strategy_state is None:
        return
    state.round_index = strategy_state.round_index
    state.cash_pnl = strategy_state.cash_pnl
    state.recovery_loss = strategy_state.recovery_loss
    state.consecutive_losses = strategy_state.consecutive_losses
    state.consecutive_max_stake_skips = strategy_state.consecutive_max_stake_skips
    state.signal_round_slug = strategy_state.signal_round_slug
    state.signal_round_open_up_price = strategy_state.signal_round_open_up_price
    state.signal_round_locked_side = strategy_state.signal_round_locked_side
    state.strategy6_last_ofi_score = strategy_state.strategy6_last_ofi_score
    state.strategy11_round_start_btc_price = strategy_state.strategy11_round_start_btc_price
    state.strategy9_signal_samples = list(strategy_state.strategy9_signal_samples)
    state.stop_loss_count = strategy_state.stop_loss_count
    state.daily_realized_pnl = strategy_state.daily_realized_pnl
    state.current_day = strategy_state.current_day
    state.last_processed_paper_event_slug = strategy_state.last_processed_paper_event_slug
    state.pending_paper_trades = list(strategy_state.pending_paper_trades)


def sync_legacy_live_state_fields(state: SessionState, strategy_ids: list[int]) -> None:
    if not strategy_ids:
        return
    prioritized_strategy_ids = [
        strategy_id
        for strategy_id in strategy_ids
        if strategy_has_pending_live_trade(state.live_strategies.get(strategy_id))
    ]
    prioritized_strategy_ids.extend(
        strategy_id
        for strategy_id in strategy_ids
        if strategy_id not in prioritized_strategy_ids
    )
    strategy_state = state.live_strategies.get(prioritized_strategy_ids[0])
    if strategy_state is None:
        return
    normalize_pending_live_trades(strategy_state, strategy_id=prioritized_strategy_ids[0])
    apply_live_strategy_state_to_session_state(state, strategy_state)
