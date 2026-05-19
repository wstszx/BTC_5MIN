from __future__ import annotations

from datetime import datetime, timezone

from models import LiveStrategyState, PendingPaperTrade, SessionState
from settlement import (
    build_frozen_pending_live_plan,
    resolve_pending_live_result,
    resolved_result_from_official_market,
    settle_pending_live_trade_if_needed,
    settle_pending_paper_trade,
)
from trader import _settle_pending_paper_trade


def test_settlement_resolves_pending_paper_trade_and_trader_reexports_helper():
    class _ResolvedClient:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "eventMetadata": {},
                "closed": True,
                "markets": [
                    {
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                        "closed": True,
                    }
                ],
            }

    state = SessionState()
    item = PendingPaperTrade(
        round_index=0,
        event_slug="btc-updown-5m-settled",
        start_time="2026-04-30T01:00:00+00:00",
        end_time="2026-04-30T01:05:00+00:00",
        side="UP",
        price=0.5,
        order_size=2.0,
        order_cost=1.0,
        expected_profit=1.0,
        strategy=5,
        entry_timing="OPEN",
    )

    updated_state, result, trade_pnl = settle_pending_paper_trade(
        client=_ResolvedClient(),
        state=state,
        item=item,
    )

    assert result == "UP"
    assert trade_pnl == 1.0
    assert updated_state.cash_pnl == 1.0
    assert _settle_pending_paper_trade is settle_pending_paper_trade


def test_settlement_flat_pending_paper_trade_does_not_accrue_recovery_loss():
    class _ResolvedClient:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "eventMetadata": {},
                "closed": True,
                "markets": [
                    {
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                        "closed": True,
                    }
                ],
            }

    state = SessionState(recovery_loss=2.0, consecutive_losses=2)
    item = PendingPaperTrade(
        round_index=0,
        event_slug="btc-updown-5m-flat-settled",
        start_time="2026-04-30T01:00:00+00:00",
        end_time="2026-04-30T01:05:00+00:00",
        side="UP",
        price=0.5,
        order_size=2.0,
        order_cost=1.0,
        expected_profit=1.0,
        strategy=7,
        entry_timing="OPEN",
        tracks_recovery_loss=False,
    )

    updated_state, result, trade_pnl = settle_pending_paper_trade(
        client=_ResolvedClient(),
        state=state,
        item=item,
    )

    assert result == "DOWN"
    assert trade_pnl == -1.0
    assert updated_state.recovery_loss == 0.0
    assert updated_state.consecutive_losses == 3


def test_frozen_pending_live_plan_preserves_flat_sizing_recovery_policy():
    state = LiveStrategyState(
        pending_live_slug="btc-updown-5m-flat-live",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_tracks_recovery_loss=False,
    )

    plan = build_frozen_pending_live_plan(state)

    assert plan is not None
    assert plan.tracks_recovery_loss is False


def test_settle_pending_live_trade_flat_plan_does_not_accrue_recovery_loss():
    class _ResolvedClient:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "eventMetadata": {},
                "closed": True,
                "markets": [
                    {
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                        "closed": True,
                    }
                ],
            }

    strategy_state = LiveStrategyState(
        round_index=1,
        recovery_loss=2.0,
        consecutive_losses=2,
        pending_live_slug="btc-updown-5m-flat-live",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_end_time="2026-04-30T01:05:00+00:00",
        pending_live_tracks_recovery_loss=False,
    )

    updated_state, status, settled = settle_pending_live_trade_if_needed(
        market_client=_ResolvedClient(),
        clob_client=None,
        strategy_state=strategy_state,
        now=datetime(2026, 4, 30, 1, 6, tzinfo=timezone.utc),
    )

    assert settled is True
    assert status is not None
    assert status["result"] == "DOWN"
    assert updated_state.cash_pnl == -1.0
    assert updated_state.recovery_loss == 0.0
    assert updated_state.consecutive_losses == 3
    assert updated_state.pending_live_tracks_recovery_loss is False


def test_official_result_uses_terminal_prices_when_final_price_is_missing():
    event = {
        "closed": True,
        "eventMetadata": {"priceToBeat": 78360.42348},
    }
    market = {
        "closed": True,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
    }

    assert resolved_result_from_official_market(event, market) == "UP"


def test_official_result_waits_when_terminal_prices_are_not_final():
    event = {
        "closed": True,
        "eventMetadata": {"priceToBeat": 78360.42348},
    }
    market = {
        "closed": True,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.99", "0.01"]',
    }

    assert resolved_result_from_official_market(event, market) is None


def test_official_result_waits_when_closed_flag_is_not_boolean_true():
    event = {
        "closed": "false",
        "eventMetadata": {"priceToBeat": 78360.42348},
    }
    market = {
        "closed": "false",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
    }

    assert resolved_result_from_official_market(event, market) is None


def test_live_result_waits_for_final_price_when_price_to_beat_exists_even_with_clob_winner():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["1", "0"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            assert condition_id == "cond-1"
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": True},
                    {"outcome": "Down", "winner": False},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-settled",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-settled",
        "skip_reason": "round_unresolved",
    }


def test_live_result_keeps_pending_when_price_to_beat_market_is_not_closed():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": False,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": False,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0.49", "0.51"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": False,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0.49", "0.51"]',
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-in-progress",
    )

    assert result is None
    assert status == {
        "status": "pending_settlement",
        "slug": "btc-updown-5m-in-progress",
        "skip_reason": "round_unresolved",
    }


def test_live_numeric_btc_round_without_final_price_does_not_use_terminal_or_redeemable_fallback():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": False,
                "eventMetadata": None,
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": False,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0.135", "0.865"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": None,
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["1", "0"]',
            }

        def get_current_positions(self, *, user: str, redeemable: bool | None = None):
            return [
                {
                    "eventSlug": "btc-updown-5m-1777950900",
                    "proxyWallet": user,
                    "redeemable": True,
                    "size": 5,
                    "outcome": "Down",
                }
            ]

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder="0xabc",
        slug="btc-updown-5m-1777950900",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-1777950900",
        "skip_reason": "round_unresolved",
    }


def test_live_numeric_btc_round_requires_complete_final_price_metadata_pair():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": False,
                "eventMetadata": {"finalPrice": 80749.08769341912},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": False,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {"finalPrice": 80749.08769341912},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-1777950900",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-1777950900",
        "skip_reason": "round_unresolved",
    }


def test_live_numeric_btc_round_resolves_from_market_endpoint_final_price_pair():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "eventMetadata": None,
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {
                    "priceToBeat": 80664.57042,
                    "finalPrice": 80749.08769341912,
                },
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-1777950900",
    )

    assert result == "UP"
    assert status is None


def test_live_result_waits_for_final_price_even_after_consensus_delay():
    round_end = datetime(2026, 5, 5, 0, 5, tzinfo=timezone.utc)

    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "endDate": round_end.isoformat(),
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            assert condition_id == "cond-1"
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": False},
                    {"outcome": "Down", "winner": True},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-consensus",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-consensus",
        "skip_reason": "round_unresolved",
    }


def test_live_result_keeps_waiting_without_final_price_when_sources_disagree():
    round_end = datetime(2026, 5, 5, 0, 5, tzinfo=timezone.utc)

    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "endDate": round_end.isoformat(),
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["0", "1"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            assert condition_id == "cond-1"
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": True},
                    {"outcome": "Down", "winner": False},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-consensus",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-consensus",
        "skip_reason": "round_unresolved",
    }


def test_live_result_waits_for_final_price_when_endpoint_lacks_price_to_beat_but_has_winner():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "event-cond",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "endpoint-cond",
                "closed": True,
                "eventMetadata": {},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": condition_id == "event-cond"},
                    {"outcome": "Down", "winner": condition_id == "endpoint-cond"},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-endpoint-fallback",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-endpoint-fallback",
        "skip_reason": "round_unresolved",
    }


def test_live_result_waits_for_final_price_when_event_lacks_price_to_beat_but_endpoint_has_it():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "eventMetadata": {},
                "markets": [
                    {
                        "conditionId": "event-cond",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "endpoint-cond",
                "closed": True,
                "eventMetadata": {"priceToBeat": 80237.82148432496},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["1", "0"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": True},
                    {"outcome": "Down", "winner": False},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-endpoint-has-price-to-beat",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-endpoint-has-price-to-beat",
        "skip_reason": "round_unresolved",
    }


def test_live_result_waits_when_gamma_terminal_prices_have_no_official_winner():
    class _Client:
        def get_event_by_slug(self, slug: str):
            return {
                "slug": slug,
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "markets": [
                    {
                        "conditionId": "cond-1",
                        "closed": True,
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

        def get_market_by_slug(self, slug: str):
            return {
                "conditionId": "cond-1",
                "closed": True,
                "eventMetadata": {"priceToBeat": 78360.42348},
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["1", "0"]',
            }

        def get_clob_market_by_condition_id(self, condition_id: str):
            return {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": False},
                    {"outcome": "Down", "winner": False},
                ],
            }

    result, status = resolve_pending_live_result(
        market_client=_Client(),
        funder=None,
        slug="btc-updown-5m-unresolved",
    )

    assert result is None
    assert status == {
        "status": "awaiting_final_price",
        "slug": "btc-updown-5m-unresolved",
        "skip_reason": "round_unresolved",
    }
