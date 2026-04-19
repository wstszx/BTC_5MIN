from pathlib import Path

from backtest import run_backtest
from config import AppConfig
from polymarket_api import parse_outcome_prices


def test_parse_outcome_prices_maps_up_and_down():
    parsed = parse_outcome_prices('["0.555", "0.445"]', '["Up", "Down"]')
    assert parsed["UP"] == 0.555
    assert parsed["DOWN"] == 0.445


def test_backtest_returns_summary_metrics():
    cfg = AppConfig(
        strategy_id=1,
        max_consecutive_losses=2,
        target_profit=0.5,
        bet_sizing_mode="TARGET_PROFIT",
        max_stake=25.0,
        max_price_threshold=0.65,
    )
    result = run_backtest(Path("tests/fixtures/sample_history.csv"), cfg)
    assert result.trade_count == 4
    assert result.skipped_round_count == 2
    assert result.stop_loss_count == 1
    assert result.max_consecutive_losses == 2
    assert round(result.total_pnl, 4) == -0.5833
    assert round(result.max_drawdown, 4) == 1.5833


def test_backtest_supports_strategy_7_with_historical_ofi_and_momentum(tmp_path):
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
        strategy_id=7,
        entry_timing="PRE_CLOSE",
        max_stake=25.0,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
    )

    result = run_backtest(csv_path, cfg)

    assert result.trade_count == 2
    assert result.skipped_round_count == 0
    assert [record.side for record in result.records] == ["UP", "DOWN"]
    assert result.total_pnl > 0


def test_backtest_strategy_7_skips_rows_without_historical_ofi():
    cfg = AppConfig(strategy_id=7)

    result = run_backtest(Path("tests/fixtures/sample_history.csv"), cfg)

    assert result.trade_count == 0
    assert result.skipped_round_count == 6


def test_backtest_strategy_7_applies_optional_staleness_and_confirm_timing_gates(tmp_path):
    csv_path = tmp_path / "strategy7_timed_history.csv"
    csv_path.write_text(
        "\n".join(
            [
                "event_id,market_id,slug,title,series_id,start_time,end_time,price_to_beat,final_price,result,up_token_id,down_token_id,entry_price_open_up,entry_price_open_down,entry_price_preclose_up,entry_price_preclose_down,strategy6_ofi_score,strategy6_signal_at,quote_fetched_at",
                "1,101,s7-stale,Round A,10684,2026-03-29T00:00:00Z,2026-03-29T00:05:00Z,100,101,UP,up-a,down-a,0.50,0.50,0.54,0.46,0.80,2026-03-29T00:04:20Z,2026-03-29T00:04:25Z",
                "2,102,s7-late,Round B,10684,2026-03-29T00:05:00Z,2026-03-29T00:10:00Z,101,100,DOWN,up-b,down-b,0.50,0.50,0.46,0.54,-0.82,2026-03-29T00:09:27Z,2026-03-29T00:09:28Z",
            ]
        ),
        encoding="utf-8",
    )
    cfg = AppConfig(
        strategy_id=7,
        entry_timing="PRE_CLOSE",
        max_stake=25.0,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=3,
        binance_signal_stale_seconds=2.0,
    )

    result = run_backtest(csv_path, cfg)

    assert result.trade_count == 0
    assert result.skipped_round_count == 2
    assert [record.skip_reason for record in result.records] == [
        "strategy7_ofi_stale",
        "strategy7_entry_too_late",
    ]
