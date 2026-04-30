from __future__ import annotations

from config import AppConfig
from models import SessionState
from risk_and_sizing import reset_after_stop_loss


def update_max_stake_skip_streak(
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


def emit_max_stake_skip_alert(
    *,
    slug: str,
    side: str,
    price: float | None,
    state: SessionState,
    cfg: AppConfig,
    skip_streak: int | None = None,
) -> None:
    printable_price = "N/A" if price is None else f"{price:.4f}"
    streak = state.consecutive_max_stake_skips if skip_streak is None else skip_streak
    print(
        "[WARN] order_cost_above_max_stake triggered "
        f"{streak} times | slug={slug} side={side} price={printable_price} "
        f"recovery_loss={state.recovery_loss:.4f} max_stake={cfg.max_stake:.4f} "
        f"max_consecutive_losses={cfg.max_consecutive_losses}"
    )


def should_reset_after_risk_gate_skip(
    state: SessionState,
    *,
    skip_reason: str | None,
    cfg: AppConfig,
    stop_loss_triggered: bool,
) -> bool:
    if stop_loss_triggered:
        return True
    if skip_reason != "order_cost_above_max_stake":
        return False
    return (state.consecutive_max_stake_skips + 1) >= max(1, cfg.max_consecutive_losses)


def apply_post_entry_risk_gate_skip(
    state: SessionState,
    *,
    skip_reason: str | None,
    cfg: AppConfig,
    stop_loss_triggered: bool,
) -> tuple[SessionState, bool, bool, int]:
    should_alert = update_max_stake_skip_streak(
        state,
        skip_reason=skip_reason,
        threshold=cfg.max_stake_skip_alert_threshold,
    )
    skip_streak = state.consecutive_max_stake_skips
    should_reset = stop_loss_triggered
    if not should_reset and skip_reason == "order_cost_above_max_stake":
        should_reset = state.consecutive_max_stake_skips >= max(1, cfg.max_consecutive_losses)
    if should_reset:
        state = reset_after_stop_loss(state)
        state.consecutive_max_stake_skips = 0
    state.round_index += 1
    return state, should_alert, should_reset, skip_streak
