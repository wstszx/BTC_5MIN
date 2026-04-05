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
