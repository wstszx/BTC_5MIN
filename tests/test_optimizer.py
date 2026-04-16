from config import AppConfig
from optimizer import build_candidate_configs, rank_optimizer_candidates


def test_build_candidate_configs_creates_strategy_specific_parameter_bundles():
    cfg = AppConfig()

    candidates = build_candidate_configs(
        cfg,
        strategy_ids=[5],
        target_profits=[0.8, 1.2],
        max_price_thresholds=[0.55],
        strategy5_thresholds=[0.012, 0.018],
    )

    assert len(candidates) == 4
    assert all(candidate.base_strategy_id == 5 for candidate in candidates)
    assert {candidate.params["TARGET_PROFIT"] for candidate in candidates} == {0.8, 1.2}
    assert {candidate.params["SIGNAL_MOMENTUM_THRESHOLD"] for candidate in candidates} == {0.012, 0.018}


def test_rank_optimizer_candidates_prefers_higher_validation_score_then_lower_drawdown():
    ranked = rank_optimizer_candidates(
        [
            {"candidate_id": "a", "validation_score": 0.6, "max_drawdown": 5.0, "total_pnl": 10.0},
            {"candidate_id": "b", "validation_score": 0.8, "max_drawdown": 7.0, "total_pnl": 9.0},
            {"candidate_id": "c", "validation_score": 0.8, "max_drawdown": 4.0, "total_pnl": 8.0},
        ]
    )

    assert [item["candidate_id"] for item in ranked] == ["c", "b", "a"]
