"""The CLI must supply everything the session reads.

This file exists because a hand-written test fixture invented an `imu_port`
attribute that the real argparse never defined. 343 tests passed and the
actual server crashed on connect with AttributeError. Fixtures can lie; the
real parser cannot.
"""

import re
import unittest
from pathlib import Path

import server


SOURCE = Path(server.__file__).read_text(encoding="utf-8")


def attributes_the_session_reads() -> set[str]:
    """Every `self.args.X` in server.py. Digits included, unlike a naive
    [a-z_]+ which silently truncates `no_core2` to `no_core`."""
    return set(re.findall(r"self\.args\.([A-Za-z_][A-Za-z0-9_]*)", SOURCE))


class TestParserCoversTheSession(unittest.TestCase):
    def setUp(self):
        self.args = server.build_parser().parse_args([])

    def test_every_attribute_the_session_reads_exists(self):
        missing = sorted(a for a in attributes_the_session_reads()
                         if not hasattr(self.args, a))
        self.assertEqual(missing, [], f"CLI is missing: {missing}")

    def test_it_actually_found_something(self):
        # Guard the guard: a broken regex would make the test above vacuous.
        found = attributes_the_session_reads()
        self.assertGreater(len(found), 8)
        self.assertIn("imu_port", found)
        self.assertIn("no_core2", found)

    def test_defaults_are_safe_with_no_arguments(self):
        self.assertEqual(self.args.host, "192.168.2.1")
        self.assertEqual(self.args.port, 8722)
        self.assertFalse(self.args.lan)
        self.assertIsNone(self.args.imu_port)
        self.assertFalse(self.args.autoconnect)

    def test_session_constructs_and_reports_from_real_args(self):
        # The smoke test the fixture could not give us: build the session from
        # genuinely parsed arguments and touch the paths connect() would.
        s = server.CameraSession(self.args)
        st = s.status()
        self.assertEqual(st["state"], "idle")
        self.assertEqual(st["host"], "192.168.2.1")
        self.assertFalse(st["armed"])

    def test_core2_gate_evaluates(self):
        # The exact expression that raised AttributeError on hardware.
        a = self.args
        self.assertIsInstance(bool(a.imu_port or not a.no_core2), bool)

    def test_lan_flag_parses(self):
        a = server.build_parser().parse_args(["--lan", "--imu-port", "COM3"])
        self.assertTrue(a.lan)
        self.assertEqual(a.imu_port, "COM3")


if __name__ == "__main__":
    unittest.main()
