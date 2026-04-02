from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState, TradeRecord
from trader import (
    _resolve_side_from_strategy,
    _update_max_stake_skip_streak,
    append_trade_log,
    place_live_order,
    run_paper_trading,
)


class _TransientPaperClient:
    def __init__(self):
        self.calls = 0

    def find_current_and_next_rounds(self, *, now):
        self.calls += 1
        if self.calls == 1:
            raise requests.exceptions.SSLError("temporary ssl failure")
        raise KeyboardInterrupt


class _LiveMarketClient:
    def find_current_and_next_rounds(self, *, now):
        window = MarketWindow(
            event_id="evt-1",
            market_id="mkt-1",
            slug="btc-updown-5m-test",
            title="BTC 5m Test",
            start_time=now - timedelta(minutes=1),
            end_time=now + timedelta(minutes=4),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None

    def get_market_by_slug(self, slug: str):
        return {
            "slug": slug,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.55", "0.45"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "bestBid": "0.54",
            "bestAsk": "0.56",
            "acceptingOrders": True,
        }

    def quote_from_market(self, _market):
        return MarketQuote(
            slug="btc-updown-5m-test",
            up_price=0.55,
            down_price=0.45,
            up_best_ask=0.56,
            fetched_at=datetime.now(timezone.utc),
        )


class _StubClobClient:
    def __init__(self):
        self.created_orders = []
        self.posted_orders = []

    def create_market_order(self, order_args):
        self.created_orders.append(order_args)
        return {"signed": True, "payload": order_args}

    def post_order(self, order, order_type):
        self.posted_orders.append((order, order_type))
        return {"success": True, "orderID": "oid-123"}


def test_run_paper_trading_continues_after_transient_exception(tmp_path, monkeypatch):
    monkeypatch.setattr("trader.time.sleep", lambda _seconds: None)
    cfg = AppConfig(poll_interval_seconds=1)
    client = _TransientPaperClient()

    with pytest.raises(KeyboardInterrupt):
        run_paper_trading(
            cfg,
            client=client,
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "paper.csv",
        )

    assert client.calls == 2


def test_place_live_order_dry_run_returns_order_plan(tmp_path):
    result = place_live_order(
        cfg=AppConfig(),
        market_client=_LiveMarketClient(),
        state_path=tmp_path / "state.json",
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert result["should_trade"] is True
    assert result["order_cost"] > 0


def test_place_live_order_submits_market_order_with_injected_clob(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient()

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
    )

    assert result["status"] == "submitted"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert result["order_id"] == "oid-123"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].side == "BUY"
    assert len(stub_clob.posted_orders) == 1


def test_place_live_order_requires_private_key_without_injected_client(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="PRIVATE_KEY"):
        place_live_order(
            cfg=cfg,
            market_client=_LiveMarketClient(),
            state_path=tmp_path / "state.json",
            dry_run=False,
        )


def test_update_max_stake_skip_streak_alerts_once_per_streak():
    state = SessionState()

    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is True
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert state.consecutive_max_stake_skips == 4

    assert _update_max_stake_skip_streak(state, skip_reason="invalid_price", threshold=3) is False
    assert state.consecutive_max_stake_skips == 0


def test_resolve_side_from_strategy_uses_quote_momentum_for_strategy_5():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_fallback_strategy_id=2,
        signal_weak_signal_mode="FALLBACK",
    )
    state = SessionState(round_index=0)

    first_quote = MarketQuote(slug="s1", up_best_ask=0.56, up_price=0.55)
    side_first = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=first_quote)
    assert side_first.side == "UP"
    assert state.signal_round_open_up_price == 0.55

    lower_quote = MarketQuote(slug="s1", up_best_ask=0.52, up_price=0.52)
    side_second = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=lower_quote)
    assert side_second.side == "DOWN"


def test_resolve_side_from_strategy_prefers_up_last_price_over_best_ask_for_signal():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_weak_signal_mode="SKIP",
    )
    state = SessionState(round_index=0)

    # Open anchor should come from up_price, not best_ask.
    first_quote = MarketQuote(slug="s1", up_best_ask=0.90, up_price=0.50)
    first = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=first_quote)
    assert first.side is None
    assert state.signal_round_open_up_price == 0.50

    # Even with extreme best_ask spike, side should be based on up_price momentum.
    second_quote = MarketQuote(slug="s1", up_best_ask=0.99, up_price=0.54)
    second = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=second_quote)
    assert second.side == "UP"
    assert second.signal_delta is not None
    assert second.signal_delta == pytest.approx(0.04)


def test_resolve_side_from_strategy_skips_weak_signal_when_mode_is_skip():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_weak_signal_mode="SKIP",
    )
    state = SessionState(round_index=0)

    quote = MarketQuote(slug="s1", up_best_ask=0.56, up_price=0.55)
    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote)

    assert decision.side is None
    assert decision.reason == "signal_too_weak_skip"


def test_resolve_side_from_strategy_locks_side_near_entry():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.01,
        signal_weak_signal_mode="SKIP",
        signal_lock_before_entry_seconds=20,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)

    up_quote = MarketQuote(slug="s1", up_best_ask=0.53, up_price=0.53)
    first = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=up_quote,
        now=now,
        entry_time=now + timedelta(seconds=5),
    )
    assert first.side == "UP"
    assert state.signal_round_locked_side == "UP"

    down_quote = MarketQuote(slug="s1", up_best_ask=0.45, up_price=0.45)
    second = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=down_quote,
        now=now + timedelta(seconds=2),
        entry_time=now + timedelta(seconds=5),
    )
    assert second.side == "UP"
    assert second.signal_locked is True


def test_append_trade_log_rotates_legacy_schema_file(tmp_path):
    log_path = tmp_path / "paper_trades.csv"
    log_path.write_text("timestamp,mode\n2026-03-31T00:00:00+00:00,paper\n", encoding="utf-8")

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="paper",
            round_index=1,
            strategy=5,
            entry_timing="OPEN",
            event_slug="s1",
            start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            end_time=datetime.now(timezone.utc),
            side="UP",
            price=0.5,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            result="UP",
            trade_pnl=1.0,
            cash_pnl=1.0,
            recovery_loss=0.0,
            consecutive_losses=0,
        ),
    )

    rotated = list(tmp_path.glob("paper_trades_legacy_*.csv"))
    assert len(rotated) == 1
    header = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert "signal_reason" in header
