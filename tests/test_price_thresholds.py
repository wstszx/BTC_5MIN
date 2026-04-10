from models import SessionState
from risk_and_sizing import build_trade_plan


def test_build_trade_plan_skips_when_price_below_threshold():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side='UP',
        price=0.35,
        target_profit=0.5,
        min_price_threshold=0.4,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
    )

    assert plan.should_trade is False
    assert plan.skip_reason == 'price_below_threshold'
