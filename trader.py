from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any

from config import AppConfig
from binance_signal import BinanceDepth5SignalService
from models import LiveStrategyState, MarketQuote, MarketWindow, PaperStrategyState, PendingPaperTrade, SessionState, TradePlan, TradeRecord
from optimizer import load_optimizer_state, save_optimizer_state
from polymarket_api import PolymarketClient, extract_token_ids, parse_iso_datetime
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from strategy import get_side_for_round, strategy7_signal_gap_ok, strategy7_strong_signal_allows_late_confirm
from runtime_control import RuntimeControl


@dataclass(slots=True)
class SideDecision:
    side: str | None
    reason: str | None = None
    signal_open_up_price: float | None = None
    signal_current_up_price: float | None = None
    signal_threshold: float | None = None
    signal_delta: float | None = None
    signal_locked: bool = False


SESSION_DAY_TZ = timezone(timedelta(hours=8))
_LIVE_STRATEGY_FIELD_NAMES = (
    "round_index",
    "cash_pnl",
    "recovery_loss",
    "consecutive_losses",
    "consecutive_max_stake_skips",
    "signal_round_slug",
    "signal_round_open_up_price",
    "signal_round_locked_side",
    "strategy6_last_ofi_score",
    "stop_loss_count",
    "daily_realized_pnl",
    "current_day",
    "pending_live_slug",
    "pending_live_side",
    "pending_live_price",
    "pending_live_order_size",
    "pending_live_order_cost",
    "pending_live_expected_profit",
    "pending_live_order_id",
    "pending_live_end_time",
)


def _hydrate_pending_paper_trades(items: list[dict[str, Any]] | list[PendingPaperTrade] | None) -> list[PendingPaperTrade]:
    return [
        item if isinstance(item, PendingPaperTrade) else PendingPaperTrade(**item)
        for item in (items or [])
    ]


def _hydrate_paper_strategy_state(payload: dict[str, Any]) -> PaperStrategyState:
    strategy_payload = dict(payload)
    pending_key = bytes([112, 101, 110, 100, 105, 110, 103, 95, 112, 97, 112, 101, 114, 95, 116, 114, 97, 100, 101, 115]).decode()
    strategy_payload[pending_key] = _hydrate_pending_paper_trades(strategy_payload.get(pending_key))
    return PaperStrategyState(**strategy_payload)


def _hydrate_live_strategy_state(payload: dict[str, Any]) -> LiveStrategyState:
    return LiveStrategyState(**dict(payload))


def _apply_live_strategy_state_to_session_state(state: SessionState, strategy_state: LiveStrategyState) -> None:
    for field_name in _LIVE_STRATEGY_FIELD_NAMES:
        setattr(state, field_name, getattr(strategy_state, field_name))


def _live_strategy_state_from_payload(payload: dict[str, Any]) -> LiveStrategyState:
    return LiveStrategyState(
        round_index=payload.get("round_index", 0),
        cash_pnl=payload.get("cash_pnl", 0.0),
        recovery_loss=payload.get("recovery_loss", 0.0),
        consecutive_losses=payload.get("consecutive_losses", 0),
        consecutive_max_stake_skips=payload.get("consecutive_max_stake_skips", 0),
        signal_round_slug=payload.get("signal_round_slug"),
        signal_round_open_up_price=payload.get("signal_round_open_up_price"),
        signal_round_locked_side=payload.get("signal_round_locked_side"),
        strategy6_last_ofi_score=payload.get("strategy6_last_ofi_score"),
        stop_loss_count=payload.get("stop_loss_count", 0),
        daily_realized_pnl=payload.get("daily_realized_pnl", 0.0),
        current_day=payload.get("current_day"),
        pending_live_slug=payload.get("pending_live_slug"),
        pending_live_side=payload.get("pending_live_side"),
        pending_live_price=payload.get("pending_live_price"),
        pending_live_order_size=payload.get("pending_live_order_size"),
        pending_live_order_cost=payload.get("pending_live_order_cost"),
        pending_live_expected_profit=payload.get("pending_live_expected_profit"),
        pending_live_order_id=payload.get("pending_live_order_id"),
        pending_live_end_time=payload.get("pending_live_end_time"),
    )


def _hydrate_live_strategy_map(payload: dict[str, Any], effective_live_strategy_ids: list[int]) -> dict[int, LiveStrategyState]:
    raw_strategy_map = payload.get("live_strategies")
    if isinstance(raw_strategy_map, dict):
        hydrated_map = {
            int(raw_key): _hydrate_live_strategy_state(raw_value)
            for raw_key, raw_value in raw_strategy_map.items()
        }
        for strategy_id in effective_live_strategy_ids:
            hydrated_map.setdefault(strategy_id, LiveStrategyState())
        return hydrated_map

    if effective_live_strategy_ids:
        legacy_state = _live_strategy_state_from_payload(payload)
        hydrated_map = {
            strategy_id: LiveStrategyState()
            for strategy_id in effective_live_strategy_ids
        }
        hydrated_map[min(effective_live_strategy_ids)] = legacy_state
        return hydrated_map

    return {}


def save_session_state(path: Path, state: SessionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def _load_session_state_legacy(path: Path) -> SessionState:
    if not path.exists():
        return SessionState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    pending_paper_trades = [
        item if isinstance(item, PendingPaperTrade) else PendingPaperTrade(**item)
        for item in payload.pop("pending_paper_trades", [])
    ]
    raw_live_strategy_map = payload.pop("live_strategies", {})
    live_strategies = {}
    if isinstance(raw_live_strategy_map, dict):
        live_strategies = {
            int(raw_key): _hydrate_live_strategy_state(raw_value)
            for raw_key, raw_value in raw_live_strategy_map.items()
        }
    return SessionState(
        pending_paper_trades=pending_paper_trades,
        live_strategies=live_strategies,
        **payload,
    )


def load_session_state(
    path: Path,
    *,
    effective_paper_strategy_ids: list[int] | None = None,
    effective_live_strategy_ids: list[int] | None = None,
) -> SessionState:
    state = _load_session_state_legacy(path)
    selected_paper_strategy_ids = list(effective_paper_strategy_ids or [])
    selected_live_strategy_ids = list(effective_live_strategy_ids or [])
    if not path.exists():
        return state

    payload = json.loads(path.read_text(encoding=bytes([117, 116, 102, 45, 56]).decode()))
    raw_paper_strategy_map = payload.get(bytes([112, 97, 112, 101, 114, 95, 115, 116, 114, 97, 116, 101, 103, 105, 101, 115]).decode())
    if selected_paper_strategy_ids:
        if not isinstance(raw_paper_strategy_map, dict):
            state.paper_strategies = {
                strategy_id: PaperStrategyState(
                    round_index=state.round_index,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    consecutive_max_stake_skips=state.consecutive_max_stake_skips,
                    signal_round_slug=state.signal_round_slug,
                    signal_round_open_up_price=state.signal_round_open_up_price,
                    signal_round_locked_side=state.signal_round_locked_side,
                    strategy6_last_ofi_score=state.strategy6_last_ofi_score,
                    stop_loss_count=state.stop_loss_count,
                    daily_realized_pnl=state.daily_realized_pnl,
                    current_day=state.current_day,
                    pending_paper_trades=list(state.pending_paper_trades),
                    last_processed_paper_event_slug=state.last_processed_paper_event_slug,
                )
                for strategy_id in selected_paper_strategy_ids
            }
        else:
            hydrated_map: dict[int, PaperStrategyState] = {}
            for raw_key, raw_value in raw_paper_strategy_map.items():
                hydrated_map[int(raw_key)] = _hydrate_paper_strategy_state(raw_value)
            for strategy_id in selected_paper_strategy_ids:
                hydrated_map.setdefault(strategy_id, PaperStrategyState())
            state.paper_strategies = hydrated_map

    if selected_live_strategy_ids:
        state.live_strategies = _hydrate_live_strategy_map(payload, selected_live_strategy_ids)
        if not isinstance(payload.get("live_strategies"), dict):
            active_live_state = state.live_strategies.get(min(selected_live_strategy_ids))
        else:
            active_live_state = None
        if active_live_state is not None:
            _apply_live_strategy_state_to_session_state(state, active_live_state)
    return state


def _list_redeemable_live_positions(
    cfg: AppConfig,
    *,
    client: PolymarketClient | Any,
) -> list[dict[str, Any]]:
    target_user = (cfg.live_funder or "").strip().lower()
    if not target_user:
        return []

    rows = client.get_current_positions(user=target_user, redeemable=True)
    positions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_user = str(row.get("proxyWallet") or row.get("user") or row.get("owner") or "").strip().lower()
        if row_user != target_user:
            continue
        if not bool(row.get("redeemable")):
            continue
        condition_id = str(row.get("conditionId") or "").strip()
        if not condition_id:
            continue
        size = row.get("size")
        try:
            parsed_size = float(size)
        except (TypeError, ValueError):
            continue
        if parsed_size <= 0:
            continue
        positions.append(
            {
                "condition_id": condition_id,
                "event_slug": str(row.get("eventSlug") or row.get("event_slug") or "").strip(),
                "outcome": str(row.get("outcome") or "").strip(),
                "size": parsed_size,
                "redeemable": True,
                "user": row_user,
            }
        )
    return positions

_LIVE_REDEEM_RUNTIME_FIELDS = (
    "enabled",
    "last_poll_at",
    "last_attempt_at",
    "last_result",
    "last_tx_hash",
    "last_submission_id",
    "last_submission_status",
    "pending_redeem_count",
)
_LIVE_REDEEM_ENTRY_FIELDS = (
    "status",
    "attempt_count",
    "last_attempt_at",
    "next_attempt_at",
    "last_tx_hash",
    "last_submission_id",
    "last_submission_status",
    "event_slug",
    "outcome",
    "size",
    "redeemable",
    "user",
    "last_error",
    "completed_at",
)

def _default_live_redeem_runtime() -> dict[str, Any]:
    return {
        "enabled": False,
        "last_poll_at": None,
        "last_attempt_at": None,
        "last_result": None,
        "last_tx_hash": None,
        "last_submission_id": None,
        "last_submission_status": None,
        "pending_redeem_count": 0,
    }


def _default_live_redeem_state() -> dict[str, Any]:
    return {"conditions": {}, "runtime": _default_live_redeem_runtime()}


def _normalize_live_redeem_runtime(payload: Any) -> dict[str, Any]:
    runtime = _default_live_redeem_runtime()
    if not isinstance(payload, dict):
        return runtime
    for field_name in _LIVE_REDEEM_RUNTIME_FIELDS:
        runtime[field_name] = payload.get(field_name)
    try:
        runtime["pending_redeem_count"] = max(0, int(runtime.get("pending_redeem_count") or 0))
    except (TypeError, ValueError):
        runtime["pending_redeem_count"] = 0
    runtime["enabled"] = bool(runtime.get("enabled"))
    return runtime


def _normalize_live_redeem_entry(payload: Any) -> dict[str, Any]:
    entry = {
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": None,
        "next_attempt_at": None,
        "last_tx_hash": None,
        "last_submission_id": None,
        "last_submission_status": None,
        "event_slug": None,
        "outcome": None,
        "size": None,
        "redeemable": True,
        "user": None,
        "last_error": None,
        "completed_at": None,
    }
    if not isinstance(payload, dict):
        return entry
    for field_name in _LIVE_REDEEM_ENTRY_FIELDS:
        entry[field_name] = payload.get(field_name)
    try:
        entry["attempt_count"] = max(0, int(entry.get("attempt_count") or 0))
    except (TypeError, ValueError):
        entry["attempt_count"] = 0
    try:
        size = entry.get("size")
        entry["size"] = None if size in (None, "") else float(size)
    except (TypeError, ValueError):
        entry["size"] = None
    entry["redeemable"] = bool(entry.get("redeemable", True))
    status = str(entry.get("status") or "pending").strip().lower()
    entry["status"] = status or "pending"
    return entry


def load_live_redeem_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_live_redeem_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = _default_live_redeem_state()
    if isinstance(payload, dict):
        raw_conditions = payload.get("conditions") or {}
        if isinstance(raw_conditions, dict):
            state["conditions"] = {
                str(condition_id): _normalize_live_redeem_entry(raw_entry)
                for condition_id, raw_entry in raw_conditions.items()
            }
        state["runtime"] = _normalize_live_redeem_runtime(payload.get("runtime"))
    return state


def save_live_redeem_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = _default_live_redeem_state()
    raw_conditions = state.get("conditions") if isinstance(state, dict) else {}
    if isinstance(raw_conditions, dict):
        sanitized["conditions"] = {
            str(condition_id): _normalize_live_redeem_entry(raw_entry)
            for condition_id, raw_entry in raw_conditions.items()
        }
    sanitized["runtime"] = _normalize_live_redeem_runtime((state or {}).get("runtime") if isinstance(state, dict) else None)
    path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")


_LIVE_REDEEM_CTF_CONTRACT = os.getenv("POLYMARKET_CTF_CONTRACT") or "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_LIVE_REDEEM_COLLATERAL_TOKEN = os.getenv("POLYMARKET_COLLATERAL_TOKEN") or "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_LIVE_REDEEM_PARENT_COLLECTION_ID = "0x" + ("00" * 32)
_LIVE_REDEEM_RELAYER_URL = os.getenv("POLYMARKET_RELAYER_URL") or "https://relayer-v2.polymarket.com"
_LIVE_REDEEM_INDEX_SETS = [1, 2]
_LIVE_REDEEM_TERMINAL_STATUSES = {"completed", "submitted", "terminal_error", "dry_run"}

_LIVE_REDEEM_CTF_ABI = [
    {
        "inputs": [
            {"internalType": "contract IERC20", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def _build_live_redeem_builder_config(cfg: AppConfig):
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=str(cfg.live_redeem_builder_api_key or ""),
            secret=str(cfg.live_redeem_builder_secret or ""),
            passphrase=str(cfg.live_redeem_builder_passphrase or ""),
        )
    )


def _build_live_redeem_relayer_client(cfg: AppConfig):
    if not cfg.live_private_key:
        raise RuntimeError("Missing PRIVATE_KEY/POLYMARKET_PRIVATE_KEY for live redeem.")

    from py_builder_relayer_client.client import RelayClient

    builder_config = None
    if getattr(cfg, "live_redeem_auth_mode", "unconfigured") == "builder":
        builder_config = _build_live_redeem_builder_config(cfg)

    return RelayClient(
        _LIVE_REDEEM_RELAYER_URL,
        chain_id=int(cfg.live_chain_id),
        private_key=cfg.live_private_key,
        builder_config=builder_config,
    )


def _build_live_redeem_safe_transaction(*, condition_id: str, index_sets: list[int]):
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address
    from py_builder_relayer_client.models import OperationType, SafeTransaction

    normalized_condition_id = str(condition_id or "").strip()
    if normalized_condition_id.startswith("0x"):
        normalized_condition_id = normalized_condition_id[2:]
    if len(normalized_condition_id) != 64:
        raise RuntimeError("Live redeem condition id must be a 32-byte hex string.")

    selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    calldata = selector + encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            to_checksum_address(_LIVE_REDEEM_COLLATERAL_TOKEN),
            bytes.fromhex(_LIVE_REDEEM_PARENT_COLLECTION_ID[2:]),
            bytes.fromhex(normalized_condition_id),
            list(index_sets),
        ],
    )
    return SafeTransaction(
        to=to_checksum_address(_LIVE_REDEEM_CTF_CONTRACT),
        operation=OperationType.Call,
        data="0x" + calldata.hex(),
        value="0",
    )


def _submit_live_redeem_via_relayer_api_key(
    cfg: AppConfig,
    *,
    condition_id: str,
    event_slug: str,
    index_sets: list[int],
) -> dict[str, Any]:
    from py_builder_relayer_client.builder.safe import build_safe_transaction_request
    from py_builder_relayer_client.endpoints import SUBMIT_TRANSACTION
    from py_builder_relayer_client.http_helpers.helpers import post
    from py_builder_relayer_client.models import SafeTransactionArgs, TransactionType

    client = _build_live_redeem_relayer_client(cfg)
    safe_address = client.get_expected_safe()
    if not client.get_deployed(safe_address):
        raise RuntimeError(f"expected safe {safe_address} is not deployed")

    from_address = client.signer.address()
    nonce_payload = client.get_nonce(from_address, TransactionType.SAFE.value)
    if nonce_payload is None or nonce_payload.get("nonce") is None:
        raise RuntimeError("invalid nonce payload received")

    request_body = build_safe_transaction_request(
        signer=client.signer,
        args=SafeTransactionArgs(
            from_address=from_address,
            nonce=nonce_payload.get("nonce"),
            chain_id=int(cfg.live_chain_id),
            transactions=[_build_live_redeem_safe_transaction(condition_id=condition_id, index_sets=index_sets)],
        ),
        config=client.contract_config,
        metadata=f"Redeem positions for {event_slug}",
    ).to_dict()

    response = post(
        f"{client.relayer_url}{SUBMIT_TRANSACTION}",
        headers={
            "RELAYER_API_KEY": str(cfg.live_redeem_relayer_api_key or ""),
            "RELAYER_API_KEY_ADDRESS": str(cfg.live_redeem_relayer_api_key_address or ""),
        },
        data=request_body,
    )
    return {
        "submission_id": response.get("transactionID"),
        "tx_hash": response.get("transactionHash"),
    }


def _execute_live_redeem_via_relayer(
    cfg: AppConfig,
    *,
    condition_id: str,
    event_slug: str,
    index_sets: list[int],
) -> dict[str, Any]:
    auth_mode = getattr(cfg, "live_redeem_auth_mode", "unconfigured")
    if auth_mode == "builder":
        client = _build_live_redeem_relayer_client(cfg)
        response = client.execute(
            [_build_live_redeem_safe_transaction(condition_id=condition_id, index_sets=index_sets)],
            metadata=f"Redeem positions for {event_slug}",
        )
        return {
            "submission_id": getattr(response, "transaction_id", None),
            "tx_hash": getattr(response, "transaction_hash", None),
        }
    if auth_mode == "relayer":
        return _submit_live_redeem_via_relayer_api_key(
            cfg,
            condition_id=condition_id,
            event_slug=event_slug,
            index_sets=index_sets,
        )
    raise RuntimeError("Missing official relayer credentials for live redeem.")

def execute_live_redeem(
    cfg: AppConfig,
    *,
    condition_id: str,
    event_slug: str,
    index_sets: list[int] | None = None,
    dry_run: bool = False,
    executor: Any | None = None,
) -> str:
    resolved_index_sets = list(index_sets or _LIVE_REDEEM_INDEX_SETS)
    if dry_run:
        return f"dry-run:{condition_id}"
    if executor is not None:
        result = executor(
            cfg=cfg,
            condition_id=condition_id,
            event_slug=event_slug,
            index_sets=resolved_index_sets,
            dry_run=False,
        )
    else:
        result = _execute_live_redeem_via_relayer(
            cfg,
            condition_id=condition_id,
            event_slug=event_slug,
            index_sets=resolved_index_sets,
        )
    if isinstance(result, dict):
        submission_id = result.get("submission_id") or result.get("transactionID") or result.get("transaction_id")
        if submission_id:
            return str(submission_id)
        tx_hash = result.get("tx_hash") or result.get("transactionHash") or result.get("transaction_hash")
        if tx_hash:
            return str(tx_hash)
    return str(result)


def _live_redeem_backoff_seconds(cfg: AppConfig, attempt_count: int) -> int:
    base = max(1, int(cfg.live_auto_redeem_initial_backoff_seconds or 1))
    maximum = max(base, int(cfg.live_auto_redeem_max_backoff_seconds or base))
    scaled = base * (2 ** max(0, int(attempt_count) - 1))
    return min(maximum, scaled)


def _is_terminal_live_redeem_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    terminal_markers = (
        "already redeemed",
        "no redeemable",
        "no position",
        "insufficient balance",
        "balance is zero",
        "zero balance",
        "nothing to redeem",
    )
    return any(marker in message for marker in terminal_markers)


def _live_redeem_entry_is_due(entry: dict[str, Any], now: datetime) -> bool:
    status = str(entry.get("status") or "pending").strip().lower()
    if status in _LIVE_REDEEM_TERMINAL_STATUSES:
        return False
    next_attempt_at = parse_iso_datetime(entry.get("next_attempt_at"))
    if next_attempt_at is not None and next_attempt_at > now:
        return False
    return True


def _upsert_live_redeem_position_state(
    state: dict[str, Any],
    position: dict[str, Any],
) -> dict[str, Any]:
    conditions = state.setdefault("conditions", {})
    condition_id = str(position.get("condition_id") or "").strip()
    entry = _normalize_live_redeem_entry(conditions.get(condition_id))
    entry["event_slug"] = position.get("event_slug") or entry.get("event_slug")
    entry["outcome"] = position.get("outcome") or entry.get("outcome")
    entry["size"] = position.get("size")
    entry["redeemable"] = bool(position.get("redeemable", True))
    entry["user"] = position.get("user") or entry.get("user")
    conditions[condition_id] = entry
    return entry


def attempt_live_redeem(
    cfg: AppConfig,
    state: dict[str, Any],
    position: dict[str, Any],
    *,
    now: datetime | None = None,
    executor: Any | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    runtime = state.setdefault("runtime", _default_live_redeem_runtime())
    entry = _upsert_live_redeem_position_state(state, position)
    condition_id = str(position.get("condition_id") or "").strip()
    event_slug = str(position.get("event_slug") or entry.get("event_slug") or "").strip()

    if not _live_redeem_entry_is_due(entry, now):
        return {"status": "skipped", "reason": "not_due", "condition_id": condition_id}

    try:
        submission_ref = execute_live_redeem(
            cfg,
            condition_id=condition_id,
            event_slug=event_slug,
            index_sets=_LIVE_REDEEM_INDEX_SETS,
            dry_run=bool(cfg.live_auto_redeem_dry_run),
            executor=executor,
        )
    except Exception as exc:
        entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
        entry["last_attempt_at"] = now.isoformat()
        entry["last_error"] = str(exc)
        if _is_terminal_live_redeem_error(exc):
            entry["status"] = "terminal_error"
            entry["next_attempt_at"] = None
            runtime["last_result"] = "terminal_error"
        else:
            entry["status"] = "retry_wait"
            entry["next_attempt_at"] = (now + timedelta(seconds=_live_redeem_backoff_seconds(cfg, entry["attempt_count"]))).isoformat()
            runtime["last_result"] = "retry_wait"
        runtime["last_attempt_at"] = entry["last_attempt_at"]
        state["conditions"][condition_id] = entry
        return {
            "status": entry["status"],
            "condition_id": condition_id,
            "error": str(exc),
            "next_attempt_at": entry.get("next_attempt_at"),
        }

    entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
    entry["last_attempt_at"] = now.isoformat()
    entry["next_attempt_at"] = None
    entry["last_error"] = None
    tx_hash = None
    submission_id = None
    if str(submission_ref).startswith("dry-run:"):
        entry["status"] = "dry_run"
    else:
        entry["status"] = "submitted"
        if str(submission_ref).startswith("0x") and len(str(submission_ref)) == 66:
            tx_hash = str(submission_ref)
        else:
            submission_id = str(submission_ref)
    entry["last_tx_hash"] = tx_hash
    entry["last_submission_id"] = submission_id
    entry["last_submission_status"] = entry["status"]
    runtime["last_attempt_at"] = entry["last_attempt_at"]
    runtime["last_result"] = entry["status"]
    runtime["last_tx_hash"] = tx_hash
    runtime["last_submission_id"] = submission_id
    runtime["last_submission_status"] = entry["status"]
    state["conditions"][condition_id] = entry
    return {
        "status": entry["status"],
        "condition_id": condition_id,
        "tx_hash": tx_hash,
        "submission_id": submission_id,
    }


def _reconcile_live_redeem_state(
    state: dict[str, Any],
    positions: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    present_condition_ids = set()
    for position in positions:
        condition_id = str(position.get("condition_id") or "").strip()
        if not condition_id:
            continue
        present_condition_ids.add(condition_id)
        _upsert_live_redeem_position_state(state, position)
    conditions = state.setdefault("conditions", {})
    for condition_id, raw_entry in list(conditions.items()):
        entry = _normalize_live_redeem_entry(raw_entry)
        if condition_id not in present_condition_ids and entry.get("status") in {"submitted", "retry_wait", "dry_run"}:
            entry["status"] = "completed"
            entry["next_attempt_at"] = None
            entry["completed_at"] = now.isoformat()
            conditions[condition_id] = entry


def run_live_redeem_worker(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    state_path: Path | None = None,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
    stop_when_safe: Callable[[], bool] | None = None,
    executor: Any | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    if cfg.trade_mode != "live":
        return {"status": "skipped_non_live"}
    client_provided = market_client is not None
    state_path_provided = state_path is not None
    market_client = market_client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "live_redeem_state.json"
    while True:
        if _is_stop_requested(stop_event):
            return {"status": "stopped"}
        if _safe_stop_requested(stop_when_safe):
            return {"status": "stopped"}
        if config_provider is not None:
            candidate_cfg = config_provider()
            if candidate_cfg is not None:
                cfg = candidate_cfg
                if not client_provided:
                    market_client.config = cfg
                if not state_path_provided:
                    state_path = cfg.logs_dir / "live_redeem_state.json"
        state = load_live_redeem_state(state_path)
        runtime = state.setdefault("runtime", _default_live_redeem_runtime())
        now = datetime.now(timezone.utc)
        runtime["enabled"] = bool(cfg.live_auto_redeem_enabled)
        runtime["last_poll_at"] = now.isoformat()
        positions: list[dict[str, Any]] = []
        if cfg.live_auto_redeem_enabled:
            validate_live_runtime_config(cfg)
            positions = _list_redeemable_live_positions(cfg, client=market_client)
            _reconcile_live_redeem_state(state, positions, now=now)
            for position in positions:
                attempt_live_redeem(cfg, state, position, now=now, executor=executor)
        else:
            runtime["last_result"] = runtime.get("last_result") or "disabled"
        runtime["pending_redeem_count"] = len(positions)
        save_live_redeem_state(state_path, state)
        if not _sleep_if_not_stopped(stop_event, max(1, cfg.live_auto_redeem_poll_seconds)):
            return {"status": "stopped"}


def _paper_strategy_ids_for_runtime(cfg: AppConfig) -> list[int]:
    strategy_ids = list(getattr(cfg, "paper_strategy_ids", []) or [])
    if strategy_ids:
        return strategy_ids
    return [cfg.strategy_id]


def _binance_signal_service_url(cfg: AppConfig) -> str:
    return cfg.binance_ws_url.rstrip("/") + "/" + cfg.binance_depth_stream.lstrip("/")


def _sync_paper_binance_signal_service(
    *,
    cfg: AppConfig,
    strategy_ids: list[int],
    service: BinanceDepth5SignalService | None,
) -> BinanceDepth5SignalService | None:
    needs_service = any(strategy_id in {6, 7} for strategy_id in strategy_ids)
    expected_url = _binance_signal_service_url(cfg)

    if not needs_service:
        if service is not None:
            service.close()
        return None

    if service is not None and getattr(service, "ws_url", None) == expected_url:
        return service

    if service is not None:
        service.close()

    service = BinanceDepth5SignalService(ws_url=cfg.binance_ws_url, stream=cfg.binance_depth_stream)
    service.start()
    return service


def _load_session_state_for_paper_runtime(path: Path, strategy_ids: list[int]) -> SessionState:
    try:
        return load_session_state(path, effective_paper_strategy_ids=strategy_ids)
    except TypeError:
        return load_session_state(path)



def _paper_strategy_state_to_session_state(state: PaperStrategyState, base_state: SessionState) -> SessionState:
    return SessionState(
        round_index=state.round_index,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        consecutive_max_stake_skips=state.consecutive_max_stake_skips,
        signal_round_slug=state.signal_round_slug,
        signal_round_open_up_price=state.signal_round_open_up_price,
        signal_round_locked_side=state.signal_round_locked_side,
        strategy6_last_ofi_score=state.strategy6_last_ofi_score,
        stop_loss_count=state.stop_loss_count,
        daily_realized_pnl=state.daily_realized_pnl,
        current_day=state.current_day,
        last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        pending_live_slug=base_state.pending_live_slug,
        pending_live_side=base_state.pending_live_side,
        pending_live_price=base_state.pending_live_price,
        pending_live_order_size=base_state.pending_live_order_size,
        pending_live_order_cost=base_state.pending_live_order_cost,
        pending_live_expected_profit=base_state.pending_live_expected_profit,
        pending_live_order_id=base_state.pending_live_order_id,
        pending_live_end_time=base_state.pending_live_end_time,
        pending_paper_trades=list(state.pending_paper_trades),
        paper_strategies=dict(base_state.paper_strategies),
        live_strategies=dict(base_state.live_strategies),
    )


def _session_state_to_paper_strategy_state(state: SessionState) -> PaperStrategyState:
    return PaperStrategyState(
        round_index=state.round_index,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        consecutive_max_stake_skips=state.consecutive_max_stake_skips,
        signal_round_slug=state.signal_round_slug,
        signal_round_open_up_price=state.signal_round_open_up_price,
        signal_round_locked_side=state.signal_round_locked_side,
        strategy6_last_ofi_score=state.strategy6_last_ofi_score,
        stop_loss_count=state.stop_loss_count,
        daily_realized_pnl=state.daily_realized_pnl,
        current_day=state.current_day,
        pending_paper_trades=list(state.pending_paper_trades),
        last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        experiment_id=getattr(state, "experiment_id", None),
    )


def _ensure_paper_strategy_state_map(state: SessionState, strategy_ids: list[int]) -> None:
    if state.paper_strategies:
        for strategy_id in strategy_ids:
            state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
        return
    state.paper_strategies = {
        strategy_id: PaperStrategyState(
            round_index=state.round_index,
            cash_pnl=state.cash_pnl,
            recovery_loss=state.recovery_loss,
            consecutive_losses=state.consecutive_losses,
            consecutive_max_stake_skips=state.consecutive_max_stake_skips,
            signal_round_slug=state.signal_round_slug,
            signal_round_open_up_price=state.signal_round_open_up_price,
            signal_round_locked_side=state.signal_round_locked_side,
            strategy6_last_ofi_score=state.strategy6_last_ofi_score,
            stop_loss_count=state.stop_loss_count,
            daily_realized_pnl=state.daily_realized_pnl,
            current_day=state.current_day,
            pending_paper_trades=list(state.pending_paper_trades),
            last_processed_paper_event_slug=state.last_processed_paper_event_slug,
        )
        for strategy_id in strategy_ids
    }


def _sync_current_live_strategy_state(state: SessionState, strategy_id: int) -> None:
    if not state.live_strategies and strategy_id not in state.live_strategies:
        return
    state.live_strategies[strategy_id] = _live_strategy_state_from_payload(asdict(state))


def _sync_legacy_paper_state_fields(state: SessionState, strategy_ids: list[int]) -> None:
    if not strategy_ids:
        return
    strategy_state = state.paper_strategies.get(strategy_ids[0])
    if strategy_state is None:
        return
    state.round_index = strategy_state.round_index
    state.cash_pnl = strategy_state.cash_pnl
    state.recovery_loss = strategy_state.recovery_loss
    state.consecutive_losses = strategy_state.consecutive_losses
    state.consecutive_max_stake_skips = strategy_state.consecutive_max_stake_skips
    state.signal_round_slug = strategy_state.signal_round_slug
    state.signal_round_open_up_price = strategy_state.signal_round_open_up_price
    state.signal_round_locked_side = strategy_state.signal_round_locked_side
    state.strategy6_last_ofi_score = strategy_state.strategy6_last_ofi_score
    state.stop_loss_count = strategy_state.stop_loss_count
    state.daily_realized_pnl = strategy_state.daily_realized_pnl
    state.current_day = strategy_state.current_day
    state.last_processed_paper_event_slug = strategy_state.last_processed_paper_event_slug
    state.pending_paper_trades = list(strategy_state.pending_paper_trades)

def _clone_session_state(state: SessionState) -> SessionState:
    payload = asdict(state)
    payload["pending_paper_trades"] = _hydrate_pending_paper_trades(payload.get("pending_paper_trades"))
    raw_strategy_map = payload.get("paper_strategies") or {}
    payload["paper_strategies"] = {
        int(raw_key): _hydrate_paper_strategy_state(raw_value)
        for raw_key, raw_value in raw_strategy_map.items()
    }
    raw_live_strategy_map = payload.get("live_strategies") or {}
    payload["live_strategies"] = {
        int(raw_key): _hydrate_live_strategy_state(raw_value)
        for raw_key, raw_value in raw_live_strategy_map.items()
    }
    return SessionState(**payload)

def _copy_session_state_into(target: SessionState, source: SessionState) -> None:
    payload = asdict(source)
    payload["pending_paper_trades"] = _hydrate_pending_paper_trades(payload.get("pending_paper_trades"))
    raw_strategy_map = payload.get("paper_strategies") or {}
    payload["paper_strategies"] = {
        int(raw_key): _hydrate_paper_strategy_state(raw_value)
        for raw_key, raw_value in raw_strategy_map.items()
    }
    raw_live_strategy_map = payload.get("live_strategies") or {}
    payload["live_strategies"] = {
        int(raw_key): _hydrate_live_strategy_state(raw_value)
        for raw_key, raw_value in raw_live_strategy_map.items()
    }
    for field_name, value in payload.items():
        setattr(target, field_name, value)




def append_trade_log(path: Path, record: TradeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(record)
    row["timestamp"] = record.timestamp.isoformat()
    row["start_time"] = record.start_time.isoformat()
    row["end_time"] = record.end_time.isoformat()
    fieldnames = list(row.keys())

    write_header = not path.exists()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_header = next(csv.reader(handle), [])
        if existing_header and existing_header != fieldnames:
            legacy_path = path.with_name(f"{path.stem}_legacy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{path.suffix}")
            path.replace(legacy_path)
            write_header = True

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def resolve_quote_price(side: str, quote: MarketQuote) -> float | None:
    if side == "UP":
        return quote.up_best_ask if quote.up_best_ask is not None else quote.up_price
    if side == "DOWN":
        return quote.down_best_ask if quote.down_best_ask is not None else quote.down_price
    raise ValueError(f"Unsupported side: {side}")


def _resolve_signal_up_price(quote: MarketQuote) -> float | None:
    # For signal direction, prefer traded/last price to reduce orderbook ask spikes noise.
    return quote.up_price if quote.up_price is not None else quote.up_best_ask


def _is_valid_signal_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def _resolve_signal_round_open_up_price(
    *,
    cfg: AppConfig,
    state: SessionState,
    market_client: PolymarketClient | None,
    window: MarketWindow | None,
    current_up_price: float | None,
    now: datetime,
) -> float | None:
    if _is_valid_signal_price(state.signal_round_open_up_price):
        return state.signal_round_open_up_price
    if (
        market_client is None
        or window is None
        or not window.up_token_id
        or not hasattr(market_client, "get_nearest_history_point")
    ):
        return current_up_price

    target_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    now_ts = int(now.timestamp())
    window_end_ts = int(window.end_time.timestamp())
    # Pull a tight history window around round open, then find the nearest point to the intended open anchor.
    start_ts = max(0, target_ts - max(30, cfg.signal_anchor_max_offset_seconds * 2))
    end_ts = max(start_ts + 1, min(window_end_ts, max(now_ts, target_ts + cfg.signal_anchor_max_offset_seconds)))
    anchor = market_client.get_nearest_history_point(
        window.up_token_id,
        target_ts=target_ts,
        start_ts=start_ts,
        end_ts=end_ts,
        fidelity=max(1, cfg.signal_history_fidelity_seconds),
        max_offset_seconds=max(0, cfg.signal_anchor_max_offset_seconds),
    )
    if anchor is None:
        return current_up_price
    return float(anchor["price"])


def _compute_signal_threshold(
    *,
    cfg: AppConfig,
    market_client: PolymarketClient | None,
    window: MarketWindow | None,
    now: datetime,
) -> float:
    base_threshold = max(0.0, cfg.signal_momentum_threshold)
    if (
        market_client is None
        or window is None
        or not window.up_token_id
        or not hasattr(market_client, "get_price_history")
    ):
        return base_threshold

    start_ts = int((window.start_time + timedelta(seconds=max(0, cfg.open_delay_seconds))).timestamp())
    end_ts = min(int(window.end_time.timestamp()), int(now.timestamp()))
    if end_ts <= start_ts:
        return base_threshold

    history_payload = market_client.get_price_history(
        window.up_token_id,
        start_ts=start_ts,
        end_ts=end_ts,
        fidelity=max(1, cfg.signal_history_fidelity_seconds),
    )
    prices: list[float] = []
    for item in history_payload.get("history", []):
        raw = item.get("p")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < value < 1:
            prices.append(value)

    if len(prices) < max(2, cfg.signal_dynamic_threshold_min_points):
        return base_threshold

    deltas = [curr - prev for prev, curr in zip(prices[:-1], prices[1:])]
    if not deltas:
        return base_threshold

    dynamic_threshold = max(0.0, cfg.signal_dynamic_threshold_k) * pstdev(deltas)
    return max(base_threshold, dynamic_threshold)


def _resolve_strategy6_ofi_score(quote: MarketQuote) -> float | None:
    raw = quote.strategy6_ofi_score if hasattr(quote, 'strategy6_ofi_score') else getattr(quote, 'strategy6_ofi_score', None)
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _is_strategy6_signal_stale(*, quote: MarketQuote, now: datetime, stale_seconds: float) -> bool:
    signal_at = getattr(quote, 'strategy6_signal_at', None) or quote.fetched_at
    if signal_at is None:
        return True
    return (now - signal_at).total_seconds() > max(0.0, stale_seconds)


def _apply_strategy6_signal_to_quote(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    binance_signal_service: BinanceDepth5SignalService | None,
    now: datetime | None = None,
) -> None:
    if cfg.strategy_id not in {6, 7} or binance_signal_service is None:
        return
    now = now or datetime.now(timezone.utc)
    latest = binance_signal_service.latest()
    if latest is None or (now - latest.signal_at).total_seconds() > max(0.0, cfg.binance_signal_stale_seconds):
        try:
            refreshed = binance_signal_service.refresh_from_rest(now=now)
        except Exception:
            refreshed = None
        if refreshed is not None:
            latest = refreshed
    if latest is None:
        return
    quote.strategy6_ofi_score = latest.ofi_score
    quote.strategy6_signal_at = latest.signal_at


def _resolve_side_from_strategy(
    *,
    cfg: AppConfig,
    state: SessionState,
    slug: str,
    quote: MarketQuote,
    market_client: PolymarketClient | None = None,
    window: MarketWindow | None = None,
    now: datetime | None = None,
    entry_time: datetime | None = None,
) -> SideDecision:
    if cfg.strategy_id == 6:
        now = now or datetime.now(timezone.utc)
        ofi_score = _resolve_strategy6_ofi_score(quote)
        state.strategy6_last_ofi_score = ofi_score
        if ofi_score is None:
            return SideDecision(side=None, reason='ofi_unavailable')
        if _is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
            return SideDecision(side=None, reason='ofi_stale', signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        if ofi_score >= cfg.ofi_threshold:
            return SideDecision(side='UP', signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        if ofi_score <= -cfg.ofi_threshold:
            return SideDecision(side='DOWN', signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)
        return SideDecision(side=None, reason='ofi_too_weak', signal_threshold=cfg.ofi_threshold, signal_delta=ofi_score)

    if cfg.strategy_id not in {5, 7}:
        return SideDecision(side=get_side_for_round(cfg.strategy_id, state.round_index))

    if state.signal_round_slug != slug:
        state.signal_round_slug = slug
        state.signal_round_open_up_price = None
        state.signal_round_locked_side = None

    signal_current_up_price = _resolve_signal_up_price(quote)
    if state.signal_round_locked_side in {"UP", "DOWN"}:
        signal_delta = None
        if _is_valid_signal_price(state.signal_round_open_up_price) and _is_valid_signal_price(signal_current_up_price):
            signal_delta = signal_current_up_price - state.signal_round_open_up_price
        if cfg.strategy_id == 7:
            candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
            if candidate_price is not None and candidate_price > cfg.strategy7_max_entry_price:
                return SideDecision(
                    side=None,
                    reason='strategy7_price_too_high',
                    signal_open_up_price=state.signal_round_open_up_price,
                    signal_current_up_price=signal_current_up_price,
                    signal_threshold=cfg.strategy7_momentum_threshold,
                    signal_delta=signal_delta,
                    signal_locked=True,
                )
        return SideDecision(
            side=state.signal_round_locked_side,
            signal_open_up_price=state.signal_round_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_delta=signal_delta,
            signal_locked=True,
        )

    now = now or datetime.now(timezone.utc)
    signal_open_up_price = _resolve_signal_round_open_up_price(
        cfg=cfg,
        state=state,
        market_client=market_client,
        window=window,
        current_up_price=signal_current_up_price,
        now=now,
    )
    state.signal_round_open_up_price = signal_open_up_price
    signal_threshold = _compute_signal_threshold(
        cfg=cfg,
        market_client=market_client,
        window=window,
        now=now,
    )

    if cfg.strategy_id == 7:
        ofi_score = _resolve_strategy6_ofi_score(quote)
        state.strategy6_last_ofi_score = ofi_score
        if ofi_score is None:
            return SideDecision(
                side=None,
                reason='strategy7_ofi_unavailable',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
            )
        if _is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
            return SideDecision(
                side=None,
                reason='strategy7_ofi_stale',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            )
        if abs(ofi_score) < cfg.strategy7_ofi_threshold:
            return SideDecision(
                side=None,
                reason='strategy7_ofi_too_weak',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_ofi_threshold,
                signal_delta=ofi_score,
            )
        if not (_is_valid_signal_price(signal_open_up_price) and _is_valid_signal_price(signal_current_up_price)):
            return SideDecision(
                side=None,
                reason='strategy7_momentum_unavailable',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
            )

        momentum_delta = signal_current_up_price - signal_open_up_price
        if abs(momentum_delta) < cfg.strategy7_momentum_threshold:
            return SideDecision(
                side=None,
                reason='strategy7_momentum_too_weak',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        if ofi_score * momentum_delta <= 0:
            return SideDecision(
                side=None,
                reason='strategy7_signal_conflict',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        effective_confirm_before_entry_seconds = _effective_strategy7_confirm_before_entry_seconds(
            cfg=cfg,
            window=window,
            entry_time=entry_time,
        )
        if strategy7_strong_signal_allows_late_confirm(
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
            ofi_threshold=cfg.strategy7_ofi_threshold,
            momentum_threshold=cfg.strategy7_momentum_threshold,
            signal_min_gap=cfg.strategy7_min_signal_gap,
            strong_signal_gap=cfg.strategy7_late_confirm_strong_signal_gap,
        ):
            effective_confirm_before_entry_seconds = max(
                0.0,
                effective_confirm_before_entry_seconds - max(0.0, float(cfg.strategy7_late_confirm_relax_seconds)),
            )
        if entry_time is not None and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds:
            return SideDecision(
                side=None,
                reason='strategy7_entry_too_late',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )

        resolved_side = 'UP' if momentum_delta > 0 else 'DOWN'
        candidate_price = resolve_quote_price(resolved_side, quote)
        if candidate_price is not None and candidate_price > cfg.strategy7_max_entry_price:
            return SideDecision(
                side=None,
                reason='strategy7_price_too_high',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        if not strategy7_signal_gap_ok(
            ofi_score=ofi_score,
            momentum_delta=momentum_delta,
            ofi_threshold=cfg.strategy7_ofi_threshold,
            momentum_threshold=cfg.strategy7_momentum_threshold,
            signal_min_gap=cfg.strategy7_min_signal_gap,
        ):
            return SideDecision(
                side=None,
                reason='strategy7_confidence_too_low',
                signal_open_up_price=signal_open_up_price,
                signal_current_up_price=signal_current_up_price,
                signal_threshold=cfg.strategy7_momentum_threshold,
                signal_delta=momentum_delta,
            )
        state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=cfg.strategy7_momentum_threshold,
            signal_delta=momentum_delta,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    weak_mode = cfg.signal_weak_signal_mode.upper()
    if weak_mode == 'FALLBACK':
        weak_mode = 'SKIP'

    if _is_valid_signal_price(signal_open_up_price) and _is_valid_signal_price(signal_current_up_price):
        signal_delta = signal_current_up_price - signal_open_up_price
        resolved_side: str | None = None
        reason: str | None = None

        if signal_delta >= signal_threshold:
            resolved_side = "UP"
        elif signal_delta <= -signal_threshold:
            resolved_side = "DOWN"
        elif weak_mode == "FALLBACK":
            resolved_side = get_side_for_round(fallback_strategy, state.round_index)
            reason = "signal_too_weak_fallback"
        else:
            reason = "signal_too_weak_skip"

        if resolved_side in {"UP", "DOWN"} and entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side

        return SideDecision(
            side=resolved_side,
            reason=reason,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=signal_threshold,
            signal_delta=signal_delta,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    if weak_mode == "FALLBACK":
        resolved_side = get_side_for_round(fallback_strategy, state.round_index)
        if entry_time is not None:
            lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
            if now >= lock_at:
                state.signal_round_locked_side = resolved_side
        return SideDecision(
            side=resolved_side,
            reason="signal_price_unavailable_fallback",
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
            signal_threshold=signal_threshold,
            signal_locked=state.signal_round_locked_side in {"UP", "DOWN"},
        )

    return SideDecision(
        side=None,
        reason="signal_price_unavailable",
        signal_open_up_price=signal_open_up_price,
        signal_current_up_price=signal_current_up_price,
        signal_threshold=signal_threshold,
    )


def _update_max_stake_skip_streak(
    state: SessionState,
    *,
    skip_reason: str | None,
    threshold: int,
) -> bool:
    if skip_reason == "order_cost_above_max_stake":
        state.consecutive_max_stake_skips += 1
        return state.consecutive_max_stake_skips == max(1, threshold)

    state.consecutive_max_stake_skips = 0
    return False


def _emit_max_stake_skip_alert(
    *,
    slug: str,
    side: str,
    price: float | None,
    state: SessionState,
    cfg: AppConfig,
    skip_streak: int | None = None,
) -> None:
    printable_price = "N/A" if price is None else f"{price:.4f}"
    streak = state.consecutive_max_stake_skips if skip_streak is None else skip_streak
    print(
        "[WARN] order_cost_above_max_stake triggered "
        f"{streak} times | slug={slug} side={side} price={printable_price} "
        f"recovery_loss={state.recovery_loss:.4f} max_stake={cfg.max_stake:.4f} "
        f"max_consecutive_losses={cfg.max_consecutive_losses}"
    )


def _should_reset_after_risk_gate_skip(
    state: SessionState,
    *,
    skip_reason: str | None,
    cfg: AppConfig,
    stop_loss_triggered: bool,
) -> bool:
    if stop_loss_triggered:
        return True
    if skip_reason != "order_cost_above_max_stake":
        return False
    return (state.consecutive_max_stake_skips + 1) >= max(1, cfg.max_consecutive_losses)


def _apply_post_entry_risk_gate_skip(
    state: SessionState,
    *,
    skip_reason: str | None,
    cfg: AppConfig,
    stop_loss_triggered: bool,
) -> tuple[SessionState, bool, bool, int]:
    should_alert = _update_max_stake_skip_streak(
        state,
        skip_reason=skip_reason,
        threshold=cfg.max_stake_skip_alert_threshold,
    )
    skip_streak = state.consecutive_max_stake_skips
    should_reset = stop_loss_triggered
    if not should_reset and skip_reason == "order_cost_above_max_stake":
        should_reset = state.consecutive_max_stake_skips >= max(1, cfg.max_consecutive_losses)
    if should_reset:
        state = reset_after_stop_loss(state)
        state.consecutive_max_stake_skips = 0
    state.round_index += 1
    return state, should_alert, should_reset, skip_streak


def _runtime_backoff_seconds(cfg: AppConfig, consecutive_errors: int) -> int:
    scaled = cfg.runtime_error_backoff_base_seconds * (2 ** max(0, consecutive_errors - 1))
    return max(1, min(cfg.runtime_error_backoff_max_seconds, scaled))


def _resolve_live_order_type(raw_order_type: str):
    from py_clob_client.clob_types import OrderType

    normalized = (raw_order_type or "FOK").upper()
    return getattr(OrderType, normalized, OrderType.FOK)


def validate_live_runtime_config(cfg: AppConfig) -> None:
    if cfg.trade_mode != 'live':
        return
    if not cfg.live_trading_enabled:
        raise RuntimeError('Live trading is disabled.')
    if not cfg.live_private_key:
        raise RuntimeError('Missing private key for live trading.')
    if not cfg.live_funder:
        raise RuntimeError('Missing POLYMARKET_FUNDER for live trading.')
    if (cfg.live_order_type or 'FOK').upper() != 'FOK':
        _resolve_live_order_type(cfg.live_order_type)
    if cfg.live_auto_redeem_enabled and getattr(cfg, 'live_redeem_auth_mode', 'unconfigured') == 'unconfigured':
        raise RuntimeError('Missing official relayer credentials for live redeem.')

def _session_day_key(now: datetime) -> str:
    return now.astimezone(SESSION_DAY_TZ).date().isoformat()


def _refresh_daily_session_state(state: SessionState, now: datetime) -> bool:
    session_day = _session_day_key(now)
    if state.current_day == session_day:
        return False
    state.current_day = session_day
    state.daily_realized_pnl = 0.0
    return True


def _clear_pending_live_trade(strategy_state: LiveStrategyState) -> None:
    strategy_state.pending_live_slug = None
    strategy_state.pending_live_side = None
    strategy_state.pending_live_price = None
    strategy_state.pending_live_order_size = None
    strategy_state.pending_live_order_cost = None
    strategy_state.pending_live_expected_profit = None
    strategy_state.pending_live_order_id = None
    strategy_state.pending_live_end_time = None


def _build_pending_live_trade_plan(state: SessionState) -> TradePlan:
    if state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if state.pending_live_price is None:
        raise RuntimeError("Pending live trade is missing entry price.")
    if state.pending_live_order_size is None or state.pending_live_order_size <= 0:
        raise RuntimeError("Pending live trade is missing order size.")
    if state.pending_live_order_cost is None or state.pending_live_order_cost <= 0:
        raise RuntimeError("Pending live trade is missing order cost.")
    if state.pending_live_expected_profit is None:
        raise RuntimeError("Pending live trade is missing expected profit.")

    return TradePlan(
        True,
        side=state.pending_live_side,
        price=state.pending_live_price,
        order_size=state.pending_live_order_size,
        order_cost=state.pending_live_order_cost,
        expected_profit=state.pending_live_expected_profit,
    )


def _coerce_positive_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _extract_live_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("orderID", "orderId", "id"):
        raw = response.get(key)
        if raw is None:
            continue
        order_id = str(raw).strip()
        if order_id:
            return order_id
    return None


def _validate_live_submission_response(response: Any) -> str:
    if not isinstance(response, dict):
        raise RuntimeError("Live order not accepted: invalid submission response.")

    if response.get("success") is False:
        reason = response.get("errorMsg") or response.get("error") or response.get("message") or "submission rejected"
        raise RuntimeError(f"Live order not accepted: {reason}")

    order_id = _extract_live_order_id(response)
    if order_id is None:
        raise RuntimeError("Live order not accepted: missing order id in submission response.")
    return order_id


def _build_verified_pending_live_trade_plan(
    strategy_state: LiveStrategyState,
    *,
    clob_client: Any | None,
) -> TradePlan | None:
    if strategy_state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if not strategy_state.pending_live_order_id:
        return None
    if clob_client is None:
        return None

    get_order = getattr(clob_client, "get_order", None)
    if not callable(get_order):
        return None

    order_payload = get_order(strategy_state.pending_live_order_id)
    if not isinstance(order_payload, dict):
        return None

    status = str(order_payload.get("status") or "").strip().lower()
    has_fill_markers = any(
        order_payload.get(key) is not None
        for key in (
            "filled_order_size",
            "filledOrderSize",
            "filled_order_cost",
            "filledOrderCost",
            "avg_price",
            "avgPrice",
        )
    )
    if status not in {"filled", "matched"} and not has_fill_markers:
        return None

    order_size = _coerce_positive_float(
        order_payload.get("filled_order_size")
        or order_payload.get("filledOrderSize")
        or order_payload.get("size_matched")
        or order_payload.get("matched_size")
    )
    order_cost = _coerce_positive_float(
        order_payload.get("filled_order_cost")
        or order_payload.get("filledOrderCost")
        or order_payload.get("filled_value")
        or order_payload.get("filledValue")
        or order_payload.get("cost")
    )
    fill_price = _coerce_positive_float(
        order_payload.get("avg_price")
        or order_payload.get("avgPrice")
        or order_payload.get("price")
    )

    if order_size is None and order_cost is not None and fill_price is not None:
        order_size = order_cost / fill_price
    if order_cost is None and order_size is not None and fill_price is not None:
        order_cost = order_size * fill_price
    if fill_price is None and order_size is not None and order_cost is not None:
        fill_price = order_cost / order_size

    if order_size is None or order_cost is None or fill_price is None or not 0 < fill_price < 1:
        return None

    return TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=order_size * (1 - fill_price),
    )


def _settle_pending_live_trade_if_needed(
    *,
    market_client: PolymarketClient | Any,
    clob_client: Any | None,
    strategy_state: LiveStrategyState,
    now: datetime,
) -> tuple[LiveStrategyState, dict[str, Any] | None, bool]:
    if not strategy_state.pending_live_slug:
        return strategy_state, None, False

    end_time = parse_iso_datetime(strategy_state.pending_live_end_time)
    if end_time is None:
        raise RuntimeError("Pending live trade is missing round end time.")

    if now < end_time:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_in_progress",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )

    plan = _build_verified_pending_live_trade_plan(strategy_state, clob_client=clob_client)
    if plan is None:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "awaiting_fill_confirmation",
                "pending_end_time": strategy_state.pending_live_end_time,
                "order_id": strategy_state.pending_live_order_id,
            },
            False,
        )

    event = market_client.get_event_by_slug(strategy_state.pending_live_slug)
    metadata = event.get("eventMetadata") or {}
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_unresolved",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )

    result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    updated_state = apply_round_outcome(strategy_state, plan, won=(result == plan.side))
    trade_pnl = updated_state.cash_pnl - strategy_state.cash_pnl
    _clear_pending_live_trade(updated_state)
    return (
        updated_state,
        {
            "status": "settled",
            "slug": strategy_state.pending_live_slug,
            "side": plan.side,
            "result": result,
            "trade_pnl": trade_pnl,
        },
        True,
    )


def _create_live_clob_client(cfg: AppConfig):
    if not cfg.live_private_key:
        raise RuntimeError("Missing PRIVATE_KEY/POLYMARKET_PRIVATE_KEY for live trading.")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    def _apply_derived_api_creds(client: Any):
        client.set_api_creds(client.create_or_derive_api_creds())

    def _is_invalid_api_key_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "invalid api key" in message or "unauthorized" in message or "status_code=401" in message

    clob_client = ClobClient(
        cfg.clob_api_base,
        chain_id=cfg.live_chain_id,
        key=cfg.live_private_key,
        signature_type=cfg.live_signature_type,
        funder=cfg.live_funder,
    )
    if cfg.live_api_key and cfg.live_api_secret and cfg.live_api_passphrase:
        clob_client.set_api_creds(
            ApiCreds(
                api_key=cfg.live_api_key,
                api_secret=cfg.live_api_secret,
                api_passphrase=cfg.live_api_passphrase,
            )
        )
        get_api_keys = getattr(clob_client, "get_api_keys", None)
        if callable(get_api_keys):
            try:
                get_api_keys()
            except Exception as exc:
                if not _is_invalid_api_key_error(exc):
                    raise
                print("[live] explicit API credentials rejected; falling back to derived credentials.", flush=True)
                _apply_derived_api_creds(clob_client)
                get_api_keys()
        return clob_client
    _apply_derived_api_creds(clob_client)
    return clob_client

def place_live_order(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    market_client = market_client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "live_session_state.json"
    log_path = log_path or cfg.logs_dir / "live_orders.csv"
    state = load_session_state(state_path)
    persist_state = not dry_run
    live_client = clob_client

    now = datetime.now(timezone.utc)
    daily_state_changed = _refresh_daily_session_state(state, now)
    if state.pending_live_slug and live_client is None and state.pending_live_order_id and cfg.live_private_key:
        live_client = _create_live_clob_client(cfg)
    strategy_state = _live_strategy_state_from_payload(asdict(state))
    strategy_state, pending_status, settled_previous_trade = _settle_pending_live_trade_if_needed(
        market_client=market_client,
        clob_client=live_client,
        strategy_state=strategy_state,
        now=now,
    )
    _apply_live_strategy_state_to_session_state(state, strategy_state)
    if pending_status is not None and pending_status["status"] == "pending_settlement":
        if daily_state_changed and persist_state:
            _sync_current_live_strategy_state(state, cfg.strategy_id)
            save_session_state(state_path, state)
        return pending_status
    if settled_previous_trade and persist_state:
        _sync_current_live_strategy_state(state, cfg.strategy_id)
        save_session_state(state_path, state)

    current_round, next_round = market_client.find_current_and_next_rounds(now=now)
    target_round = _select_target_round(cfg, now=now, current_round=current_round, next_round=next_round)
    if target_round is None:
        if daily_state_changed and persist_state:
            _sync_current_live_strategy_state(state, cfg.strategy_id)
            save_session_state(state_path, state)
        return {"status": "no_market"}

    entry_time = _entry_time_for_round(cfg, target_round)
    market = market_client.get_market_by_slug(target_round.slug)
    quote = market_client.quote_from_market(market)
    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=binance_signal_service)
    print('[live] quote {' + _describe_quote_source(quote) + '}', flush=True)
    print('[live] ws_runtime {' + _describe_ws_runtime(market_client) + '}', flush=True)
    side_decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug=target_round.slug,
        quote=quote,
        market_client=market_client,
        window=target_round,
        now=now,
        entry_time=entry_time,
    )
    if side_decision.side is None:
        if dry_run:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": None,
                "token_id": None,
                "price": None,
                "should_trade": False,
                "skip_reason": side_decision.reason or "signal_unavailable",
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if now < entry_time:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": None,
                "token_id": None,
                "price": None,
                "should_trade": False,
                "skip_reason": side_decision.reason or "signal_unavailable",
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if persist_state:
            append_trade_log(
                log_path,
                TradeRecord(
                    timestamp=datetime.now(timezone.utc),
                    mode="live",
                    round_index=state.round_index,
                    strategy=cfg.strategy_id,
                    entry_timing=cfg.entry_timing,
                    event_slug=target_round.slug,
                    start_time=target_round.start_time,
                    end_time=target_round.end_time,
                    side="SKIP",
                    price=None,
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    skip_reason=side_decision.reason or "signal_unavailable",
                    **_signal_record_kwargs(side_decision),
                ),
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": None,
            "token_id": None,
            "price": None,
            "should_trade": False,
            "skip_reason": side_decision.reason or "signal_unavailable",
            "entry_time": entry_time.isoformat(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    side = side_decision.side
    price = resolve_quote_price(side, quote)

    if _ws_is_stale_for_trade(market_client, cfg):
        skip_reason = "ws_stale"
        if dry_run:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": side,
                "token_id": None,
                "price": price,
                "should_trade": False,
                "skip_reason": skip_reason,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if now < entry_time:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": side,
                "token_id": None,
                "price": price,
                "should_trade": False,
                "skip_reason": skip_reason,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if persist_state:
            append_trade_log(
                log_path,
                TradeRecord(
                    timestamp=datetime.now(timezone.utc),
                    mode="live",
                    round_index=state.round_index,
                    strategy=cfg.strategy_id,
                    entry_timing=cfg.entry_timing,
                    event_slug=target_round.slug,
                    start_time=target_round.start_time,
                    end_time=target_round.end_time,
                    side=side,
                    price=price,
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    skip_reason=skip_reason,
                    **_signal_record_kwargs(side_decision),
                ),
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "token_id": None,
            "price": price,
            "should_trade": False,
            "skip_reason": skip_reason,
            "entry_time": entry_time.isoformat(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    plan = build_trade_plan(
        state=state,
        side=side,
        price=price,
        target_profit=cfg.target_profit,
        min_price_threshold=getattr(cfg, 'min_price_threshold', None),
        max_price_threshold=cfg.max_price_threshold,
        max_stake=cfg.max_stake,
        max_consecutive_losses=cfg.max_consecutive_losses,
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
    )
    token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
    token_id = token_ids.get(side)

    if dry_run:
        projected_streak = (
            state.consecutive_max_stake_skips + 1
            if plan.skip_reason == "order_cost_above_max_stake"
            else 0
        )
        return {
            "status": "dry_run",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": plan.should_trade,
            "skip_reason": plan.skip_reason,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "projected_max_stake_skip_streak": projected_streak,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }

    if not cfg.live_trading_enabled:
        raise RuntimeError("Live trading is disabled. Set LIVE_TRADING_ENABLED=true (or config flag) to submit orders.")
    if not plan.should_trade:
        skip_stop_loss_triggered = _should_reset_after_risk_gate_skip(
            state,
            skip_reason=plan.skip_reason,
            cfg=cfg,
            stop_loss_triggered=plan.stop_loss_triggered,
        )
        if now < entry_time:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": side,
                "token_id": token_id,
                "price": price,
                "should_trade": False,
                "skip_reason": plan.skip_reason,
                "order_size": plan.order_size,
                "order_cost": plan.order_cost,
                "expected_profit": plan.expected_profit,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="live",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=price,
                order_size=0.0,
                order_cost=0.0,
                expected_profit=0.0,
                result=None,
                trade_pnl=0.0,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
                skip_reason=plan.skip_reason,
                stop_loss_triggered=skip_stop_loss_triggered,
                **_signal_record_kwargs(side_decision),
            ),
        )
        state, should_alert, skip_stop_loss_triggered, skip_streak = _apply_post_entry_risk_gate_skip(
            state,
            skip_reason=plan.skip_reason,
            cfg=cfg,
            stop_loss_triggered=plan.stop_loss_triggered,
        )
        if should_alert:
            _emit_max_stake_skip_alert(
                slug=target_round.slug,
                side=side,
                price=price,
                state=state,
                cfg=cfg,
                skip_streak=skip_streak,
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "price": price,
            "skip_reason": plan.skip_reason,
            "max_stake_skip_streak": state.consecutive_max_stake_skips,
            "stop_loss_triggered": skip_stop_loss_triggered,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    state.consecutive_max_stake_skips = 0
    if token_id is None:
        raise RuntimeError(f"Missing token id for side={side} on market={target_round.slug}")
    if now < entry_time:
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "waiting_for_entry",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": True,
            "skip_reason": None,
            "entry_time": entry_time.isoformat(),
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }

    live_client = live_client or _create_live_clob_client(cfg)
    order_type = (cfg.live_order_type or 'FOK').upper() if clob_client is not None else _resolve_live_order_type(cfg.live_order_type)
    market_order_price = cfg.strategy7_max_entry_price if cfg.strategy_id == 7 else None
    if clob_client is None:
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=plan.order_cost,
            side=BUY,
            price=market_order_price or 0,
            order_type=order_type,
        )
    else:
        order_args = type(
            'InjectedMarketOrderArgs',
            (),
            {
                'token_id': token_id,
                'amount': plan.order_cost,
                'side': 'BUY',
                'order_type': order_type,
                'price': market_order_price,
                'fee_rate_bps': None,
            },
        )()
    signed_order = live_client.create_market_order(order_args)
    response = live_client.post_order(signed_order, order_type)
    order_id = _validate_live_submission_response(response)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=state.round_index,
            strategy=cfg.strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=target_round.slug,
            start_time=target_round.start_time,
            end_time=target_round.end_time,
            side=side,
            price=plan.price,
            order_size=plan.order_size,
            order_cost=plan.order_cost,
            expected_profit=plan.expected_profit,
            result=None,
            trade_pnl=0.0,
            cash_pnl=state.cash_pnl,
            recovery_loss=state.recovery_loss,
            consecutive_losses=state.consecutive_losses,
            **_signal_record_kwargs(side_decision),
        ),
    )
    state.pending_live_slug = target_round.slug
    state.pending_live_side = side
    state.pending_live_price = plan.price
    state.pending_live_order_size = plan.order_size
    state.pending_live_order_cost = plan.order_cost
    state.pending_live_expected_profit = plan.expected_profit
    state.pending_live_order_id = order_id
    state.pending_live_end_time = target_round.end_time.isoformat()
    state.round_index += 1
    if persist_state:
        _sync_current_live_strategy_state(state, cfg.strategy_id)
        save_session_state(state_path, state)

    return {
        "status": "submitted",
        "slug": target_round.slug,
        "side": side,
        "token_id": token_id,
        "price": price,
        "order_size": plan.order_size,
        "order_cost": plan.order_cost,
        "expected_profit": plan.expected_profit,
        "order_type": cfg.live_order_type.upper(),
        "order_id": order_id,
        "response": response,
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
    }


def _entry_time_for_round(cfg: AppConfig, window: MarketWindow) -> datetime:
    if cfg.entry_timing.upper() == "PRE_CLOSE":
        return window.end_time - timedelta(seconds=cfg.preclose_seconds)
    return window.start_time + timedelta(seconds=cfg.open_delay_seconds)


def _entry_window_missed(now: datetime, entry_time: datetime, *, grace_seconds: float = 0.0) -> bool:
    return now > (entry_time + timedelta(seconds=max(0.0, grace_seconds)))


def _poll_interval_for_target_round(
    *,
    cfg: AppConfig,
    now: datetime,
    target_round: MarketWindow | None,
) -> float:
    base_interval = max(0.0, float(cfg.poll_interval_seconds))
    if target_round is None:
        return base_interval
    entry_time = _entry_time_for_round(cfg, target_round)
    if _entry_window_missed(now, entry_time, grace_seconds=cfg.entry_grace_seconds):
        return base_interval
    remaining = (entry_time - now).total_seconds()
    near_entry_window = max(0.0, float(cfg.near_entry_poll_window_seconds))
    if remaining <= near_entry_window:
        return min(base_interval, max(0.0, float(cfg.fast_poll_interval_seconds)))
    return base_interval


def _effective_strategy7_confirm_before_entry_seconds(
    *,
    cfg: AppConfig,
    window: MarketWindow | None,
    entry_time: datetime | None,
) -> float:
    configured = max(0.0, float(cfg.strategy7_confirm_before_entry_seconds))
    if window is None or entry_time is None:
        return configured
    available = max(0.0, (entry_time - window.start_time).total_seconds())
    return min(configured, available)


def _select_target_round(
    cfg: AppConfig,
    *,
    now: datetime,
    current_round: MarketWindow | None,
    next_round: MarketWindow | None,
) -> MarketWindow | None:
    if current_round is not None:
        current_entry_time = _entry_time_for_round(cfg, current_round)
        if not _entry_window_missed(now, current_entry_time, grace_seconds=cfg.entry_grace_seconds):
            return current_round
    return next_round if next_round is not None else current_round


def _sleep_until_round_end(
    cfg: AppConfig,
    window: MarketWindow,
    stop_event: threading.Event | None = None,
) -> bool:
    if _is_stop_requested(stop_event):
        return False
    while datetime.now(timezone.utc) < window.end_time:
        if not _sleep_if_not_stopped(stop_event, cfg.poll_interval_seconds):
            return False
    return True


def _is_stop_requested(stop_event: threading.Event | None) -> bool:
    return bool(stop_event and stop_event.is_set())


def _sleep_if_not_stopped(stop_event: threading.Event | None, seconds: float) -> bool:
    if _is_stop_requested(stop_event):
        return False
    time.sleep(seconds)
    return not _is_stop_requested(stop_event)


def _runtime_log(message: str) -> None:
    print('[' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' UTC] ' + message, flush=True)


def _update_runtime_control(
    runtime_control: RuntimeControl | None,
    **changes,
) -> None:
    if runtime_control is None:
        return
    runtime_control.update_worker_state(**changes)


def _safe_stop_requested(stop_when_safe: Callable[[], bool] | None) -> bool:
    return bool(stop_when_safe and stop_when_safe())


def _fmt_price(value: float | None) -> str:
    return 'N/A' if value is None else f'{value:.4f}'


def _signal_record_kwargs(side_decision: SideDecision) -> dict[str, Any]:
    return {
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
        "signal_reason": side_decision.reason,
    }


def _describe_side_decision(side_decision: SideDecision) -> str:
    signal_bits = []
    if side_decision.signal_open_up_price is not None:
        signal_bits.append('open_up=' + _fmt_price(side_decision.signal_open_up_price))
    if side_decision.signal_current_up_price is not None:
        signal_bits.append('current_up=' + _fmt_price(side_decision.signal_current_up_price))
    if side_decision.signal_threshold is not None:
        signal_bits.append('threshold=' + _fmt_price(side_decision.signal_threshold))
    if side_decision.signal_delta is not None:
        signal_bits.append('delta=' + _fmt_price(side_decision.signal_delta))
    signal_bits.append('locked=' + str(side_decision.signal_locked))
    if side_decision.reason:
        signal_bits.append('reason=' + side_decision.reason)
    return ', '.join(signal_bits)


def _describe_quote_source(quote: MarketQuote) -> str:
    source = quote.source or 'http'
    return (
        'source=' + source
        + ', up_best_ask=' + _fmt_price(quote.up_best_ask)
        + ', up_price=' + _fmt_price(quote.up_price)
        + ', down_best_ask=' + _fmt_price(quote.down_best_ask)
        + ', down_price=' + _fmt_price(quote.down_price)
    )


def _describe_ws_runtime(client: PolymarketClient | Any) -> str:
    get_stats = getattr(client, 'get_ws_runtime_stats', None)
    if not callable(get_stats):
        return 'ws_stats_unavailable'
    stats = get_stats()
    return (
        'ws_enabled=' + str(stats.get('ws_enabled'))
        + ', ws_available=' + str(stats.get('ws_available'))
        + ', ws_connected=' + str(stats.get('ws_connected'))
        + ', reconnects=' + str(stats.get('ws_reconnect_count'))
        + ', invalid_ops=' + str(stats.get('ws_invalid_operation_count'))
        + ', connect_attempts=' + str(stats.get('ws_connect_attempts'))
        + ', subscribed_assets=' + str(stats.get('ws_subscribed_asset_count'))
        + ', cached_assets=' + str(stats.get('ws_cached_asset_count'))
        + ', last_message_age_s=' + _fmt_price(stats.get('ws_last_message_age_seconds'))
        + ', last_error=' + str(stats.get('ws_last_error'))
    )


def _ws_is_stale_for_trade(client: PolymarketClient | Any, cfg: AppConfig) -> bool:
    get_stats = getattr(client, 'get_ws_runtime_stats', None)
    if not callable(get_stats):
        return False
    stats = get_stats()
    if not stats.get('ws_enabled'):
        return False
    if not stats.get('ws_available'):
        return False
    age = stats.get('ws_last_message_age_seconds')
    if not isinstance(age, (int, float)):
        return False
    return age > max(0.0, cfg.ws_trade_guard_stale_seconds)


def _pending_paper_trade_exists(state: SessionState, slug: str) -> bool:
    return any(item.event_slug == slug for item in state.pending_paper_trades)


def _build_pending_paper_trade(
    *,
    state: SessionState,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    cfg: AppConfig,
    side_decision: SideDecision,
    experiment_id: str | None,
) -> PendingPaperTrade:
    return PendingPaperTrade(
        round_index=state.round_index,
        event_slug=window.slug,
        start_time=window.start_time.isoformat(),
        end_time=window.end_time.isoformat(),
        side=side,
        price=float(plan.price or 0.0),
        order_size=plan.order_size,
        order_cost=plan.order_cost,
        expected_profit=plan.expected_profit,
        strategy=cfg.strategy_id,
        entry_timing=cfg.entry_timing,
        signal_open_up_price=side_decision.signal_open_up_price,
        signal_current_up_price=side_decision.signal_current_up_price,
        signal_threshold=side_decision.signal_threshold,
        signal_delta=side_decision.signal_delta,
        signal_locked=side_decision.signal_locked,
        signal_reason=side_decision.reason,
        queued_at=datetime.now(timezone.utc).isoformat(),
        experiment_id=experiment_id,
    )


def _queue_pending_paper_trade(
    *,
    state: SessionState,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    cfg: AppConfig,
    side_decision: SideDecision,
    experiment_id: str | None,
) -> bool:
    if _pending_paper_trade_exists(state, window.slug):
        return False
    state.pending_paper_trades.append(
        _build_pending_paper_trade(
            state=state,
            window=window,
            plan=plan,
            side=side,
            cfg=cfg,
            side_decision=side_decision,
            experiment_id=experiment_id,
        )
    )
    return True


def _paper_experiment_id(strategy_id: int, strategy_state: PaperStrategyState) -> str:
    experiment_id = str(strategy_state.experiment_id or '').strip()
    if not experiment_id:
        experiment_id = f"strategy-{strategy_id}"
        strategy_state.experiment_id = experiment_id
    return experiment_id


def _candidate_cfg_with_params(base_cfg: AppConfig, base_strategy_id: int, params: dict[str, Any] | None) -> AppConfig:
    params = params or {}
    kwargs: dict[str, Any] = {"strategy_id": int(base_strategy_id)}
    if "TARGET_PROFIT" in params:
        kwargs["target_profit"] = float(params["TARGET_PROFIT"])
    if "MAX_PRICE_THRESHOLD" in params:
        kwargs["max_price_threshold"] = float(params["MAX_PRICE_THRESHOLD"])
    if "SIGNAL_MOMENTUM_THRESHOLD" in params:
        kwargs["signal_momentum_threshold"] = float(params["SIGNAL_MOMENTUM_THRESHOLD"])
    if "OFI_THRESHOLD" in params:
        kwargs["ofi_threshold"] = float(params["OFI_THRESHOLD"])
    if "MAX_ENTRY_PRICE" in params:
        kwargs["max_entry_price"] = float(params["MAX_ENTRY_PRICE"])
    if "STRATEGY7_OFI_THRESHOLD" in params:
        kwargs["strategy7_ofi_threshold"] = float(params["STRATEGY7_OFI_THRESHOLD"])
    if "STRATEGY7_MOMENTUM_THRESHOLD" in params:
        kwargs["strategy7_momentum_threshold"] = float(params["STRATEGY7_MOMENTUM_THRESHOLD"])
    if "STRATEGY7_MAX_ENTRY_PRICE" in params:
        kwargs["strategy7_max_entry_price"] = float(params["STRATEGY7_MAX_ENTRY_PRICE"])
    return replace(base_cfg, **kwargs)


def _load_active_optimizer_challengers(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = load_optimizer_state(path)
    active = payload.get("active_challengers")
    challengers = active if isinstance(active, list) else []
    for challenger in challengers:
        if not isinstance(challenger, dict):
            continue
        raw_state = challenger.get("paper_state")
        if isinstance(raw_state, dict):
            paper_state = _hydrate_paper_strategy_state(raw_state)
        else:
            paper_state = PaperStrategyState()
        paper_state.experiment_id = str(challenger.get("candidate_id") or paper_state.experiment_id or "").strip() or None
        challenger["_paper_state"] = paper_state
    return payload, [item for item in challengers if isinstance(item, dict)]


def _save_active_optimizer_challengers(path: Path, payload: dict[str, Any], challengers: list[dict[str, Any]]) -> None:
    serialized = dict(payload)
    serialized["active_challengers"] = []
    for challenger in challengers:
        item = {key: value for key, value in challenger.items() if key != "_paper_state"}
        paper_state = challenger.get("_paper_state")
        if isinstance(paper_state, PaperStrategyState):
            item["paper_state"] = asdict(paper_state)
        serialized["active_challengers"].append(item)
    save_optimizer_state(path, serialized)


def _build_frozen_pending_paper_plan(item: PendingPaperTrade) -> TradePlan:
    return TradePlan(
        True,
        side=item.side,
        price=item.price,
        order_size=item.order_size,
        order_cost=item.order_cost,
        expected_profit=item.expected_profit,
    )


def _settle_pending_paper_trade(
    *,
    client: PolymarketClient | Any,
    state: SessionState,
    item: PendingPaperTrade,
) -> tuple[SessionState, str, float]:
    event = client.get_event_by_slug(item.event_slug)
    metadata = event.get('eventMetadata') or {}
    if metadata.get('priceToBeat') is None or metadata.get('finalPrice') is None:
        raise RuntimeError(f'Round {item.event_slug} is not resolved yet.')

    result = 'UP' if float(metadata['finalPrice']) >= float(metadata['priceToBeat']) else 'DOWN'
    plan = _build_frozen_pending_paper_plan(item)
    updated_state = apply_round_outcome(state, plan, won=(result == item.side))
    trade_pnl = updated_state.cash_pnl - state.cash_pnl
    return updated_state, result, trade_pnl


def _settle_pending_paper_trades(
    *,
    client: PolymarketClient | Any,
    state: SessionState,
    log_path: Path,
) -> tuple[SessionState, bool]:
    if not state.pending_paper_trades:
        return state, False

    updated_state = state
    changed = False
    remaining: list[PendingPaperTrade] = []
    for item in updated_state.pending_paper_trades:
        try:
            next_state, result, trade_pnl = _settle_pending_paper_trade(
                client=client,
                state=updated_state,
                item=item,
            )
        except RuntimeError as exc:
            if 'is not resolved yet' in str(exc):
                _runtime_log('round=' + item.event_slug + ' pending resolution')
                remaining.append(item)
                continue
            raise

        updated_state = next_state
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode='paper',
                experiment_id=item.experiment_id,
                round_index=item.round_index,
                strategy=item.strategy,
                entry_timing=item.entry_timing,
                event_slug=item.event_slug,
                start_time=parse_iso_datetime(item.start_time) or datetime.now(timezone.utc),
                end_time=parse_iso_datetime(item.end_time) or datetime.now(timezone.utc),
                side=item.side,
                price=item.price,
                order_size=item.order_size,
                order_cost=item.order_cost,
                expected_profit=item.expected_profit,
                result=result,
                trade_pnl=trade_pnl,
                cash_pnl=updated_state.cash_pnl,
                recovery_loss=updated_state.recovery_loss,
                consecutive_losses=updated_state.consecutive_losses,
                signal_open_up_price=item.signal_open_up_price,
                signal_current_up_price=item.signal_current_up_price,
                signal_threshold=item.signal_threshold,
                signal_delta=item.signal_delta,
                signal_locked=item.signal_locked,
                signal_reason=item.signal_reason,
            ),
        )
        _runtime_log(
            'round=' + item.event_slug
            + ' settled result=' + result
            + ' trade_pnl=' + f'{trade_pnl:.4f}'
            + ' total_cash_pnl=' + f'{updated_state.cash_pnl:.4f}'
            + ' consecutive_losses=' + str(updated_state.consecutive_losses)
        )
        changed = True

    updated_state.pending_paper_trades = remaining
    return updated_state, changed


def _settle_paper_trade(
    client: PolymarketClient,
    state: SessionState,
    window: MarketWindow,
    price: float,
    *,
    side: str,
    cfg: AppConfig,
) -> tuple[SessionState, str]:
    event = client.get_event_by_slug(window.slug)
    metadata = event.get("eventMetadata") or {}
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        raise RuntimeError(f"Round {window.slug} is not resolved yet.")

    result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    plan = build_trade_plan(
        state=state,
        side=side,
        price=price,
        target_profit=cfg.target_profit,
        min_price_threshold=getattr(cfg, 'min_price_threshold', None),
        max_price_threshold=cfg.max_price_threshold,
        max_stake=cfg.max_stake,
        max_consecutive_losses=cfg.max_consecutive_losses,
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
    )
    updated_state = apply_round_outcome(state, plan, won=(result == side))
    return updated_state, result


def run_live_trading(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
    runtime_control: RuntimeControl | None = None,
    stop_when_safe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    validate_live_runtime_config(cfg)
    client_provided = market_client is not None
    state_path_provided = state_path is not None
    log_path_provided = log_path is not None
    market_client = market_client or PolymarketClient(cfg)
    binance_signal_service = (
        BinanceDepth5SignalService(ws_url=cfg.binance_ws_url, stream=cfg.binance_depth_stream)
        if cfg.strategy_id in {6, 7}
        else None
    )
    if binance_signal_service is not None:
        binance_signal_service.start()
    state_path = state_path or cfg.logs_dir / 'live_session_state.json'
    log_path = log_path or cfg.logs_dir / 'live_orders.csv'
    initial_state = load_session_state(state_path)
    _update_runtime_control(
        runtime_control,
        current_round_slug=initial_state.pending_live_slug,
        round_in_progress=bool(initial_state.pending_live_slug),
        safe_to_switch=not bool(initial_state.pending_live_slug),
        pending_live_order=bool(initial_state.pending_live_slug),
    )

    try:
        while True:
            if _is_stop_requested(stop_event):
                return {'status': 'stopped'}
            if _safe_stop_requested(stop_when_safe) and runtime_control is not None:
                snapshot = runtime_control.snapshot()
                if snapshot.safe_to_switch and not snapshot.round_in_progress and not snapshot.pending_live_order:
                    return {'status': 'stopped'}
            if config_provider is not None:
                candidate_cfg = config_provider()
                if candidate_cfg is not None:
                    validate_live_runtime_config(candidate_cfg)
                    cfg = candidate_cfg
                    if not client_provided:
                        market_client.config = cfg
                    if not state_path_provided:
                        state_path = cfg.logs_dir / 'live_session_state.json'
                    if not log_path_provided:
                        log_path = cfg.logs_dir / 'live_orders.csv'
            result = place_live_order(
                cfg=cfg,
                market_client=market_client,
                binance_signal_service=binance_signal_service,
                clob_client=clob_client,
                state_path=state_path,
                log_path=log_path,
            )
            pending_live_order = bool(result.get('status') == 'pending_settlement')
            current_round_slug = result.get('slug') if pending_live_order else None
            _update_runtime_control(
                runtime_control,
                current_round_slug=current_round_slug,
                round_in_progress=pending_live_order,
                safe_to_switch=not pending_live_order,
                pending_live_order=pending_live_order,
            )
            if _is_stop_requested(stop_event):
                return {'status': 'stopped'}
            if _safe_stop_requested(stop_when_safe) and result.get('status') == 'pending_settlement':
                return result
            if result.get('status') in {'submitted', 'skipped', 'waiting_for_entry', 'pending_settlement', 'no_market'}:
                if not _sleep_if_not_stopped(stop_event, cfg.poll_interval_seconds):
                    return {'status': 'stopped'}
                continue
            return result
    finally:
        if binance_signal_service is not None:
            binance_signal_service.close()


def run_paper_trading(
    cfg: AppConfig | None = None,
    *,
    client: PolymarketClient | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run_once: bool = False,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
    runtime_control: RuntimeControl | None = None,
    stop_when_safe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    client_provided = client is not None
    state_path_provided = state_path is not None
    log_path_provided = log_path is not None
    client = client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "session_state.json"
    log_path = log_path or cfg.logs_dir / "paper_trades.csv"
    optimizer_state_path = cfg.logs_dir / "optimizer_state.json"
    strategy_ids = _paper_strategy_ids_for_runtime(cfg)
    optimizer_state_payload, active_challengers = _load_active_optimizer_challengers(optimizer_state_path)
    binance_signal_service = _sync_paper_binance_signal_service(
        cfg=cfg,
        strategy_ids=strategy_ids,
        service=binance_signal_service,
    )
    loaded_state = _load_session_state_for_paper_runtime(state_path, strategy_ids)
    state = _clone_session_state(loaded_state)
    _ensure_paper_strategy_state_map(state, strategy_ids)
    _sync_legacy_paper_state_fields(state, strategy_ids)
    consecutive_errors = 0
    _update_runtime_control(
        runtime_control,
        current_round_slug=None,
        round_in_progress=False,
        safe_to_switch=True,
        pending_live_order=False,
    )
    _runtime_log(
        'paper-trade started | strategies=' + ','.join(str(item) for item in strategy_ids)
        + ' entry_timing=' + cfg.entry_timing
        + ' poll=' + str(cfg.poll_interval_seconds)
        + 's dry_run_once=' + str(dry_run_once)
    )

    while True:
        if _is_stop_requested(stop_event):
            return {"status": "stopped"}
        try:
            if config_provider is not None:
                candidate_cfg = config_provider()
                if candidate_cfg is not None:
                    cfg = candidate_cfg
                    strategy_ids = _paper_strategy_ids_for_runtime(cfg)
                    binance_signal_service = _sync_paper_binance_signal_service(
                        cfg=cfg,
                        strategy_ids=strategy_ids,
                        service=binance_signal_service,
                    )
                    if not client_provided:
                        client.config = cfg
                    if not state_path_provided:
                        state_path = cfg.logs_dir / "session_state.json"
                    if not log_path_provided:
                        log_path = cfg.logs_dir / "paper_trades.csv"
                    optimizer_state_path = cfg.logs_dir / "optimizer_state.json"
                    optimizer_state_payload, active_challengers = _load_active_optimizer_challengers(optimizer_state_path)
                    _ensure_paper_strategy_state_map(state, strategy_ids)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
            now = datetime.now(timezone.utc)
            pending_strategy_ids: list[int] = []
            settled_any_pending = False
            state_changed = False
            challenger_state_changed = False
            for strategy_id in strategy_ids:
                strategy_state = state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
                strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                if _refresh_daily_session_state(strategy_session, now):
                    state_changed = True
                strategy_session, settled_changed = _settle_pending_paper_trades(
                    client=client,
                    state=strategy_session,
                    log_path=log_path,
                )
                if settled_changed:
                    settled_any_pending = True
                    state_changed = True
                state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                if state.paper_strategies[strategy_id].pending_paper_trades:
                    pending_strategy_ids.append(strategy_id)
            for challenger in active_challengers:
                strategy_state = challenger.get("_paper_state")
                if not isinstance(strategy_state, PaperStrategyState):
                    continue
                strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                strategy_session, settled_changed = _settle_pending_paper_trades(
                    client=client,
                    state=strategy_session,
                    log_path=log_path,
                )
                next_state = _session_state_to_paper_strategy_state(strategy_session)
                next_state.experiment_id = str(challenger.get("candidate_id") or next_state.experiment_id or "").strip() or None
                challenger["_paper_state"] = next_state
                if settled_changed:
                    settled_any_pending = True
                    challenger_state_changed = True
            if state_changed or settled_any_pending:
                _sync_legacy_paper_state_fields(state, strategy_ids)
                round_completed = True
                _copy_session_state_into(loaded_state, state)
                save_session_state(state_path, state)
            if challenger_state_changed:
                _save_active_optimizer_challengers(optimizer_state_path, optimizer_state_payload, active_challengers)
            if pending_strategy_ids:
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=state.paper_strategies[pending_strategy_ids[0]].pending_paper_trades[0].event_slug,
                    round_in_progress=True,
                    safe_to_switch=False,
                    pending_live_order=False,
                )
                if _safe_stop_requested(stop_when_safe):
                    return {"status": "pending_settlement"}
            elif _safe_stop_requested(stop_when_safe):
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=None,
                    round_in_progress=False,
                    safe_to_switch=True,
                    pending_live_order=False,
                )
                return {"status": "stopped"}
            current_round, next_round = client.find_current_and_next_rounds(now=now)
            current_entry_time = _entry_time_for_round(cfg, current_round) if current_round is not None else None
            should_log_missed_current_round = (
                not dry_run_once
                and current_round is not None
                and next_round is not None
                and current_entry_time is not None
                and _entry_window_missed(now, current_entry_time, grace_seconds=cfg.entry_grace_seconds)
            )
            target_round = (
                current_round
                if should_log_missed_current_round
                else _select_target_round(cfg, now=now, current_round=current_round, next_round=next_round)
            )
            if target_round is None:
                if dry_run_once:
                    return {"status": "no_market"}
                _runtime_log('no active round found; waiting ' + str(cfg.poll_interval_seconds) + 's')
                consecutive_errors = 0
                if not _sleep_if_not_stopped(
                    stop_event,
                    _poll_interval_for_target_round(cfg=cfg, now=now, target_round=None),
                ):
                    return {"status": "stopped"}
                continue

            entry_time = _entry_time_for_round(cfg, target_round)
            market = client.get_market_by_slug(target_round.slug)
            quote = client.quote_from_market(market)
            any_processed = False
            round_completed = False
            for strategy_id in strategy_ids:
                strategy_state = state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
                experiment_id = _paper_experiment_id(strategy_id, strategy_state)
                if strategy_state.pending_paper_trades:
                    continue
                if strategy_state.last_processed_paper_event_slug == target_round.slug:
                    any_processed = True
                    round_completed = True
                    continue
                strategy_cfg = replace(cfg, strategy_id=strategy_id)
                strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                strategy_quote = replace(quote)
                _apply_strategy6_signal_to_quote(
                    cfg=strategy_cfg,
                    quote=strategy_quote,
                    binance_signal_service=binance_signal_service,
                )
                _runtime_log('strategy=' + str(strategy_id) + ' round=' + target_round.slug + ' quote {' + _describe_quote_source(strategy_quote) + '}')
                _runtime_log('strategy=' + str(strategy_id) + ' round=' + target_round.slug + ' ws_runtime {' + _describe_ws_runtime(client) + '}')
                side_decision = _resolve_side_from_strategy(
                    cfg=strategy_cfg,
                    state=strategy_session,
                    slug=target_round.slug,
                    quote=strategy_quote,
                    market_client=client,
                    window=target_round,
                    now=now,
                    entry_time=entry_time,
                )
                _runtime_log(
                    'strategy=' + str(strategy_id)
                    + ' round=' + target_round.slug
                    + ' side=' + str(side_decision.side)
                    + ' entry_at=' + entry_time.isoformat()
                    + ' signal={' + _describe_side_decision(side_decision) + '}'
                    + ' quote_source=' + str(strategy_quote.source)
                )
                state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                any_processed = True

                if side_decision.side is None:
                    if dry_run_once:
                        _runtime_log(
                            'dry-run strategy=' + str(strategy_id)
                            + ' round=' + target_round.slug
                            + ' skip due to signal; reason=' + str(side_decision.reason or 'signal_unavailable')
                        )
                        return {
                            "status": "dry_run",
                            "slug": target_round.slug,
                            "side": None,
                            "price": None,
                            "should_trade": False,
                            "skip_reason": side_decision.reason or "signal_unavailable",
                            "entry_time": entry_time.isoformat(),
                            "signal_open_up_price": side_decision.signal_open_up_price,
                            "signal_current_up_price": side_decision.signal_current_up_price,
                            "signal_threshold": side_decision.signal_threshold,
                            "signal_delta": side_decision.signal_delta,
                            "signal_locked": side_decision.signal_locked,
                        }
                    if (entry_time - now).total_seconds() > 1:
                        continue
                    append_trade_log(
                        log_path,
                        TradeRecord(
                            timestamp=datetime.now(timezone.utc),
                            mode="paper",
                            experiment_id=experiment_id,
                            round_index=strategy_session.round_index,
                            strategy=strategy_id,
                            entry_timing=strategy_cfg.entry_timing,
                            event_slug=target_round.slug,
                            start_time=target_round.start_time,
                            end_time=target_round.end_time,
                            side="SKIP",
                            price=None,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            cash_pnl=strategy_session.cash_pnl,
                            recovery_loss=strategy_session.recovery_loss,
                            consecutive_losses=strategy_session.consecutive_losses,
                            skip_reason=side_decision.reason or "signal_unavailable",
                            **_signal_record_kwargs(side_decision),
                        ),
                    )
                    strategy_session.last_processed_paper_event_slug = target_round.slug
                    strategy_session.round_index += 1
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
                    continue

                side = side_decision.side
                price = resolve_quote_price(side, strategy_quote)
                if _ws_is_stale_for_trade(client, strategy_cfg):
                    if dry_run_once:
                        return {
                            "status": "dry_run",
                            "slug": target_round.slug,
                            "side": side,
                            "price": price,
                            "should_trade": False,
                            "skip_reason": "ws_stale",
                            "entry_time": entry_time.isoformat(),
                            "projected_max_stake_skip_streak": 0,
                            "signal_open_up_price": side_decision.signal_open_up_price,
                            "signal_current_up_price": side_decision.signal_current_up_price,
                            "signal_threshold": side_decision.signal_threshold,
                            "signal_delta": side_decision.signal_delta,
                            "signal_locked": side_decision.signal_locked,
                        }
                    if (entry_time - now).total_seconds() > 1:
                        continue
                    append_trade_log(
                        log_path,
                        TradeRecord(
                            timestamp=datetime.now(timezone.utc),
                            mode="paper",
                            experiment_id=experiment_id,
                            round_index=strategy_session.round_index,
                            strategy=strategy_id,
                            entry_timing=strategy_cfg.entry_timing,
                            event_slug=target_round.slug,
                            start_time=target_round.start_time,
                            end_time=target_round.end_time,
                            side=side,
                            price=price,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            cash_pnl=strategy_session.cash_pnl,
                            recovery_loss=strategy_session.recovery_loss,
                            consecutive_losses=strategy_session.consecutive_losses,
                            skip_reason="ws_stale",
                            **_signal_record_kwargs(side_decision),
                        ),
                    )
                    strategy_session.last_processed_paper_event_slug = target_round.slug
                    strategy_session.round_index += 1
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
                    continue

                if _entry_window_missed(now, entry_time, grace_seconds=strategy_cfg.entry_grace_seconds):
                    if dry_run_once:
                        return {
                            "status": "dry_run",
                            "slug": target_round.slug,
                            "side": side,
                            "price": price,
                            "should_trade": False,
                            "skip_reason": "entry_window_missed",
                            "entry_time": entry_time.isoformat(),
                            "projected_max_stake_skip_streak": 0,
                            "signal_open_up_price": side_decision.signal_open_up_price,
                            "signal_current_up_price": side_decision.signal_current_up_price,
                            "signal_threshold": side_decision.signal_threshold,
                            "signal_delta": side_decision.signal_delta,
                            "signal_locked": side_decision.signal_locked,
                        }
                    append_trade_log(
                        log_path,
                        TradeRecord(
                            timestamp=datetime.now(timezone.utc),
                            mode="paper",
                            experiment_id=experiment_id,
                            round_index=strategy_session.round_index,
                            strategy=strategy_id,
                            entry_timing=strategy_cfg.entry_timing,
                            event_slug=target_round.slug,
                            start_time=target_round.start_time,
                            end_time=target_round.end_time,
                            side=side,
                            price=price,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            cash_pnl=strategy_session.cash_pnl,
                            recovery_loss=strategy_session.recovery_loss,
                            consecutive_losses=strategy_session.consecutive_losses,
                            skip_reason="entry_window_missed",
                            **_signal_record_kwargs(side_decision),
                        ),
                    )
                    strategy_session.last_processed_paper_event_slug = target_round.slug
                    strategy_session.round_index += 1
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
                    continue

                plan = build_trade_plan(
                    state=strategy_session,
                    side=side,
                    price=price,
                    target_profit=strategy_cfg.target_profit,
                    min_price_threshold=getattr(strategy_cfg, 'min_price_threshold', None),
                    max_price_threshold=strategy_cfg.max_price_threshold,
                    max_stake=strategy_cfg.max_stake,
                    max_consecutive_losses=strategy_cfg.max_consecutive_losses,
                    bet_sizing_mode=strategy_cfg.bet_sizing_mode,
                    base_order_cost=strategy_cfg.base_order_cost,
                )
                if dry_run_once:
                    projected_streak = (
                        strategy_session.consecutive_max_stake_skips + 1
                        if plan.skip_reason == "order_cost_above_max_stake"
                        else 0
                    )
                    return {
                        "status": "dry_run",
                        "slug": target_round.slug,
                        "side": side,
                        "price": price,
                        "should_trade": plan.should_trade,
                        "skip_reason": plan.skip_reason,
                        "entry_time": entry_time.isoformat(),
                        "projected_max_stake_skip_streak": projected_streak,
                        "signal_open_up_price": side_decision.signal_open_up_price,
                        "signal_current_up_price": side_decision.signal_current_up_price,
                        "signal_threshold": side_decision.signal_threshold,
                        "signal_delta": side_decision.signal_delta,
                        "signal_locked": side_decision.signal_locked,
                    }
                if not plan.should_trade:
                    if (entry_time - now).total_seconds() > 1:
                        continue
                    skip_stop_loss_triggered = _should_reset_after_risk_gate_skip(
                        strategy_session,
                        skip_reason=plan.skip_reason,
                        cfg=strategy_cfg,
                        stop_loss_triggered=plan.stop_loss_triggered,
                    )
                    append_trade_log(
                        log_path,
                        TradeRecord(
                            timestamp=datetime.now(timezone.utc),
                            mode="paper",
                            experiment_id=experiment_id,
                            round_index=strategy_session.round_index,
                            strategy=strategy_id,
                            entry_timing=strategy_cfg.entry_timing,
                            event_slug=target_round.slug,
                            start_time=target_round.start_time,
                            end_time=target_round.end_time,
                            side=side,
                            price=price,
                            order_size=0.0,
                            order_cost=0.0,
                            expected_profit=0.0,
                            result=None,
                            trade_pnl=0.0,
                            cash_pnl=strategy_session.cash_pnl,
                            recovery_loss=strategy_session.recovery_loss,
                            consecutive_losses=strategy_session.consecutive_losses,
                            stop_loss_triggered=skip_stop_loss_triggered,
                            skip_reason=plan.skip_reason,
                            **_signal_record_kwargs(side_decision),
                        ),
                    )
                    strategy_session.last_processed_paper_event_slug = target_round.slug
                    strategy_session, should_alert, _, skip_streak = _apply_post_entry_risk_gate_skip(
                        strategy_session,
                        skip_reason=plan.skip_reason,
                        cfg=strategy_cfg,
                        stop_loss_triggered=plan.stop_loss_triggered,
                    )
                    if should_alert:
                        _emit_max_stake_skip_alert(
                            slug=target_round.slug,
                            side=side,
                            price=price,
                            state=strategy_session,
                            cfg=strategy_cfg,
                            skip_streak=skip_streak,
                        )
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
                    continue

                strategy_session.consecutive_max_stake_skips = 0
                if (entry_time - now).total_seconds() > 1:
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    continue
                queued = _queue_pending_paper_trade(
                    state=strategy_session,
                    window=target_round,
                    plan=plan,
                    side=side,
                    cfg=strategy_cfg,
                    side_decision=side_decision,
                    experiment_id=experiment_id,
                )
                if queued:
                    strategy_session.last_processed_paper_event_slug = target_round.slug
                    strategy_session.round_index += 1
                state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                _sync_legacy_paper_state_fields(state, strategy_ids)
                round_completed = True
                _copy_session_state_into(loaded_state, state)
                save_session_state(state_path, state)

            for challenger in active_challengers:
                strategy_state = challenger.get("_paper_state")
                if not isinstance(strategy_state, PaperStrategyState):
                    continue
                if strategy_state.pending_paper_trades:
                    continue
                experiment_id = str(challenger.get("candidate_id") or "").strip() or _paper_experiment_id(
                    int(challenger.get("base_strategy_id") or 0),
                    strategy_state,
                )
                base_strategy_id = int(challenger.get("base_strategy_id") or 0)
                if base_strategy_id < 1:
                    continue
                strategy_cfg = _candidate_cfg_with_params(cfg, base_strategy_id, challenger.get("params"))
                strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                strategy_quote = replace(quote)
                _apply_strategy6_signal_to_quote(
                    cfg=strategy_cfg,
                    quote=strategy_quote,
                    binance_signal_service=binance_signal_service,
                )
                side_decision = _resolve_side_from_strategy(
                    cfg=strategy_cfg,
                    state=strategy_session,
                    slug=target_round.slug,
                    quote=strategy_quote,
                    market_client=client,
                    window=target_round,
                    now=now,
                    entry_time=entry_time,
                )
                if side_decision.side is None:
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    continue

                side = side_decision.side
                price = resolve_quote_price(side, strategy_quote)
                if _ws_is_stale_for_trade(client, strategy_cfg):
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    continue

                if _entry_window_missed(now, entry_time, grace_seconds=strategy_cfg.entry_grace_seconds):
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    continue

                plan = build_trade_plan(
                    state=strategy_session,
                    side=side,
                    price=price,
                    target_profit=strategy_cfg.target_profit,
                    min_price_threshold=getattr(strategy_cfg, 'min_price_threshold', None),
                    max_price_threshold=strategy_cfg.max_price_threshold,
                    max_stake=strategy_cfg.max_stake,
                    max_consecutive_losses=strategy_cfg.max_consecutive_losses,
                    bet_sizing_mode=strategy_cfg.bet_sizing_mode,
                    base_order_cost=strategy_cfg.base_order_cost,
                )
                if not plan.should_trade:
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    continue

                if (entry_time - now).total_seconds() > 1:
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    continue

                queued = _queue_pending_paper_trade(
                    state=strategy_session,
                    window=target_round,
                    plan=plan,
                    side=side,
                    cfg=strategy_cfg,
                    side_decision=side_decision,
                    experiment_id=experiment_id,
                )
                if queued:
                    strategy_session.round_index += 1
                next_state = _session_state_to_paper_strategy_state(strategy_session)
                next_state.experiment_id = experiment_id
                challenger["_paper_state"] = next_state
                challenger_state_changed = True

            if challenger_state_changed:
                _save_active_optimizer_challengers(optimizer_state_path, optimizer_state_payload, active_challengers)

            consecutive_errors = 0
            if round_completed:
                if not _sleep_until_round_end(cfg, target_round, stop_event):
                    return {"status": "stopped"}
                continue
            if not any_processed and pending_strategy_ids:
                if not _sleep_if_not_stopped(
                    stop_event,
                    _poll_interval_for_target_round(
                        cfg=cfg,
                        now=datetime.now(timezone.utc),
                        target_round=target_round,
                    ),
                ):
                    return {"status": "stopped"}
                continue
            if any_processed and datetime.now(timezone.utc) < target_round.end_time:
                if not _sleep_if_not_stopped(
                    stop_event,
                    _poll_interval_for_target_round(
                        cfg=cfg,
                        now=datetime.now(timezone.utc),
                        target_round=target_round,
                    ),
                ):
                    return {"status": "stopped"}
                continue
            if not _sleep_if_not_stopped(
                stop_event,
                _poll_interval_for_target_round(
                    cfg=cfg,
                    now=datetime.now(timezone.utc),
                    target_round=target_round,
                ),
            ):
                return {"status": "stopped"}
            continue
        except Exception as exc:
            if dry_run_once:
                return {"status": "error", "error": str(exc)}
            consecutive_errors += 1
            backoff = _runtime_backoff_seconds(cfg, consecutive_errors)
            _runtime_log('runtime error #' + str(consecutive_errors) + ': ' + str(exc) + ' | backoff=' + str(backoff) + 's')
            if not _sleep_if_not_stopped(stop_event, backoff):
                return {"status": "stopped"}
