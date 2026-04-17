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


def test_strategy_research_supports_strategy_7_candidates(tmp_path):
    csv_path = tmp_path / "strategy7_history.csv"
    csv_path.write_text(
        "\n".join(
            [
                "event_id,market_id,slug,title,series_id,start_time,end_time,price_to_beat,final_price,result,up_token_id,down_token_id,entry_price_open_up,entry_price_open_down,entry_price_preclose_up,entry_price_preclose_down,strategy6_ofi_score",
                "1,101,s7-a,Round A,10684,2026-03-29T00:00:00Z,2026-03-29T00:05:00Z,100,101,UP,up-a,down-a,0.50,0.50,0.54,0.46,0.80",
                "2,102,s7-b,Round B,10684,2026-03-29T00:05:00Z,2026-03-29T00:10:00Z,101,100,DOWN,up-b,down-b,0.50,0.50,0.46,0.54,-0.82",
            ]
        ),
        encoding="utf-8",
    )
    cfg = AppConfig(
        max_stake=25.0,
        max_price_threshold=0.65,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
    )

    report = run_strategy_research(
        csv_path,
        cfg,
        strategy_ids=[7],
        reset_rounds=[2],
        target_profits=[0.5],
        entry_timing="PRE_CLOSE",
        segments=2,
        top_n=1,
    )

    assert report.candidate_count == 1
    assert len(report.top_candidates) == 1
    assert report.top_candidates[0].strategy_id == 7
    assert report.top_candidates[0].trades == 2
    assert report.top_candidates[0].total_pnl > 0


def test_strategy_research_strategy_7_skips_rows_without_historical_ofi():
    cfg = AppConfig(max_stake=25.0)

    report = run_strategy_research(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_ids=[7],
        reset_rounds=[2],
        target_profits=[0.5],
    )

    assert report.candidate_count == 1
    assert report.top_candidates[0].strategy_id == 7
    assert report.top_candidates[0].trades == 0
    assert report.top_candidates[0].skipped == 6
