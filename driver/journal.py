"""Crash-safe, deliberately narrow Shot Studio workspace persistence."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
from pathlib import Path

from . import moves

MAX_BYTES = 16 * 1024 * 1024
MAX_TAKES = 2000
MAX_WAYPOINTS = 512
MAX_STRING = 4096
_TOP = {"version", "revision", "draft", "slate", "takes"}
_TAKE_REQUIRED = {"id", "scene", "shot", "take", "note", "circled", "motion"}
_TAKE_OPTIONAL = {"move", "duration", "setup", "at", "run_id", "source",
                  "fingerprint", "setup_fields", "recording", "path"}
_MOTION_FIELDS = {"verdict", "repeatable", "peak_error", "peak_error_pitch",
                  "peak_error_yaw", "clamp_events", "telemetry_gaps",
                  "cues_waited", "samples", "elapsed", "aborted", "trace"}


class WorkspaceJournal:
    """One local draft, never a record of camera authority or intent.

    A damaged current file is intentionally not healed in place.  A valid
    backup remains visible for recovery, while saves stay blocked until an
    operator chooses how to handle the damaged file.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.warning: str | None = None
        self.revision = 0
        self._lock = threading.RLock()
        self._blocked = False

    @property
    def path(self) -> Path:
        return self.root / "workspace.json"

    @property
    def backup_path(self) -> Path:
        return self.root / "workspace.backup.json"

    def load(self) -> dict | None:
        """Return a detached safe snapshot, or the backup after corruption."""
        with self._lock:
            if not self.path.exists() and not self.path.is_symlink():
                if not self.backup_path.exists() and not self.backup_path.is_symlink():
                    return None
                self._blocked = True
                try:
                    payload = self._read(self.backup_path)
                except ValueError as exc:
                    self.warning = "Workspace backup needs recovery: " + str(exc)
                    return None
                self.warning = "Workspace file is missing; restored backup read-only"
                self.revision = payload["revision"]
                return self._snapshot(payload)
            try:
                payload = self._read(self.path)
            except ValueError as exc:
                self._blocked = True
                self.warning = "Workspace file needs recovery: " + str(exc)
                try:
                    payload = self._read(self.backup_path)
                except ValueError:
                    return None
                self.warning += "; restored the previous snapshot read-only"
            self.revision = payload["revision"]
            return self._snapshot(payload)

    def save(self, *, draft: dict, slate: dict, takes: list) -> None:
        """Atomically save a canonical workspace after validating its bounds."""
        with self._lock:
            if self._blocked:
                raise RuntimeError("workspace recovery is required before saving")
            if self.path.exists() or self.path.is_symlink():
                try:
                    previous = self._read(self.path)
                except ValueError:
                    # Do the same conservative recovery assessment load() does
                    # before refusing to write over evidence of the failure.
                    self.load()
                    raise RuntimeError("workspace recovery is required before saving")
            else:
                previous = None
            canonical = self._payload(draft, slate, takes, self.revision + 1)
            raw = self._encode(canonical)
            self._ensure_root()
            # Keep an independently durable copy of the last known-good file.
            if previous is not None:
                self._write_atomic(self.backup_path, self._encode(previous))
            self._write_atomic(self.path, raw)
            self.revision = canonical["revision"]
            self.warning = None

    def recovery_info(self) -> dict:
        """Read-only, path-free recovery facts for an explicit operator choice."""
        with self._lock:
            return self._recovery_summary(self._recovery_state())

    def recover(self, action: str, *, expected_fingerprint: str,
                draft: dict | None = None, slate: dict | None = None,
                takes: list | None = None) -> dict:
        """Explicitly repair a damaged workspace without destroying evidence.

        ``keep_current`` needs the caller's in-memory draft, slate and takes;
        ``restore_backup`` uses only the validated backup snapshot.
        """
        with self._lock:
            state = self._recovery_state()
            info = self._recovery_summary(state)
            if not info["needed"]:
                raise RuntimeError("workspace does not need recovery")
            # Any explicit recovery attempt keeps ordinary saves closed until
            # this method has durably completed its chosen replacement.
            self._blocked = True
            if (not isinstance(expected_fingerprint, str)
                    or expected_fingerprint != info["fingerprint"]):
                raise RuntimeError("workspace changed; inspect recovery again")
            if action == "restore_backup":
                if not info["can_restore_backup"]:
                    raise RuntimeError("no valid backup is available to restore")
                backup = state["backup"]["payload"]
                payload = self._payload(backup["draft"], backup["slate"],
                                        backup["takes"], info["revision"] + 1)
            elif action == "keep_current":
                if not info["can_keep_current"]:
                    raise RuntimeError("current workspace cannot be safely preserved")
                if draft is None or slate is None or takes is None:
                    raise ValueError("keep_current needs draft, slate and takes")
                payload = self._payload(draft, slate, takes, info["revision"] + 1)
            else:
                raise ValueError("unknown workspace recovery action")
            self._ensure_root()
            self._preserve_recovery_copies(state)
            # The copies preserve the inspected bytes; do not replace anything
            # if another process touched either original while they were made.
            if expected_fingerprint != self._recovery_summary(self._recovery_state())["fingerprint"]:
                raise RuntimeError("workspace changed; inspect recovery again")
            self._write_atomic(self.path, self._encode(payload))
            self.revision = payload["revision"]
            self._blocked = False
            self.warning = None
            return self._snapshot(payload)

    def _read(self, path: Path) -> dict:
        try:
            if path.is_symlink():
                raise ValueError(f"{path.name} is unsafe")
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("file exceeds 16 MB")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path.name} is unreadable") from exc
        return self._validated_payload(payload)

    def _recovery_state(self) -> dict:
        current = self._inspect_file(self.path)
        backup = self._inspect_file(self.backup_path)
        needed = (current["state"] != "valid"
                  and (current["state"] != "missing" or backup["state"] != "missing"))
        safe = all(item["safe"] for item in (current, backup))
        revisions = [item["payload"]["revision"] for item in (current, backup)
                     if item["payload"] is not None]
        return {"current": current, "backup": backup, "needed": needed,
                "revision": max(revisions, default=self.revision), "safe": safe}

    def _inspect_file(self, path: Path) -> dict:
        if path.is_symlink():
            return {"state": "invalid", "payload": None, "raw": None, "safe": False}
        try:
            if not path.exists():
                return {"state": "missing", "payload": None, "raw": None, "safe": True}
            if path.stat().st_size > MAX_BYTES:
                return {"state": "invalid", "payload": None, "raw": None, "safe": False}
            raw = path.read_bytes()
            payload = self._validated_payload(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return {"state": "invalid", "payload": None,
                    "raw": locals().get("raw"), "safe": "raw" in locals()}
        return {"state": "valid", "payload": payload, "raw": raw, "safe": True}

    def _recovery_summary(self, state: dict) -> dict:
        fingerprint = self._recovery_fingerprint(state) if state["safe"] and state["needed"] else None
        return {"needed": state["needed"], "revision": state["revision"],
                "can_restore_backup": bool(state["needed"] and state["safe"]
                                           and state["backup"]["state"] == "valid"),
                "can_keep_current": bool(state["needed"] and state["safe"]),
                "current": state["current"]["state"],
                "backup": state["backup"]["state"], "fingerprint": fingerprint}

    @staticmethod
    def _recovery_fingerprint(state: dict) -> str:
        digest = hashlib.sha256()
        for label in ("current", "backup"):
            item = state[label]
            digest.update(label.encode("ascii") + b":" + item["state"].encode("ascii") + b"\0")
            raw = item["raw"]
            if raw is not None:
                digest.update(len(raw).to_bytes(8, "big") + raw)
        return digest.hexdigest()

    def _ensure_root(self) -> None:
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise RuntimeError("workspace root is unsafe")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise RuntimeError("workspace root is unsafe")

    def _preserve_recovery_copies(self, state: dict) -> None:
        for label in ("current", "backup"):
            raw = state[label]["raw"]
            if raw is not None:
                path = self.root / f"workspace.recovery-{uuid.uuid4().hex}.{label}.json"
                self._write_atomic(path, raw)

    def _payload(self, draft: dict, slate: dict, takes: list, revision: int) -> dict:
        return self._validated_payload({
            "version": 1, "revision": revision, "draft": draft,
            "slate": slate, "takes": copy.deepcopy(takes),
        })

    @staticmethod
    def _snapshot(payload: dict) -> dict:
        return copy.deepcopy({key: payload[key] for key in ("draft", "slate", "takes")})

    def _validated_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != _TOP:
            raise ValueError("workspace schema has unknown or missing fields")
        if (isinstance(payload["version"], bool)
                or not isinstance(payload["version"], int)
                or payload["version"] != 1):
            raise ValueError("workspace schema version is unsupported")
        revision = payload["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("workspace revision is invalid")
        draft = payload["draft"]
        if (not isinstance(draft, dict) or "waypoints" not in draft
                or not isinstance(draft["waypoints"], list)):
            raise ValueError("workspace draft is invalid")
        if len(draft["waypoints"]) > MAX_WAYPOINTS:
            raise ValueError("workspace draft has too many waypoints")
        try:
            draft = moves.Move.from_dict(draft).to_dict()
        except (TypeError, ValueError) as exc:
            raise ValueError("workspace draft is invalid") from exc
        slate = payload["slate"]
        if (not isinstance(slate, dict) or set(slate) != {"scene", "shot", "take"}
                or not isinstance(slate["scene"], str)
                or not isinstance(slate["shot"], str)
                or isinstance(slate["take"], bool)
                or not isinstance(slate["take"], int) or slate["take"] < 1):
            raise ValueError("workspace slate is invalid")
        takes = payload["takes"]
        if not isinstance(takes, list) or len(takes) > MAX_TAKES:
            raise ValueError("workspace takes are invalid")
        for index, take in enumerate(takes):
            self._validated_take(take, index)
        result = {"version": 1, "revision": revision, "draft": draft,
                  "slate": dict(slate), "takes": copy.deepcopy(takes)}
        self._json_safe(result)
        return result

    def _validated_take(self, take: object, index: int) -> None:
        if not isinstance(take, dict) or not _TAKE_REQUIRED <= set(take):
            raise ValueError("workspace take is missing required fields")
        if set(take) - (_TAKE_REQUIRED | _TAKE_OPTIONAL):
            raise ValueError("workspace take has unknown fields")
        ident = take["id"]
        if (isinstance(ident, bool) or not isinstance(ident, int)
                or ident < 0 or ident != index):
            raise ValueError("workspace take id is not sequential")
        if (not all(isinstance(take[key], str) for key in ("scene", "shot", "note"))
                or isinstance(take["take"], bool)
                or not isinstance(take["take"], int) or take["take"] < 1
                or not isinstance(take["circled"], bool)):
            raise ValueError("workspace take fields are invalid")
        for key in ("move", "setup", "at", "source"):
            if key in take and not isinstance(take[key], str):
                raise ValueError("workspace take provenance is invalid")
        for key in ("run_id", "fingerprint"):
            if key in take and take[key] is not None and not isinstance(take[key], str):
                raise ValueError("workspace take provenance is invalid")
        if "duration" in take and (isinstance(take["duration"], bool)
                                   or not isinstance(take["duration"], (int, float))
                                   or not math.isfinite(take["duration"])):
            raise ValueError("workspace take duration is invalid")
        recording = take.get("recording")
        if recording is not None and (not isinstance(recording, dict)
                                      or set(recording) != {"requested", "reported"}
                                      or not all(isinstance(value, bool)
                                                 for value in recording.values())):
            raise ValueError("workspace take recording is invalid")
        if "setup_fields" in take and not isinstance(take["setup_fields"], dict):
            raise ValueError("workspace take setup fields are invalid")
        if "path" in take and take["path"] is not None:
            if not isinstance(take["path"], dict):
                raise ValueError("workspace take path is invalid")
            try:
                take["path"] = moves.Move.from_dict(take["path"]).to_dict()
            except (TypeError, ValueError) as exc:
                raise ValueError("workspace take path is invalid") from exc
        motion = take["motion"]
        if motion is None:
            return
        if not isinstance(motion, dict) or set(motion) - _MOTION_FIELDS:
            raise ValueError("workspace take motion is invalid")
        if "trace" in motion:
            trace = motion["trace"]
            if not isinstance(trace, list) or len(trace) > 300:
                raise ValueError("workspace take trace is invalid")
            for point in trace:
                if (not isinstance(point, list) or len(point) != 3
                        or any(isinstance(value, bool)
                               or not isinstance(value, (int, float))
                               or not math.isfinite(value) for value in point)):
                    raise ValueError("workspace take trace is invalid")

    @staticmethod
    def _json_safe(value: object, depth: int = 0) -> None:
        if depth > 20:
            raise ValueError("workspace nesting is too deep")
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return
        if isinstance(value, float):
            if math.isfinite(value):
                return
            raise ValueError("workspace has a non-finite number")
        if isinstance(value, str):
            if len(value) <= MAX_STRING:
                return
            raise ValueError("workspace string is too long")
        if isinstance(value, list):
            for item in value:
                WorkspaceJournal._json_safe(item, depth + 1)
            return
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            for item in value.values():
                WorkspaceJournal._json_safe(item, depth + 1)
            return
        raise ValueError("workspace contains a non-JSON value")

    @staticmethod
    def _encode(payload: dict) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("workspace exceeds 16 MB")
        return raw

    def _write_atomic(self, path: Path, raw: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=".workspace-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
