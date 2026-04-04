from __future__ import annotations

import csv
from pathlib import Path

import pytest

from config import AppConfig
import main
from test_table_builder import build_augmented_test_table


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_build_augmented_test_table_fills_missing_prices_and_adds_summary(tmp_path: Path):
    input_csv = tmp_path / "input.csv"
    input_csv.write_text(
        (
            "event_id,market_id,slug,title,series_id,start_time,end_time,price_to_beat,final_price,result,"
            "up_token_id,down_token_id,up_last_price,down_last_price,up_best_bid,up_best_ask,down_best_bid,down_best_ask,"
            "entry_price_open_up,entry_price_open_down,entry_price_preclose_up,entry_price_preclose_down\n"
            "1,101,r1,Round1,10684,2026-03-29T00:00:00Z,2026-03-29T00:05:00Z,100,101,UP,up,down,0.5,0.5,0.49,0.50,0.49,0.50,,,,\n"
            "2,102,r2,Round2,10684,2026-03-29T00:05:00Z,2026-03-29T00:10:00Z,101,100,DOWN,up,down,0.5,0.5,0.49,0.50,0.49,0.50,0.52,0.48,0.53,0.47\n"
        ),
        encoding="utf-8",
    )

    output_csv = tmp_path / "augmented.csv"
    result = build_augmented_test_table(
        input_csv=input_csv,
        output_csv=output_csv,
        cfg=AppConfig(strategy_id=1, target_profit=1.0, max_consecutive_losses=4, max_stake=25.0, max_price_threshold=0.65),
        seed=7,
        fill_min_price=0.45,
        fill_max_price=0.60,
    )

    rows = _read_rows(output_csv)
    assert result["rows"] == 2
    assert len(rows) == 3
    assert rows[0]["entry_price_open_up"] != ""
    assert rows[0]["entry_price_open_down"] != ""
    assert rows[0]["下注项"] in {"UP", "DOWN"}
    assert rows[0]["累计盈亏"] != ""
    assert rows[-1]["slug"] == "SUMMARY"
    assert rows[-1]["下注项"] == "TOTAL_PNL"
    assert rows[-1]["下注金额"] == rows[-1]["累计盈亏"]

    raw = output_csv.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")


def test_main_rejects_legacy_build_test_table_subcommand():
    with pytest.raises(SystemExit) as exc:
        main.main(["build-test-table"])

    assert exc.value.code == 2
