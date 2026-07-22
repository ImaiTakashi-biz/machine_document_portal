from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator, Literal

from app.services.next_day_sheet_service import SheetTarget


logger = logging.getLogger(__name__)
NotificationStatus = Literal["pending", "completed", "ambiguous"]
NotificationPhase = Literal["initial", "recheck"]

_locks_guard = Lock()
_path_locks: dict[str, RLock] = {}


def _shared_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _path_locks.setdefault(key, RLock())


@dataclass(frozen=True, slots=True)
class PrintItemState:
    part_number: str
    status: str


@dataclass(frozen=True, slots=True)
class PrintRecoveryState:
    target_key: str
    target: SheetTarget
    items: tuple[PrintItemState, ...]
    attention_required: bool
    completed: bool

    @property
    def unresolved_items(self) -> tuple[PrintItemState, ...]:
        return tuple(
            item
            for item in self.items
            if item.status in {"pending", "failed", "uncertain"}
        )

    @property
    def uncertain_items(self) -> tuple[PrintItemState, ...]:
        return tuple(item for item in self.items if item.status == "uncertain")

    @property
    def retryable_items(self) -> tuple[PrintItemState, ...]:
        return tuple(
            item
            for item in self.items
            if item.status in {"pending", "failed"}
        )

    @property
    def manual_items(self) -> tuple[PrintItemState, ...]:
        return tuple(
            item
            for item in self.items
            if item.status in {"missing", "manual_required"}
        )

    @property
    def attention_count(self) -> int:
        return max(len(self.unresolved_items), 1 if self.attention_required else 0)


class ScheduledJobStateStore:
    """Persist notification and per-part printing state for idempotent daily jobs."""

    def __init__(self, path: Path, *, spreadsheet_id: str | None) -> None:
        self.path = path
        self.spreadsheet_id = spreadsheet_id or ""
        self._lock = _shared_lock(path)

    @contextmanager
    def operation_lock(self) -> Iterator[None]:
        """Serialize automatic and user-requested printing in the single worker."""

        with self._lock:
            yield

    def record_daily_target(self, run_date: date, target: SheetTarget) -> str:
        with self._lock:
            state = self._load()
            target_key = self.target_key(target)
            state["daily_targets"][run_date.isoformat()] = target_key
            state["targets"].setdefault(target_key, self._new_target_record(target))
            self._save(state)
            return target_key

    def target_for_run_date(self, run_date: date) -> tuple[str, SheetTarget] | None:
        with self._lock:
            state = self._load()
            target_key = state["daily_targets"].get(run_date.isoformat())
            return self._target_from_state(state, str(target_key)) if target_key else None

    def target_by_key(self, target_key: str) -> SheetTarget | None:
        with self._lock:
            resolved = self._target_from_state(self._load(), target_key)
            return resolved[1] if resolved else None

    def notification_status(
        self,
        target_key: str,
        *,
        phase: NotificationPhase = "initial",
    ) -> NotificationStatus:
        with self._lock:
            record = self._target_record(self._load(), target_key)
            status_key, _ = self._notification_fields(phase)
            candidate = record.get(status_key, "pending")
            if candidate in {"completed", "ambiguous"}:
                return candidate
            return "pending"

    def mark_notification(
        self,
        target_key: str,
        status: NotificationStatus,
        *,
        phase: NotificationPhase = "initial",
    ) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            status_key, completed_at_key = self._notification_fields(phase)
            record[status_key] = status
            record[completed_at_key] = (
                datetime.now(timezone.utc).isoformat() if status == "completed" else None
            )
            self._save(state)

    def register_print_items(self, target_key: str, part_numbers: list[str]) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            items = record.setdefault("print_items", {})
            record["active_part_numbers"] = list(part_numbers)
            printed = {
                str(value)
                for value in record.get("printed_part_numbers", [])
                if isinstance(value, str)
            }
            for part_number in part_numbers:
                item = items.setdefault(part_number, {})
                item.setdefault(
                    "status", "submitted" if part_number in printed else "pending"
                )
                item.setdefault("last_error", None)
                item.setdefault("updated_at", None)
            self._save(state)

    def print_item_statuses(self, target_key: str) -> dict[str, str]:
        with self._lock:
            record = self._target_record(self._load(), target_key)
            items = record.get("print_items", {})
            if not isinstance(items, dict):
                return {}
            return {
                str(part_number): str(item.get("status", "pending"))
                for part_number, item in items.items()
                if isinstance(item, dict)
            }

    def mark_print_item(
        self,
        target_key: str,
        part_number: str,
        status: str,
        *,
        error: str | None = None,
        job_id: int | None = None,
    ) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            items = record.setdefault("print_items", {})
            item = items.setdefault(part_number, {})
            item["status"] = status
            item["last_error"] = error
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            if job_id is not None:
                item["job_id"] = job_id
            if status == "submitted":
                values = record.setdefault("printed_part_numbers", [])
                if part_number not in values:
                    values.append(part_number)
            self._save(state)

    def printed_part_numbers(self, target_key: str) -> set[str]:
        with self._lock:
            record = self._target_record(self._load(), target_key)
            values = record.get("printed_part_numbers", [])
            return {str(value) for value in values if isinstance(value, str)}

    def mark_part_printed(
        self, target_key: str, part_number: str, *, job_id: int | None = None
    ) -> None:
        self.mark_print_item(
            target_key, part_number, "submitted", job_id=job_id
        )

    def printing_completed(self, target_key: str) -> bool:
        with self._lock:
            record = self._target_record(self._load(), target_key)
            return bool(record.get("printing_completed", False))

    def mark_printing_completed(self, target_key: str) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            record["printing_completed"] = True
            record["printing_completed_at"] = datetime.now(timezone.utc).isoformat()
            record["print_attention_required"] = False
            record["next_print_retry_at"] = None
            record["last_print_error"] = None
            self._save(state)

    def record_automatic_print_failure(
        self,
        target_key: str,
        *,
        now: datetime,
        retry_delays: tuple[int, ...],
        error: str,
        uncertain: bool = False,
    ) -> str:
        """Schedule a safe retry or switch the target to user attention."""

        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            attempts = int(record.get("automatic_print_attempts", 0)) + 1
            record["automatic_print_attempts"] = attempts
            record["last_print_error"] = error
            if uncertain or attempts > len(retry_delays):
                record["print_attention_required"] = True
                record["next_print_retry_at"] = None
                result = "action_required"
            else:
                from datetime import timedelta

                next_attempt = now.astimezone(timezone.utc) + timedelta(
                    seconds=retry_delays[attempts - 1]
                )
                record["next_print_retry_at"] = next_attempt.isoformat()
                record["print_attention_required"] = False
                result = "retry_scheduled"
            self._save(state)
            return result

    def mark_manual_print_incomplete(self, target_key: str, *, error: str) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            record["print_attention_required"] = True
            record["next_print_retry_at"] = None
            record["last_print_error"] = error
            self._save(state)

    def automatic_print_due(self, target_key: str, now: datetime) -> bool:
        with self._lock:
            record = self._target_record(self._load(), target_key)
            if record.get("printing_completed") or record.get("print_attention_required"):
                return False
            next_value = record.get("next_print_retry_at")
            if not next_value:
                return True
            try:
                next_attempt = datetime.fromisoformat(str(next_value))
            except ValueError:
                return True
            return next_attempt <= now.astimezone(timezone.utc)

    def confirm_part_printed(self, target_key: str, part_number: str) -> None:
        with self.operation_lock():
            self.mark_print_item(target_key, part_number, "submitted")
            self._complete_if_resolved(target_key)

    def mark_part_not_printed(self, target_key: str, part_number: str) -> None:
        with self._lock:
            self.mark_print_item(target_key, part_number, "failed")

    def clear_attention(self, target_key: str) -> None:
        with self._lock:
            state = self._load()
            record = self._target_record(state, target_key)
            record["print_attention_required"] = False
            self._save(state)

    def latest_print_state(self, *, attention_only: bool = False) -> PrintRecoveryState | None:
        with self._lock:
            state = self._load()
            candidates: list[tuple[str, dict[str, Any]]] = []
            for target_key, record in state["targets"].items():
                if not isinstance(record, dict):
                    continue
                if attention_only and not record.get("print_attention_required"):
                    continue
                if not record.get("print_items") and not record.get("print_attention_required"):
                    continue
                candidates.append((str(target_key), record))
            if not candidates:
                return None
            target_key, record = max(
                candidates,
                key=lambda item: str(item[1].get("target_date", "")),
            )
            target = self._target_from_record(record)
            if target is None:
                return None
            raw_items = record.get("print_items", {})
            active = record.get("active_part_numbers", list(raw_items))
            items = tuple(
                PrintItemState(
                    part_number=str(part_number),
                    status=str(raw_items.get(part_number, {}).get("status", "pending")),
                )
                for part_number in active
                if isinstance(part_number, str)
                and isinstance(raw_items.get(part_number), dict)
            )
            return PrintRecoveryState(
                target_key=target_key,
                target=target,
                items=items,
                attention_required=bool(record.get("print_attention_required", False)),
                completed=bool(record.get("printing_completed", False)),
            )

    def target_key(self, target: SheetTarget) -> str:
        source = "|".join(
            (
                self.spreadsheet_id,
                target.target_date.isoformat(),
                str(target.sheet_id),
                target.sheet_name,
            )
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _complete_if_resolved(self, target_key: str) -> None:
        state = self.latest_print_state_for_key(target_key)
        if state is not None and not state.unresolved_items:
            self.mark_printing_completed(target_key)

    def latest_print_state_for_key(self, target_key: str) -> PrintRecoveryState | None:
        with self._lock:
            state = self._load()
            record = state["targets"].get(target_key)
            if not isinstance(record, dict):
                return None
            target = self._target_from_record(record)
            if target is None:
                return None
            raw_items = record.get("print_items", {})
            active = record.get("active_part_numbers", list(raw_items))
            items = tuple(
                PrintItemState(
                    part_number=str(part_number),
                    status=str(raw_items.get(part_number, {}).get("status", "pending")),
                )
                for part_number in active
                if isinstance(part_number, str)
                and isinstance(raw_items.get(part_number), dict)
            )
            return PrintRecoveryState(
                target_key=target_key,
                target=target,
                items=items,
                attention_required=bool(record.get("print_attention_required", False)),
                completed=bool(record.get("printing_completed", False)),
            )

    @staticmethod
    def _new_target_record(target: SheetTarget) -> dict[str, Any]:
        return {
            "target_date": target.target_date.isoformat(),
            "sheet_id": target.sheet_id,
            "sheet_name": target.sheet_name,
            "notification_status": "pending",
            "notification_completed_at": None,
            "recheck_notification_status": "pending",
            "recheck_notification_completed_at": None,
            "printing_completed": False,
            "printing_completed_at": None,
            "printed_part_numbers": [],
            "active_part_numbers": [],
            "print_items": {},
            "automatic_print_attempts": 0,
            "next_print_retry_at": None,
            "print_attention_required": False,
            "last_print_error": None,
        }

    @staticmethod
    def _notification_fields(phase: NotificationPhase) -> tuple[str, str]:
        if phase == "recheck":
            return (
                "recheck_notification_status",
                "recheck_notification_completed_at",
            )
        return "notification_status", "notification_completed_at"

    @staticmethod
    def _target_record(state: dict[str, Any], target_key: str) -> dict[str, Any]:
        record = state["targets"].get(target_key)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown scheduled target: {target_key}")
        return record

    def _target_from_state(
        self, state: dict[str, Any], target_key: str
    ) -> tuple[str, SheetTarget] | None:
        record = state["targets"].get(target_key)
        if not isinstance(record, dict):
            return None
        target = self._target_from_record(record)
        return (target_key, target) if target is not None else None

    @staticmethod
    def _target_from_record(record: dict[str, Any]) -> SheetTarget | None:
        try:
            return SheetTarget(
                target_date=date.fromisoformat(str(record["target_date"])),
                sheet_id=int(record["sheet_id"]),
                sheet_name=str(record["sheet_name"]),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Scheduled job state contains an invalid target")
            return None

    def _load(self) -> dict[str, Any]:
        empty = {"version": 2, "daily_targets": {}, "targets": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, ValueError):
            logger.exception("Scheduled job state could not be read")
            return empty
        if not isinstance(payload, dict):
            return empty
        daily_targets = payload.get("daily_targets")
        targets = payload.get("targets")
        if not isinstance(daily_targets, dict) or not isinstance(targets, dict):
            return empty
        return {"version": 2, "daily_targets": daily_targets, "targets": targets}

    def _save(self, state: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            logger.exception("Scheduled job state could not be saved")
            raise
