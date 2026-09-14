"""Host-local AI settings never serialize or disclose provider secrets."""

import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from driver.hostsettings import HostSettings, WinCredVault


class FakeVault:
    available = True
    def __init__(self):
        self.items = {}
        self.reads = 0
    def read(self, target):
        self.reads += 1
        return self.items.get(target)
    def write(self, target, secret):
        self.items[target] = secret
    def delete(self, target):
        self.items.pop(target, None)


class TestHostSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = FakeVault()

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_injected_key_stay_memory_only_until_requested(self):
        settings = HostSettings(self.root, self.vault, initial_key="env-key")
        self.assertEqual(settings.public()["model"], "gpt-5.6-luna")
        self.assertEqual(settings.public()["request_limit"], 20)
        self.assertEqual(self.vault.reads, 0)
        settings.update({"enabled": True})
        self.assertNotIn("env-key", settings.path.read_text(encoding="utf-8"))
        self.assertEqual(self.vault.reads, 0)

    def test_preferences_restart_but_secret_never_enters_json_or_public(self):
        settings = HostSettings(self.root, self.vault, initial_key="top-secret")
        settings.update({"enabled": True, "model": "gpt-5.6-sol", "effort": "high",
                         "allow_lan": True, "budget_usd": 2.5, "request_limit": 20,
                         "remember_host": True})
        self.assertEqual(self.vault.items[settings.credential_target], "top-secret")
        self.assertNotIn("top-secret", settings.path.read_text(encoding="utf-8"))
        fresh = HostSettings(self.root, self.vault)
        self.assertEqual(self.vault.reads, 0)
        public = fresh.load()
        self.assertTrue(public["enabled"])
        self.assertNotIn("provider_key", public)
        self.assertEqual(fresh.provider_key(), "top-secret")

    def test_missing_remembered_key_disables_ai_without_using_injected_fallback(self):
        settings = HostSettings(self.root, self.vault, initial_key="first")
        settings.update({"enabled": True, "remember_host": True})
        self.vault.delete(settings.credential_target)
        fresh = HostSettings(self.root, self.vault, initial_key="env-fallback")
        self.assertFalse(fresh.load()["enabled"])
        self.assertIsNone(fresh.provider_key())

    def test_memory_only_enabled_key_restart_is_safely_disabled(self):
        settings = HostSettings(self.root, self.vault, initial_key='session-only')
        settings.update({'enabled': True})
        fresh = HostSettings(self.root, self.vault)
        self.assertFalse(fresh.load()['enabled'])
        self.assertFalse(fresh.public()['key_configured'])
        self.assertIsNone(fresh.provider_key())

    def test_remember_requires_available_vault_and_key_without_mutating(self):
        settings = HostSettings(self.root)
        before = settings.public()
        with self.assertRaises(ValueError):
            settings.update({"remember_host": True})
        self.assertEqual(settings.public(), before)
        settings = HostSettings(self.root, self.vault)
        before = settings.public()
        with self.assertRaises(ValueError):
            settings.update({"remember_host": True})
        self.assertEqual(settings.public(), before)

    def test_remove_key_deletes_remembered_secret_and_disables_ai(self):
        settings = HostSettings(self.root, self.vault, initial_key="key")
        settings.update({"enabled": True, "remember_host": True})
        settings.update({"remove_key": True})
        self.assertFalse(settings.public()["enabled"])
        self.assertFalse(settings.public()["remember_host"])
        self.assertIsNone(settings.provider_key())
        self.assertNotIn(settings.credential_target, self.vault.items)

    def test_invalid_preferences_and_write_failure_roll_back_memory_and_secret(self):
        settings = HostSettings(self.root, self.vault, initial_key="old")
        settings.update({"enabled": True, "remember_host": True})
        before = settings.public()
        with self.assertRaises(ValueError):
            settings.update({"model": "unknown"})
        self.assertEqual(settings.public(), before)
        with self.assertRaises(ValueError):
            settings.update({"request_limit": 21})
        with self.assertRaises(ValueError):
            settings.update({"provider_key": " space"})
        with self.assertRaises(ValueError):
            settings.update({"provider_key": "non-ascii-€"})
        with mock.patch.object(settings, "_write_preferences", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                settings.update({"provider_key": "new", "remember_host": True})
        self.assertEqual(settings.provider_key(), "old")
        self.assertEqual(self.vault.items[settings.credential_target], "old")

    def test_unknown_fields_and_unsafe_file_are_rejected(self):
        settings = HostSettings(self.root, self.vault, initial_key="key")
        with self.assertRaises(ValueError):
            settings.update({"provider_key": "key", "extra": True})
        settings.path.write_text(json.dumps({"version": 1, "prefs": {}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            settings.load()
        settings.path.write_text(json.dumps({"version": True, "prefs": {}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            settings.load()

    def test_load_refuses_a_symlink_root_before_reading_preferences(self):
        settings = HostSettings(self.root, self.vault)
        with mock.patch("driver.hostsettings.Path.is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                settings.load()

    def test_windows_vault_binds_last_error_aware_native_signatures_lazily(self):
        vault = WinCredVault()
        native = mock.Mock()
        with mock.patch("driver.hostsettings.os.name", "nt"), \
             mock.patch.object(ctypes, "WinDLL", return_value=native, create=True) as dll:
            self.assertIsNone(vault._native)
            self.assertIs(vault._api(), native)
        dll.assert_called_once_with("advapi32", use_last_error=True)
        self.assertIsNotNone(native.CredReadW.argtypes)
        self.assertIsNotNone(native.CredWriteW.argtypes)


if __name__ == "__main__":
    unittest.main()
