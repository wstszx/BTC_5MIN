from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@contextmanager
def patched_env(overrides: dict[str, str]):
    previous: dict[str, str | None] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def build_config_from_env_values(values: dict[str, str]) -> AppConfig:
    with patched_env(values):
        return AppConfig()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class AppConfig:
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    series_id: int = 10684
    series_slug: str = "btc-up-or-down-5m"
    trade_mode: str = "paper"
    strategy_id: int = field(default_factory=lambda: _env_int("STRATEGY_ID", 2))
    target_profit: float = field(default_factory=lambda: _env_float("TARGET_PROFIT", 1.0))
    bet_sizing_mode: str = field(default_factory=lambda: (os.getenv("BET_SIZING_MODE") or "FIXED_BASE_COST").upper())
    base_order_cost: float = field(default_factory=lambda: _env_float("BASE_ORDER_COST", 1.0))
    max_consecutive_losses: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_LOSSES", 6))
    max_stake: float = field(default_factory=lambda: _env_float("MAX_STAKE", 15.0))
    max_price_threshold: float = field(default_factory=lambda: _env_float("MAX_PRICE_THRESHOLD", 0.65))
    signal_momentum_threshold: float = field(default_factory=lambda: _env_float("SIGNAL_MOMENTUM_THRESHOLD", 0.015))
    signal_fallback_strategy_id: int = field(default_factory=lambda: _env_int("SIGNAL_FALLBACK_STRATEGY_ID", 2))
    signal_weak_signal_mode: str = field(default_factory=lambda: (os.getenv("SIGNAL_WEAK_SIGNAL_MODE") or "SKIP").upper())
    signal_history_fidelity_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_HISTORY_FIDELITY_SECONDS", 5))
    signal_anchor_max_offset_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_ANCHOR_MAX_OFFSET_SECONDS", 20))
    signal_dynamic_threshold_k: float = field(default_factory=lambda: _env_float("SIGNAL_DYNAMIC_THRESHOLD_K", 1.5))
    signal_dynamic_threshold_min_points: int = field(default_factory=lambda: _env_int("SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS", 8))
    signal_lock_before_entry_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_LOCK_BEFORE_ENTRY_SECONDS", 20))
    max_stake_skip_alert_threshold: int = field(default_factory=lambda: _env_int("MAX_STAKE_SKIP_ALERT_THRESHOLD", 5))
    daily_loss_cap: float = 50.0
    poll_interval_seconds: int = 5
    ws_enabled: bool = field(default_factory=lambda: _env_bool("WS_ENABLED", True))
    ws_market_url: str = field(default_factory=lambda: os.getenv("WS_MARKET_URL") or "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    ws_quote_stale_seconds: int = field(default_factory=lambda: _env_int("WS_QUOTE_STALE_SECONDS", 3))
    ws_trade_guard_stale_seconds: float = field(default_factory=lambda: _env_float("WS_TRADE_GUARD_STALE_SECONDS", 1.5))
    ws_connect_timeout_seconds: int = field(default_factory=lambda: _env_int("WS_CONNECT_TIMEOUT_SECONDS", 5))
    ws_log_every_updates: int = field(default_factory=lambda: _env_int("WS_LOG_EVERY_UPDATES", 200))
    runtime_error_backoff_base_seconds: int = 5
    runtime_error_backoff_max_seconds: int = 60
    api_retry_count: int = 4
    api_retry_base_delay_seconds: float = 1.0
    api_retry_max_delay_seconds: float = 8.0
    entry_timing: str = "OPEN"
    open_delay_seconds: int = 5
    preclose_seconds: int = 30
    history_lookback_seconds: int = 900
    history_entry_fidelity_seconds: int = field(default_factory=lambda: _env_int("HISTORY_ENTRY_FIDELITY_SECONDS", 5))
    history_entry_max_offset_seconds: int = field(default_factory=lambda: _env_int("HISTORY_ENTRY_MAX_OFFSET_SECONDS", 120))
    history_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    live_trading_enabled: bool = field(default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False))
    live_private_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY"))
    live_chain_id: int = field(default_factory=lambda: _env_int("POLYMARKET_CHAIN_ID", 137))
    live_signature_type: int = field(default_factory=lambda: _env_int("POLYMARKET_SIGNATURE_TYPE", 0))
    live_funder: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER"))
    live_order_type: str = field(default_factory=lambda: (os.getenv("POLYMARKET_ORDER_TYPE") or "FOK").upper())
