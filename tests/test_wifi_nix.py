"""Joining the camera AP on Linux, so the rig can run on the Pi.

Every one of these tests exists because the failure it guards is silent. An
nmcli profile with the wrong flag connects and reports success, and what you
notice later is that the machine has no route to anything, or that the camera
never came up at all.
"""

import subprocess
import unittest
from unittest import mock

from driver import wifi, wifi_nix


def completed(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=["nmcli"], returncode=rc,
                                       stdout=stdout, stderr=stderr)


class Recorder:
    """Stands in for nmcli and records what it was told."""

    def __init__(self, *responses):
        self.calls: list[list[str]] = []
        self.responses = list(responses)

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if self.responses:
            return self.responses.pop(0)
        return completed()

    def call_with(self, *needles) -> list[str] | None:
        for c in self.calls:
            if all(n in c for n in needles):
                return c
        return None


class TestJoin(unittest.TestCase):
    def _join(self, **kw):
        rec = Recorder(
            completed("OsmoPocket4-TEST-CAMERA\nHomeWifi\n"),   # wait_for_ssid rescan
            completed(),                                   # delete
            completed(),                                   # add
            completed(),                                   # up
        )
        with mock.patch.object(wifi_nix.shutil, "which", return_value="/usr/bin/nmcli"), \
             mock.patch.object(wifi_nix.subprocess, "run", rec):
            wifi_nix.join("OsmoPocket4-TEST-CAMERA", "pw", **kw)
        return rec

    def test_it_creates_the_profile_with_the_ssid_and_key(self):
        rec = self._join()
        add = rec.call_with("connection", "add")
        self.assertIsNotNone(add, rec.calls)
        self.assertIn("OsmoPocket4-TEST-CAMERA", add)
        self.assertIn("pw", add)
        self.assertIn("wpa-psk", add)

    def test_the_camera_ap_is_never_the_default_route(self):
        """The AP has no internet. Left as the default route it blackholes
        everything else on the machine -- including the SSH session running
        the rig."""
        add = self._join().call_with("connection", "add")
        i = add.index("ipv4.never-default")
        self.assertEqual(add[i + 1], "yes")
        j = add.index("ipv6.never-default")
        self.assertEqual(add[j + 1], "yes")

    def test_it_does_not_autoconnect_on_boot(self):
        """Otherwise it grabs the radio at every reboot, whether or not
        anyone is shooting."""
        add = self._join().call_with("connection", "add")
        i = add.index("connection.autoconnect")
        self.assertEqual(add[i + 1], "no")

    def test_it_deletes_the_old_profile_before_adding(self):
        """The SSID changes with the camera. A stale profile naming the
        previous one connects to nothing and reports success."""
        rec = self._join()
        order = [i for i, c in enumerate(rec.calls)
                 if "connection" in c and ("delete" in c or "add" in c)]
        delete_at = next(i for i, c in enumerate(rec.calls) if "delete" in c)
        add_at = next(i for i, c in enumerate(rec.calls) if "add" in c)
        self.assertLess(delete_at, add_at, rec.calls)

    def test_it_brings_the_profile_up(self):
        self.assertIsNotNone(self._join().call_with("connection", "up"))

    def test_an_interface_is_passed_through(self):
        add = self._join(interface="wlan0").call_with("connection", "add")
        self.assertIn("ifname", add)
        self.assertIn("wlan0", add)

    def test_it_waits_for_the_ap_before_trying_to_connect(self):
        """The AP is woken over BLE and takes a moment. Connecting before it
        beacons only burns the timeout."""
        rec = self._join()
        self.assertIsNotNone(rec.call_with("device", "wifi", "list"))
        scan_at = next(i for i, c in enumerate(rec.calls) if "list" in c)
        add_at = next(i for i, c in enumerate(rec.calls) if "add" in c)
        self.assertLess(scan_at, add_at)

    def test_an_ap_that_never_appears_raises_a_timeout(self):
        rec = Recorder(completed("SomeoneElsesWifi\n"))
        with mock.patch.object(wifi_nix.shutil, "which", return_value="/usr/bin/nmcli"), \
             mock.patch.object(wifi_nix.subprocess, "run", rec), \
             mock.patch.object(wifi_nix.time, "sleep"), \
             mock.patch.object(wifi_nix.time, "monotonic", side_effect=[0, 1, 99]):
            with self.assertRaises(TimeoutError):
                wifi_nix.join("OsmoPocket4-TEST-CAMERA", "pw", timeout=1.0)

    def test_a_failed_add_is_reported_not_swallowed(self):
        rec = Recorder(
            completed("OsmoPocket4-TEST-CAMERA\n"),
            completed(),
            completed(rc=1, stderr="no such device"),
        )
        with mock.patch.object(wifi_nix.shutil, "which", return_value="/usr/bin/nmcli"), \
             mock.patch.object(wifi_nix.subprocess, "run", rec):
            with self.assertRaises(RuntimeError) as caught:
                wifi_nix.join("OsmoPocket4-TEST-CAMERA", "pw")
        self.assertIn("no such device", str(caught.exception))


class TestReading(unittest.TestCase):
    def _with(self, stdout):
        rec = Recorder(completed(stdout))
        return mock.patch.object(wifi_nix.subprocess, "run", rec), rec

    def test_current_ssid_reads_the_active_row(self):
        patch, _ = self._with("no:HomeWifi:wlan0\nyes:OsmoPocket4-TEST-CAMERA:wlan0\n")
        with mock.patch.object(wifi_nix.shutil, "which", return_value="x"), patch:
            self.assertEqual(wifi_nix.current_ssid(), "OsmoPocket4-TEST-CAMERA")

    def test_nothing_active_is_none_not_empty_string(self):
        patch, _ = self._with("no:HomeWifi:wlan0\n")
        with mock.patch.object(wifi_nix.shutil, "which", return_value="x"), patch:
            self.assertIsNone(wifi_nix.current_ssid())

    def test_it_can_be_asked_about_one_interface(self):
        patch, _ = self._with("yes:HomeWifi:wlan1\nyes:Osmo:wlan0\n")
        with mock.patch.object(wifi_nix.shutil, "which", return_value="x"), patch:
            self.assertEqual(wifi_nix.current_ssid("wlan0"), "Osmo")

    def test_list_interfaces_returns_only_wifi_devices(self):
        patch, _ = self._with("eth0:ethernet\nwlan0:wifi\nlo:loopback\n")
        with mock.patch.object(wifi_nix.shutil, "which", return_value="x"), patch:
            self.assertEqual(wifi_nix.list_interfaces(), ["wlan0"])


class TestWithoutNmcli(unittest.TestCase):
    def test_available_says_so(self):
        with mock.patch.object(wifi_nix.shutil, "which", return_value=None):
            self.assertFalse(wifi_nix.available())

    def test_join_explains_rather_than_crashing_obscurely(self):
        with mock.patch.object(wifi_nix.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                wifi_nix.join("x", "y")
        self.assertIn("nmcli", str(caught.exception))

    def test_forget_is_a_no_op_rather_than_an_error(self):
        """Tearing down is best effort -- it must not be the thing that fails
        a shutdown."""
        with mock.patch.object(wifi_nix.shutil, "which", return_value=None):
            wifi_nix.forget("anything")


class TestTheDispatcher(unittest.TestCase):
    """The rest of the code must never ask what it is running on."""

    def test_it_picks_a_backend(self):
        self.assertIn(wifi.NAME, ("wifi_win", "wifi_nix"))

    def test_both_backends_offer_the_same_names(self):
        """A backend missing a function is a crash on the other platform,
        found only once the rig is on set."""
        from driver import wifi_win
        for name in ("join", "list_interfaces", "current_ssid", "is_connected",
                     "wait_for_ssid", "reconnect", "forget"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(wifi_win, name)))
                self.assertTrue(callable(getattr(wifi_nix, name)))

    def test_the_dispatcher_exposes_every_one_of_them(self):
        for name in ("join", "list_interfaces", "current_ssid", "is_connected",
                     "wait_for_ssid", "reconnect", "forget", "available"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(wifi, name)))


if __name__ == "__main__":
    unittest.main()


class TestNobodyReachesPastTheDispatcher(unittest.TestCase):
    """`driver.wifi` exists so the rest of the code never asks what platform
    it is on. An import of a backend by name is a module that works on one
    machine and raises on the other -- found only once the rig is on set.

    run.py did exactly this and would have crashed on the Pi. It imported
    wifi_win, which does not exist there in any usable form, and nothing
    caught it because the Windows suite was perfectly happy.
    """

    ALLOWED = {"driver/wifi.py"}          # the dispatcher itself, obviously
    # The canvas-adoption brief requires an untouched source backup in this
    # directory.  It is evidence, not executable project source, so scanning
    # it would report historical imports as current violations.
    EXCLUDED_PREFIXES = (".venv/", "tests/", "Claude-Design/_pre-canvas-backup/")

    def _sources(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(self.EXCLUDED_PREFIXES) or "__pycache__" in rel:
                continue
            if rel in self.ALLOWED or rel.startswith("driver/wifi_"):
                continue
            yield rel, path.read_text(encoding="utf-8", errors="replace")

    def test_no_module_imports_a_backend_directly(self):
        import re
        offenders = []
        for rel, text in self._sources():
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"\bimport\b.*\bwifi_(win|nix)\b", stripped):
                    offenders.append(f"{rel}: {stripped}")
        self.assertEqual(offenders, [],
                         "import driver.wifi instead:\n  " + "\n  ".join(offenders))

    def test_no_module_calls_a_backend_directly(self):
        offenders = []
        for rel, text in self._sources():
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "wifi_win." in stripped or "wifi_nix." in stripped:
                    offenders.append(f"{rel}: {stripped}")
        self.assertEqual(offenders, [],
                         "call through driver.wifi instead:\n  " + "\n  ".join(offenders))

    def test_the_guard_can_actually_see_the_tree(self):
        """A rglob that matched nothing would make both tests above pass for
        the wrong reason."""
        seen = [rel for rel, _ in self._sources()]
        self.assertIn("server.py", seen)
        self.assertIn("run.py", seen)
        self.assertGreater(len(seen), 15)
