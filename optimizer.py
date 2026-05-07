from __future__ import annotations

import argparse
import json
import csv
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from itertools import product
from statistics import mean
from typing import Any, Callable, Iterable, Sequence

from atomic_io import atomic_write_text
from config import AppConfig, build_config_from_env_values, load_env_file_values
from backtest import run_backtest
from paper_evaluator import compare_paper_candidates_from_csv
from promotion_policy import evaluate_promotion
from walk_forward import WalkForwardWindow, build_walk_forward_windows


@dataclass(frozen=True, slots=True)
class OptimizerCandidate:
    candidate_id: str
    base_strategy_id: int
    params: dict[str, float | int | str]


def build_candidate_configs(
    cfg: AppConfig,
    *,
    strategy_ids: Iterable[int],
    target_profits: Iterable[float],
    max_price_thresholds: Iterable[float],
    strategy5_thresholds: Iterable[float],
) -> list[OptimizerCandidate]:
    candidates: list[OptimizerCandidate] = []
    for strategy_id, target_profit, max_price_threshold in product(strategy_ids, target_profits, max_price_thresholds):
        if strategy_id == 5:
            for momentum_threshold in strategy5_thresholds:
                params = {
                    "TARGET_PROFIT": float(target_profit),
                    "MAX_PRICE_THRESHOLD": float(max_price_threshold),
                    "SIGNAL_MOMENTUM_THRESHOLD": float(momentum_threshold),
                }
                candidates.append(
                    OptimizerCandidate(
                        candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}-sm{momentum_threshold}",
                        base_strategy_id=strategy_id,
                        params=params,
                    )
                )
        elif strategy_id == 7:
            for ofi_threshold, momentum_threshold, max_entry_price in product(
                [0.65, 0.7, 0.75],
                [0.02, 0.025, 0.03],
                [0.53, 0.54, 0.55],
            ):
                params = {
                    "TARGET_PROFIT": float(target_profit),
                    "BET_SIZING_MODE": "FLAT_BASE_COST",
                    "STRATEGY7_OFI_THRESHOLD": float(ofi_threshold),
                    "STRATEGY7_MOMENTUM_THRESHOLD": float(momentum_threshold),
                    "STRATEGY7_MAX_ENTRY_PRICE": float(max_entry_price),
                }
                candidates.append(
                    OptimizerCandidate(
                        candidate_id=f"s{strategy_id}-tp{target_profit}-ofi{ofi_threshold}-sm{momentum_threshold}-me{max_entry_price}",
                        base_strategy_id=strategy_id,
                        params=params,
                    )
                )
        else:
            params = {
                "TARGET_PROFIT": float(target_profit),
                "MAX_PRICE_THRESHOLD": float(max_price_threshold),
            }
            candidates.append(
                OptimizerCandidate(
                    candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}",
                    base_strategy_id=strategy_id,
                    params=params,
                )
            )
    return candidates


def rank_optimizer_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            float(item.get("validation_score", 0.0)),
            -float(item.get("max_drawdown", 0.0)),
            float(item.get("total_pnl", 0.0)),
        ),
        reverse=True,
    )


def evaluate_candidates_with_walk_forward(
    candidates: Sequence[dict[str, Any] | OptimizerCandidate],
    *,
    rows: Sequence[dict[str, Any]],
    windows: Sequence[WalkForwardWindow],
    scorer: Callable[[dict[str, Any], Sequence[dict[str, Any]], Sequence[dict[str, Any]]], dict[str, float]],
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = raw_candidate if isinstance(raw_candidate, dict) else {
            "candidate_id": raw_candidate.candidate_id,
            "base_strategy_id": raw_candidate.base_strategy_id,
            "params": dict(raw_candidate.params),
        }
        scores: list[dict[str, float]] = []
        for window in windows:
            train_rows = rows[window.train_start:window.train_end]
            validation_rows = rows[window.validation_start:window.validation_end]
            scores.append(scorer(candidate, train_rows, validation_rows))
        if scores:
            evaluated.append(
                {
                    **candidate,
                    "window_count": len(scores),
                    "total_pnl": mean(item.get("total_pnl", 0.0) for item in scores),
                    "max_drawdown": mean(item.get("max_drawdown", 0.0) for item in scores),
                    "validation_score": mean(item.get("validation_score", 0.0) for item in scores),
                }
            )
        else:
            evaluated.append(
                {
                    **candidate,
                    "window_count": 0,
                    "total_pnl": 0.0,
                    "max_drawdown": 0.0,
                    "validation_score": 0.0,
                }
            )
    return rank_optimizer_candidates(evaluated)


def build_optimizer_state(
    *,
    ranked_candidates: list[dict[str, Any]],
    champion_id: str | None,
    top_n: int,
    last_run_at: str,
) -> dict[str, Any]:
    active_challengers = [
        {
            "candidate_id": item.get("candidate_id"),
            "base_strategy_id": item.get("base_strategy_id"),
            "params": item.get("params") or {},
            "validation_score": item.get("validation_score"),
        }
        for item in ranked_candidates[: max(0, top_n)]
    ]
    promotable_candidates = [
        {
            "candidate_id": item.get("candidate_id"),
            "base_strategy_id": item.get("base_strategy_id"),
            "params": item.get("params") or {},
            "validation_score": item.get("validation_score"),
        }
        for item in ranked_candidates
        if bool(item.get("promotable"))
    ]
    return {
        "enabled": True,
        "last_run_at": last_run_at,
        "champion_id": champion_id,
        "active_challengers": active_challengers,
        "promotable_candidates": promotable_candidates,
    }


def save_optimizer_state(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2), encoding="utf-8")


def load_optimizer_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "enabled": False,
            "last_run_at": None,
            "champion_id": None,
            "active_challengers": [],
            "promotable_candidates": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid optimizer state payload in {path}")
    return payload


def run_optimizer_cycle(
    *,
    ranked_candidates: list[dict[str, Any]],
    champion_id: str | None,
    output_path: Path,
    top_n: int,
    last_run_at: str,
) -> dict[str, Any]:
    payload = build_optimizer_state(
        ranked_candidates=ranked_candidates,
        champion_id=champion_id,
        top_n=top_n,
        last_run_at=last_run_at,
    )
    save_optimizer_state(output_path, payload)
    return payload


def refresh_optimizer_state_from_paper_results(
    *,
    state_path: Path,
    paper_log_path: Path,
    min_trade_count: int,
    required_pnl_edge: float,
    max_drawdown_multiplier: float,
) -> dict[str, Any]:
    payload = load_optimizer_state(state_path)
    champion_id = payload.get("champion_id")
    active_challengers = payload.get("active_challengers")
    if not isinstance(active_challengers, list):
        active_challengers = []

    promotable_candidates: list[dict[str, Any]] = []
    refreshed_challengers: list[dict[str, Any]] = []
    for challenger in active_challengers:
        if not isinstance(challenger, dict):
            continue
        candidate_id = str(challenger.get("candidate_id") or "").strip()
        if not candidate_id or not champion_id:
            refreshed_challengers.append(challenger)
            continue
        metrics = compare_paper_candidates_from_csv(
            paper_log_path,
            champion_id=str(champion_id),
            challenger_id=candidate_id,
        )
        decision = evaluate_promotion(
            champion_trade_count=metrics.champion_trade_count,
            challenger_trade_count=metrics.challenger_trade_count,
            champion_total_pnl=metrics.champion_total_pnl,
            challenger_total_pnl=metrics.challenger_total_pnl,
            champion_max_drawdown=metrics.champion_max_drawdown,
            challenger_max_drawdown=metrics.challenger_max_drawdown,
            min_trade_count=min_trade_count,
            required_pnl_edge=required_pnl_edge,
            max_drawdown_multiplier=max_drawdown_multiplier,
        )
        updated = dict(challenger)
        updated["paper_metrics"] = {
            "champion_trade_count": metrics.champion_trade_count,
            "challenger_trade_count": metrics.challenger_trade_count,
            "champion_total_pnl": metrics.champion_total_pnl,
            "challenger_total_pnl": metrics.challenger_total_pnl,
            "champion_max_drawdown": metrics.champion_max_drawdown,
            "challenger_max_drawdown": metrics.challenger_max_drawdown,
            "challenger_advantage": metrics.challenger_advantage,
        }
        updated["promotion_decision"] = {
            "state": decision.state,
            "reason": decision.reason,
            "promotable": decision.promotable,
        }
        refreshed_challengers.append(updated)
        if decision.promotable:
            promotable_candidates.append(updated)

    payload["active_challengers"] = refreshed_challengers
    payload["promotable_candidates"] = promotable_candidates
    save_optimizer_state(state_path, payload)
    return payload


_CANDIDATE_PARAM_ATTR_MAP: dict[str, str] = {
    "TARGET_PROFIT": "target_profit",
    "BET_SIZING_MODE": "bet_sizing_mode",
    "MAX_PRICE_THRESHOLD": "max_price_threshold",
    "SIGNAL_MOMENTUM_THRESHOLD": "signal_momentum_threshold",
    "OFI_THRESHOLD": "ofi_threshold",
    "MAX_ENTRY_PRICE": "max_entry_price",
    "STRATEGY7_OFI_THRESHOLD": "strategy7_ofi_threshold",
    "STRATEGY7_MOMENTUM_THRESHOLD": "strategy7_momentum_threshold",
    "STRATEGY7_MAX_ENTRY_PRICE": "strategy7_max_entry_price",
}


def _candidate_to_config(base_cfg: AppConfig, candidate: dict[str, Any]) -> AppConfig:
    kwargs: dict[str, Any] = {"strategy_id": int(candidate["base_strategy_id"])}
    for key, value in (candidate.get("params") or {}).items():
        attr = _CANDIDATE_PARAM_ATTR_MAP.get(str(key))
        if attr:
            kwargs[attr] = value
    if "strategy7_max_entry_price" in kwargs and "max_entry_price" not in kwargs:
        kwargs["max_entry_price"] = kwargs["strategy7_max_entry_price"]
    return replace(base_cfg, **kwargs)


def score_candidate_with_backtest_rows(
    candidate: dict[str, Any],
    *,
    rows: Sequence[dict[str, Any]],
    base_cfg: AppConfig,
) -> dict[str, float]:
    if not rows:
        return {"total_pnl": 0.0, "max_drawdown": 0.0, "validation_score": 0.0, "trade_count": 0.0}

    cfg = _candidate_to_config(base_cfg, candidate)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", suffix=".csv", delete=False) as handle:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(handle.name)
    try:
        result = run_backtest(temp_path, cfg)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return {
        "total_pnl": float(result.total_pnl),
        "max_drawdown": float(result.max_drawdown),
        "validation_score": float(result.total_pnl - result.max_drawdown),
        "trade_count": float(result.trade_count),
    }


def run_optimizer_from_history_csv(
    *,
    csv_path: Path,
    base_cfg: AppConfig,
    output_path: Path,
    strategy_ids: Iterable[int],
    target_profits: Iterable[float],
    max_price_thresholds: Iterable[float],
    strategy5_thresholds: Iterable[float],
    train_size: int,
    validation_size: int,
    step_size: int,
    top_n: int,
    champion_id: str | None,
    last_run_at: str,
) -> dict[str, Any]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    windows = build_walk_forward_windows(
        rows,
        train_size=train_size,
        validation_size=validation_size,
        step_size=step_size,
    )
    candidates = build_candidate_configs(
        base_cfg,
        strategy_ids=strategy_ids,
        target_profits=target_profits,
        max_price_thresholds=max_price_thresholds,
        strategy5_thresholds=strategy5_thresholds,
    )
    ranked_candidates = evaluate_candidates_with_walk_forward(
        candidates,
        rows=rows,
        windows=windows,
        scorer=lambda candidate, _train_rows, validation_rows: score_candidate_with_backtest_rows(
            candidate,
            rows=validation_rows,
            base_cfg=base_cfg,
        ),
    )
    return run_optimizer_cycle(
        ranked_candidates=ranked_candidates,
        champion_id=champion_id,
        output_path=output_path,
        top_n=top_n,
        last_run_at=last_run_at,
    )


def run_optimizer_scheduler(
    *,
    csv_path: Path,
    paper_log_path: Path,
    base_cfg: AppConfig,
    output_path: Path,
    champion_id: str | None,
    optimize_interval_seconds: int,
    refresh_interval_seconds: int,
    poll_interval_seconds: int,
    max_loops: int | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    optimize_runner: Callable[..., dict[str, Any]] = run_optimizer_from_history_csv,
    refresh_runner: Callable[..., dict[str, Any]] = refresh_optimizer_state_from_paper_results,
) -> None:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleep_fn or (lambda seconds: __import__("time").sleep(seconds))
    next_optimize_at: datetime | None = None
    next_refresh_at: datetime | None = None
    loop_count = 0

    while max_loops is None or loop_count < max_loops:
        optimize_now = now_fn()
        if next_optimize_at is None or optimize_now >= next_optimize_at:
            optimize_runner(
                csv_path=csv_path,
                base_cfg=base_cfg,
                output_path=output_path,
                strategy_ids=[5, 6],
                target_profits=[0.8, 1.0, 1.2],
                max_price_thresholds=[0.55, 0.60, 0.65],
                strategy5_thresholds=[0.012, 0.015, 0.018],
                train_size=3,
                validation_size=3,
                step_size=3,
                top_n=3,
                champion_id=champion_id,
                last_run_at=optimize_now.isoformat(),
            )
            next_optimize_at = optimize_now + timedelta(seconds=optimize_interval_seconds)
        refresh_now = now_fn()
        if next_refresh_at is None or refresh_now >= next_refresh_at:
            refresh_runner(
                state_path=output_path,
                paper_log_path=paper_log_path,
                min_trade_count=2,
                required_pnl_edge=1.0,
                max_drawdown_multiplier=1.25,
            )
            next_refresh_at = refresh_now + timedelta(seconds=refresh_interval_seconds)
        loop_count += 1
        if max_loops is None or loop_count < max_loops:
            sleep_fn(float(poll_interval_seconds))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline paper strategy optimization from historical CSV.")
    parser.add_argument("--csv", required=True, dest="csv_path")
    parser.add_argument("--paper-log", default="logs/paper_trades.csv", dest="paper_log_path")
    parser.add_argument("--env-file", default=".env.dashboard", dest="env_file")
    parser.add_argument("--output", default="logs/optimizer_state.json", dest="output_path")
    parser.add_argument("--champion-id", default="champion-paper", dest="champion_id")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--optimize-interval-seconds", type=int, default=3600)
    parser.add_argument("--refresh-interval-seconds", type=int, default=900)
    parser.add_argument("--poll-interval-seconds", type=int, default=30)
    args = parser.parse_args(list(argv) if argv is not None else None)

    env_path = Path(args.env_file)
    csv_path = Path(args.csv_path)
    paper_log_path = Path(args.paper_log_path)
    output_path = Path(args.output_path)
    cfg = build_config_from_env_values(load_env_file_values(env_path))
    last_run_at = datetime.now(timezone.utc).isoformat()

    if args.watch:
        run_optimizer_scheduler(
            csv_path=csv_path,
            paper_log_path=paper_log_path,
            base_cfg=cfg,
            output_path=output_path,
            champion_id=args.champion_id,
            optimize_interval_seconds=int(args.optimize_interval_seconds),
            refresh_interval_seconds=int(args.refresh_interval_seconds),
            poll_interval_seconds=int(args.poll_interval_seconds),
        )
        return 0

    payload = run_optimizer_from_history_csv(
        csv_path=csv_path,
        base_cfg=cfg,
        output_path=output_path,
        strategy_ids=[5, 6],
        target_profits=[0.8, 1.0, 1.2],
        max_price_thresholds=[0.55, 0.60, 0.65],
        strategy5_thresholds=[0.012, 0.015, 0.018],
        train_size=3,
        validation_size=3,
        step_size=3,
        top_n=3,
        champion_id=args.champion_id,
        last_run_at=last_run_at,
    )
    print(f"Optimizer state written to {output_path}")
    print(f"Champion: {payload.get('champion_id')}")
    print(f"Active challengers: {len(payload.get('active_challengers') or [])}")
    print(f"Promotable candidates: {len(payload.get('promotable_candidates') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
