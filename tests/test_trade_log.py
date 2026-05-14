from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from models import TradeRecord
from trade_log import append_trade_log
from trader import append_trade_log as trader_append_trade_log


def test_trade_log_appends_records_and_trader_reexports_helper(tmp_path):
    log_path = tmp_path / "paper_trades.csv"
    start = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=30),
            mode="paper",
            round_index=1,
            strategy=5,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-paper",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            side="UP",
            price=0.5,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            result="UP",
            trade_pnl=1.0,
            cash_pnl=1.0,
        ),
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert "experiment_id" in lines[0]
    assert "btc-updown-5m-paper" in lines[1]
    assert trader_append_trade_log is append_trade_log


def test_trade_log_updates_existing_paper_strategy_round_instead_of_duplicating(tmp_path):
    log_path = tmp_path / "paper_trades.csv"
    start = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=1),
            mode="paper",
            round_index=849,
            strategy=7,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-1778162400",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            side="DOWN",
            price=0.54,
            order_size=5.217391304347826,
            order_cost=2.8173913043478263,
            expected_profit=2.4,
            result="DOWN",
            trade_pnl=2.4,
            cash_pnl=67.43538189344434,
            recovery_loss=0.0,
        ),
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=2),
            mode="paper",
            round_index=849,
            strategy=7,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-1778162400",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            side="DOWN",
            price=0.54,
            order_size=2.222222222222222,
            order_cost=1.2,
            expected_profit=1.022222222222222,
            result="DOWN",
            trade_pnl=1.022222222222222,
            cash_pnl=68.45760411566657,
            recovery_loss=0.0,
        ),
    )

    rows = list(csv.DictReader(log_path.open(encoding="utf-8", newline="")))

    assert len(rows) == 1
    assert rows[0]["order_cost"] == "1.2"
    assert rows[0]["cash_pnl"] == "68.45760411566657"


def test_trade_log_migrates_existing_rows_when_new_columns_are_inserted(tmp_path):
    log_path = tmp_path / "paper_trades.csv"
    legacy_header = [
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
        "experiment_id",
        "balance_error",
        "tracks_recovery_loss",
    ]
    legacy_row = [
        "2026-05-13T03:45:13+00:00",
        "paper",
        "1528",
        "7",
        "OPEN",
        "btc-updown-5m-legacy",
        "2026-05-13T03:45:00+00:00",
        "2026-05-13T03:50:00+00:00",
        "SKIP",
        "",
        "0.0",
        "0.0",
        "0.0",
        "",
        "0.0",
        "2.856212545300159",
        "0.0",
        "0",
        "False",
        "strategy7_ofi_too_weak",
        "0.505",
        "0.66",
        "0.62",
        "0.3230945968990371",
        "False",
        "strategy7_ofi_too_weak",
        "strategy-7",
        "",
        "True",
    ]
    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(legacy_header)
        writer.writerow(legacy_row)

    start = datetime(2026, 5, 14, 1, 45, tzinfo=timezone.utc)
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=13),
            mode="paper",
            round_index=1790,
            strategy=7,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-current",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            side="SKIP",
            price=None,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            skip_reason="strategy7_ofi_too_weak",
            signal_max_entry_price=None,
            sizing_multiplier=1.0,
            experiment_id="strategy-7",
        ),
    )

    assert list(tmp_path.glob("paper_trades_legacy_*.csv")) == []
    rows = list(csv.DictReader(log_path.open(encoding="utf-8", newline="")))
    assert [row["event_slug"] for row in rows] == [
        "btc-updown-5m-legacy",
        "btc-updown-5m-current",
    ]
    assert rows[0]["experiment_id"] == "strategy-7"
    assert rows[0]["signal_max_entry_price"] == ""
    assert rows[0]["sizing_multiplier"] == ""
