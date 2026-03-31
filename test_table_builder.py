from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any

from backtest import run_backtest
from config import AppConfig


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_valid_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def _clamp_price(value: float) -> float:
    return max(0.001, min(0.999, value))


def _format_price(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _fill_binary_prices(
    *,
    up_raw: str | None,
    down_raw: str | None,
    rng: random.Random,
    min_price: float,
    max_price: float,
) -> tuple[float, float]:
    up = _optional_float(up_raw)
    down = _optional_float(down_raw)

    if _is_valid_price(up) and _is_valid_price(down):
        return up, down
    if _is_valid_price(up) and not _is_valid_price(down):
        return up, _clamp_price(1 - up)
    if _is_valid_price(down) and not _is_valid_price(up):
        return _clamp_price(1 - down), down

    sampled_up = rng.uniform(min_price, max_price)
    sampled_down = _clamp_price(1 - sampled_up)
    return sampled_up, sampled_down


def _prepare_rows_with_filled_prices(
    *,
    input_csv: Path,
    seed: int,
    fill_min_price: float,
    fill_max_price: float,
) -> tuple[list[dict[str, str]], list[str]]:
    required_price_fields = [
        "entry_price_open_up",
        "entry_price_open_down",
        "entry_price_preclose_up",
        "entry_price_preclose_down",
    ]
    with input_csv.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    for field in required_price_fields:
        if field not in fieldnames:
            fieldnames.append(field)
    for row in rows:
        for field in required_price_fields:
            row.setdefault(field, "")

    rng = random.Random(seed)
    for row in rows:
        open_up, open_down = _fill_binary_prices(
            up_raw=row.get("entry_price_open_up"),
            down_raw=row.get("entry_price_open_down"),
            rng=rng,
            min_price=fill_min_price,
            max_price=fill_max_price,
        )
        preclose_up, preclose_down = _fill_binary_prices(
            up_raw=row.get("entry_price_preclose_up"),
            down_raw=row.get("entry_price_preclose_down"),
            rng=rng,
            min_price=fill_min_price,
            max_price=fill_max_price,
        )
        row["entry_price_open_up"] = _format_price(open_up)
        row["entry_price_open_down"] = _format_price(open_down)
        row["entry_price_preclose_up"] = _format_price(preclose_up)
        row["entry_price_preclose_down"] = _format_price(preclose_down)

    return rows, fieldnames


def build_augmented_test_table(
    *,
    input_csv: Path,
    output_csv: Path,
    cfg: AppConfig,
    seed: int = 42,
    fill_min_price: float = 0.45,
    fill_max_price: float = 0.60,
) -> dict[str, Any]:
    if not (0 < fill_min_price < 1 and 0 < fill_max_price < 1 and fill_min_price < fill_max_price):
        raise ValueError("fill_min_price/fill_max_price must satisfy 0 < min < max < 1.")

    rows, base_fields = _prepare_rows_with_filled_prices(
        input_csv=input_csv,
        seed=seed,
        fill_min_price=fill_min_price,
        fill_max_price=fill_max_price,
    )

    temp_filled_csv = output_csv.with_suffix(".filled.tmp.csv")
    temp_filled_csv.parent.mkdir(parents=True, exist_ok=True)
    with temp_filled_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=base_fields)
        writer.writeheader()
        writer.writerows(rows)

    backtest_result = run_backtest(temp_filled_csv, cfg)
    annotated_rows: list[dict[str, str]] = []
    for row, record in zip(rows, backtest_result.records):
        enriched = dict(row)
        enriched["下注项"] = record.side
        enriched["下注金额"] = f"{record.order_cost:.6f}" if record.order_cost > 0 else ""
        enriched["本局盈亏"] = f"{record.trade_pnl:.6f}" if record.order_cost > 0 else ""
        enriched["累计盈亏"] = f"{record.cash_pnl:.6f}"
        enriched["是否跳过"] = "是" if record.order_cost <= 0 else "否"
        enriched["跳过原因"] = record.skip_reason or ""
        annotated_rows.append(enriched)

    output_fields = list(base_fields) + ["下注项", "下注金额", "本局盈亏", "累计盈亏", "是否跳过", "跳过原因"]
    summary = {field: "" for field in output_fields}
    summary["slug"] = "SUMMARY"
    summary["下注项"] = "TOTAL_PNL"
    summary["下注金额"] = f"{backtest_result.total_pnl:.6f}"
    summary["本局盈亏"] = f"{backtest_result.total_pnl:.6f}"
    summary["累计盈亏"] = f"{backtest_result.total_pnl:.6f}"
    summary["是否跳过"] = "-"
    summary["跳过原因"] = "最终收益(+)或亏损(-)"
    annotated_rows.append(summary)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    # Use utf-8-sig so Excel on Windows opens Chinese headers without mojibake.
    with output_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        writer.writerows(annotated_rows)

    try:
        temp_filled_csv.unlink()
    except OSError:
        pass

    return {
        "output_csv": str(output_csv),
        "rows": len(rows),
        "seed": seed,
        "total_pnl": backtest_result.total_pnl,
        "trade_count": backtest_result.trade_count,
        "skipped_round_count": backtest_result.skipped_round_count,
    }
