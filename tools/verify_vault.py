"""Explicit Windows-only probe using disposable synthetic credentials, never API keys."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from driver.hostsettings import HostSettings, WinCredVault


def check(condition: bool, phase: str) -> None:
    if not condition:
        raise RuntimeError(phase)


def probe(root: Path, vault) -> None:
    settings = HostSettings(root, vault)
    target = settings.credential_target
    # Collision refusal is OUTSIDE cleanup: never delete someone else's entry.
    check(vault.read(target) is None, "credential target collision")
    keys = ["synthetic-vault-probe-" + secrets.token_hex(24) for _ in range(2)]

    def private(instance):
        public = json.dumps(instance.public())
        disk = instance.path.read_text(encoding="utf-8")
        check(all(key not in public and key not in disk for key in keys), "secret exclusion")
        check("provider_key" not in instance.public(), "public schema")

    try:
        settings.load()
        settings.update({"provider_key": keys[0], "enabled": True})
        check(vault.read(target) is None, "memory-only storage")
        fresh = HostSettings(root, vault)
        check(not fresh.load()["enabled"], "memory-only restart")
        private(settings)
        print("PASS: memory-only default and safe restart")

        settings.update({"remember_host": True})
        fresh = HostSettings(root, vault)
        check(fresh.load()["enabled"] and fresh.provider_key() == keys[0], "remembered reload")
        private(fresh)
        print("PASS: native vault save and fresh settings reload")

        fresh.update({"provider_key": keys[1]})
        rotated = HostSettings(root, vault)
        check(rotated.load()["enabled"] and rotated.provider_key() == keys[1], "rotation reload")
        private(rotated)
        print("PASS: key rotation and secret exclusion")

        rotated.update({"remove_key": True})
        check(vault.read(target) is None, "key removal")
        empty = HostSettings(root, vault)
        check(not empty.load()["enabled"] and empty.provider_key() is None, "removed restart")
        print("PASS: removal disables AI across restart")

        empty.update({"provider_key": keys[1], "enabled": True, "remember_host": True})
        vault.delete(target)
        missing = HostSettings(root, vault, initial_key=keys[0])
        check(not missing.load()["enabled"] and missing.provider_key() is None, "missing no fallback")
        private(missing)
        print("PASS: missing credential disables AI without fallback")
    finally:
        vault.delete(target)
        check(vault.read(target) is None, "cleanup verification")
        print("PASS: owned synthetic credential removed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-test-credential", action="store_true",
                        help="Allow creating and deleting one disposable synthetic vault entry")
    args = parser.parse_args(argv)
    if not args.confirm_test_credential:
        print("REFUSED: --confirm-test-credential is required; no state changed")
        return 2
    if os.name != "nt":
        print("UNSUPPORTED: Windows Credential Manager is required")
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="osmodesk-vault-probe-") as temporary:
            probe(Path(temporary), WinCredVault())
    except Exception as exc:
        # Native and filesystem exceptions can contain private paths/values.
        print("FAIL: vault verification or cleanup failed (" + type(exc).__name__ + ")")
        return 1
    print("PASS: native vault verification complete; no provider request made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
