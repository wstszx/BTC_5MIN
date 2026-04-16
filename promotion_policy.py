from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    state: str
    reason: str
    promotable: bool


def evaluate_promotion(
    *,
    champion_trade_count: int,
    challenger_trade_count: int,
    champion_total_pnl: float,
    challenger_total_pnl: float,
    champion_max_drawdown: float,
    challenger_max_drawdown: float,
    min_trade_count: int,
    required_pnl_edge: float,
    max_drawdown_multiplier: float,
) -> PromotionDecision:
    if challenger_trade_count < min_trade_count:
        return PromotionDecision(state="challenger", reason="insufficient_trade_count", promotable=False)
    if challenger_total_pnl - champion_total_pnl < required_pnl_edge:
        return PromotionDecision(state="challenger", reason="insufficient_pnl_edge", promotable=False)
    if champion_max_drawdown > 0 and challenger_max_drawdown > champion_max_drawdown * max_drawdown_multiplier:
        return PromotionDecision(state="rejected", reason="drawdown_too_high", promotable=False)
    return PromotionDecision(state="promotable", reason="thresholds_passed", promotable=True)
