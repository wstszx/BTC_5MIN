from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PaperComparisonMetrics:
    champion_trade_count: int
    challenger_trade_count: int
    champion_total_pnl: float
    challenger_total_pnl: float
    champion_max_drawdown: float
    challenger_max_drawdown: float
    challenger_advantage: float


def _max_drawdown(cash_values: list[float]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for value in cash_values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    return max_drawdown


def compare_paper_candidates(
    rows: list[dict[str, str]],
    *,
    champion_id: str,
    challenger_id: str,
) -> PaperComparisonMetrics:
    champion_rows = [row for row in rows if str(row.get("experiment_id")) == champion_id]
    challenger_rows = [row for row in rows if str(row.get("experiment_id")) == challenger_id]

    champion_cash = [float(row.get("cash_pnl") or 0.0) for row in champion_rows]
    challenger_cash = [float(row.get("cash_pnl") or 0.0) for row in challenger_rows]
    champion_total = champion_cash[-1] if champion_cash else 0.0
    challenger_total = challenger_cash[-1] if challenger_cash else 0.0

    return PaperComparisonMetrics(
        champion_trade_count=len(champion_rows),
        challenger_trade_count=len(challenger_rows),
        champion_total_pnl=champion_total,
        challenger_total_pnl=challenger_total,
        champion_max_drawdown=_max_drawdown(champion_cash),
        challenger_max_drawdown=_max_drawdown(challenger_cash),
        challenger_advantage=challenger_total - champion_total,
    )


def compare_paper_candidates_from_csv(
    csv_path: Path,
    *,
    champion_id: str,
    challenger_id: str,
) -> PaperComparisonMetrics:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return compare_paper_candidates(rows, champion_id=champion_id, challenger_id=challenger_id)
