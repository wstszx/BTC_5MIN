from __future__ import annotations

from models import PendingPaperTrade, SessionState
from settlement import resolved_result_from_official_market, settle_pending_paper_trade
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


def test_live_official_result_waits_for_final_price_when_price_to_beat_is_known():
    event = {
        "closed": True,
        "eventMetadata": {"priceToBeat": 78360.42348},
    }
    market = {
        "closed": True,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["1", "0"]',
    }

    assert resolved_result_from_official_market(event, market) is None
