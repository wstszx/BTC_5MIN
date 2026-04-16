from walk_forward import WalkForwardWindow, build_walk_forward_windows


def test_build_walk_forward_windows_splits_rows_into_ordered_train_and_validation_ranges():
    rows = [{"start_time": f"2026-04-01T00:{minute:02d}:00Z"} for minute in range(12)]

    windows = build_walk_forward_windows(
        rows,
        train_size=6,
        validation_size=3,
        step_size=3,
    )

    assert windows == [
        WalkForwardWindow(train_start=0, train_end=6, validation_start=6, validation_end=9),
        WalkForwardWindow(train_start=3, train_end=9, validation_start=9, validation_end=12),
    ]


def test_build_walk_forward_windows_returns_empty_when_not_enough_rows():
    rows = [{"start_time": f"2026-04-01T00:{minute:02d}:00Z"} for minute in range(5)]

    windows = build_walk_forward_windows(
        rows,
        train_size=4,
        validation_size=3,
        step_size=2,
    )

    assert windows == []


def test_build_walk_forward_windows_rejects_non_positive_sizes():
    rows = [{"start_time": "2026-04-01T00:00:00Z"}]

    try:
        build_walk_forward_windows(rows, train_size=0, validation_size=1, step_size=1)
    except ValueError as exc:
        assert "train_size" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
