from __future__ import annotations

import csv
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import AppConfig
from models import BacktestResult, MarketQuote, SessionState, TradeRecord
from polymarket_api import normalize_outcome_label, parse_iso_datetime
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from strategy import get_side_for_round, strategy7_strong_signal_allows_late_confirm
from strategy_decision import SideDecision, effective_decision_order_cost_multiplier, evaluate_strategy7_consensus_signal, resolve_side_from_strategy


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


def _select_ofi_score(row: dict[str, str]) -> float | None:
    return _optional_float(row.get("strategy6_ofi_score") or row.get("ofi_score"))


def _select_quote_fetched_at(row: dict[str, str]) -> datetime | None:
    return (
        parse_iso_datetime(row.get("quote_fetched_at"))
        or parse_iso_datetime(row.get("fetched_at"))
        or parse_iso_datetime(row.get("signal_observed_at"))
    )


def _select_strategy6_signal_at(row: dict[str, str]) -> datetime | None:
    return (
        parse_iso_datetime(row.get("strategy6_signal_at"))
        or parse_iso_datetime(row.get("signal_at"))
        or _select_quote_fetched_at(row)
    )


def _historical_entry_time(row: dict[str, str], cfg: AppConfig) -> datetime | None:
    start_time = parse_iso_datetime(row.get("start_time"))
    end_time = parse_iso_datetime(row.get("end_time"))
    if cfg.entry_timing.upper() == "PRE_CLOSE":
        if end_time is None:
            return None
        return end_time - timedelta(seconds=cfg.preclose_seconds)
    if start_time is None:
        return None
    return start_time + timedelta(seconds=cfg.open_delay_seconds)


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
    sizing_multiplier: float = 1.0,
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
        sizing_multiplier=sizing_multiplier,
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
        signal_open_up_price = _optional_float(row.get("entry_price_open_up"))
        signal_current_up_price = _select_signal_current_up_price(row, cfg.entry_timing)
        ofi_score = _select_ofi_score(row)
        if cfg.strategy_id in {7, 8, 9}:
            strategy_prefix = "strategy7" if cfg.strategy_id == 7 else ("strategy8" if cfg.strategy_id == 8 else "strategy9")
            quote_fetched_at = _select_quote_fetched_at(row)
            strategy6_signal_at = _select_strategy6_signal_at(row)
            if cfg.strategy_id == 9:
                historical_slug = row.get("slug", "")
                if state.signal_round_slug != historical_slug:
                    state.signal_round_slug = historical_slug
                    state.signal_round_open_up_price = signal_open_up_price
                    state.signal_round_locked_side = None
                    state.strategy9_signal_samples = []
                signal_quote_time = quote_fetched_at or strategy6_signal_at or _historical_entry_time(row, cfg) or datetime.now(timezone.utc)
                signal_quote = MarketQuote(
                    slug=historical_slug,
                    up_price=signal_current_up_price,
                    down_price=1 - signal_current_up_price if signal_current_up_price is not None else None,
                    up_best_ask=signal_current_up_price,
                    down_best_ask=1 - signal_current_up_price if signal_current_up_price is not None else None,
                    strategy6_ofi_score=ofi_score,
                    strategy6_signal_at=strategy6_signal_at or signal_quote_time,
                    fetched_at=quote_fetched_at or signal_quote_time,
                )
                side_decision = resolve_side_from_strategy(
                    cfg=cfg,
                    state=state,
                    slug=historical_slug,
                    quote=signal_quote,
                    now=signal_quote_time,
                    entry_time=_historical_entry_time(row, cfg),
                )
                if side_decision.side is None:
                    records.append(
                        _build_record(
                            cfg=cfg,
                            state=state,
                            row=row,
                            side="SKIP",
                            price=side_decision.candidate_price,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            skip_reason=side_decision.reason,
                        )
                    )
                    skipped_round_count += 1
                    state.round_index += 1
                    continue
                side = side_decision.side
            elif cfg.strategy_id == 7:
                signal_quote_time = quote_fetched_at or strategy6_signal_at or _historical_entry_time(row, cfg) or datetime.now(timezone.utc)
                signal_quote = MarketQuote(
                    slug=row.get("slug", ""),
                    strategy6_ofi_score=ofi_score,
                    strategy6_signal_at=strategy6_signal_at or signal_quote_time,
                    fetched_at=quote_fetched_at or signal_quote_time,
                )
                signal_check = evaluate_strategy7_consensus_signal(
                    cfg=cfg,
                    quote=signal_quote,
                    now=signal_quote_time,
                    signal_open_up_price=signal_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                )
                if signal_check.decision.side is None:
                    records.append(
                        _build_record(
                            cfg=cfg,
                            state=state,
                            row=row,
                            side="SKIP",
                            price=None,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            skip_reason=signal_check.decision.reason,
                        )
                    )
                    skipped_round_count += 1
                    state.round_index += 1
                    continue
                side = signal_check.decision.side
            else:
                try:
                    side = get_side_for_round(
                        cfg.strategy_id,
                        state.round_index,
                        signal_open_up_price=signal_open_up_price,
                        signal_current_up_price=signal_current_up_price,
                        signal_threshold=cfg.strategy7_momentum_threshold,
                        signal_fallback_strategy_id=cfg.signal_fallback_strategy_id,
                        ofi_score=ofi_score,
                        ofi_threshold=cfg.strategy7_ofi_threshold,
                        signal_min_gap=cfg.strategy7_min_signal_gap,
                    )
                except ValueError:
                    records.append(
                        _build_record(
                            cfg=cfg,
                            state=state,
                            row=row,
                            side="SKIP",
                            price=None,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            skip_reason=f"{strategy_prefix}_signal_unavailable",
                        )
                    )
                    skipped_round_count += 1
                    state.round_index += 1
                    continue
            if (
                cfg.strategy_id == 8
                and
                quote_fetched_at is not None
                and strategy6_signal_at is not None
                and (quote_fetched_at - strategy6_signal_at).total_seconds() > max(0.0, cfg.binance_signal_stale_seconds)
            ):
                records.append(
                    _build_record(
                        cfg=cfg,
                        state=state,
                        row=row,
                        side="SKIP",
                        price=None,
                        order_size=0.0,
                        order_cost=0.0,
                        expected_profit=0.0,
                        result=None,
                        trade_pnl=0.0,
                        skip_reason=f"{strategy_prefix}_ofi_stale",
                    )
                )
                skipped_round_count += 1
                state.round_index += 1
                continue
            entry_time = _historical_entry_time(row, cfg)
            effective_confirm_before_entry_seconds = max(0.0, float(cfg.strategy7_confirm_before_entry_seconds))
            if (
                ofi_score is not None
                and signal_open_up_price is not None
                and signal_current_up_price is not None
                and strategy7_strong_signal_allows_late_confirm(
                    ofi_score=ofi_score,
                    momentum_delta=signal_current_up_price - signal_open_up_price,
                    ofi_threshold=cfg.strategy7_ofi_threshold,
                    momentum_threshold=cfg.strategy7_momentum_threshold,
                    signal_min_gap=cfg.strategy7_min_signal_gap,
                    strong_signal_gap=cfg.strategy7_late_confirm_strong_signal_gap,
                )
            ):
                effective_confirm_before_entry_seconds = max(
                    0.0,
                    effective_confirm_before_entry_seconds - max(0.0, float(cfg.strategy7_late_confirm_relax_seconds)),
                )
            if (
                quote_fetched_at is not None
                and entry_time is not None
                and effective_confirm_before_entry_seconds > 0
                and (entry_time - quote_fetched_at).total_seconds() < effective_confirm_before_entry_seconds
            ):
                records.append(
                    _build_record(
                        cfg=cfg,
                        state=state,
                        row=row,
                        side="SKIP",
                        price=None,
                        order_size=0.0,
                        order_cost=0.0,
                        expected_profit=0.0,
                        result=None,
                        trade_pnl=0.0,
                        skip_reason=f"{strategy_prefix}_entry_too_late",
                    )
                )
                skipped_round_count += 1
                state.round_index += 1
                continue
        else:
            side = get_side_for_round(
                cfg.strategy_id,
                state.round_index,
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.signal_momentum_threshold,
                signal_fallback_strategy_id=cfg.signal_fallback_strategy_id,
                ofi_score=ofi_score,
                ofi_threshold=cfg.ofi_threshold,
            )
        price = _select_entry_price(row, side, cfg.entry_timing)
        if cfg.strategy_id in {7, 8, 9} and price is not None and price > getattr(cfg, "max_entry_price", cfg.max_price_threshold):
            strategy_prefix = "strategy7" if cfg.strategy_id == 7 else ("strategy8" if cfg.strategy_id == 8 else "strategy9")
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
                    skip_reason=f"{strategy_prefix}_price_too_high",
                )
            )
            skipped_round_count += 1
            state.round_index += 1
            continue

        plan = build_trade_plan(
            state=state,
            side=side,
            price=price,
            min_entry_price=getattr(cfg, "min_entry_price", getattr(cfg, "min_price_threshold", None)),
            max_entry_price=getattr(cfg, "max_entry_price", cfg.max_price_threshold),
            min_price_threshold=getattr(cfg, "min_price_threshold", None),
            max_price_threshold=cfg.max_price_threshold,
            min_stake=getattr(cfg, "min_stake", None),
            max_stake=cfg.max_stake,
            max_consecutive_losses=cfg.max_consecutive_losses,
            base_order_cost=cfg.base_order_cost,
            order_cost_multiplier=effective_decision_order_cost_multiplier(
                cfg=cfg,
                decision=SideDecision(
                    side=side,
                    signal_delta=signal_current_up_price - signal_open_up_price
                    if signal_current_up_price is not None and signal_open_up_price is not None
                    else None,
                    ofi_score=ofi_score,
                ),
                price=price,
            )
            if cfg.strategy_id in {7, 9}
            else 1.0,
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
                sizing_multiplier=plan.order_cost_multiplier,
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
