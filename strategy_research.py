from __future__ import annotations

import csv
import warnings
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from statistics import median
from typing import Iterable

from config import AppConfig
from polymarket_api import normalize_outcome_label
from strategy import get_side_for_round


@dataclass(slots=True)
class SegmentMetrics:
    pnl: float
    trades: int
    wins: int
    losses: int
    skipped: int
    max_drawdown: float
    max_loss_streak: int
    max_single_order_cost: float
    required_bankroll: float


@dataclass(slots=True)
class CandidateMetrics:
    strategy_id: int
    reset_round: int
    target_profit: float
    entry_timing: str
    total_pnl: float
    trades: int
    wins: int
    losses: int
    skipped: int
    hit_rate: float
    max_drawdown: float
    max_loss_streak: int
    max_single_order_cost: float
    required_bankroll: float
    recommended_bankroll: float
    profitable_segments: int
    segment_count: int
    worst_segment_pnl: float
    median_segment_pnl: float
    score: float


@dataclass(slots=True)
class StrategyResearchReport:
    csv_path: Path
    analyzed_round_count: int
    candidate_count: int
    top_candidates: list[CandidateMetrics]
    all_candidates: list[CandidateMetrics]


def _optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


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


def _resolve_result(row: dict[str, str]) -> str | None:
    if row.get("result"):
        return normalize_outcome_label(row["result"])

    price_to_beat = _optional_float(row.get("price_to_beat"))
    final_price = _optional_float(row.get("final_price"))
    if price_to_beat is None or final_price is None:
        return None
    return "UP" if final_price >= price_to_beat else "DOWN"


def _load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: row.get("start_time") or "")
    return rows


def _split_segments(rows: list[dict[str, str]], segments: int) -> list[tuple[int, list[dict[str, str]]]]:
    if segments <= 1 or len(rows) <= 1:
        return [(0, rows)]

    count = min(segments, len(rows))
    base = len(rows) // count
    remainder = len(rows) % count
    output: list[tuple[int, list[dict[str, str]]]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        end = start + size
        output.append((start, rows[start:end]))
        start = end
    return output


def _simulate_segment(
    rows: list[dict[str, str]],
    cfg: AppConfig,
    *,
    strategy_id: int,
    reset_round: int,
    target_profit: float,
    entry_timing: str,
    start_round_index: int,
) -> SegmentMetrics:
    pnl = 0.0
    recovery_loss = 0.0
    consecutive_losses = 0
    peak_pnl = 0.0
    max_drawdown = 0.0
    max_loss_streak = 0
    max_single_order_cost = 0.0
    required_bankroll = 0.0
    trades = 0
    wins = 0
    losses = 0
    skipped = 0

    round_index = start_round_index
    sizing_mode = cfg.bet_sizing_mode.upper()
    for row in rows:
        if consecutive_losses >= reset_round:
            recovery_loss = 0.0
            consecutive_losses = 0

        side = get_side_for_round(
            strategy_id,
            round_index,
            signal_open_up_price=_optional_float(row.get("entry_price_open_up")),
            signal_current_up_price=_select_signal_current_up_price(row, entry_timing),
            signal_threshold=cfg.signal_momentum_threshold,
            signal_fallback_strategy_id=cfg.signal_fallback_strategy_id,
        )
        round_index += 1

        price = _select_entry_price(row, side, entry_timing)
        if price is None or price <= 0 or price >= 1:
            skipped += 1
            continue
        if price > cfg.max_price_threshold:
            skipped += 1
            continue

        if sizing_mode == "FIXED_BASE_COST":
            base_order_cost = target_profit
            if base_order_cost <= 0:
                skipped += 1
                continue
            if recovery_loss <= 0:
                order_cost = base_order_cost
                order_size = order_cost / price
            else:
                expected_profit_target = recovery_loss + base_order_cost
                order_size = expected_profit_target / (1 - price)
                order_cost = order_size * price
        else:
            order_size = (recovery_loss + target_profit) / (1 - price)
            order_cost = order_size * price
        if order_cost > cfg.max_stake:
            skipped += 1
            continue

        result = _resolve_result(row)
        if result not in {"UP", "DOWN"}:
            skipped += 1
            continue

        trades += 1
        max_single_order_cost = max(max_single_order_cost, order_cost)
        required_bankroll = max(required_bankroll, max(0.0, order_cost - pnl))

        if result == side:
            profit = order_size * (1 - price)
            pnl += profit
            wins += 1
            recovery_loss = 0.0
            consecutive_losses = 0
        else:
            pnl -= order_cost
            losses += 1
            recovery_loss += order_cost
            consecutive_losses += 1
            max_loss_streak = max(max_loss_streak, consecutive_losses)

        peak_pnl = max(peak_pnl, pnl)
        max_drawdown = max(max_drawdown, peak_pnl - pnl)

    return SegmentMetrics(
        pnl=pnl,
        trades=trades,
        wins=wins,
        losses=losses,
        skipped=skipped,
        max_drawdown=max_drawdown,
        max_loss_streak=max_loss_streak,
        max_single_order_cost=max_single_order_cost,
        required_bankroll=required_bankroll,
    )


def run_strategy_research(
    csv_path: Path,
    cfg: AppConfig,
    *,
    strategy_ids: Iterable[int],
    reset_rounds: Iterable[int],
    target_profits: Iterable[float],
    entry_timing: str = "OPEN",
    segments: int = 5,
    bankroll_safety_multiplier: float = 1.5,
    top_n: int = 5,
) -> StrategyResearchReport:
    rows = _load_rows(csv_path)
    if not rows:
        raise ValueError(f"No rows found in CSV: {csv_path}")
    if bankroll_safety_multiplier < 1.0:
        raise ValueError("bankroll_safety_multiplier must be >= 1.0")
    if 5 in set(strategy_ids):
        overlap_ratio = _signal_snapshot_overlap_ratio(rows, entry_timing)
        if overlap_ratio >= 0.9:
            warnings.warn(
                (
                    "strategy_id=5 signal quality degraded in this CSV: "
                    f"{overlap_ratio:.1%} rows have identical open/current UP prices. "
                    "Research ranking may not reflect real intraround momentum behavior."
                ),
                RuntimeWarning,
                stacklevel=2,
            )

    segment_rows = _split_segments(rows, segments)
    candidates: list[CandidateMetrics] = []

    for strategy_id, reset_round, target_profit in product(strategy_ids, reset_rounds, target_profits):
        if reset_round < 1 or target_profit <= 0:
            continue

        full = _simulate_segment(
            rows,
            cfg,
            strategy_id=strategy_id,
            reset_round=reset_round,
            target_profit=target_profit,
            entry_timing=entry_timing,
            start_round_index=0,
        )

        segment_metrics = [
            _simulate_segment(
                segment,
                cfg,
                strategy_id=strategy_id,
                reset_round=reset_round,
                target_profit=target_profit,
                entry_timing=entry_timing,
                start_round_index=offset,
            )
            for offset, segment in segment_rows
        ]
        segment_pnls = [item.pnl for item in segment_metrics]
        profitable_segments = sum(1 for value in segment_pnls if value > 0)
        segment_count = len(segment_metrics)
        hit_rate = full.wins / full.trades if full.trades else 0.0
        recommended_bankroll = full.required_bankroll * bankroll_safety_multiplier
        roi = full.pnl / recommended_bankroll if recommended_bankroll > 0 else 0.0
        stability = profitable_segments / segment_count if segment_count else 0.0
        drawdown_ratio = full.max_drawdown / recommended_bankroll if recommended_bankroll > 0 else 0.0
        score = roi * 0.7 + stability * 0.3 - drawdown_ratio * 0.25

        candidates.append(
            CandidateMetrics(
                strategy_id=strategy_id,
                reset_round=reset_round,
                target_profit=target_profit,
                entry_timing=entry_timing.upper(),
                total_pnl=full.pnl,
                trades=full.trades,
                wins=full.wins,
                losses=full.losses,
                skipped=full.skipped,
                hit_rate=hit_rate,
                max_drawdown=full.max_drawdown,
                max_loss_streak=full.max_loss_streak,
                max_single_order_cost=full.max_single_order_cost,
                required_bankroll=full.required_bankroll,
                recommended_bankroll=recommended_bankroll,
                profitable_segments=profitable_segments,
                segment_count=segment_count,
                worst_segment_pnl=min(segment_pnls) if segment_pnls else 0.0,
                median_segment_pnl=median(segment_pnls) if segment_pnls else 0.0,
                score=score,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.score,
            item.total_pnl,
            item.profitable_segments,
            -item.recommended_bankroll,
        ),
        reverse=True,
    )
    return StrategyResearchReport(
        csv_path=csv_path,
        analyzed_round_count=len(rows),
        candidate_count=len(candidates),
        top_candidates=candidates[: max(1, top_n)] if candidates else [],
        all_candidates=candidates,
    )


def export_strategy_research_csv(output_path: Path, report: StrategyResearchReport) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "strategy_id",
        "reset_round",
        "target_profit",
        "entry_timing",
        "total_pnl",
        "trades",
        "wins",
        "losses",
        "skipped",
        "hit_rate",
        "max_drawdown",
        "max_loss_streak",
        "max_single_order_cost",
        "required_bankroll",
        "recommended_bankroll",
        "profitable_segments",
        "segment_count",
        "worst_segment_pnl",
        "median_segment_pnl",
        "score",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report.all_candidates:
            writer.writerow(
                {
                    "strategy_id": row.strategy_id,
                    "reset_round": row.reset_round,
                    "target_profit": row.target_profit,
                    "entry_timing": row.entry_timing,
                    "total_pnl": row.total_pnl,
                    "trades": row.trades,
                    "wins": row.wins,
                    "losses": row.losses,
                    "skipped": row.skipped,
                    "hit_rate": row.hit_rate,
                    "max_drawdown": row.max_drawdown,
                    "max_loss_streak": row.max_loss_streak,
                    "max_single_order_cost": row.max_single_order_cost,
                    "required_bankroll": row.required_bankroll,
                    "recommended_bankroll": row.recommended_bankroll,
                    "profitable_segments": row.profitable_segments,
                    "segment_count": row.segment_count,
                    "worst_segment_pnl": row.worst_segment_pnl,
                    "median_segment_pnl": row.median_segment_pnl,
                    "score": row.score,
                }
            )
    return output_path
