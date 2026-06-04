from __future__ import annotations

from dataclasses import fields

import pytest

from config import AppConfig
from runtime_config import (
    cfg_for_live_strategy,
    cfg_for_paper_strategy,
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


def test_runtime_config_keeps_paper_and_live_strategy_profiles_identical_without_mode_overrides():
    cfg = AppConfig(
        strategy_id=7,
        paper_strategy_ids=[5, 7, 9, 10, 11, 12],
        live_strategy_ids=[5, 7, 9, 10, 11, 12],
    )
    ignored_fields = {
        "trade_mode",
        "live_trading_enabled",
        "live_private_key",
        "live_funder",
        "live_api_key",
        "live_api_secret",
        "live_api_passphrase",
        "paper_simulated_wallet_balance",
        "strategy_ids",
        "paper_strategy_ids",
        "live_strategy_ids",
        "paper_timeframes",
        "market_timeframe",
        "paper_profiles",
        "paper_strategy_profiles",
        "live_profiles",
        "logs_dir",
        "data_dir",
        "db_path",
        "paper_trades_csv",
        "live_trades_csv",
    }
    compared_fields = [
        field.name
        for field in fields(AppConfig)
        if field.init and field.name not in ignored_fields
    ]

    for strategy_id in range(1, 13):
        paper_cfg = cfg_for_paper_strategy(cfg, strategy_id)
        live_cfg = cfg_for_live_strategy(cfg, strategy_id)
        assert {
            name: (getattr(paper_cfg, name), getattr(live_cfg, name))
            for name in compared_fields
            if getattr(paper_cfg, name) != getattr(live_cfg, name)
        } == {}


def test_runtime_config_uses_live_strategy_profile_for_paper_by_default():
    cfg = AppConfig(
        strategy_id=10,
        paper_strategy_ids=[10],
        live_strategy_ids=[10],
    )
    cfg.paper_strategy_profiles[10].min_entry_price = 0.45
    cfg.paper_strategy_profiles[10].base_order_cost = 1.0
    cfg.paper_strategy_profiles[10].strategy10_min_edge = 0.035
    cfg.paper_strategy_profiles[10].strategy10_min_momentum_delta = -0.02
    cfg.paper_strategy_profiles[10].strategy10_max_momentum_delta = 0.02
    cfg.paper_strategy_profiles[10].strategy10_down_min_edge = 0.07
    cfg.live_profiles[10].min_entry_price = 0.50
    cfg.live_profiles[10].base_order_cost = 2.0
    cfg.live_profiles[10].strategy10_min_edge = 0.05

    paper_cfg = cfg_for_paper_strategy(cfg, 10)
    live_cfg = cfg_for_live_strategy(cfg, 10)

    assert paper_cfg.min_entry_price == pytest.approx(live_cfg.min_entry_price)
    assert paper_cfg.base_order_cost == pytest.approx(live_cfg.base_order_cost)
    assert paper_cfg.strategy10_min_edge == pytest.approx(live_cfg.strategy10_min_edge)
    assert paper_cfg.strategy10_min_momentum_delta == live_cfg.strategy10_min_momentum_delta
    assert paper_cfg.strategy10_max_momentum_delta == live_cfg.strategy10_max_momentum_delta
    assert paper_cfg.strategy10_down_min_edge == live_cfg.strategy10_down_min_edge


def test_runtime_config_can_use_paper_experiment_strategy_profile_overrides():
    cfg = AppConfig(
        strategy_id=10,
        paper_strategy_ids=[10],
        live_strategy_ids=[10],
        paper_use_live_profiles=False,
    )
    cfg.paper_strategy_profiles[10].min_entry_price = 0.45
    cfg.paper_strategy_profiles[10].base_order_cost = 1.0
    cfg.paper_strategy_profiles[10].strategy10_min_edge = 0.035
    cfg.paper_strategy_profiles[10].strategy10_min_momentum_delta = -0.02
    cfg.paper_strategy_profiles[10].strategy10_max_momentum_delta = 0.02
    cfg.paper_strategy_profiles[10].strategy10_down_min_edge = 0.07
    cfg.live_profiles[10].min_entry_price = 0.50
    cfg.live_profiles[10].base_order_cost = 2.0
    cfg.live_profiles[10].strategy10_min_edge = 0.05

    paper_cfg = cfg_for_paper_strategy(cfg, 10)
    live_cfg = cfg_for_live_strategy(cfg, 10)

    assert paper_cfg.min_entry_price == pytest.approx(0.45)
    assert paper_cfg.base_order_cost == pytest.approx(1.0)
    assert paper_cfg.strategy10_min_edge == pytest.approx(0.035)
    assert paper_cfg.strategy10_min_momentum_delta == pytest.approx(-0.02)
    assert paper_cfg.strategy10_max_momentum_delta == pytest.approx(0.02)
    assert paper_cfg.strategy10_down_min_edge == pytest.approx(0.07)
    assert live_cfg.min_entry_price == pytest.approx(0.50)
    assert live_cfg.base_order_cost == pytest.approx(2.0)
    assert live_cfg.strategy10_min_edge == pytest.approx(0.05)
    assert live_cfg.strategy10_min_momentum_delta is None
    assert live_cfg.strategy10_max_momentum_delta is None
    assert live_cfg.strategy10_down_min_edge is None


def test_runtime_config_validates_live_runtime_credentials():
    cfg = AppConfig(trade_mode="live", live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="private key"):
        validate_live_runtime_config(cfg)


def test_runtime_config_rejects_limit_order_types_for_live_market_orders():
    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        live_order_type="GTC",
    )

    with pytest.raises(RuntimeError, match="FOK or FAK"):
        validate_live_runtime_config(cfg)
