from pathlib import Path

from backtest import run_backtest
from config import AppConfig
from polymarket_api import parse_outcome_prices


def test_parse_outcome_prices_maps_up_and_down():
    parsed = parse_outcome_prices('["0.555", "0.445"]', '["Up", "Down"]')
    assert parsed["UP"] == 0.555
    assert parsed["DOWN"] == 0.445


def test_backtest_returns_summary_metrics():
    cfg = AppConfig(max_consecutive_losses=2)
    result = run_backtest(Path("tests/fixtures/sample_history.csv"), cfg)
    assert result.trade_count == 4
    assert result.skipped_round_count == 2
    assert result.stop_loss_count == 1
    assert result.max_consecutive_losses == 2
    assert round(result.total_pnl, 4) == -0.5833
    assert round(result.max_drawdown, 4) == 1.5833
