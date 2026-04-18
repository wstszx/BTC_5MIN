from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MARKET_TIMEFRAME = "MARKET_TIMEFRAME"
PAPER_STRATEGY_IDS = bytes([80, 65, 80, 69, 82, 95, 83, 84, 82, 65, 84, 69, 71, 89, 95, 73, 68, 83]).decode()
STRATEGY_ID = bytes([83, 84, 82, 65, 84, 69, 71, 89, 95, 73, 68]).decode()


_ENV_FILE_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gbk")


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
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
        if strategy_id < 1 or strategy_id > 7 or strategy_id in seen:
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


@dataclass(slots=True)
class AppConfig:
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    market_timeframe: str = field(default_factory=lambda: _env_market_timeframe("5m"))
    paper_strategy_ids: list[int] = field(default_factory=lambda: _parse_strategy_id_list(os.getenv(PAPER_STRATEGY_IDS), fallback=_env_int(STRATEGY_ID, 2)))
    trade_mode: str = field(default_factory=lambda: (os.getenv("TRADE_MODE") or "paper").strip().lower() or "paper")
    strategy_id: int = field(default_factory=lambda: _env_int("STRATEGY_ID", 2))
    target_profit: float = field(default_factory=lambda: _env_float("TARGET_PROFIT", 1.0))
    bet_sizing_mode: str = field(default_factory=lambda: (os.getenv("BET_SIZING_MODE") or "FIXED_BASE_COST").upper())
    base_order_cost: float = field(default_factory=lambda: _env_float("BASE_ORDER_COST", 1.0))
    max_consecutive_losses: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_LOSSES", 6))
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
    ofi_threshold: float = field(default_factory=lambda: _env_float('OFI_THRESHOLD', 0.65))
    max_entry_price: float = field(default_factory=lambda: _env_float('MAX_ENTRY_PRICE', 0.56))
    strategy7_ofi_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_OFI_THRESHOLD", 0.7))
    strategy7_momentum_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_MOMENTUM_THRESHOLD", 0.025))
    strategy7_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY7_MAX_ENTRY_PRICE", 0.54))
    strategy7_min_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY7_MIN_SIGNAL_GAP", 0.03))
    strategy7_confirm_before_entry_seconds: int = field(default_factory=lambda: _env_int("STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS", 12))
    binance_ws_url: str = field(default_factory=lambda: os.getenv('BINANCE_WS_URL') or 'wss://stream.binance.com:9443/ws')
    binance_depth_stream: str = field(default_factory=lambda: os.getenv('BINANCE_DEPTH_STREAM') or 'btcusdt@depth5')
    binance_signal_stale_seconds: float = field(default_factory=lambda: _env_float('BINANCE_SIGNAL_STALE_SECONDS', 2.0))
    poll_interval_seconds: int = 5
    ws_enabled: bool = field(default_factory=lambda: _env_bool("WS_ENABLED", True))
    ws_market_url: str = field(default_factory=lambda: os.getenv("WS_MARKET_URL") or "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    ws_quote_stale_seconds: int = field(default_factory=lambda: _env_int("WS_QUOTE_STALE_SECONDS", 3))
    ws_trade_guard_stale_seconds: float = field(default_factory=lambda: _env_float("WS_TRADE_GUARD_STALE_SECONDS", 1.5))
    ws_connect_timeout_seconds: int = field(default_factory=lambda: _env_int("WS_CONNECT_TIMEOUT_SECONDS", 5))
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
    live_redeem_builder_api_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_BUILDER_API_KEY"))
    live_redeem_builder_secret: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_BUILDER_SECRET"))
    live_redeem_builder_passphrase: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_BUILDER_PASSPHRASE"))
    live_redeem_relayer_api_key: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_RELAYER_API_KEY"))
    live_redeem_relayer_api_key_address: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_RELAYER_API_KEY_ADDRESS"))
    live_chain_id: int = field(default_factory=lambda: _env_int("POLYMARKET_CHAIN_ID", 137))
    live_signature_type: int = field(default_factory=lambda: _env_int("POLYMARKET_SIGNATURE_TYPE", 0))
    live_funder: str | None = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER"))
    live_order_type: str = field(default_factory=lambda: (os.getenv("POLYMARKET_ORDER_TYPE") or "FOK").upper())
    live_auto_redeem_enabled: bool = field(default_factory=lambda: _env_bool("LIVE_AUTO_REDEEM_ENABLED", False))
    live_auto_redeem_poll_seconds: int = field(default_factory=lambda: _env_int("LIVE_AUTO_REDEEM_POLL_SECONDS", 20))
    live_auto_redeem_max_retries: int = field(default_factory=lambda: _env_int("LIVE_AUTO_REDEEM_MAX_RETRIES", 6))
    live_auto_redeem_initial_backoff_seconds: int = field(default_factory=lambda: _env_int("LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS", 30))
    live_auto_redeem_max_backoff_seconds: int = field(default_factory=lambda: _env_int("LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS", 300))
    live_auto_redeem_dry_run: bool = field(default_factory=lambda: _env_bool("LIVE_AUTO_REDEEM_DRY_RUN", False))

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

    @property
    def live_redeem_auth_mode(self) -> str:
        if (
            self.live_redeem_builder_api_key
            and self.live_redeem_builder_secret
            and self.live_redeem_builder_passphrase
        ):
            return "builder"
        if self.live_redeem_relayer_api_key and self.live_redeem_relayer_api_key_address:
            return "relayer"
        return "unconfigured"
