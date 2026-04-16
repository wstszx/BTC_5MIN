from pathlib import Path

from paper_evaluator import compare_paper_candidates, compare_paper_candidates_from_csv


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


def test_compare_paper_candidates_from_csv_reads_real_log_rows(tmp_path: Path):
    csv_path = tmp_path / "paper_trades.csv"
    csv_path.write_text(
        "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason,experiment_id\n"
        "2026-04-16T10:00:00+00:00,paper,1,5,OPEN,slug-a,2026-04-16T09:55:00+00:00,2026-04-16T10:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,1.0,0.0,0,False,,,,,,False,,champion\n"
        "2026-04-16T10:05:00+00:00,paper,2,5,OPEN,slug-b,2026-04-16T10:00:00+00:00,2026-04-16T10:05:00+00:00,DOWN,0.50,2.0,1.0,1.0,DOWN,1.5,2.5,0.0,0,False,,,,,,False,,challenger-a\n",
        encoding="utf-8",
    )

    metrics = compare_paper_candidates_from_csv(csv_path, champion_id="champion", challenger_id="challenger-a")

    assert metrics.champion_trade_count == 1
    assert metrics.challenger_trade_count == 1
    assert metrics.champion_total_pnl == 1.0
    assert metrics.challenger_total_pnl == 2.5
