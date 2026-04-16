from promotion_policy import evaluate_promotion


def test_evaluate_promotion_marks_candidate_promotable_only_when_sample_and_drawdown_rules_pass():
    decision = evaluate_promotion(
        champion_trade_count=40,
        challenger_trade_count=42,
        champion_total_pnl=8.0,
        challenger_total_pnl=12.0,
        champion_max_drawdown=4.0,
        challenger_max_drawdown=4.5,
        min_trade_count=30,
        required_pnl_edge=2.0,
        max_drawdown_multiplier=1.25,
    )

    assert decision.state == "promotable"
    assert decision.promotable is True


def test_evaluate_promotion_rejects_candidate_with_too_few_trades():
    decision = evaluate_promotion(
        champion_trade_count=40,
        challenger_trade_count=8,
        champion_total_pnl=8.0,
        challenger_total_pnl=12.0,
        champion_max_drawdown=4.0,
        challenger_max_drawdown=3.0,
        min_trade_count=30,
        required_pnl_edge=2.0,
        max_drawdown_multiplier=1.25,
    )

    assert decision.state == "challenger"
    assert decision.promotable is False
