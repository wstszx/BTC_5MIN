from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from config import AppConfig
from models import PaperStrategyState
from optimizer import load_optimizer_state, save_optimizer_state
from state_manager import hydrate_paper_strategy_state


def paper_experiment_id(strategy_id: int, strategy_state: PaperStrategyState) -> str:
    experiment_id = str(strategy_state.experiment_id or "").strip()
    if not experiment_id:
        experiment_id = f"strategy-{strategy_id}"
        strategy_state.experiment_id = experiment_id
    return experiment_id


def candidate_cfg_with_params(base_cfg: AppConfig, base_strategy_id: int, params: dict[str, Any] | None) -> AppConfig:
    params = params or {}
    kwargs: dict[str, Any] = {"strategy_id": int(base_strategy_id)}
    if "BASE_ORDER_COST" in params:
        kwargs["base_order_cost"] = float(params["BASE_ORDER_COST"])
    if "MAX_PRICE_THRESHOLD" in params:
        kwargs["max_price_threshold"] = float(params["MAX_PRICE_THRESHOLD"])
    if "SIGNAL_MOMENTUM_THRESHOLD" in params:
        kwargs["signal_momentum_threshold"] = float(params["SIGNAL_MOMENTUM_THRESHOLD"])
    if "OFI_THRESHOLD" in params:
        kwargs["ofi_threshold"] = float(params["OFI_THRESHOLD"])
    if "MAX_ENTRY_PRICE" in params:
        kwargs["max_entry_price"] = float(params["MAX_ENTRY_PRICE"])
    if "STRATEGY7_OFI_THRESHOLD" in params:
        kwargs["strategy7_ofi_threshold"] = float(params["STRATEGY7_OFI_THRESHOLD"])
    if "STRATEGY7_MOMENTUM_THRESHOLD" in params:
        kwargs["strategy7_momentum_threshold"] = float(params["STRATEGY7_MOMENTUM_THRESHOLD"])
    if "STRATEGY7_MAX_ENTRY_PRICE" in params:
        kwargs["strategy7_max_entry_price"] = float(params["STRATEGY7_MAX_ENTRY_PRICE"])
        if "MAX_ENTRY_PRICE" not in params:
            kwargs["max_entry_price"] = float(params["STRATEGY7_MAX_ENTRY_PRICE"])
    return replace(base_cfg, **kwargs)


def load_active_optimizer_challengers(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = load_optimizer_state(path)
    active = payload.get("active_challengers")
    challengers = active if isinstance(active, list) else []
    for challenger in challengers:
        if not isinstance(challenger, dict):
            continue
        raw_state = challenger.get("paper_state")
        if isinstance(raw_state, dict):
            paper_state = hydrate_paper_strategy_state(raw_state)
        else:
            paper_state = PaperStrategyState()
        paper_state.experiment_id = str(challenger.get("candidate_id") or paper_state.experiment_id or "").strip() or None
        challenger["_paper_state"] = paper_state
    return payload, [item for item in challengers if isinstance(item, dict)]


def save_active_optimizer_challengers(path: Path, payload: dict[str, Any], challengers: list[dict[str, Any]]) -> None:
    serialized = dict(payload)
    serialized["active_challengers"] = []
    for challenger in challengers:
        item = {key: value for key, value in challenger.items() if key != "_paper_state"}
        paper_state = challenger.get("_paper_state")
        if isinstance(paper_state, PaperStrategyState):
            item["paper_state"] = asdict(paper_state)
        serialized["active_challengers"].append(item)
    save_optimizer_state(path, serialized)
