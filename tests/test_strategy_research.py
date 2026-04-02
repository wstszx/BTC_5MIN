from pathlib import Path

from config import AppConfig
from strategy_research import run_strategy_research


def test_strategy_research_returns_all_candidate_combinations():
    cfg = AppConfig(max_stake=25.0, max_price_threshold=0.65)
    report = run_strategy_research(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_ids=[1, 2],
        reset_rounds=[2, 3],
        target_profits=[0.5],
        segments=3,
        top_n=2,
    )

    assert report.analyzed_round_count == 6
    assert report.candidate_count == 4
    assert len(report.top_candidates) == 2
    assert report.top_candidates[0].score >= report.top_candidates[1].score
    assert all(item.segment_count == 3 for item in report.all_candidates)


def test_strategy_research_bankroll_scales_with_target_profit():
    cfg = AppConfig(max_stake=25.0, max_price_threshold=0.65)
    report = run_strategy_research(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_ids=[1],
        reset_rounds=[3],
        target_profits=[0.5, 1.0],
        segments=2,
        top_n=2,
    )

    by_profit = {item.target_profit: item for item in report.all_candidates}
    assert by_profit[1.0].required_bankroll >= by_profit[0.5].required_bankroll
    assert by_profit[1.0].recommended_bankroll >= by_profit[1.0].required_bankroll


def test_strategy_research_fixed_base_cost_uses_base_order_cost_not_target_profit():
    cfg = AppConfig(
        max_stake=25.0,
        max_price_threshold=0.65,
        bet_sizing_mode="FIXED_BASE_COST",
        base_order_cost=1.0,
    )
    report = run_strategy_research(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_ids=[1],
        reset_rounds=[3],
        target_profits=[0.5, 2.0],
        segments=2,
        top_n=2,
    )

    by_profit = {item.target_profit: item for item in report.all_candidates}
    assert by_profit[2.0].required_bankroll == by_profit[0.5].required_bankroll
    assert by_profit[2.0].total_pnl == by_profit[0.5].total_pnl
