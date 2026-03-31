from pathlib import Path

import pytest

from config import AppConfig
from streak_analysis import analyze_streak_risk, compute_max_affordable_round


def test_compute_max_affordable_round_respects_stake_cap():
    affordable = compute_max_affordable_round(
        target_profit=0.5,
        max_stake=25.0,
        worst_case_price=0.65,
        search_max_round=12,
    )
    assert affordable == 4


def test_analyze_streak_risk_recommends_cap_round_when_occurrence_target_is_strict():
    cfg = AppConfig(max_stake=25.0, target_profit=0.5, max_price_threshold=0.65)
    analysis = analyze_streak_risk(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_id=1,
        target_occurrence=0.01,
        min_round=2,
        max_round=6,
    )
    assert analysis.analyzed_round_count == 6
    assert round(analysis.hit_rate, 4) == round(2 / 6, 4)
    assert analysis.max_loss_streak == 4
    assert analysis.max_affordable_round == 4
    assert analysis.recommended_reset_round == 4


def test_analyze_streak_risk_uses_target_occurrence_when_possible():
    cfg = AppConfig(max_stake=25.0, target_profit=0.5, max_price_threshold=0.65)
    analysis = analyze_streak_risk(
        Path("tests/fixtures/sample_history.csv"),
        cfg,
        strategy_id=1,
        target_occurrence=0.2,
        min_round=2,
        max_round=6,
    )
    # In sample data, only one loss streak of length 4 -> occurrence for K=2 is 1/6 ~= 16.67%
    assert analysis.recommended_reset_round == 2


def test_analyze_streak_risk_rejects_signal_strategy():
    cfg = AppConfig()
    with pytest.raises(ValueError, match="strategy_id=5"):
        analyze_streak_risk(
            Path("tests/fixtures/sample_history.csv"),
            cfg,
            strategy_id=5,
            target_occurrence=0.2,
            min_round=2,
            max_round=6,
        )
