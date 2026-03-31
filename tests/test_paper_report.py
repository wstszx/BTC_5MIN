from __future__ import annotations

import csv
from pathlib import Path

from paper_report import summarize_paper_trades


def _write_rows(csv_path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "timestamp",
        "mode",
        "round_index",
        "strategy",
        "entry_timing",
        "event_slug",
        "start_time",
        "end_time",
        "side",
        "price",
        "order_size",
        "order_cost",
        "expected_profit",
        "result",
        "trade_pnl",
        "cash_pnl",
        "recovery_loss",
        "consecutive_losses",
        "stop_loss_triggered",
        "skip_reason",
        "signal_open_up_price",
        "signal_current_up_price",
        "signal_threshold",
        "signal_delta",
        "signal_locked",
        "signal_reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_summarize_paper_trades_builds_daily_metrics(tmp_path: Path):
    csv_path = tmp_path / "paper_trades.csv"
    _write_rows(
        csv_path,
        [
            {
                "timestamp": "2026-03-31T01:00:00+00:00",
                "mode": "paper",
                "round_index": "1",
                "strategy": "5",
                "entry_timing": "OPEN",
                "event_slug": "s1",
                "start_time": "2026-03-31T00:55:00+00:00",
                "end_time": "2026-03-31T01:00:00+00:00",
                "side": "UP",
                "price": "0.48",
                "order_size": "2.5",
                "order_cost": "1.2",
                "expected_profit": "1.0",
                "result": "UP",
                "trade_pnl": "1.2",
                "cash_pnl": "1.2",
                "recovery_loss": "0.0",
                "consecutive_losses": "0",
                "stop_loss_triggered": "False",
                "skip_reason": "",
                "signal_open_up_price": "0.50",
                "signal_current_up_price": "0.52",
                "signal_threshold": "0.01",
                "signal_delta": "0.02",
                "signal_locked": "True",
                "signal_reason": "",
            },
            {
                "timestamp": "2026-03-31T01:05:00+00:00",
                "mode": "paper",
                "round_index": "2",
                "strategy": "5",
                "entry_timing": "OPEN",
                "event_slug": "s2",
                "start_time": "2026-03-31T01:00:00+00:00",
                "end_time": "2026-03-31T01:05:00+00:00",
                "side": "DOWN",
                "price": "0.50",
                "order_size": "2.0",
                "order_cost": "1.0",
                "expected_profit": "1.0",
                "result": "UP",
                "trade_pnl": "-1.0",
                "cash_pnl": "0.2",
                "recovery_loss": "1.0",
                "consecutive_losses": "1",
                "stop_loss_triggered": "False",
                "skip_reason": "",
                "signal_open_up_price": "0.50",
                "signal_current_up_price": "0.47",
                "signal_threshold": "0.01",
                "signal_delta": "-0.03",
                "signal_locked": "False",
                "signal_reason": "",
            },
            {
                "timestamp": "2026-03-31T01:10:00+00:00",
                "mode": "paper",
                "round_index": "3",
                "strategy": "5",
                "entry_timing": "OPEN",
                "event_slug": "s3",
                "start_time": "2026-03-31T01:05:00+00:00",
                "end_time": "2026-03-31T01:10:00+00:00",
                "side": "SKIP",
                "price": "",
                "order_size": "0",
                "order_cost": "0",
                "expected_profit": "0",
                "result": "",
                "trade_pnl": "0",
                "cash_pnl": "0.2",
                "recovery_loss": "1.0",
                "consecutive_losses": "1",
                "stop_loss_triggered": "False",
                "skip_reason": "signal_too_weak_skip",
                "signal_open_up_price": "0.50",
                "signal_current_up_price": "0.502",
                "signal_threshold": "0.01",
                "signal_delta": "0.002",
                "signal_locked": "False",
                "signal_reason": "signal_too_weak_skip",
            },
        ],
    )

    summaries = summarize_paper_trades(csv_path, tz_offset="+00:00")
    assert len(summaries) == 1
    day = summaries[0]
    assert day.rows == 3
    assert day.trade_rows == 2
    assert day.skip_rows == 1
    assert day.wins == 1
    assert day.losses == 1
    assert round(day.hit_rate, 4) == 0.5
    assert round(day.total_pnl, 4) == 0.2
    assert round(day.avg_trade_pnl, 4) == 0.1
    assert round(day.max_drawdown, 4) == 1.0
    assert day.signal_rows == 3
    assert round(day.avg_abs_signal_delta, 4) == round((0.02 + 0.03 + 0.002) / 3, 4)
    assert round(day.strong_signal_rate, 4) == round(2 / 3, 4)
    assert round(day.signal_locked_rate, 4) == round(1 / 3, 4)
    assert day.skip_reason_counts["signal_too_weak_skip"] == 1


def test_summarize_paper_trades_respects_date_filter(tmp_path: Path):
    csv_path = tmp_path / "paper_trades.csv"
    _write_rows(
        csv_path,
        [
            {
                "timestamp": "2026-03-30T23:59:00+00:00",
                "mode": "paper",
                "round_index": "1",
                "strategy": "2",
                "entry_timing": "OPEN",
                "event_slug": "s1",
                "start_time": "2026-03-30T23:55:00+00:00",
                "end_time": "2026-03-31T00:00:00+00:00",
                "side": "UP",
                "price": "0.5",
                "order_size": "2",
                "order_cost": "1",
                "expected_profit": "1",
                "result": "UP",
                "trade_pnl": "1",
                "cash_pnl": "1",
                "recovery_loss": "0",
                "consecutive_losses": "0",
                "stop_loss_triggered": "False",
                "skip_reason": "",
                "signal_open_up_price": "",
                "signal_current_up_price": "",
                "signal_threshold": "",
                "signal_delta": "",
                "signal_locked": "False",
                "signal_reason": "",
            },
            {
                "timestamp": "2026-03-31T00:01:00+00:00",
                "mode": "paper",
                "round_index": "2",
                "strategy": "2",
                "entry_timing": "OPEN",
                "event_slug": "s2",
                "start_time": "2026-03-31T00:00:00+00:00",
                "end_time": "2026-03-31T00:05:00+00:00",
                "side": "DOWN",
                "price": "0.5",
                "order_size": "2",
                "order_cost": "1",
                "expected_profit": "1",
                "result": "UP",
                "trade_pnl": "-1",
                "cash_pnl": "0",
                "recovery_loss": "1",
                "consecutive_losses": "1",
                "stop_loss_triggered": "False",
                "skip_reason": "",
                "signal_open_up_price": "",
                "signal_current_up_price": "",
                "signal_threshold": "",
                "signal_delta": "",
                "signal_locked": "False",
                "signal_reason": "",
            },
        ],
    )

    summaries = summarize_paper_trades(
        csv_path,
        tz_offset="+00:00",
        start_date="2026-03-31",
        end_date="2026-03-31",
    )
    assert len(summaries) == 1
    assert summaries[0].date == "2026-03-31"
    assert round(summaries[0].total_pnl, 4) == -1.0
