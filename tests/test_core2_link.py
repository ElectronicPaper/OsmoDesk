"""Core2 uplink parsing.

The firmware appends diagnostic key=value fields to its messages, so these
pin the rule that the driver reads the leading token and ignores the rest.
A stricter match silently inverted the clutch: every grab parsed as a release.
"""

import unittest

from driver.core2 import Core2Link


class Link(Core2Link):
    """Core2Link without touching a serial port."""

    def __init__(self):
        self.latest = __import__("driver.core2", fromlist=["ImuSample"]).ImuSample()
        self.status = __import__("driver.core2", fromlist=["Core2Status"]).Core2Status()
        self.engages, self.actions = [], []
        self.on_engage = self.engages.append
        self.on_action = self.actions.append
        self._serial = None
        self._last_state_sent = ""


class TestClutchParsing(unittest.TestCase):
    def setUp(self):
        self.link = Link()

    def test_bare_grab_and_release(self):
        self.link._handle("E 1")
        self.link._handle("E 0")
        self.assertEqual(self.link.engages, [True, False])

    def test_grab_with_diagnostic_fields(self):
        # The exact line firmware 1.3 emits.
        self.link._handle("E 1 btnB=0 scr=1 since=0")
        self.assertEqual(self.link.engages, [True])
        self.assertTrue(self.link.status.engaged)

    def test_release_with_reason(self):
        self.link._handle("E 1 btnB=0 scr=1 since=0")
        self.link._handle("E 0 reason=linklost")
        self.assertEqual(self.link.engages, [True, False])
        self.assertFalse(self.link.status.engaged)

    def test_release_with_debounce_fields(self):
        self.link._handle("E 0 btnB=0 scr=0 since=140")
        self.assertEqual(self.link.engages, [False])

    def test_malformed_engage_does_not_raise(self):
        for line in ("E", "E ", "E x", "E  "):
            with self.subTest(line=line):
                self.link._handle(line)
        self.assertNotIn(True, self.link.engages)


class TestOtherUplink(unittest.TestCase):
    def setUp(self):
        self.link = Link()

    def test_imu(self):
        self.link._handle("I 1.50 -2.25 0.75")
        self.assertAlmostEqual(self.link.latest.pitch, 1.5)
        self.assertAlmostEqual(self.link.latest.roll, -2.25)
        self.assertAlmostEqual(self.link.latest.yaw_rate, 0.75)
        self.assertEqual(self.link.status.samples, 1)

    def test_short_imu_ignored(self):
        self.link._handle("I 1.0 2.0")
        self.assertEqual(self.link.status.samples, 0)

    def test_known_action(self):
        self.link._handle("B abort")
        self.assertEqual(self.link.actions, ["abort"])

    def test_unknown_action_ignored(self):
        self.link._handle("B selfdestruct")
        self.assertEqual(self.link.actions, [])

    def test_hello_records_firmware_and_features(self):
        self.link._handle("H 1.3-linkfix imu,touch,leds,haptic,speaker,lvgl")
        self.assertEqual(self.link.status.firmware, "1.3-linkfix")
        self.assertIn("lvgl", self.link.status.features)

    def test_hello_forces_state_resend(self):
        # Firmware restarted: whatever we last sent is no longer on its screen.
        self.link._last_state_sent = "S armed=1"
        self.link._handle("H 1.3 imu")
        self.assertEqual(self.link._last_state_sent, "")

    def test_diagnostic_lines_are_harmless(self):
        self.link._handle("D board=2 touchEnabled=1 touchPoints=0 disp=320x240")
        self.assertEqual(self.link.actions, [])
        self.assertEqual(self.link.engages, [])


if __name__ == "__main__":
    unittest.main()
