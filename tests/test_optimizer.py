from config import AppConfig
from optimizer import (
    build_candidate_configs,
    build_optimizer_state,
    load_optimizer_state,
    rank_optimizer_candidates,
    run_optimizer_cycle,
    save_optimizer_state,
)


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


def test_build_optimizer_state_selects_top_challengers_and_promotable_candidates(tmp_path):
    state = build_optimizer_state(
        ranked_candidates=[
            {
                "candidate_id": "cand-a",
                "base_strategy_id": 5,
                "params": {"TARGET_PROFIT": 1.2},
                "validation_score": 0.9,
                "promotable": True,
            },
            {
                "candidate_id": "cand-b",
                "base_strategy_id": 6,
                "params": {"OFI_THRESHOLD": 0.7},
                "validation_score": 0.8,
                "promotable": False,
            },
            {
                "candidate_id": "cand-c",
                "base_strategy_id": 5,
                "params": {"TARGET_PROFIT": 0.8},
                "validation_score": 0.7,
                "promotable": False,
            },
        ],
        champion_id="champion-1",
        top_n=2,
        last_run_at="2026-04-16T10:00:00+00:00",
    )

    assert state["enabled"] is True
    assert state["champion_id"] == "champion-1"
    assert [item["candidate_id"] for item in state["active_challengers"]] == ["cand-a", "cand-b"]
    assert [item["candidate_id"] for item in state["promotable_candidates"]] == ["cand-a"]


def test_optimizer_state_roundtrip_persists_runtime_payload(tmp_path):
    path = tmp_path / "optimizer_state.json"
    payload = {
        "enabled": True,
        "last_run_at": "2026-04-16T10:00:00+00:00",
        "champion_id": "champion-1",
        "active_challengers": [{"candidate_id": "cand-a"}],
        "promotable_candidates": [{"candidate_id": "cand-a"}],
    }

    save_optimizer_state(path, payload)

    loaded = load_optimizer_state(path)

    assert loaded == payload


def test_run_optimizer_cycle_writes_optimizer_state_file(tmp_path):
    output_path = tmp_path / "optimizer_state.json"

    payload = run_optimizer_cycle(
        ranked_candidates=[
            {
                "candidate_id": "cand-a",
                "base_strategy_id": 5,
                "params": {"TARGET_PROFIT": 1.2},
                "validation_score": 0.9,
                "promotable": True,
            },
            {
                "candidate_id": "cand-b",
                "base_strategy_id": 6,
                "params": {"OFI_THRESHOLD": 0.7},
                "validation_score": 0.8,
                "promotable": False,
            },
        ],
        champion_id="champion-1",
        output_path=output_path,
        top_n=1,
        last_run_at="2026-04-16T10:00:00+00:00",
    )

    assert payload["enabled"] is True
    assert output_path.exists()
    assert load_optimizer_state(output_path)["active_challengers"][0]["candidate_id"] == "cand-a"
