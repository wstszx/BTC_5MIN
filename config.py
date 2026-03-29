from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    series_id: int = 10684
    series_slug: str = "btc-up-or-down-5m"
    trade_mode: str = "paper"
    strategy_id: int = 1
    target_profit: float = 0.5
    max_consecutive_losses: int = 8
    max_stake: float = 25.0
    max_price_threshold: float = 0.65
    daily_loss_cap: float = 50.0
    poll_interval_seconds: int = 5
    entry_timing: str = "OPEN"
    open_delay_seconds: int = 5
    preclose_seconds: int = 30
    history_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    live_trading_enabled: bool = False
