import csv
from pathlib import Path
from datetime import datetime, timedelta, timezone

from config import AppConfig
from optimizer import (
    build_candidate_configs,
    build_optimizer_state,
    evaluate_candidates_with_walk_forward,
    refresh_optimizer_state_from_paper_results,
    load_optimizer_state,
    main,
    rank_optimizer_candidates,
    run_optimizer_cycle,
    run_optimizer_scheduler,
    run_optimizer_from_history_csv,
    score_candidate_with_backtest_rows,
    save_optimizer_state,
)
from walk_forward import WalkForwardWindow


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


def test_build_candidate_configs_creates_strategy7_parameter_bundles():
    cfg = AppConfig()

    candidates = build_candidate_configs(
        cfg,
        strategy_ids=[7],
        target_profits=[1.0],
        max_price_thresholds=[0.55],
        strategy5_thresholds=[0.012],
    )

    assert len(candidates) > 0
    assert all(candidate.base_strategy_id == 7 for candidate in candidates)
    assert all('STRATEGY7_OFI_THRESHOLD' in candidate.params for candidate in candidates)
    assert all('STRATEGY7_MOMENTUM_THRESHOLD' in candidate.params for candidate in candidates)
    assert all('STRATEGY7_MAX_ENTRY_PRICE' in candidate.params for candidate in candidates)


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


def test_evaluate_candidates_with_walk_forward_aggregates_validation_scores():
    candidates = [
        {
            "candidate_id": "cand-a",
            "base_strategy_id": 5,
            "params": {"TARGET_PROFIT": 1.2},
        },
        {
            "candidate_id": "cand-b",
            "base_strategy_id": 6,
            "params": {"OFI_THRESHOLD": 0.7},
        },
    ]
    rows = [{"index": idx} for idx in range(12)]
    windows = [
        WalkForwardWindow(train_start=0, train_end=6, validation_start=6, validation_end=9),
        WalkForwardWindow(train_start=3, train_end=9, validation_start=9, validation_end=12),
    ]

    def fake_scorer(candidate, train_rows, validation_rows):
        return {
            "total_pnl": float(len(validation_rows)) + (1.0 if candidate["candidate_id"] == "cand-a" else 0.0),
            "max_drawdown": 1.0 if candidate["candidate_id"] == "cand-a" else 2.0,
            "validation_score": 0.9 if candidate["candidate_id"] == "cand-a" else 0.4,
        }

    ranked = evaluate_candidates_with_walk_forward(
        candidates,
        rows=rows,
        windows=windows,
        scorer=fake_scorer,
    )

    assert [item["candidate_id"] for item in ranked] == ["cand-a", "cand-b"]
    assert ranked[0]["window_count"] == 2
    assert ranked[0]["validation_score"] == 0.9
    assert ranked[1]["validation_score"] == 0.4


def test_score_candidate_with_backtest_rows_returns_backtest_metrics():
    fixture_path = Path("tests/fixtures/sample_history.csv")
    with fixture_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    metrics = score_candidate_with_backtest_rows(
        {
            "candidate_id": "cand-s2",
            "base_strategy_id": 2,
            "params": {
                "TARGET_PROFIT": 1.0,
                "MAX_PRICE_THRESHOLD": 0.65,
            },
        },
        rows=rows,
        base_cfg=AppConfig(strategy_id=2),
    )

    assert "total_pnl" in metrics
    assert "max_drawdown" in metrics
    assert "validation_score" in metrics
    assert metrics["trade_count"] >= 0
    assert metrics["validation_score"] == metrics["total_pnl"] - metrics["max_drawdown"]


def test_run_optimizer_from_history_csv_writes_optimizer_state_from_real_history(tmp_path):
    csv_path = Path("tests/fixtures/sample_history.csv")
    output_path = tmp_path / "optimizer_state.json"

    payload = run_optimizer_from_history_csv(
        csv_path=csv_path,
        base_cfg=AppConfig(strategy_id=2),
        output_path=output_path,
        strategy_ids=[2],
        target_profits=[1.0],
        max_price_thresholds=[0.65],
        strategy5_thresholds=[0.015],
        train_size=3,
        validation_size=3,
        step_size=3,
        top_n=1,
        champion_id="champion-1",
        last_run_at="2026-04-16T10:00:00+00:00",
    )

    assert payload["enabled"] is True
    assert output_path.exists()
    loaded = load_optimizer_state(output_path)
    assert loaded["champion_id"] == "champion-1"
    assert len(loaded["active_challengers"]) == 1
    assert loaded["active_challengers"][0]["candidate_id"].startswith("s2-")


def test_optimizer_main_runs_history_optimization_command(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_run_optimizer_from_history_csv(**kwargs):
        captured.update(kwargs)
        return {
            "enabled": True,
            "champion_id": kwargs["champion_id"],
            "active_challengers": [{"candidate_id": "cand-a"}],
            "promotable_candidates": [],
            "last_run_at": kwargs["last_run_at"],
        }

    monkeypatch.setattr("optimizer.run_optimizer_from_history_csv", fake_run_optimizer_from_history_csv)

    csv_path = tmp_path / "history.csv"
    csv_path.write_text(
        "event_id,market_id,slug,title,series_id,start_time,end_time,price_to_beat,final_price,result,up_token_id,down_token_id,up_last_price,down_last_price,up_best_bid,up_best_ask,down_best_bid,down_best_ask,entry_price_open_up,entry_price_open_down,entry_price_preclose_up,entry_price_preclose_down\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("STRATEGY_ID=5\n", encoding="utf-8")
    output_path = tmp_path / "optimizer_state.json"

    exit_code = main(
        [
            "--csv",
            str(csv_path),
            "--env-file",
            str(env_file),
            "--output",
            str(output_path),
            "--champion-id",
            "champion-1",
        ]
    )

    assert exit_code == 0
    assert captured["csv_path"] == csv_path
    assert captured["output_path"] == output_path
    assert captured["champion_id"] == "champion-1"
    assert captured["strategy_ids"] == [5, 6]
    assert captured["top_n"] == 3


def test_optimizer_main_rejects_missing_csv_argument():
    try:
        main([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected SystemExit")


def test_optimizer_main_runs_scheduler_in_watch_mode(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_scheduler(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("optimizer.run_optimizer_scheduler", fake_scheduler)

    csv_path = tmp_path / "history.csv"
    csv_path.write_text("h\n", encoding="utf-8")
    paper_log_path = tmp_path / "paper.csv"
    paper_log_path.write_text("h\n", encoding="utf-8")
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("STRATEGY_ID=5\n", encoding="utf-8")
    output_path = tmp_path / "optimizer_state.json"

    exit_code = main(
        [
            "--csv",
            str(csv_path),
            "--paper-log",
            str(paper_log_path),
            "--env-file",
            str(env_file),
            "--output",
            str(output_path),
            "--champion-id",
            "champion-1",
            "--watch",
            "--optimize-interval-seconds",
            "3600",
            "--refresh-interval-seconds",
            "900",
            "--poll-interval-seconds",
            "30",
        ]
    )

    assert exit_code == 0
    assert captured["csv_path"] == csv_path
    assert captured["paper_log_path"] == paper_log_path
    assert captured["output_path"] == output_path
    assert captured["champion_id"] == "champion-1"
    assert captured["optimize_interval_seconds"] == 3600
    assert captured["refresh_interval_seconds"] == 900
    assert captured["poll_interval_seconds"] == 30


def test_refresh_optimizer_state_from_paper_results_updates_challenger_decisions(tmp_path):
    state_path = tmp_path / "optimizer_state.json"
    paper_log_path = tmp_path / "paper_trades.csv"

    save_optimizer_state(
        state_path,
        {
            "enabled": True,
            "last_run_at": "2026-04-16T10:00:00+00:00",
            "champion_id": "champion-paper",
            "active_challengers": [
                {
                    "candidate_id": "challenger-a",
                    "base_strategy_id": 5,
                    "params": {"TARGET_PROFIT": 1.2},
                    "validation_score": 0.9,
                }
            ],
            "promotable_candidates": [],
        },
    )
    paper_log_path.write_text(
        "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason,experiment_id\n"
        "2026-04-16T10:00:00+00:00,paper,1,2,OPEN,slug-a,2026-04-16T09:55:00+00:00,2026-04-16T10:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,1.0,0.0,0,False,,,,,,False,,champion-paper\n"
        "2026-04-16T10:05:00+00:00,paper,2,2,OPEN,slug-b,2026-04-16T10:00:00+00:00,2026-04-16T10:05:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,2.0,0.0,0,False,,,,,,False,,champion-paper\n"
        "2026-04-16T10:00:00+00:00,paper,1,5,OPEN,slug-c,2026-04-16T09:55:00+00:00,2026-04-16T10:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,2.0,2.0,0.0,0,False,,,,,,False,,challenger-a\n"
        "2026-04-16T10:05:00+00:00,paper,2,5,OPEN,slug-d,2026-04-16T10:00:00+00:00,2026-04-16T10:05:00+00:00,UP,0.50,2.0,1.0,1.0,UP,2.0,4.0,0.0,0,False,,,,,,False,,challenger-a\n",
        encoding="utf-8",
    )

    payload = refresh_optimizer_state_from_paper_results(
        state_path=state_path,
        paper_log_path=paper_log_path,
        min_trade_count=2,
        required_pnl_edge=1.0,
        max_drawdown_multiplier=1.25,
    )

    challenger = payload["active_challengers"][0]
    assert challenger["paper_metrics"]["challenger_advantage"] == 2.0
    assert challenger["promotion_decision"]["state"] == "promotable"
    assert payload["promotable_candidates"][0]["candidate_id"] == "challenger-a"


def test_run_optimizer_scheduler_triggers_optimize_and_refresh_when_due(tmp_path):
    calls: list[tuple[str, Path]] = []
    output_path = tmp_path / "optimizer_state.json"
    csv_path = tmp_path / "history.csv"
    paper_log_path = tmp_path / "paper.csv"
    csv_path.write_text("h\n", encoding="utf-8")
    paper_log_path.write_text("h\n", encoding="utf-8")

    times = iter(
        [
            datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 16, 10, 5, tzinfo=timezone.utc),
            datetime(2026, 4, 16, 10, 5, tzinfo=timezone.utc),
        ]
    )

    def fake_now():
        return next(times)

    def fake_optimize(**kwargs):
        calls.append(("optimize", kwargs["output_path"]))
        save_optimizer_state(
            kwargs["output_path"],
            {
                "enabled": True,
                "last_run_at": kwargs["last_run_at"],
                "champion_id": kwargs["champion_id"],
                "active_challengers": [],
                "promotable_candidates": [],
            },
        )
        return load_optimizer_state(kwargs["output_path"])

    def fake_refresh(**kwargs):
        calls.append(("refresh", kwargs["state_path"]))
        return load_optimizer_state(kwargs["state_path"])

    run_optimizer_scheduler(
        csv_path=csv_path,
        paper_log_path=paper_log_path,
        base_cfg=AppConfig(strategy_id=2),
        output_path=output_path,
        champion_id="champion-1",
        optimize_interval_seconds=60,
        refresh_interval_seconds=60,
        poll_interval_seconds=1,
        max_loops=2,
        now_fn=fake_now,
        sleep_fn=lambda _seconds: None,
        optimize_runner=fake_optimize,
        refresh_runner=fake_refresh,
    )

    assert calls == [
        ("optimize", output_path),
        ("refresh", output_path),
        ("optimize", output_path),
        ("refresh", output_path),
    ]
