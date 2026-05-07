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
