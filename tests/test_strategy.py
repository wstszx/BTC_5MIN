import pytest

from config import AppConfig
from main import build_parser
from strategy import get_side_for_round


def test_default_config_targets_btc_5m_series():
    cfg = AppConfig()
    assert cfg.series_id == 10684
    assert cfg.series_slug == "btc-up-or-down-5m"
    assert cfg.trade_mode == "paper"


@pytest.mark.parametrize(
    ("strategy_id", "expected"),
    [
        (1, ["UP", "DOWN", "UP", "DOWN", "UP", "DOWN"]),
        (2, ["UP", "UP", "DOWN", "DOWN", "UP", "UP"]),
        (3, ["UP", "UP", "UP", "DOWN", "DOWN", "DOWN"]),
        (4, ["UP", "UP", "UP", "UP", "DOWN", "DOWN"]),
    ],
)
def test_strategy_sequences(strategy_id, expected):
    actual = [get_side_for_round(strategy_id, idx) for idx in range(len(expected))]
    assert actual == expected


def test_cli_exposes_expected_commands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"fetch-history", "backtest", "analyze-streak", "research-strategy", "paper-trade", "live-trade"} <= set(choices)


def test_signal_strategy_chooses_up_when_momentum_exceeds_threshold():
    side = get_side_for_round(
        5,
        10,
        signal_open_up_price=0.50,
        signal_current_up_price=0.53,
        signal_threshold=0.02,
        signal_fallback_strategy_id=2,
    )
    assert side == "UP"


def test_signal_strategy_falls_back_when_momentum_is_small():
    # strategy 2 at round index 3 -> DOWN
    side = get_side_for_round(
        5,
        3,
        signal_open_up_price=0.50,
        signal_current_up_price=0.505,
        signal_threshold=0.02,
        signal_fallback_strategy_id=2,
    )
    assert side == "DOWN"
