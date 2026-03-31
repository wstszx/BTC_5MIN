from __future__ import annotations

import csv
import warnings
from datetime import datetime, timezone
from pathlib import Path

from config import AppConfig
from models import BacktestResult, SessionState, TradeRecord
from polymarket_api import normalize_outcome_label, parse_iso_datetime
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from strategy import get_side_for_round


def _optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _select_entry_price(row: dict[str, str], side: str, entry_timing: str) -> float | None:
    timing = entry_timing.upper()
    side_key = side.lower()
    if timing == "PRE_CLOSE":
        return _optional_float(row.get(f"entry_price_preclose_{side_key}"))
    return _optional_float(row.get(f"entry_price_open_{side_key}"))


def _select_signal_current_up_price(row: dict[str, str], entry_timing: str) -> float | None:
    if entry_timing.upper() == "PRE_CLOSE":
        return _optional_float(row.get("entry_price_preclose_up"))
    return _optional_float(row.get("entry_price_open_up"))


def _signal_snapshot_overlap_ratio(rows: list[dict[str, str]], entry_timing: str) -> float:
    comparable = 0
    overlap = 0
    for row in rows:
        open_up = _optional_float(row.get("entry_price_open_up"))
        current_up = _select_signal_current_up_price(row, entry_timing)
        if open_up is None or current_up is None:
            continue
        comparable += 1
        if abs(open_up - current_up) < 1e-9:
            overlap += 1
    if comparable == 0:
        return 0.0
    return overlap / comparable


def _resolve_result(row: dict[str, str]) -> str:
    if row.get("result"):
        return normalize_outcome_label(row["result"])

    price_to_beat = _optional_float(row.get("price_to_beat"))
    final_price = _optional_float(row.get("final_price"))
    if price_to_beat is None or final_price is None:
        raise ValueError(f"Unable to resolve result for row {row.get('slug', '')}")
    return "UP" if final_price >= price_to_beat else "DOWN"


def _build_record(
    *,
    cfg: AppConfig,
    state: SessionState,
    row: dict[str, str],
    side: str,
    price: float | None,
    order_size: float,
    order_cost: float,
    expected_profit: float,
    result: str | None,
    trade_pnl: float,
    skip_reason: str | None = None,
    stop_loss_triggered: bool = False,
) -> TradeRecord:
    return TradeRecord(
        timestamp=datetime.now(timezone.utc),
        mode="backtest",
        round_index=state.round_index,
        strategy=cfg.strategy_id,
        entry_timing=cfg.entry_timing,
        event_slug=row.get("slug", ""),
        start_time=parse_iso_datetime(row.get("start_time")) or datetime.now(timezone.utc),
        end_time=parse_iso_datetime(row.get("end_time")) or datetime.now(timezone.utc),
        side=side,
        price=price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=expected_profit,
        result=result,
        trade_pnl=trade_pnl,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        stop_loss_triggered=stop_loss_triggered,
        skip_reason=skip_reason,
    )


def run_backtest(csv_path: Path, cfg: AppConfig | None = None) -> BacktestResult:
    cfg = cfg or AppConfig()
    state = SessionState()
    records: list[TradeRecord] = []
    skipped_round_count = 0
    trade_count = 0
    max_consecutive_losses_seen = 0
    max_drawdown = 0.0
    peak_pnl = 0.0

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if cfg.strategy_id == 5:
        overlap_ratio = _signal_snapshot_overlap_ratio(rows, cfg.entry_timing)
        if overlap_ratio >= 0.9:
            warnings.warn(
                (
                    "strategy_id=5 signal quality degraded in this CSV: "
                    f"{overlap_ratio:.1%} rows have identical open/current UP prices. "
                    "Momentum decision may effectively fall back to pattern logic."
                ),
                RuntimeWarning,
                stacklevel=2,
            )

    for row in rows:
        side = get_side_for_round(
            cfg.strategy_id,
            state.round_index,
            signal_open_up_price=_optional_float(row.get("entry_price_open_up")),
            signal_current_up_price=_select_signal_current_up_price(row, cfg.entry_timing),
            signal_threshold=cfg.signal_momentum_threshold,
            signal_fallback_strategy_id=cfg.signal_fallback_strategy_id,
        )
        price = _select_entry_price(row, side, cfg.entry_timing)

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

        if not plan.should_trade:
            if plan.stop_loss_triggered:
                state = reset_after_stop_loss(state)
            records.append(
                _build_record(
                    cfg=cfg,
                    state=state,
                    row=row,
                    side=side,
                    price=price,
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    skip_reason=plan.skip_reason,
                    stop_loss_triggered=plan.stop_loss_triggered,
                )
            )
            skipped_round_count += 1
            state.round_index += 1
            continue

        prior_cash = state.cash_pnl
        resolved_result = _resolve_result(row)
        updated_state = apply_round_outcome(state, plan, won=(resolved_result == side))
        updated_state.round_index = state.round_index + 1
        trade_pnl = updated_state.cash_pnl - prior_cash
        state = updated_state

        trade_count += 1
        max_consecutive_losses_seen = max(max_consecutive_losses_seen, state.consecutive_losses)
        peak_pnl = max(peak_pnl, state.cash_pnl)
        max_drawdown = max(max_drawdown, peak_pnl - state.cash_pnl)

        records.append(
            _build_record(
                cfg=cfg,
                state=state,
                row=row,
                side=side,
                price=plan.price,
                order_size=plan.order_size,
                order_cost=plan.order_cost,
                expected_profit=plan.expected_profit,
                result=resolved_result,
                trade_pnl=trade_pnl,
            )
        )

    average = state.cash_pnl / trade_count if trade_count else 0.0
    return BacktestResult(
        total_pnl=state.cash_pnl,
        max_consecutive_losses=max_consecutive_losses_seen,
        stop_loss_count=state.stop_loss_count,
        average_pnl_per_round=average,
        max_drawdown=max_drawdown,
        trade_count=trade_count,
        skipped_round_count=skipped_round_count,
        records=records,
    )
