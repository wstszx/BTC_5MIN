from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_text
from config import AppConfig
from polymarket_api import PolymarketClient, parse_iso_datetime
from utils import _is_stop_requested, _safe_stop_requested, _sleep_if_not_stopped


def _resolve_live_order_type(raw_order_type: str):
    from py_clob_client_v2 import OrderType

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
    sanitized = _default_live_redeem_state()
    raw_conditions = state.get("conditions") if isinstance(state, dict) else {}
    if isinstance(raw_conditions, dict):
        sanitized["conditions"] = {
            str(condition_id): _normalize_live_redeem_entry(raw_entry)
            for condition_id, raw_entry in raw_conditions.items()
        }
    sanitized["runtime"] = _normalize_live_redeem_runtime((state or {}).get("runtime") if isinstance(state, dict) else None)
    atomic_write_text(path, json.dumps(sanitized, indent=2), encoding="utf-8")


_LIVE_REDEEM_CTF_CONTRACT = os.getenv("POLYMARKET_CTF_CONTRACT") or "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_LIVE_REDEEM_COLLATERAL_TOKEN = os.getenv("POLYMARKET_COLLATERAL_TOKEN") or "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_LIVE_REDEEM_PARENT_COLLECTION_ID = "0x" + ("00" * 32)
_LIVE_REDEEM_RELAYER_URL = os.getenv("POLYMARKET_RELAYER_URL") or "https://relayer-v2.polymarket.com"
_LIVE_REDEEM_INDEX_SETS = [1, 2]
_LIVE_REDEEM_TERMINAL_STATUSES = {"completed", "submitted", "terminal_error", "dry_run"}


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
