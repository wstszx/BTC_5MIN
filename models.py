from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class MarketWindow:
    event_id: str
    market_id: str
    slug: str
    title: str
    start_time: datetime
    end_time: datetime
    up_token_id: str | None = None
    down_token_id: str | None = None


@dataclass(slots=True)
class MarketQuote:
    slug: str
    source: str = "http"
    up_price: float | None = None
    down_price: float | None = None
    up_best_bid: float | None = None
    up_best_ask: float | None = None
    down_best_bid: float | None = None
    down_best_ask: float | None = None
    accepting_orders: bool = False
    fetched_at: datetime | None = None


@dataclass(slots=True)
class ResolvedRound:
    event_id: str
    market_id: str
    slug: str
    title: str
    start_time: datetime
    end_time: datetime
    price_to_beat: float
    final_price: float
    result: str
    up_token_id: str | None = None
    down_token_id: str | None = None
    entry_price_open_up: float | None = None
    entry_price_open_down: float | None = None
    entry_price_preclose_up: float | None = None
    entry_price_preclose_down: float | None = None


@dataclass(slots=True)
class TradePlan:
    should_trade: bool
    side: str
    price: float | None = None
    order_size: float = 0.0
    order_cost: float = 0.0
    expected_profit: float = 0.0
    skip_reason: str | None = None
    stop_loss_triggered: bool = False


@dataclass(slots=True)
class TradeRecord:
    timestamp: datetime
    mode: str
    round_index: int
    strategy: int
    entry_timing: str
    event_slug: str
    start_time: datetime
    end_time: datetime
    side: str
    price: float | None
    order_size: float
    order_cost: float
    expected_profit: float
    result: str | None = None
    trade_pnl: float = 0.0
    cash_pnl: float = 0.0
    recovery_loss: float = 0.0
    consecutive_losses: int = 0
    stop_loss_triggered: bool = False
    skip_reason: str | None = None
    signal_open_up_price: float | None = None
    signal_current_up_price: float | None = None
    signal_threshold: float | None = None
    signal_delta: float | None = None
    signal_locked: bool = False
    signal_reason: str | None = None


@dataclass(slots=True)
class SessionState:
    round_index: int = 0
    cash_pnl: float = 0.0
    recovery_loss: float = 0.0
    consecutive_losses: int = 0
    consecutive_max_stake_skips: int = 0
    signal_round_slug: str | None = None
    signal_round_open_up_price: float | None = None
    signal_round_locked_side: str | None = None
    stop_loss_count: int = 0
    daily_realized_pnl: float = 0.0
    current_day: str | None = None
    pending_live_slug: str | None = None
    pending_live_side: str | None = None
    pending_live_price: float | None = None
    pending_live_order_size: float | None = None
    pending_live_order_cost: float | None = None
    pending_live_expected_profit: float | None = None
    pending_live_order_id: str | None = None
    pending_live_end_time: str | None = None


@dataclass(slots=True)
class BacktestResult:
    total_pnl: float = 0.0
    max_consecutive_losses: int = 0
    stop_loss_count: int = 0
    average_pnl_per_round: float = 0.0
    max_drawdown: float = 0.0
    trade_count: int = 0
    skipped_round_count: int = 0
    records: list[TradeRecord] = field(default_factory=list)
