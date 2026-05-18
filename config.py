from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MARKET_TIMEFRAME = "MARKET_TIMEFRAME"
PAPER_TIMEFRAMES = "PAPER_TIMEFRAMES"
PAPER_STRATEGY_IDS = bytes([80, 65, 80, 69, 82, 95, 83, 84, 82, 65, 84, 69, 71, 89, 95, 73, 68, 83]).decode()
LIVE_STRATEGY_IDS = "LIVE_STRATEGY_IDS"
STRATEGY_IDS = "STRATEGY_IDS"
STRATEGY_ID = "STRATEGY_ID"


_ENV_FILE_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk")
_BOOL_TRUE_VALUES = {"1", "true", "yes", "on"}
_BOOL_FALSE_VALUES = {"0", "false", "no", "off"}
_STRATEGY_ID_MIN = 1
_STRATEGY_ID_MAX = 10

_INT_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "STRATEGY_ID",
        "MAX_CONSECUTIVE_LOSSES",
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
        "STRATEGY9_STABILITY_SAMPLE_COUNT",
        "STRATEGY9_STABILITY_REQUIRED_COUNT",
        "WS_QUOTE_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
        "WS_LOG_EVERY_UPDATES",
        "OPEN_DELAY_SECONDS",
        "ENTRY_GRACE_SECONDS",
        "HISTORY_ENTRY_FIDELITY_SECONDS",
        "HISTORY_ENTRY_MAX_OFFSET_SECONDS",
        "POLYMARKET_CHAIN_ID",
        "POLYMARKET_SIGNATURE_TYPE",
    }
)
_FLOAT_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "PAPER_SIMULATED_WALLET_BALANCE",
        "TARGET_PROFIT",
        "BASE_ORDER_COST",
        "MIN_STAKE",
        "MAX_STAKE",
        "MAX_PRICE_THRESHOLD",
        "MIN_PRICE_THRESHOLD",
        "MIN_ENTRY_PRICE",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "OFI_THRESHOLD",
        "MAX_ENTRY_PRICE",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MAX_MOMENTUM_DELTA",
        "STRATEGY7_MAX_ENTRY_PRICE",
        "STRATEGY7_MIN_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
        "STRATEGY7_SIZING_REFERENCE_PRICE",
        "STRATEGY7_SIZING_PRICE_STEP",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
        "STRATEGY7_SIZING_MIN_MULTIPLIER",
        "STRATEGY7_SIZING_MAX_MULTIPLIER",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
        "STRATEGY9_SIZING_REFERENCE_PRICE",
        "STRATEGY9_SIZING_PRICE_STEP",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
        "STRATEGY9_SIZING_MIN_MULTIPLIER",
        "STRATEGY9_SIZING_MAX_MULTIPLIER",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
        "STRATEGY9_STABILITY_WINDOW_SECONDS",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
        "STRATEGY9_MAX_SIGNAL_DECAY",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
        "STRATEGY9_STRONG_SIGNAL_GAP",
        "STRATEGY9_ULTRA_SIGNAL_GAP",
        "STRATEGY10_MIN_EDGE",
        "STRATEGY10_EDGE_BUFFER",
        "STRATEGY10_OFI_WEIGHT",
        "STRATEGY10_MOMENTUM_WEIGHT",
        "STRATEGY10_MAX_FAIR_VALUE",
        "BINANCE_SIGNAL_STALE_SECONDS",
        "NEAR_ENTRY_POLL_WINDOW_SECONDS",
        "POLL_INTERVAL_SECONDS",
        "FAST_POLL_INTERVAL_SECONDS",
        "FINAL_PRICE_WAIT_SECONDS",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS",
        "WS_TRADE_GUARD_STALE_SECONDS",
        "WS_PING_INTERVAL_SECONDS",
    }
)
_BOOL_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "LIVE_TRADING_ENABLED",
        "WS_ENABLED",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED",
    }
)
_SELECT_CONFIG_OPTIONS: dict[str, tuple[str, ...]] = {
    "TRADE_MODE": ("paper", "live", "both"),
    "BET_SIZING_MODE": ("FLAT_BASE_COST", "FIXED_BASE_COST", "TARGET_PROFIT"),
    "SIGNAL_WEAK_SIGNAL_MODE": ("SKIP", "FALLBACK", "FORCE"),
    "POLYMARKET_ORDER_TYPE": ("FOK", "FAK", "GTC", "GTD"),
}


def _base_config_key(key: str) -> str:
    parts = key.split("_")
    if len(parts) >= 3 and parts[0] == "PAPER" and parts[1] in {"5M", "15M"}:
        return "_".join(parts[2:])
    if len(parts) >= 4 and parts[0] in {"LIVE", "PAPER"} and parts[1] == "STRATEGY" and parts[2].isdigit():
        return "_".join(parts[3:])
    if len(parts) >= 3 and parts[0] == "STRATEGY" and parts[1].isdigit():
        return "_".join(parts[2:])
    return key


def _read_env_file_text(path: Path) -> str:
    raw_bytes = path.read_bytes()
    last_error: UnicodeDecodeError | None = None
    for encoding in _ENV_FILE_ENCODINGS:
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise UnicodeDecodeError(
        last_error.encoding,
        last_error.object,
        last_error.start,
        last_error.end,
        f"Unsupported env file encoding for {path}. Expected one of: {', '.join(_ENV_FILE_ENCODINGS)}",
    )


def load_env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in _read_env_file_text(path).splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


@contextmanager
def patched_env(overrides: dict[str, str]):
    previous: dict[str, str | None] = {}
    assigned_keys: list[str] = []

    def _restore() -> None:
        for key in reversed(assigned_keys):
            old_value = previous.get(key)
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
        assigned_keys.clear()

    try:
        for key, value in overrides.items():
            previous[key] = os.environ.get(key)
            os.environ[key] = value
            assigned_keys.append(key)
    except Exception:
        _restore()
        raise

    try:
        yield
    finally:
        _restore()


def build_config_from_env_values(values: dict[str, str]) -> AppConfig:
    with patched_env(values):
        return AppConfig()


def _invalid_value_warning(name: str, expected: str, raw: str) -> str:
    return f"Invalid value for {name}: expected {expected}, got {raw!r}"


def _invalid_strategy_entries(raw: str) -> list[str]:
    invalid: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            strategy_id = int(candidate)
        except ValueError:
            invalid.append(candidate)
            continue
        if strategy_id < _STRATEGY_ID_MIN or strategy_id > _STRATEGY_ID_MAX:
            invalid.append(candidate)
    return invalid


def _collect_strategy_list_warning(name: str, raw: str) -> str | None:
    invalid = _invalid_strategy_entries(raw)
    if invalid:
        return f"Invalid entries for {name} ignored: {', '.join(invalid)}"
    if not _parse_strategy_id_list(raw, fallback=2):
        return _invalid_value_warning(name, f"comma-separated strategy ids 1-{_STRATEGY_ID_MAX}", raw)
    return None


def collect_config_warnings(values: dict[str, str]) -> dict[str, str]:
    warnings: dict[str, str] = {}
    for key, raw_value in values.items():
        key = str(key).strip()
        raw = str(raw_value).strip()
        if not key or raw == "":
            continue
        base_key = _base_config_key(key)

        if base_key in {STRATEGY_ID, "SIGNAL_FALLBACK_STRATEGY_ID"}:
            try:
                strategy_id = int(raw)
            except ValueError:
                warnings[key] = _invalid_value_warning(key, f"strategy id 1-{_STRATEGY_ID_MAX}", raw)
                continue
            if strategy_id < _STRATEGY_ID_MIN or strategy_id > _STRATEGY_ID_MAX:
                warnings[key] = _invalid_value_warning(key, f"strategy id 1-{_STRATEGY_ID_MAX}", raw)
            continue

        if base_key in {STRATEGY_IDS, PAPER_STRATEGY_IDS, LIVE_STRATEGY_IDS} or (
            base_key == "STRATEGY_IDS" and key != base_key
        ):
            warning = _collect_strategy_list_warning(key, raw)
            if warning is not None:
                warnings[key] = warning
            continue

        if base_key == PAPER_TIMEFRAMES:
            invalid_timeframes = [
                item.strip()
                for item in raw.lower().split(",")
                if item.strip() and item.strip() not in MARKET_TIMEFRAME_DEFINITIONS
            ]
            if invalid_timeframes:
                warnings[key] = f"Invalid entries for {key} ignored: {', '.join(invalid_timeframes)}"
            continue

        if base_key == MARKET_TIMEFRAME:
            normalized_timeframe = raw.lower()
            if normalized_timeframe not in MARKET_TIMEFRAME_DEFINITIONS:
                expected = ", ".join(MARKET_TIMEFRAME_DEFINITIONS)
                warnings[key] = _invalid_value_warning(key, f"one of {expected}", raw)
            continue

        if base_key in _BOOL_CONFIG_KEYS:
            normalized = raw.lower()
            if normalized not in _BOOL_TRUE_VALUES and normalized not in _BOOL_FALSE_VALUES:
                warnings[key] = _invalid_value_warning(key, "true/false", raw)
            continue

        if base_key in _INT_CONFIG_KEYS:
            try:
                int(raw)
            except ValueError:
                warnings[key] = _invalid_value_warning(key, "integer", raw)
            continue

        if base_key in _FLOAT_CONFIG_KEYS:
            try:
                float(raw)
            except ValueError:
                warnings[key] = _invalid_value_warning(key, "number", raw)
            continue

        allowed = _SELECT_CONFIG_OPTIONS.get(base_key)
        if allowed is not None:
            raw_lower = raw.lower()
            raw_upper = raw.upper()
            if raw not in allowed and raw_lower not in allowed and raw_upper not in allowed:
                warnings[key] = _invalid_value_warning(key, f"one of {', '.join(allowed)}", raw)

    return warnings


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _BOOL_TRUE_VALUES:
        return True
    if normalized in _BOOL_FALSE_VALUES:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_optional_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_strategy_id_list(raw: str | None, *, fallback: int) -> list[int]:
    if raw is None:
        return [fallback]

    strategy_ids: list[int] = []
    seen: set[int] = set()
    for item in raw.split(','):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            strategy_id = int(candidate)
        except ValueError:
            continue
        if strategy_id < _STRATEGY_ID_MIN or strategy_id > _STRATEGY_ID_MAX or strategy_id in seen:
            continue
        seen.add(strategy_id)
        strategy_ids.append(strategy_id)

    return strategy_ids or [fallback]


@dataclass(frozen=True, slots=True)
class MarketTimeframeDefinition:
    timeframe: str
    series_id: int
    series_slug: str
    slug_prefixes: tuple[str, ...]


MARKET_TIMEFRAME_DEFINITIONS: dict[str, MarketTimeframeDefinition] = {
    "5m": MarketTimeframeDefinition(
        timeframe="5m",
        series_id=10684,
        series_slug="btc-up-or-down-5m",
        slug_prefixes=("btc-updown-5m-",),
    ),
    "15m": MarketTimeframeDefinition(
        timeframe="15m",
        series_id=10192,
        series_slug="btc-up-or-down-15m",
        slug_prefixes=("btc-up-or-down-15m-", "btc-updown-15m-"),
    ),
}


def _env_market_timeframe(default: str = "5m") -> str:
    raw = (os.getenv(MARKET_TIMEFRAME) or default).strip().lower()
    if raw in MARKET_TIMEFRAME_DEFINITIONS:
        return raw
    return default


def _env_paper_timeframes(default_timeframe: str) -> list[str]:
    raw = (os.getenv(PAPER_TIMEFRAMES) or "").strip().lower()
    if not raw:
        return [default_timeframe]

    timeframes: list[str] = []
    for item in raw.split(","):
        timeframe = item.strip().lower()
        if timeframe in MARKET_TIMEFRAME_DEFINITIONS and timeframe not in timeframes:
            timeframes.append(timeframe)
    return timeframes or [default_timeframe]


def _paper_profile_strategy_ids(prefix: str, fallback_ids: list[int], fallback_strategy_id: int) -> list[int]:
    raw = os.getenv(f"{prefix}_STRATEGY_IDS")
    if raw is None:
        return list(fallback_ids)
    return _parse_strategy_id_list(raw, fallback=fallback_strategy_id)


def _live_profile_prefix(strategy_id: int) -> str:
    return f"LIVE_STRATEGY_{strategy_id}"


def _paper_strategy_profile_prefix(strategy_id: int) -> str:
    return f"PAPER_STRATEGY_{strategy_id}"


def _strategy_profile_prefix(strategy_id: int) -> str:
    return f"STRATEGY_{strategy_id}"


def _strategy_mode_env(strategy_id: int, *, legacy_prefix: str, global_mode: str) -> str:
    return (
        os.getenv(f"{_strategy_profile_prefix(strategy_id)}_BET_SIZING_MODE")
        or os.getenv(f"{legacy_prefix}_BET_SIZING_MODE")
        or global_mode
    ).upper()


@dataclass(frozen=True, slots=True)
class PaperTimeframeProfile:
    timeframe: str
    strategy_id: int
    paper_strategy_ids: list[int]
    target_profit: float
    bet_sizing_mode: str
    base_order_cost: float
    max_consecutive_losses: int
    min_stake: float | None
    max_stake: float | None
    open_delay_seconds: int
    signal_momentum_threshold: float
    ofi_threshold: float
    binance_signal_stale_seconds: float
    strategy7_ofi_threshold: float
    strategy7_momentum_threshold: float
    strategy7_max_momentum_delta: float | None
    strategy7_min_signal_gap: float
    strategy7_confirm_before_entry_seconds: int
    strategy7_late_confirm_strong_signal_gap: float
    strategy7_late_confirm_relax_seconds: float
    strategy7_dynamic_sizing_enabled: bool
    strategy7_sizing_reference_price: float
    strategy7_sizing_price_step: float
    strategy7_sizing_price_step_reduction: float
    strategy7_sizing_min_multiplier: float
    strategy7_sizing_max_multiplier: float
    strategy7_sizing_strong_signal_gap: float
    strategy7_sizing_strong_signal_boost: float
    strategy9_dynamic_sizing_enabled: bool
    strategy9_sizing_reference_price: float
    strategy9_sizing_price_step: float
    strategy9_sizing_price_step_reduction: float
    strategy9_sizing_min_multiplier: float
    strategy9_sizing_max_multiplier: float
    strategy9_sizing_strong_signal_gap: float
    strategy9_sizing_strong_signal_boost: float
    strategy9_stability_sample_count: int
    strategy9_stability_required_count: int
    strategy9_stability_window_seconds: float
    strategy9_reversal_lookback_seconds: float
    strategy9_max_signal_decay: float
    strategy9_base_max_entry_price: float
    strategy9_strong_max_entry_price: float
    strategy9_ultra_max_entry_price: float
    strategy9_strong_signal_gap: float
    strategy9_ultra_signal_gap: float
    strategy10_min_edge: float
    strategy10_edge_buffer: float
    strategy10_ofi_weight: float
    strategy10_momentum_weight: float
    strategy10_max_fair_value: float
    min_entry_price: float | None
    max_entry_price: float
    strategy7_max_entry_price: float


@dataclass(slots=True)
class LiveStrategyProfile:
    strategy_id: int
    target_profit: float
    bet_sizing_mode: str
    base_order_cost: float
    max_consecutive_losses: int
    min_stake: float | None
    max_stake: float | None
    open_delay_seconds: int
    signal_momentum_threshold: float
    signal_fallback_strategy_id: int
    signal_weak_signal_mode: str
    signal_history_fidelity_seconds: int
    signal_anchor_max_offset_seconds: int
    signal_dynamic_threshold_k: float
    signal_dynamic_threshold_min_points: int
    signal_lock_before_entry_seconds: int
    max_stake_skip_alert_threshold: int
    ofi_threshold: float
    min_entry_price: float | None
    max_entry_price: float
    binance_signal_stale_seconds: float
    strategy7_ofi_threshold: float
    strategy7_momentum_threshold: float
    strategy7_max_momentum_delta: float | None
    strategy7_max_entry_price: float
    strategy7_min_signal_gap: float
    strategy7_confirm_before_entry_seconds: int
    strategy7_late_confirm_strong_signal_gap: float
    strategy7_late_confirm_relax_seconds: float
    strategy7_dynamic_sizing_enabled: bool
    strategy7_sizing_reference_price: float
    strategy7_sizing_price_step: float
    strategy7_sizing_price_step_reduction: float
    strategy7_sizing_min_multiplier: float
    strategy7_sizing_max_multiplier: float
    strategy7_sizing_strong_signal_gap: float
    strategy7_sizing_strong_signal_boost: float
    strategy9_dynamic_sizing_enabled: bool
    strategy9_sizing_reference_price: float
    strategy9_sizing_price_step: float
    strategy9_sizing_price_step_reduction: float
    strategy9_sizing_min_multiplier: float
    strategy9_sizing_max_multiplier: float
    strategy9_sizing_strong_signal_gap: float
    strategy9_sizing_strong_signal_boost: float
    strategy9_stability_sample_count: int
    strategy9_stability_required_count: int
    strategy9_stability_window_seconds: float
    strategy9_reversal_lookback_seconds: float
    strategy9_max_signal_decay: float
    strategy9_base_max_entry_price: float
    strategy9_strong_max_entry_price: float
    strategy9_ultra_max_entry_price: float
    strategy9_strong_signal_gap: float
    strategy9_ultra_signal_gap: float
    strategy10_min_edge: float
    strategy10_edge_buffer: float
    strategy10_ofi_weight: float
    strategy10_momentum_weight: float
    strategy10_max_fair_value: float


def _base_live_profile_for_strategy(cfg: AppConfig, strategy_id: int) -> LiveStrategyProfile:
    return LiveStrategyProfile(
        strategy_id=strategy_id,
        target_profit=cfg.target_profit,
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
        max_consecutive_losses=cfg.max_consecutive_losses,
        min_stake=cfg.min_stake,
        max_stake=cfg.max_stake,
        open_delay_seconds=cfg.open_delay_seconds,
        signal_momentum_threshold=cfg.signal_momentum_threshold,
        signal_fallback_strategy_id=cfg.signal_fallback_strategy_id,
        signal_weak_signal_mode=cfg.signal_weak_signal_mode,
        signal_history_fidelity_seconds=cfg.signal_history_fidelity_seconds,
        signal_anchor_max_offset_seconds=cfg.signal_anchor_max_offset_seconds,
        signal_dynamic_threshold_k=cfg.signal_dynamic_threshold_k,
        signal_dynamic_threshold_min_points=cfg.signal_dynamic_threshold_min_points,
        signal_lock_before_entry_seconds=cfg.signal_lock_before_entry_seconds,
        max_stake_skip_alert_threshold=cfg.max_stake_skip_alert_threshold,
        ofi_threshold=cfg.ofi_threshold,
        min_entry_price=cfg.min_entry_price,
        max_entry_price=cfg.max_entry_price,
        binance_signal_stale_seconds=cfg.binance_signal_stale_seconds,
        strategy7_ofi_threshold=cfg.strategy7_ofi_threshold,
        strategy7_momentum_threshold=cfg.strategy7_momentum_threshold,
        strategy7_max_momentum_delta=cfg.strategy7_max_momentum_delta,
        strategy7_max_entry_price=cfg.strategy7_max_entry_price,
        strategy7_min_signal_gap=cfg.strategy7_min_signal_gap,
        strategy7_confirm_before_entry_seconds=cfg.strategy7_confirm_before_entry_seconds,
        strategy7_late_confirm_strong_signal_gap=cfg.strategy7_late_confirm_strong_signal_gap,
        strategy7_late_confirm_relax_seconds=cfg.strategy7_late_confirm_relax_seconds,
        strategy7_dynamic_sizing_enabled=cfg.strategy7_dynamic_sizing_enabled,
        strategy7_sizing_reference_price=cfg.strategy7_sizing_reference_price,
        strategy7_sizing_price_step=cfg.strategy7_sizing_price_step,
        strategy7_sizing_price_step_reduction=cfg.strategy7_sizing_price_step_reduction,
        strategy7_sizing_min_multiplier=cfg.strategy7_sizing_min_multiplier,
        strategy7_sizing_max_multiplier=cfg.strategy7_sizing_max_multiplier,
        strategy7_sizing_strong_signal_gap=cfg.strategy7_sizing_strong_signal_gap,
        strategy7_sizing_strong_signal_boost=cfg.strategy7_sizing_strong_signal_boost,
        strategy9_dynamic_sizing_enabled=cfg.strategy9_dynamic_sizing_enabled,
        strategy9_sizing_reference_price=cfg.strategy9_sizing_reference_price,
        strategy9_sizing_price_step=cfg.strategy9_sizing_price_step,
        strategy9_sizing_price_step_reduction=cfg.strategy9_sizing_price_step_reduction,
        strategy9_sizing_min_multiplier=cfg.strategy9_sizing_min_multiplier,
        strategy9_sizing_max_multiplier=cfg.strategy9_sizing_max_multiplier,
        strategy9_sizing_strong_signal_gap=cfg.strategy9_sizing_strong_signal_gap,
        strategy9_sizing_strong_signal_boost=cfg.strategy9_sizing_strong_signal_boost,
        strategy9_stability_sample_count=cfg.strategy9_stability_sample_count,
        strategy9_stability_required_count=cfg.strategy9_stability_required_count,
        strategy9_stability_window_seconds=cfg.strategy9_stability_window_seconds,
        strategy9_reversal_lookback_seconds=cfg.strategy9_reversal_lookback_seconds,
        strategy9_max_signal_decay=cfg.strategy9_max_signal_decay,
        strategy9_base_max_entry_price=cfg.strategy9_base_max_entry_price,
        strategy9_strong_max_entry_price=cfg.strategy9_strong_max_entry_price,
        strategy9_ultra_max_entry_price=cfg.strategy9_ultra_max_entry_price,
        strategy9_strong_signal_gap=cfg.strategy9_strong_signal_gap,
        strategy9_ultra_signal_gap=cfg.strategy9_ultra_signal_gap,
        strategy10_min_edge=cfg.strategy10_min_edge,
        strategy10_edge_buffer=cfg.strategy10_edge_buffer,
        strategy10_ofi_weight=cfg.strategy10_ofi_weight,
        strategy10_momentum_weight=cfg.strategy10_momentum_weight,
        strategy10_max_fair_value=cfg.strategy10_max_fair_value,
    )


def _cap_profile_safety_limits(cfg: AppConfig, profile: LiveStrategyProfile) -> LiveStrategyProfile:
    if cfg.min_stake is not None and profile.min_stake is not None:
        profile.min_stake = max(profile.min_stake, cfg.min_stake)
    if cfg.max_stake is not None and profile.max_stake is not None:
        profile.max_stake = min(profile.max_stake, cfg.max_stake)
    return profile


def _profile_for_strategy_prefix(cfg: AppConfig, strategy_id: int, prefix: str) -> LiveStrategyProfile:
    min_stake_key = f"{prefix}_MIN_STAKE"
    max_stake_key = f"{prefix}_MAX_STAKE"
    legacy_strategy7_max_entry_key = f"{prefix}_STRATEGY7_MAX_ENTRY_PRICE"

    return _cap_profile_safety_limits(
        cfg,
        LiveStrategyProfile(
            strategy_id=strategy_id,
            target_profit=_env_float(f"{prefix}_TARGET_PROFIT", cfg.target_profit),
            bet_sizing_mode=_strategy_mode_env(strategy_id, legacy_prefix=prefix, global_mode=cfg.bet_sizing_mode),
            base_order_cost=_env_float(f"{prefix}_BASE_ORDER_COST", cfg.base_order_cost),
            max_consecutive_losses=_env_int(f"{prefix}_MAX_CONSECUTIVE_LOSSES", cfg.max_consecutive_losses),
            min_stake=_env_optional_float(min_stake_key) if os.getenv(min_stake_key) is not None else cfg.min_stake,
            max_stake=_env_optional_float(max_stake_key) if os.getenv(max_stake_key) is not None else cfg.max_stake,
            open_delay_seconds=_env_int(f"{prefix}_OPEN_DELAY_SECONDS", cfg.open_delay_seconds),
            signal_momentum_threshold=_env_float(f"{prefix}_SIGNAL_MOMENTUM_THRESHOLD", cfg.signal_momentum_threshold),
            signal_fallback_strategy_id=_env_int(
                f"{prefix}_SIGNAL_FALLBACK_STRATEGY_ID",
                cfg.signal_fallback_strategy_id,
            ),
            signal_weak_signal_mode=(os.getenv(f"{prefix}_SIGNAL_WEAK_SIGNAL_MODE") or cfg.signal_weak_signal_mode).upper(),
            signal_history_fidelity_seconds=_env_int(
                f"{prefix}_SIGNAL_HISTORY_FIDELITY_SECONDS",
                cfg.signal_history_fidelity_seconds,
            ),
            signal_anchor_max_offset_seconds=_env_int(
                f"{prefix}_SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
                cfg.signal_anchor_max_offset_seconds,
            ),
            signal_dynamic_threshold_k=_env_float(
                f"{prefix}_SIGNAL_DYNAMIC_THRESHOLD_K",
                cfg.signal_dynamic_threshold_k,
            ),
            signal_dynamic_threshold_min_points=_env_int(
                f"{prefix}_SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
                cfg.signal_dynamic_threshold_min_points,
            ),
            signal_lock_before_entry_seconds=_env_int(
                f"{prefix}_SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
                cfg.signal_lock_before_entry_seconds,
            ),
            max_stake_skip_alert_threshold=_env_int(
                f"{prefix}_MAX_STAKE_SKIP_ALERT_THRESHOLD",
                cfg.max_stake_skip_alert_threshold,
            ),
            ofi_threshold=_env_float(f"{prefix}_OFI_THRESHOLD", cfg.ofi_threshold),
            min_entry_price=_env_optional_float(f"{prefix}_MIN_ENTRY_PRICE")
            if os.getenv(f"{prefix}_MIN_ENTRY_PRICE") is not None
            else cfg.min_entry_price,
            max_entry_price=_env_float(
                f"{prefix}_MAX_ENTRY_PRICE",
                _env_float(legacy_strategy7_max_entry_key, cfg.max_entry_price)
                if strategy_id in {7, 8, 9, 10} and os.getenv(legacy_strategy7_max_entry_key) is not None
                else cfg.max_entry_price,
            ),
            binance_signal_stale_seconds=_env_float(
                f"{prefix}_BINANCE_SIGNAL_STALE_SECONDS",
                cfg.binance_signal_stale_seconds,
            ),
            strategy7_ofi_threshold=_env_float(f"{prefix}_STRATEGY7_OFI_THRESHOLD", cfg.strategy7_ofi_threshold),
            strategy7_momentum_threshold=_env_float(
                f"{prefix}_STRATEGY7_MOMENTUM_THRESHOLD",
                cfg.strategy7_momentum_threshold,
            ),
            strategy7_max_momentum_delta=(
                _env_optional_float(f"{prefix}_STRATEGY7_MAX_MOMENTUM_DELTA")
                if os.getenv(f"{prefix}_STRATEGY7_MAX_MOMENTUM_DELTA") is not None
                else cfg.strategy7_max_momentum_delta
            ),
            strategy7_max_entry_price=_env_float(
                f"{prefix}_STRATEGY7_MAX_ENTRY_PRICE",
                cfg.strategy7_max_entry_price,
            ),
            strategy7_min_signal_gap=_env_float(
                f"{prefix}_STRATEGY7_MIN_SIGNAL_GAP",
                cfg.strategy7_min_signal_gap,
            ),
            strategy7_confirm_before_entry_seconds=_env_int(
                f"{prefix}_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
                cfg.strategy7_confirm_before_entry_seconds,
            ),
            strategy7_late_confirm_strong_signal_gap=_env_float(
                f"{prefix}_STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
                cfg.strategy7_late_confirm_strong_signal_gap,
            ),
            strategy7_late_confirm_relax_seconds=_env_float(
                f"{prefix}_STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
                cfg.strategy7_late_confirm_relax_seconds,
            ),
            strategy7_dynamic_sizing_enabled=_env_bool(
                f"{prefix}_STRATEGY7_DYNAMIC_SIZING_ENABLED",
                cfg.strategy7_dynamic_sizing_enabled,
            ),
            strategy7_sizing_reference_price=_env_float(
                f"{prefix}_STRATEGY7_SIZING_REFERENCE_PRICE",
                cfg.strategy7_sizing_reference_price,
            ),
            strategy7_sizing_price_step=_env_float(
                f"{prefix}_STRATEGY7_SIZING_PRICE_STEP",
                cfg.strategy7_sizing_price_step,
            ),
            strategy7_sizing_price_step_reduction=_env_float(
                f"{prefix}_STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
                cfg.strategy7_sizing_price_step_reduction,
            ),
            strategy7_sizing_min_multiplier=_env_float(
                f"{prefix}_STRATEGY7_SIZING_MIN_MULTIPLIER",
                cfg.strategy7_sizing_min_multiplier,
            ),
            strategy7_sizing_max_multiplier=_env_float(
                f"{prefix}_STRATEGY7_SIZING_MAX_MULTIPLIER",
                cfg.strategy7_sizing_max_multiplier,
            ),
            strategy7_sizing_strong_signal_gap=_env_float(
                f"{prefix}_STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
                cfg.strategy7_sizing_strong_signal_gap,
            ),
            strategy7_sizing_strong_signal_boost=_env_float(
                f"{prefix}_STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
                cfg.strategy7_sizing_strong_signal_boost,
            ),
            strategy9_dynamic_sizing_enabled=_env_bool(
                f"{prefix}_STRATEGY9_DYNAMIC_SIZING_ENABLED",
                cfg.strategy9_dynamic_sizing_enabled,
            ),
            strategy9_sizing_reference_price=_env_float(
                f"{prefix}_STRATEGY9_SIZING_REFERENCE_PRICE",
                cfg.strategy9_sizing_reference_price,
            ),
            strategy9_sizing_price_step=_env_float(
                f"{prefix}_STRATEGY9_SIZING_PRICE_STEP",
                cfg.strategy9_sizing_price_step,
            ),
            strategy9_sizing_price_step_reduction=_env_float(
                f"{prefix}_STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
                cfg.strategy9_sizing_price_step_reduction,
            ),
            strategy9_sizing_min_multiplier=_env_float(
                f"{prefix}_STRATEGY9_SIZING_MIN_MULTIPLIER",
                cfg.strategy9_sizing_min_multiplier,
            ),
            strategy9_sizing_max_multiplier=_env_float(
                f"{prefix}_STRATEGY9_SIZING_MAX_MULTIPLIER",
                cfg.strategy9_sizing_max_multiplier,
            ),
            strategy9_sizing_strong_signal_gap=_env_float(
                f"{prefix}_STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
                cfg.strategy9_sizing_strong_signal_gap,
            ),
            strategy9_sizing_strong_signal_boost=_env_float(
                f"{prefix}_STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
                cfg.strategy9_sizing_strong_signal_boost,
            ),
            strategy9_stability_sample_count=_env_int(
                f"{prefix}_STRATEGY9_STABILITY_SAMPLE_COUNT",
                cfg.strategy9_stability_sample_count,
            ),
            strategy9_stability_required_count=_env_int(
                f"{prefix}_STRATEGY9_STABILITY_REQUIRED_COUNT",
                cfg.strategy9_stability_required_count,
            ),
            strategy9_stability_window_seconds=_env_float(
                f"{prefix}_STRATEGY9_STABILITY_WINDOW_SECONDS",
                cfg.strategy9_stability_window_seconds,
            ),
            strategy9_reversal_lookback_seconds=_env_float(
                f"{prefix}_STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
                cfg.strategy9_reversal_lookback_seconds,
            ),
            strategy9_max_signal_decay=_env_float(
                f"{prefix}_STRATEGY9_MAX_SIGNAL_DECAY",
                cfg.strategy9_max_signal_decay,
            ),
            strategy9_base_max_entry_price=_env_float(
                f"{prefix}_STRATEGY9_BASE_MAX_ENTRY_PRICE",
                cfg.strategy9_base_max_entry_price,
            ),
            strategy9_strong_max_entry_price=_env_float(
                f"{prefix}_STRATEGY9_STRONG_MAX_ENTRY_PRICE",
                cfg.strategy9_strong_max_entry_price,
            ),
            strategy9_ultra_max_entry_price=_env_float(
                f"{prefix}_STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
                cfg.strategy9_ultra_max_entry_price,
            ),
            strategy9_strong_signal_gap=_env_float(
                f"{prefix}_STRATEGY9_STRONG_SIGNAL_GAP",
                cfg.strategy9_strong_signal_gap,
            ),
            strategy9_ultra_signal_gap=_env_float(
                f"{prefix}_STRATEGY9_ULTRA_SIGNAL_GAP",
                cfg.strategy9_ultra_signal_gap,
            ),
            strategy10_min_edge=_env_float(f"{prefix}_STRATEGY10_MIN_EDGE", cfg.strategy10_min_edge),
            strategy10_edge_buffer=_env_float(f"{prefix}_STRATEGY10_EDGE_BUFFER", cfg.strategy10_edge_buffer),
            strategy10_ofi_weight=_env_float(f"{prefix}_STRATEGY10_OFI_WEIGHT", cfg.strategy10_ofi_weight),
            strategy10_momentum_weight=_env_float(
                f"{prefix}_STRATEGY10_MOMENTUM_WEIGHT",
                cfg.strategy10_momentum_weight,
            ),
            strategy10_max_fair_value=_env_float(
                f"{prefix}_STRATEGY10_MAX_FAIR_VALUE",
                cfg.strategy10_max_fair_value,
            ),
        ),
    )


def _live_profile_for_strategy(cfg: AppConfig, strategy_id: int) -> LiveStrategyProfile:
    return _profile_for_strategy_prefix(cfg, strategy_id, _live_profile_prefix(strategy_id))


def _paper_strategy_profile_for_strategy(cfg: AppConfig, strategy_id: int) -> LiveStrategyProfile:
    return _profile_for_strategy_prefix(cfg, strategy_id, _paper_strategy_profile_prefix(strategy_id))


@dataclass(slots=True)
class AppConfig:
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    market_timeframe: str = field(default_factory=lambda: _env_market_timeframe("5m"))
    paper_timeframes: list[str] = field(default_factory=list)
    strategy_ids: list[int] = field(default_factory=list)
    paper_strategy_ids: list[int] = field(default_factory=lambda: _parse_strategy_id_list(os.getenv(PAPER_STRATEGY_IDS), fallback=_env_int(STRATEGY_ID, 2)))
    paper_simulated_wallet_balance: float = field(default_factory=lambda: _env_float("PAPER_SIMULATED_WALLET_BALANCE", 1_000_000.0))
    trade_mode: str = field(default_factory=lambda: (os.getenv("TRADE_MODE") or "paper").strip().lower() or "paper")
    strategy_id: int = field(default_factory=lambda: _env_int("STRATEGY_ID", 2))
    live_strategy_ids: list[int] = field(default_factory=list)
    target_profit: float = field(default_factory=lambda: _env_float("TARGET_PROFIT", 1.0))
    bet_sizing_mode: str = field(default_factory=lambda: (os.getenv("BET_SIZING_MODE") or "FIXED_BASE_COST").upper())
    base_order_cost: float = field(default_factory=lambda: _env_float("BASE_ORDER_COST", 1.0))
    max_consecutive_losses: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_LOSSES", 6))
    min_stake: float | None = field(default_factory=lambda: _env_optional_float("MIN_STAKE"))
    max_stake: float | None = field(default_factory=lambda: _env_optional_float("MAX_STAKE"))
    max_price_threshold: float = field(default_factory=lambda: _env_float("MAX_PRICE_THRESHOLD", 0.65))
    signal_momentum_threshold: float = field(default_factory=lambda: _env_float("SIGNAL_MOMENTUM_THRESHOLD", 0.015))
    signal_fallback_strategy_id: int = field(default_factory=lambda: _env_int("SIGNAL_FALLBACK_STRATEGY_ID", 2))
    signal_weak_signal_mode: str = field(default_factory=lambda: (os.getenv("SIGNAL_WEAK_SIGNAL_MODE") or "SKIP").upper())
    signal_history_fidelity_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_HISTORY_FIDELITY_SECONDS", 5))
    signal_anchor_max_offset_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_ANCHOR_MAX_OFFSET_SECONDS", 20))
    signal_dynamic_threshold_k: float = field(default_factory=lambda: _env_float("SIGNAL_DYNAMIC_THRESHOLD_K", 1.5))
    signal_dynamic_threshold_min_points: int = field(default_factory=lambda: _env_int("SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS", 8))
    signal_lock_before_entry_seconds: int = field(default_factory=lambda: _env_int("SIGNAL_LOCK_BEFORE_ENTRY_SECONDS", 20))
    max_stake_skip_alert_threshold: int = field(default_factory=lambda: _env_int("MAX_STAKE_SKIP_ALERT_THRESHOLD", 5))
    min_price_threshold: float | None = field(default_factory=lambda: _env_optional_float('MIN_PRICE_THRESHOLD'))
    min_entry_price: float | None = field(
        default_factory=lambda: _env_optional_float("MIN_ENTRY_PRICE")
        if os.getenv("MIN_ENTRY_PRICE") is not None
        else _env_optional_float("MIN_PRICE_THRESHOLD")
    )
    ofi_threshold: float = field(default_factory=lambda: _env_float('OFI_THRESHOLD', 0.65))
    max_entry_price: float = field(
        default_factory=lambda: _env_float("MAX_ENTRY_PRICE", _env_float("MAX_PRICE_THRESHOLD", 0.56))
    )
    strategy7_ofi_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_OFI_THRESHOLD", 0.7))
    strategy7_momentum_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_MOMENTUM_THRESHOLD", 0.025))
    strategy7_max_momentum_delta: float | None = field(default_factory=lambda: _env_optional_float("STRATEGY7_MAX_MOMENTUM_DELTA"))
    strategy7_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY7_MAX_ENTRY_PRICE", 0.54))
    strategy7_min_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY7_MIN_SIGNAL_GAP", 0.03))
    strategy7_confirm_before_entry_seconds: int = field(default_factory=lambda: _env_int("STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS", 12))
    strategy7_late_confirm_strong_signal_gap: float = field(
        default_factory=lambda: _env_float("STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP", 0.02)
    )
    strategy7_late_confirm_relax_seconds: float = field(
        default_factory=lambda: _env_float("STRATEGY7_LATE_CONFIRM_RELAX_SECONDS", 0.0)
    )
    strategy7_dynamic_sizing_enabled: bool = field(default_factory=lambda: _env_bool("STRATEGY7_DYNAMIC_SIZING_ENABLED", False))
    strategy7_sizing_reference_price: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_REFERENCE_PRICE", 0.50))
    strategy7_sizing_price_step: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_PRICE_STEP", 0.01))
    strategy7_sizing_price_step_reduction: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_PRICE_STEP_REDUCTION", 0.10))
    strategy7_sizing_min_multiplier: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_MIN_MULTIPLIER", 0.50))
    strategy7_sizing_max_multiplier: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_MAX_MULTIPLIER", 1.00))
    strategy7_sizing_strong_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_STRONG_SIGNAL_GAP", 0.02))
    strategy7_sizing_strong_signal_boost: float = field(default_factory=lambda: _env_float("STRATEGY7_SIZING_STRONG_SIGNAL_BOOST", 0.20))
    strategy9_dynamic_sizing_enabled: bool = field(default_factory=lambda: _env_bool("STRATEGY9_DYNAMIC_SIZING_ENABLED", False))
    strategy9_sizing_reference_price: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_REFERENCE_PRICE", _env_float("STRATEGY7_SIZING_REFERENCE_PRICE", 0.50)))
    strategy9_sizing_price_step: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_PRICE_STEP", _env_float("STRATEGY7_SIZING_PRICE_STEP", 0.01)))
    strategy9_sizing_price_step_reduction: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_PRICE_STEP_REDUCTION", _env_float("STRATEGY7_SIZING_PRICE_STEP_REDUCTION", 0.10)))
    strategy9_sizing_min_multiplier: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_MIN_MULTIPLIER", _env_float("STRATEGY7_SIZING_MIN_MULTIPLIER", 0.50)))
    strategy9_sizing_max_multiplier: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_MAX_MULTIPLIER", _env_float("STRATEGY7_SIZING_MAX_MULTIPLIER", 1.00)))
    strategy9_sizing_strong_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_STRONG_SIGNAL_GAP", _env_float("STRATEGY7_SIZING_STRONG_SIGNAL_GAP", 0.02)))
    strategy9_sizing_strong_signal_boost: float = field(default_factory=lambda: _env_float("STRATEGY9_SIZING_STRONG_SIGNAL_BOOST", _env_float("STRATEGY7_SIZING_STRONG_SIGNAL_BOOST", 0.20)))
    strategy9_stability_sample_count: int = field(default_factory=lambda: _env_int("STRATEGY9_STABILITY_SAMPLE_COUNT", 3))
    strategy9_stability_required_count: int = field(default_factory=lambda: _env_int("STRATEGY9_STABILITY_REQUIRED_COUNT", 2))
    strategy9_stability_window_seconds: float = field(default_factory=lambda: _env_float("STRATEGY9_STABILITY_WINDOW_SECONDS", 6.0))
    strategy9_reversal_lookback_seconds: float = field(default_factory=lambda: _env_float("STRATEGY9_REVERSAL_LOOKBACK_SECONDS", 6.0))
    strategy9_max_signal_decay: float = field(default_factory=lambda: _env_float("STRATEGY9_MAX_SIGNAL_DECAY", 0.35))
    strategy9_base_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY9_BASE_MAX_ENTRY_PRICE", 0.52))
    strategy9_strong_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY9_STRONG_MAX_ENTRY_PRICE", 0.53))
    strategy9_ultra_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY9_ULTRA_MAX_ENTRY_PRICE", 0.54))
    strategy9_strong_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY9_STRONG_SIGNAL_GAP", 0.02))
    strategy9_ultra_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY9_ULTRA_SIGNAL_GAP", 0.04))
    strategy10_min_edge: float = field(default_factory=lambda: _env_float("STRATEGY10_MIN_EDGE", 0.04))
    strategy10_edge_buffer: float = field(default_factory=lambda: _env_float("STRATEGY10_EDGE_BUFFER", 0.005))
    strategy10_ofi_weight: float = field(default_factory=lambda: _env_float("STRATEGY10_OFI_WEIGHT", 0.08))
    strategy10_momentum_weight: float = field(default_factory=lambda: _env_float("STRATEGY10_MOMENTUM_WEIGHT", 1.0))
    strategy10_max_fair_value: float = field(default_factory=lambda: _env_float("STRATEGY10_MAX_FAIR_VALUE", 0.85))
    binance_ws_url: str = field(default_factory=lambda: os.getenv('BINANCE_WS_URL') or 'wss://stream.binance.com:9443/ws')
    binance_depth_stream: str = field(default_factory=lambda: os.getenv('BINANCE_DEPTH_STREAM') or 'btcusdt@depth5')
    binance_signal_stale_seconds: float = field(default_factory=lambda: _env_float('BINANCE_SIGNAL_STALE_SECONDS', 2.0))
    poll_interval_seconds: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 5))
    near_entry_poll_window_seconds: float = field(
        default_factory=lambda: _env_float("NEAR_ENTRY_POLL_WINDOW_SECONDS", 10.0)
    )
    fast_poll_interval_seconds: float = field(
        default_factory=lambda: _env_float("FAST_POLL_INTERVAL_SECONDS", 1.0)
    )
    final_price_wait_seconds: float = field(
        default_factory=lambda: _env_float("FINAL_PRICE_WAIT_SECONDS", 4.0)
    )
    final_price_poll_interval_seconds: float = field(
        default_factory=lambda: _env_float("FINAL_PRICE_POLL_INTERVAL_SECONDS", 0.75)
    )
    ws_enabled: bool = field(default_factory=lambda: _env_bool("WS_ENABLED", True))
    ws_market_url: str = field(default_factory=lambda: os.getenv("WS_MARKET_URL") or "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    ws_quote_stale_seconds: int = field(default_factory=lambda: _env_int("WS_QUOTE_STALE_SECONDS", 3))
    ws_trade_guard_stale_seconds: float = field(default_factory=lambda: _env_float("WS_TRADE_GUARD_STALE_SECONDS", 1.5))
    ws_connect_timeout_seconds: int = field(default_factory=lambda: _env_int("WS_CONNECT_TIMEOUT_SECONDS", 5))
    ws_ping_interval_seconds: float = field(default_factory=lambda: _env_float("WS_PING_INTERVAL_SECONDS", 10.0))
    ws_log_every_updates: int = field(default_factory=lambda: _env_int("WS_LOG_EVERY_UPDATES", 200))
    runtime_error_backoff_base_seconds: int = 5
    runtime_error_backoff_max_seconds: int = 60
    api_retry_count: int = 4
    api_retry_base_delay_seconds: float = 1.0
    api_retry_max_delay_seconds: float = 8.0
    entry_timing: str = "OPEN"
    open_delay_seconds: int = field(default_factory=lambda: _env_int("OPEN_DELAY_SECONDS", 5))
    entry_grace_seconds: int = field(default_factory=lambda: _env_int("ENTRY_GRACE_SECONDS", 5))
    preclose_seconds: int = 30
    history_lookback_seconds: int = 900
    history_entry_fidelity_seconds: int = field(default_factory=lambda: _env_int("HISTORY_ENTRY_FIDELITY_SECONDS", 5))
    history_entry_max_offset_seconds: int = field(default_factory=lambda: _env_int("HISTORY_ENTRY_MAX_OFFSET_SECONDS", 120))
    history_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    live_trading_enabled: bool = field(default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False))
    live_private_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY"))
    live_api_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY"))
    live_api_secret: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET"))
    live_api_passphrase: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE"))
    live_chain_id: int = field(default_factory=lambda: _env_int("POLYMARKET_CHAIN_ID", 137))
    live_signature_type: int = field(default_factory=lambda: _env_int("POLYMARKET_SIGNATURE_TYPE", 0))
    live_funder: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER"))
    live_order_type: str = field(default_factory=lambda: (os.getenv("POLYMARKET_ORDER_TYPE") or "FOK").upper())
    paper_profiles: dict[str, PaperTimeframeProfile] = field(init=False)
    paper_strategy_profiles: dict[int, LiveStrategyProfile] = field(init=False)
    live_profiles: dict[int, LiveStrategyProfile] = field(init=False)

    def __post_init__(self) -> None:
        if os.getenv("MAX_ENTRY_PRICE") is None and self.strategy7_max_entry_price != 0.54:
            self.max_entry_price = self.strategy7_max_entry_price
        if not self.paper_timeframes:
            self.paper_timeframes = _env_paper_timeframes(self.market_timeframe)
        raw_strategy_ids = os.getenv(STRATEGY_IDS)
        legacy_strategy_ids: list[int] = []
        if not self.strategy_ids and raw_strategy_ids is not None and raw_strategy_ids.strip():
            self.strategy_ids = _parse_strategy_id_list(raw_strategy_ids, fallback=self.strategy_id)
        if raw_strategy_ids is not None and raw_strategy_ids.strip() and self.strategy_ids:
            legacy_strategy_ids = list(self.strategy_ids)
        if os.getenv(PAPER_STRATEGY_IDS) is None and legacy_strategy_ids:
            self.paper_strategy_ids = list(legacy_strategy_ids)
        if os.getenv(LIVE_STRATEGY_IDS) is None and legacy_strategy_ids:
            self.live_strategy_ids = list(legacy_strategy_ids)
        elif not self.live_strategy_ids:
            self.live_strategy_ids = _parse_strategy_id_list(os.getenv(LIVE_STRATEGY_IDS), fallback=self.strategy_id)
        self.paper_profiles = {}
        for timeframe in self.paper_timeframes:
            prefix = f"PAPER_{timeframe.upper()}"
            strategy_id = _env_int(f"{prefix}_STRATEGY_ID", self.strategy_id)
            self.paper_profiles[timeframe] = PaperTimeframeProfile(
                timeframe=timeframe,
                strategy_id=strategy_id,
                paper_strategy_ids=_paper_profile_strategy_ids(prefix, self.paper_strategy_ids, strategy_id),
                target_profit=_env_float(f"{prefix}_TARGET_PROFIT", self.target_profit),
                bet_sizing_mode=_strategy_mode_env(strategy_id, legacy_prefix=prefix, global_mode=self.bet_sizing_mode),
                base_order_cost=_env_float(f"{prefix}_BASE_ORDER_COST", self.base_order_cost),
                max_consecutive_losses=_env_int(f"{prefix}_MAX_CONSECUTIVE_LOSSES", self.max_consecutive_losses),
                min_stake=(
                    _env_optional_float(f"{prefix}_MIN_STAKE")
                    if os.getenv(f"{prefix}_MIN_STAKE") is not None
                    else self.min_stake
                ),
                max_stake=(
                    _env_optional_float(f"{prefix}_MAX_STAKE")
                    if os.getenv(f"{prefix}_MAX_STAKE") is not None
                    else self.max_stake
                ),
                open_delay_seconds=_env_int(f"{prefix}_OPEN_DELAY_SECONDS", self.open_delay_seconds),
                signal_momentum_threshold=_env_float(
                    f"{prefix}_SIGNAL_MOMENTUM_THRESHOLD",
                    self.signal_momentum_threshold,
                ),
                ofi_threshold=_env_float(f"{prefix}_OFI_THRESHOLD", self.ofi_threshold),
                binance_signal_stale_seconds=_env_float(
                    f"{prefix}_BINANCE_SIGNAL_STALE_SECONDS",
                    self.binance_signal_stale_seconds,
                ),
                strategy7_ofi_threshold=_env_float(
                    f"{prefix}_STRATEGY7_OFI_THRESHOLD",
                    self.strategy7_ofi_threshold,
                ),
                strategy7_momentum_threshold=_env_float(
                    f"{prefix}_STRATEGY7_MOMENTUM_THRESHOLD",
                    self.strategy7_momentum_threshold,
                ),
                strategy7_max_momentum_delta=(
                    _env_optional_float(f"{prefix}_STRATEGY7_MAX_MOMENTUM_DELTA")
                    if os.getenv(f"{prefix}_STRATEGY7_MAX_MOMENTUM_DELTA") is not None
                    else self.strategy7_max_momentum_delta
                ),
                strategy7_min_signal_gap=_env_float(
                    f"{prefix}_STRATEGY7_MIN_SIGNAL_GAP",
                    self.strategy7_min_signal_gap,
                ),
                strategy7_confirm_before_entry_seconds=_env_int(
                    f"{prefix}_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
                    self.strategy7_confirm_before_entry_seconds,
                ),
                strategy7_late_confirm_strong_signal_gap=_env_float(
                    f"{prefix}_STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
                    self.strategy7_late_confirm_strong_signal_gap,
                ),
                strategy7_late_confirm_relax_seconds=_env_float(
                    f"{prefix}_STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
                    self.strategy7_late_confirm_relax_seconds,
                ),
                strategy7_dynamic_sizing_enabled=_env_bool(
                    f"{prefix}_STRATEGY7_DYNAMIC_SIZING_ENABLED",
                    self.strategy7_dynamic_sizing_enabled,
                ),
                strategy7_sizing_reference_price=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_REFERENCE_PRICE",
                    self.strategy7_sizing_reference_price,
                ),
                strategy7_sizing_price_step=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_PRICE_STEP",
                    self.strategy7_sizing_price_step,
                ),
                strategy7_sizing_price_step_reduction=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
                    self.strategy7_sizing_price_step_reduction,
                ),
                strategy7_sizing_min_multiplier=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_MIN_MULTIPLIER",
                    self.strategy7_sizing_min_multiplier,
                ),
                strategy7_sizing_max_multiplier=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_MAX_MULTIPLIER",
                    self.strategy7_sizing_max_multiplier,
                ),
                strategy7_sizing_strong_signal_gap=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
                    self.strategy7_sizing_strong_signal_gap,
                ),
                strategy7_sizing_strong_signal_boost=_env_float(
                    f"{prefix}_STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
                    self.strategy7_sizing_strong_signal_boost,
                ),
                strategy9_dynamic_sizing_enabled=_env_bool(
                    f"{prefix}_STRATEGY9_DYNAMIC_SIZING_ENABLED",
                    self.strategy9_dynamic_sizing_enabled,
                ),
                strategy9_sizing_reference_price=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_REFERENCE_PRICE",
                    self.strategy9_sizing_reference_price,
                ),
                strategy9_sizing_price_step=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_PRICE_STEP",
                    self.strategy9_sizing_price_step,
                ),
                strategy9_sizing_price_step_reduction=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
                    self.strategy9_sizing_price_step_reduction,
                ),
                strategy9_sizing_min_multiplier=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_MIN_MULTIPLIER",
                    self.strategy9_sizing_min_multiplier,
                ),
                strategy9_sizing_max_multiplier=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_MAX_MULTIPLIER",
                    self.strategy9_sizing_max_multiplier,
                ),
                strategy9_sizing_strong_signal_gap=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
                    self.strategy9_sizing_strong_signal_gap,
                ),
                strategy9_sizing_strong_signal_boost=_env_float(
                    f"{prefix}_STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
                    self.strategy9_sizing_strong_signal_boost,
                ),
                strategy9_stability_sample_count=_env_int(
                    f"{prefix}_STRATEGY9_STABILITY_SAMPLE_COUNT",
                    self.strategy9_stability_sample_count,
                ),
                strategy9_stability_required_count=_env_int(
                    f"{prefix}_STRATEGY9_STABILITY_REQUIRED_COUNT",
                    self.strategy9_stability_required_count,
                ),
                strategy9_stability_window_seconds=_env_float(
                    f"{prefix}_STRATEGY9_STABILITY_WINDOW_SECONDS",
                    self.strategy9_stability_window_seconds,
                ),
                strategy9_reversal_lookback_seconds=_env_float(
                    f"{prefix}_STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
                    self.strategy9_reversal_lookback_seconds,
                ),
                strategy9_max_signal_decay=_env_float(
                    f"{prefix}_STRATEGY9_MAX_SIGNAL_DECAY",
                    self.strategy9_max_signal_decay,
                ),
                strategy9_base_max_entry_price=_env_float(
                    f"{prefix}_STRATEGY9_BASE_MAX_ENTRY_PRICE",
                    self.strategy9_base_max_entry_price,
                ),
                strategy9_strong_max_entry_price=_env_float(
                    f"{prefix}_STRATEGY9_STRONG_MAX_ENTRY_PRICE",
                    self.strategy9_strong_max_entry_price,
                ),
                strategy9_ultra_max_entry_price=_env_float(
                    f"{prefix}_STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
                    self.strategy9_ultra_max_entry_price,
                ),
                strategy9_strong_signal_gap=_env_float(
                    f"{prefix}_STRATEGY9_STRONG_SIGNAL_GAP",
                    self.strategy9_strong_signal_gap,
                ),
                strategy9_ultra_signal_gap=_env_float(
                    f"{prefix}_STRATEGY9_ULTRA_SIGNAL_GAP",
                    self.strategy9_ultra_signal_gap,
                ),
                strategy10_min_edge=_env_float(f"{prefix}_STRATEGY10_MIN_EDGE", self.strategy10_min_edge),
                strategy10_edge_buffer=_env_float(f"{prefix}_STRATEGY10_EDGE_BUFFER", self.strategy10_edge_buffer),
                strategy10_ofi_weight=_env_float(f"{prefix}_STRATEGY10_OFI_WEIGHT", self.strategy10_ofi_weight),
                strategy10_momentum_weight=_env_float(
                    f"{prefix}_STRATEGY10_MOMENTUM_WEIGHT",
                    self.strategy10_momentum_weight,
                ),
                strategy10_max_fair_value=_env_float(
                    f"{prefix}_STRATEGY10_MAX_FAIR_VALUE",
                    self.strategy10_max_fair_value,
                ),
                min_entry_price=(
                    _env_optional_float(f"{prefix}_MIN_ENTRY_PRICE")
                    if os.getenv(f"{prefix}_MIN_ENTRY_PRICE") is not None
                    else self.min_entry_price
                ),
                max_entry_price=_env_float(f"{prefix}_MAX_ENTRY_PRICE", self.max_entry_price),
                strategy7_max_entry_price=_env_float(
                    f"{prefix}_STRATEGY7_MAX_ENTRY_PRICE",
                    self.strategy7_max_entry_price,
                ),
            )
        self.paper_strategy_profiles = {
            strategy_id: _paper_strategy_profile_for_strategy(self, strategy_id)
            for strategy_id in self.paper_strategy_ids
        }
        self.live_profiles = {
            strategy_id: _live_profile_for_strategy(self, strategy_id)
            for strategy_id in self.live_strategy_ids
        }

    @property
    def market_definition(self) -> MarketTimeframeDefinition:
        return MARKET_TIMEFRAME_DEFINITIONS[self.market_timeframe]

    @property
    def series_id(self) -> int:
        return self.market_definition.series_id

    @property
    def series_slug(self) -> str:
        return self.market_definition.series_slug

    @property
    def series_slug_prefixes(self) -> tuple[str, ...]:
        return self.market_definition.slug_prefixes
