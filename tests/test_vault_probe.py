"""Offline guards for the explicitly opt-in native credential probe."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from driver.hostsettings import HostSettings
from tests.test_hostsettings import FakeVault
from tools.verify_vault import main, probe


class TestVaultProbe(unittest.TestCase):
    def test_without_opt_in_no_vault_or_temporary_directory(self):
        with mock.patch("tools.verify_vault.WinCredVault") as vault, \
             mock.patch("tools.verify_vault.tempfile.TemporaryDirectory") as temporary:
            self.assertEqual(main([]), 2)
        vault.assert_not_called()
        temporary.assert_not_called()

    def test_collision_never_deletes_existing_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, vault = Path(temporary), FakeVault()
            target = HostSettings(root, vault).credential_target
            vault.items[target] = "existing-fixture"
            with self.assertRaisesRegex(RuntimeError, "collision"):
                probe(root, vault)
            self.assertEqual(vault.items, {target: "existing-fixture"})
            self.assertFalse(HostSettings(root, vault).path.exists())

    def test_probe_roundtrip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = FakeVault()
            probe(Path(temporary), vault)
            self.assertEqual(vault.items, {})

    def test_cleanup_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = FakeVault()
            with mock.patch.object(vault, "delete", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    probe(Path(temporary), vault)
