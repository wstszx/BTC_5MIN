import main
from config import AppConfig
from runtime_control import RuntimeControl, RuntimeSnapshot



def test_runtime_control_starts_idle_in_paper_mode():
    control = RuntimeControl(initial_mode="paper")

    snapshot = control.snapshot()

    assert snapshot == RuntimeSnapshot(
        active_mode="paper",
        desired_mode="paper",
        switch_state="idle",
        switch_reason=None,
        current_round_slug=None,
        round_in_progress=False,
        safe_to_switch=True,
        pending_live_order=False,
        last_transition_at=None,
    )


def test_requesting_new_mode_enters_pending_state():
    control = RuntimeControl(initial_mode="paper")

    control.set_desired_mode("live")

    snapshot = control.snapshot()
    assert snapshot.desired_mode == "live"
    assert snapshot.switch_state == "pending"


def test_reverting_to_active_mode_clears_pending_state():
    control = RuntimeControl(initial_mode="paper")
    control.set_desired_mode("live")

    control.set_desired_mode("paper")

    snapshot = control.snapshot()
    assert snapshot.active_mode == "paper"
    assert snapshot.desired_mode == "paper"
    assert snapshot.switch_state == "idle"



def test_runtime_manager_waits_for_safe_boundary_before_switching(tmp_path):
    cfg = AppConfig(trade_mode='paper')
    manager = main.RuntimeManager(
        env_file=tmp_path / '.env.dashboard',
        host='127.0.0.1',
        port=8787,
        startup_cfg=cfg,
        dashboard_runtime_factory=lambda **kwargs: None,
        validate_live_config=lambda cfg: None,
    )
    manager.request_mode_change('live')
    manager.runtime_control.update_worker_state(round_in_progress=True, safe_to_switch=False)

    manager.poll_once()

    snapshot = manager.snapshot()
    assert snapshot.active_mode == 'paper'
    assert snapshot.desired_mode == 'live'
    assert snapshot.switch_state == 'pending'



def test_runtime_manager_blocks_when_live_config_is_invalid(tmp_path):
    cfg = AppConfig(trade_mode='paper')
    manager = main.RuntimeManager(
        env_file=tmp_path / '.env.dashboard',
        host='127.0.0.1',
        port=8787,
        startup_cfg=cfg,
        dashboard_runtime_factory=lambda **kwargs: None,
        validate_live_config=lambda cfg: (_ for _ in ()).throw(RuntimeError('Live trading is disabled.')),
    )
    manager.request_mode_change('live')
    manager.runtime_control.update_worker_state(round_in_progress=False, safe_to_switch=True)

    manager.poll_once()

    snapshot = manager.snapshot()
    assert snapshot.active_mode == 'paper'
    assert snapshot.switch_state == 'blocked'
    assert 'Live trading' in str(snapshot.switch_reason)



def test_runtime_manager_latest_desired_mode_wins(tmp_path):
    cfg = AppConfig(trade_mode='paper')
    manager = main.RuntimeManager(
        env_file=tmp_path / '.env.dashboard',
        host='127.0.0.1',
        port=8787,
        startup_cfg=cfg,
        dashboard_runtime_factory=lambda **kwargs: None,
        validate_live_config=lambda cfg: None,
    )
    manager.request_mode_change('live')
    manager.request_mode_change('paper')

    snapshot = manager.snapshot()
    assert snapshot.active_mode == 'paper'
    assert snapshot.desired_mode == 'paper'
    assert snapshot.switch_state == 'idle'
