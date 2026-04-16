import pytest

from config import AppConfig
import main
from strategy import compute_ofi_score, get_side_for_round


def test_default_config_targets_btc_5m_series():
    cfg = AppConfig()
    assert cfg.market_timeframe == '5m'
    assert cfg.series_id == 10684
    assert cfg.series_slug == 'btc-up-or-down-5m'
    assert cfg.trade_mode == 'paper'


@pytest.mark.parametrize(
    ('strategy_id', 'expected'),
    [
        (1, ['UP', 'DOWN', 'UP', 'DOWN', 'UP', 'DOWN']),
        (2, ['UP', 'UP', 'DOWN', 'DOWN', 'UP', 'UP']),
        (3, ['UP', 'UP', 'UP', 'DOWN', 'DOWN', 'DOWN']),
        (4, ['UP', 'UP', 'UP', 'UP', 'DOWN', 'DOWN']),
    ],
)
def test_strategy_sequences(strategy_id, expected):
    actual = [get_side_for_round(strategy_id, idx) for idx in range(len(expected))]
    assert actual == expected


def test_main_rejects_legacy_cli_subcommands():
    with pytest.raises(SystemExit) as exc:
        main.main(['backtest'])

    assert exc.value.code == 2


def test_signal_strategy_chooses_up_when_momentum_exceeds_threshold():
    side = get_side_for_round(
        5,
        10,
        signal_open_up_price=0.50,
        signal_current_up_price=0.53,
        signal_threshold=0.02,
        signal_fallback_strategy_id=2,
    )
    assert side == 'UP'


def test_signal_strategy_falls_back_when_momentum_is_small():
    with pytest.raises(ValueError, match='weak momentum'):
        get_side_for_round(
            5,
            3,
            signal_open_up_price=0.50,
            signal_current_up_price=0.505,
            signal_threshold=0.02,
            signal_fallback_strategy_id=2,
        )


def test_compute_ofi_score_returns_positive_when_bid_pressure_dominates():
    score = compute_ofi_score(100000.0, 10.0, 100001.0, 1.0)

    assert score is not None
    assert score > 0.8


def test_strategy_6_chooses_up_for_strong_positive_ofi():
    side = get_side_for_round(
        6,
        0,
        ofi_score=0.8,
        ofi_threshold=0.65,
    )

    assert side == 'UP'


def test_strategy_6_requires_ofi_signal_context():
    with pytest.raises(ValueError, match='ofi_score'):
        get_side_for_round(6, 0)
