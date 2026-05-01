from __future__ import annotations

import pytest

from config import AppConfig
from runtime_config import (
    cfg_for_live_strategy,
    live_strategy_ids_for_runtime,
    paper_strategy_ids_for_runtime,
    validate_live_runtime_config,
)
from trader import _cfg_for_live_strategy, _live_strategy_ids_for_runtime


def test_runtime_config_resolves_strategy_ids_and_profile_overrides_with_trader_reexports():
    cfg = AppConfig(strategy_id=2, live_strategy_ids=[5, 2])
    cfg.live_profiles[5].base_order_cost = 12.5

    strategy_cfg = cfg_for_live_strategy(cfg, 5)

    assert live_strategy_ids_for_runtime(cfg) == [5, 2]
    assert strategy_cfg.strategy_id == 5
    assert strategy_cfg.base_order_cost == 12.5
    assert _live_strategy_ids_for_runtime is live_strategy_ids_for_runtime
    assert _cfg_for_live_strategy is cfg_for_live_strategy


def test_runtime_config_uses_split_strategy_ids_even_when_legacy_strategy_ids_exist():
    cfg = AppConfig(
        strategy_id=2,
        strategy_ids=[8, 7],
        paper_strategy_ids=[1, 2],
        live_strategy_ids=[6],
    )

    assert paper_strategy_ids_for_runtime(cfg) == [1, 2]
    assert live_strategy_ids_for_runtime(cfg) == [6]


def test_runtime_config_validates_live_runtime_credentials():
    cfg = AppConfig(trade_mode="live", live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="private key"):
        validate_live_runtime_config(cfg)
