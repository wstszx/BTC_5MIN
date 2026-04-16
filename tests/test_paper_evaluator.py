from paper_evaluator import compare_paper_candidates


def test_compare_paper_candidates_summarizes_champion_and_challenger_rows():
    rows = [
        {"experiment_id": "champion", "trade_pnl": "1.0", "cash_pnl": "1.0"},
        {"experiment_id": "champion", "trade_pnl": "-0.5", "cash_pnl": "0.5"},
        {"experiment_id": "challenger-a", "trade_pnl": "1.5", "cash_pnl": "1.5"},
        {"experiment_id": "challenger-a", "trade_pnl": "0.5", "cash_pnl": "2.0"},
    ]

    metrics = compare_paper_candidates(rows, champion_id="champion", challenger_id="challenger-a")

    assert metrics.champion_trade_count == 2
    assert metrics.challenger_trade_count == 2
    assert metrics.champion_total_pnl == 0.5
    assert metrics.challenger_total_pnl == 2.0
    assert metrics.challenger_advantage == 1.5
