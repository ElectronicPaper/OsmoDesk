"""Host-local AI preferences with optional Windows Credential Manager storage."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import tempfile
from pathlib import Path

MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"}
EFFORTS = {"low", "medium", "high"}
_FIELDS = {"enabled", "allow_lan", "model", "effort", "budget_usd",
           "request_limit", "remember_host"}
_UPDATE = _FIELDS | {"provider_key", "remove_key"}
_DEFAULTS = {"enabled": False, "allow_lan": False, "model": "gpt-5.6-luna",
             "effort": "low", "budget_usd": 1.0, "request_limit": 20,
             "remember_host": False}


class WinCredVault:
    """A small no-fallback adapter; it never writes a key outside WinCred."""

    def __init__(self):
        self._native = None

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def read(self, target: str) -> str | None:
        if not self.available:
            return None
        native = self._api()
        credential = ctypes.POINTER(_CREDENTIALW)()
        if not native.CredReadW(target, 1, 0, ctypes.byref(credential)):
            code = ctypes.get_last_error()
            if code == 1168:  # ERROR_NOT_FOUND
                return None
            raise OSError(code, "Credential Manager read failed")
        try:
            raw = ctypes.string_at(credential.contents.CredentialBlob,
                                   credential.contents.CredentialBlobSize)
            return raw.decode("utf-8")
        finally:
            native.CredFree(credential)

    def write(self, target: str, secret: str) -> None:
        if not self.available:
            raise RuntimeError("Windows Credential Manager is unavailable")
        native = self._api()
        raw = secret.encode("utf-8")
        blob = ctypes.create_string_buffer(raw)
        credential = _CREDENTIALW(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                                  CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte)),
                                  Persist=2, UserName="OsmoDesk")
        if not native.CredWriteW(ctypes.byref(credential), 0):
            raise OSError(ctypes.get_last_error(), "Credential Manager write failed")

    def delete(self, target: str) -> None:
        if not self.available:
            return
        if not self._api().CredDeleteW(target, 1, 0):
            code = ctypes.get_last_error()
            if code != 1168:
                raise OSError(code, "Credential Manager delete failed")

    def _api(self):
        if self._native is None:
            native = ctypes.WinDLL("advapi32", use_last_error=True)
            native.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
            native.CredReadW.restype = ctypes.c_int
            native.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
            native.CredWriteW.restype = ctypes.c_int
            native.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            native.CredDeleteW.restype = ctypes.c_int
            native.CredFree.argtypes = [ctypes.c_void_p]
            native.CredFree.restype = None
            self._native = native
        return self._native


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
                ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
                ("LastWritten", ctypes.c_uint64), ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


class HostSettings:
    """No provider key is read or persisted unless the host explicitly loads."""

    def __init__(self, root: Path, vault=None, *, initial_key: str | None = None):
        self.root = Path(root)
        self.vault = vault
        self._prefs = dict(_DEFAULTS)
        self._key = self._clean_key(initial_key) if initial_key else None
        self._loaded = False

    @property
    def path(self) -> Path:
        return self.root / "host-settings.json"

    @property
    def credential_target(self) -> str:
        digest = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).hexdigest()
        return "OsmoDesk.HostSettings." + digest

    def load(self) -> dict:
        """Explicitly load preferences and, only if requested, the vault key."""
        self._assert_safe_root()
        if self.path.exists():
            self._prefs = self._read_preferences()
        if self._prefs["remember_host"]:
            key = self._vault_read()
            if key is None:
                self._key = None
                self._prefs["enabled"] = False
                self._prefs["remember_host"] = False
            else:
                self._key = self._clean_key(key)
        if self._key is None:
            # Memory-only secrets intentionally do not survive a restart. A
            # stored enabled preference must not resurrect a missing provider.
            self._prefs['enabled'] = False
        self._loaded = True
        return self.public()

    def update(self, data: dict) -> dict:
        """Apply a strict partial update; provider_key is memory-only by default."""
        if not isinstance(data, dict) or set(data) - _UPDATE:
            raise ValueError("unknown host setting")
        prefs = dict(self._prefs)
        for key in _FIELDS & set(data):
            prefs[key] = data[key]
        self._validate(prefs)
        remove = data.get("remove_key", False)
        if type(remove) is not bool:
            raise ValueError("remove_key must be boolean")
        key = self._key
        if "provider_key" in data:
            key = self._clean_key(data["provider_key"])
        if remove:
            key, prefs["enabled"], prefs["remember_host"] = None, False, False
        if prefs["remember_host"]:
            if not self._vault_available():
                raise ValueError("remember_host is unavailable on this host")
            if key is None:
                raise ValueError("remember_host needs a provider key")
        if prefs["enabled"] and key is None:
            raise ValueError("AI cannot be enabled without a provider key")
        old_prefs, old_key = self._prefs, self._key
        changed_vault = False
        try:
            if remove or ("remember_host" in data and not prefs["remember_host"]):
                if self._vault_available():
                    self.vault.delete(self.credential_target)
                    changed_vault = True
            elif prefs["remember_host"] and (key != old_key or not old_prefs["remember_host"]):
                self.vault.write(self.credential_target, key)
                changed_vault = True
            self._write_preferences(prefs)
        except Exception:
            if changed_vault:
                self._restore_vault(old_prefs, old_key)
            raise
        self._prefs, self._key = prefs, key
        return self.public()

    def public(self) -> dict:
        return {**copy.deepcopy(self._prefs), "key_configured": self._key is not None,
                "remember_supported": self._vault_available()}

    def provider_key(self) -> str | None:
        """Internal provider-construction hook; never expose this through HTTP."""
        return self._key

    def _read_preferences(self) -> dict:
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 64 * 1024:
                raise ValueError("host settings file is unsafe")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("host settings file is unreadable") from exc
        if (not isinstance(raw, dict) or set(raw) != {"version", "prefs"}
                or type(raw["version"]) is not int or raw["version"] != 1):
            raise ValueError("host settings schema is invalid")
        prefs = raw["prefs"]
        self._validate(prefs)
        return dict(prefs)

    def _validate(self, prefs: object) -> None:
        if not isinstance(prefs, dict) or set(prefs) != _FIELDS:
            raise ValueError("host settings are invalid")
        if any(type(prefs[key]) is not bool for key in ("enabled", "allow_lan", "remember_host")):
            raise ValueError("host settings booleans are invalid")
        if (not isinstance(prefs["model"], str) or prefs["model"] not in MODELS
                or not isinstance(prefs["effort"], str) or prefs["effort"] not in EFFORTS):
            raise ValueError("host model or effort is invalid")
        budget = prefs["budget_usd"]
        if type(budget) not in (int, float) or not 0.01 <= budget <= 100:
            raise ValueError("host budget is invalid")
        limit = prefs["request_limit"]
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("host request limit is invalid")

    @staticmethod
    def _clean_key(key: object) -> str:
        if (not isinstance(key, str) or not key or len(key) > 4096 or not key.isascii()
                or any(ord(character) <= 32 or ord(character) == 127 for character in key)):
            raise ValueError("provider key is invalid")
        return key

    def _vault_available(self) -> bool:
        return bool(self.vault is not None and getattr(self.vault, "available", False))

    def _vault_read(self) -> str | None:
        return self.vault.read(self.credential_target) if self._vault_available() else None

    def _assert_safe_root(self) -> None:
        if (any(part.is_symlink() for part in (self.root, *self.root.parents))
                or (self.root.exists() and not self.root.is_dir())):
            raise ValueError("host settings root is unsafe")

    def _ensure_root(self) -> None:
        self._assert_safe_root()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("host settings root is unsafe")

    def _write_preferences(self, prefs: dict) -> None:
        self._ensure_root()
        if self.path.is_symlink():
            raise ValueError("host settings file is unsafe")
        raw = json.dumps({"version": 1, "prefs": prefs}, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".host-settings-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _restore_vault(self, prefs: dict, key: str | None) -> None:
        try:
            if prefs["remember_host"] and key is not None:
                self.vault.write(self.credential_target, key)
            else:
                self.vault.delete(self.credential_target)
        except Exception:
            pass
