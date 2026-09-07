"""Read and update the local credentials file.

The camera SoftAP passphrase lives here. Nothing in this module logs, prints or
returns a value in a way meant for a terminal -- callers get the strings and
are expected to keep them out of stdout. `mask` exists for when something must
be shown.

File precedence: `.env.local` overrides `.env`. Real environment variables
override both, so a shell export always wins.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAMES = (".env.local", ".env")

# Accepted spellings, most specific first.
ALIASES = {
    "ssid": ("DJI_OSMO_POCKET_WIFI_SSID", "OSMO_SSID", "CAMERA_SSID",
             "WIFI_SSID", "SSID"),
    "password": ("DJI_OSMO_POCKET_WIFI_PASS", "DJI_OSMO_POCKET_WIFI_PASSWORD",
                 "OSMO_PASSWORD", "OSMO_PASS", "CAMERA_PASSWORD",
                 "WIFI_PASSWORD", "WIFI_PASS", "PASSWORD"),
    "pin": ("OSMO_PIN", "PAIRING_PIN", "PIN"),
    "name": ("OSMO_NAME", "CAMERA_NAME"),
    "wifi_interface": ("OSMO_WIFI_INTERFACE", "WIFI_INTERFACE"),
    "imu_port": ("OSMO_IMU_PORT", "IMU_PORT", "CORE2_PORT"),
}

# What `--pair-only` writes back.
CANONICAL = {"ssid": "OSMO_SSID", "password": "OSMO_PASSWORD"}


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, comments and malformed lines.

    Strips one layer of matching quotes and an optional `export ` prefix.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not _is_identifier(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def merge_env(text: str, updates: dict[str, str]) -> str:
    """Return `text` with `updates` applied in place, appending what is new.

    Comments, ordering and unrelated keys survive. This is what lets the tool
    rewrite a credentials file it is not allowed to display.
    """
    remaining = dict(updates)
    lines = text.splitlines()
    out: list[str] = []

    for raw in lines:
        stripped = raw.strip()
        body = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        key = body.partition("=")[0].strip()
        if stripped and not stripped.startswith("#") and key in remaining:
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(raw)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={_quote(value)}")

    return "\n".join(out) + "\n"


def resolve(env: dict[str, str], field: str) -> str | None:
    """First non-empty alias for `field`, or None."""
    for key in ALIASES.get(field, ()):
        value = env.get(key)
        if value:
            return value
    return None


def mask(value: str | None) -> str:
    """A form safe to print. Never reveals more than the length."""
    if not value:
        return "(unset)"
    return "*" * min(len(value), 12)


def find_file(root: Path | str = ".") -> Path | None:
    root = Path(root)
    for name in FILENAMES:
        path = root / name
        if path.is_file():
            return path
    return None


def load(root: Path | str = ".") -> dict[str, str]:
    """File values, then real environment variables on top."""
    env: dict[str, str] = {}
    root = Path(root)
    for name in reversed(FILENAMES):  # .env first, .env.local wins
        path = root / name
        if path.is_file():
            env.update(parse_env(path.read_text(encoding="utf-8")))
    for keys in ALIASES.values():
        for key in keys:
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


def save(updates: dict[str, str], root: Path | str = ".") -> Path:
    """Merge `updates` into the credentials file, creating it if absent.

    Never reads the old content back to the caller -- only the merged text goes
    to disk.
    """
    root = Path(root)
    path = find_file(root) or (root / FILENAMES[0])
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(merge_env(old, updates), encoding="utf-8")
    return path


def _quote(value: str) -> str:
    return f'"{value}"' if (value == "" or any(c in value for c in ' \t"#')) else value


def _is_identifier(key: str) -> bool:
    return key[0].isalpha() or key[0] == "_" if key else False
