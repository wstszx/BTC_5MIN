from __future__ import annotations

from dataclasses import asdict, replace

from config import AppConfig
from clob_adapter import resolve_live_order_type


def paper_strategy_ids_for_runtime(cfg: AppConfig) -> list[int]:
    strategy_ids = list(getattr(cfg, "paper_strategy_ids", []) or [])
    if strategy_ids:
        return strategy_ids
    return [cfg.strategy_id]


def live_strategy_ids_for_runtime(cfg: AppConfig) -> list[int]:
    strategy_ids = list(getattr(cfg, "live_strategy_ids", []) or [])
    if strategy_ids:
        return strategy_ids
    return [cfg.strategy_id]


def cfg_for_live_strategy(cfg: AppConfig, strategy_id: int) -> AppConfig:
    profile = getattr(cfg, "live_profiles", {}).get(strategy_id)
    if profile is None:
        return replace(cfg, strategy_id=strategy_id)
    overrides = asdict(profile)
    overrides["strategy_id"] = strategy_id
    return replace(cfg, **overrides)


def cfg_for_paper_strategy(cfg: AppConfig, strategy_id: int) -> AppConfig:
    profile = getattr(cfg, "paper_strategy_profiles", {}).get(strategy_id)
    if profile is None:
        return replace(cfg, strategy_id=strategy_id)
    overrides = asdict(profile)
    overrides["strategy_id"] = strategy_id
    return replace(cfg, **overrides)


def validate_live_runtime_config(cfg: AppConfig) -> None:
    if cfg.trade_mode != "live":
        return
    if not cfg.live_trading_enabled:
        raise RuntimeError("Live trading is disabled.")
    if not cfg.live_private_key:
        raise RuntimeError("Missing private key for live trading.")
    if not cfg.live_funder:
        raise RuntimeError("Missing POLYMARKET_FUNDER for live trading.")
    if (cfg.live_order_type or "FOK").upper() != "FOK":
        resolve_live_order_type(cfg.live_order_type)
