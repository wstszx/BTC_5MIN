from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import threading


@dataclass(slots=True)
class RuntimeSnapshot:
    active_mode: str
    desired_mode: str
    switch_state: str
    switch_reason: str | None
    current_round_slug: str | None
    round_in_progress: bool
    safe_to_switch: bool
    pending_live_order: bool
    runtime_alert_code: str | None
    runtime_alert_message: str | None
    runtime_alert_level: str | None
    runtime_alert_at: str | None
    last_transition_at: str | None


class RuntimeControl:
    _VALID_STATES = {"idle", "pending", "switching", "blocked"}
    _MODE_MAP = {"paper": "paper", "live": "live", "both": "both"}

    def __init__(self, initial_mode: str) -> None:
        normalized = self._normalize_mode(initial_mode)
        self._lock = threading.Lock()
        self._paper_worker_states: dict[str, dict[str, object | None]] = {}
        self._snapshot = RuntimeSnapshot(
            active_mode=normalized,
            desired_mode=normalized,
            switch_state="idle",
            switch_reason=None,
            current_round_slug=None,
            round_in_progress=False,
            safe_to_switch=True,
            pending_live_order=False,
            runtime_alert_code=None,
            runtime_alert_message=None,
            runtime_alert_level=None,
            runtime_alert_at=None,
            last_transition_at=None,
        )

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return replace(self._snapshot)

    def set_desired_mode(self, mode: str) -> RuntimeSnapshot:
        normalized = self._normalize_mode(mode)
        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}

            if snapshot.desired_mode != normalized:
                updates["desired_mode"] = normalized

            if normalized == snapshot.active_mode:
                if snapshot.switch_state != "idle" or snapshot.switch_reason is not None:
                    updates["switch_state"] = "idle"
                    updates["switch_reason"] = None
            else:
                if snapshot.switch_state != "pending" or snapshot.desired_mode != normalized:
                    updates["switch_state"] = "pending"
                    updates["switch_reason"] = None

            return self._apply_updates(snapshot, updates)

    def mark_pending(self, reason: str | None = None) -> RuntimeSnapshot:
        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}
            if snapshot.switch_state != "pending":
                updates["switch_state"] = "pending"
            if snapshot.switch_reason != reason:
                updates["switch_reason"] = reason
            if updates:
                return self._apply_updates(snapshot, updates)
            return replace(snapshot)

    def update_worker_state(self, **changes) -> RuntimeSnapshot:
        allowed = {
            "current_round_slug",
            "round_in_progress",
            "safe_to_switch",
            "pending_live_order",
            "runtime_alert_code",
            "runtime_alert_message",
            "runtime_alert_level",
            "runtime_alert_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unexpected worker state keys: {invalid}")

        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}
            for key, value in changes.items():
                if getattr(snapshot, key) != value:
                    updates[key] = value
            if any(
                key in updates
                for key in ("runtime_alert_code", "runtime_alert_message", "runtime_alert_level")
            ) and "runtime_alert_at" not in changes:
                updates["runtime_alert_at"] = self._iso_timestamp() if changes.get("runtime_alert_code") else None

            return self._apply_updates(snapshot, updates)

    def update_paper_worker_state(self, worker_key: str, **changes) -> RuntimeSnapshot:
        if not worker_key:
            raise ValueError("worker_key must be provided")
        allowed = {
            "current_round_slug",
            "round_in_progress",
            "safe_to_switch",
            "pending_live_order",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"unexpected worker state keys: {invalid}")

        with self._lock:
            current = dict(self._paper_worker_states.get(worker_key) or {})
            for field_name in allowed:
                if field_name in changes:
                    current[field_name] = changes[field_name]
                else:
                    current.setdefault(field_name, None if field_name == "current_round_slug" else False)
            self._paper_worker_states[worker_key] = current

            snapshot = self._snapshot
            worker_states = list(self._paper_worker_states.values())
            current_round_slug = next(
                (str(item.get("current_round_slug")) for item in worker_states if item.get("current_round_slug")),
                None,
            )
            updates: dict[str, object | None] = {
                "current_round_slug": current_round_slug,
                "round_in_progress": any(bool(item.get("round_in_progress")) for item in worker_states),
                "safe_to_switch": all(bool(item.get("safe_to_switch")) for item in worker_states) if worker_states else True,
                "pending_live_order": any(bool(item.get("pending_live_order")) for item in worker_states),
            }
            return self._apply_updates(snapshot, updates)

    def clear_paper_worker_states(self) -> RuntimeSnapshot:
        with self._lock:
            self._paper_worker_states.clear()
            snapshot = self._snapshot
            return self._apply_updates(
                snapshot,
                {
                    "current_round_slug": None,
                    "round_in_progress": False,
                    "safe_to_switch": True,
                    "pending_live_order": False,
                },
            )

    def mark_switching(self, reason: str | None = None) -> RuntimeSnapshot:
        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}
            if snapshot.switch_state != "switching":
                updates["switch_state"] = "switching"
            if snapshot.switch_reason != reason:
                updates["switch_reason"] = reason
            if updates:
                return self._apply_updates(snapshot, updates)
            return replace(snapshot)

    def mark_blocked(self, reason: str) -> RuntimeSnapshot:
        if not reason:
            raise ValueError("blocked reason must be provided")

        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}
            if snapshot.switch_state != "blocked":
                updates["switch_state"] = "blocked"
            if snapshot.switch_reason != reason:
                updates["switch_reason"] = reason
            if updates:
                return self._apply_updates(snapshot, updates)
            return replace(snapshot)

    def mark_active_mode(self, mode: str) -> RuntimeSnapshot:
        normalized = self._normalize_mode(mode)
        with self._lock:
            snapshot = self._snapshot
            updates = {}
            if normalized not in {"paper", "both"}:
                self._paper_worker_states.clear()
            if snapshot.active_mode != normalized:
                updates["active_mode"] = normalized
            if snapshot.desired_mode != normalized:
                updates["desired_mode"] = normalized
            if snapshot.switch_state != "idle" or snapshot.switch_reason is not None:
                updates["switch_state"] = "idle"
                updates["switch_reason"] = None

            return self._apply_updates(snapshot, updates)

    def _apply_updates(
        self, snapshot: RuntimeSnapshot, updates: dict[str, object | None]
    ) -> RuntimeSnapshot:
        if not updates:
            return replace(snapshot)

        updates = dict(updates)
        updates["last_transition_at"] = self._iso_timestamp()
        self._snapshot = replace(snapshot, **updates)
        return replace(self._snapshot)

    def _normalize_mode(self, mode: str) -> str:
        if not isinstance(mode, str):
            raise TypeError("mode must be a string")
        normalized = mode.strip().lower()
        if normalized in self._MODE_MAP:
            return self._MODE_MAP[normalized]
        raise ValueError(f"unknown mode: {mode}")

    def _iso_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()
