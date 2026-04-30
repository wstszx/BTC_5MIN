from __future__ import annotations

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
