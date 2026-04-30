from __future__ import annotations

from config import AppConfig
from models import PaperStrategyState, PendingPaperTrade
from optimizer import save_optimizer_state
from optimizer_runtime import candidate_cfg_with_params, load_active_optimizer_challengers, paper_experiment_id
from trader import _candidate_cfg_with_params, _paper_experiment_id


def test_optimizer_runtime_applies_candidate_params_and_trader_reexports_helpers():
    base_cfg = AppConfig(strategy_id=7, target_profit=1.0, strategy7_max_entry_price=0.56)
    candidate_cfg = candidate_cfg_with_params(
        base_cfg,
        7,
        {
            "TARGET_PROFIT": "1.25",
            "STRATEGY7_MAX_ENTRY_PRICE": "0.53",
        },
    )
    state = PaperStrategyState()

    assert candidate_cfg.strategy_id == 7
    assert candidate_cfg.target_profit == 1.25
    assert candidate_cfg.strategy7_max_entry_price == 0.53
    assert paper_experiment_id(5, state) == "strategy-5"
    assert state.experiment_id == "strategy-5"
    assert _candidate_cfg_with_params is candidate_cfg_with_params
    assert _paper_experiment_id is paper_experiment_id


def test_optimizer_runtime_hydrates_active_challenger_paper_state(tmp_path):
    path = tmp_path / "optimizer_state.json"
    save_optimizer_state(
        path,
        {
            "enabled": True,
            "active_challengers": [
                {
                    "candidate_id": "challenger-s5-a",
                    "paper_state": {
                        "round_index": 2,
                        "pending_paper_trades": [
                            {
                                "round_index": 2,
                                "event_slug": "btc-updown-5m",
                                "start_time": "2026-04-30T01:00:00+00:00",
                                "end_time": "2026-04-30T01:05:00+00:00",
                                "side": "UP",
                                "price": 0.5,
                                "order_size": 2.0,
                                "order_cost": 1.0,
                                "expected_profit": 1.0,
                                "strategy": 5,
                                "entry_timing": "OPEN",
                            }
                        ],
                    },
                }
            ],
        },
    )

    payload, challengers = load_active_optimizer_challengers(path)

    assert payload["enabled"] is True
    assert len(challengers) == 1
    paper_state = challengers[0]["_paper_state"]
    assert isinstance(paper_state, PaperStrategyState)
    assert paper_state.experiment_id == "challenger-s5-a"
    assert isinstance(paper_state.pending_paper_trades[0], PendingPaperTrade)
