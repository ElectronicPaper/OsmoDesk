"""Shot Studio vertical contract: real session + real persistence, no camera."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from driver import moves
from driver.liveview import LiveView
from tests.test_server import make_args
from tests import test_server as fixtures


class TestStudioWorkspace(unittest.TestCase):
    def test_restart_restores_authoring_not_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            s = server.CameraSession(make_args(), workspace_root=root)
            s.set_move({"name": "reveal", "waypoints": [
                {"name": "P1", "pitch": 90, "yaw": 0},
                {"name": "P2", "pitch": 100, "yaw": 10}]})
            s.set_slate("12", "C", 4)
            s.log_take("blocking note")
            s.armed, s.owner, s.recording = True, "program", True
            s.set_setup({"mount": "tripod"})
            restored = server.CameraSession(make_args(), workspace_root=root)
            self.assertEqual(restored.move.name, "reveal")
            self.assertEqual(restored.slate, {"scene": "12", "shot": "C", "take": 5})
            self.assertEqual(restored.takes[0]["note"], "blocking note")
            self.assertEqual(restored.state, "idle")
            self.assertFalse(restored.armed)
            self.assertFalse(restored.recording)
            self.assertEqual(restored.owner, "none")
            self.assertGreater(restored.status()["workspace"]["revision"], 0)
            self.assertEqual(len(restored.move_path()["points"]), 241)
            self.assertTrue(restored.preflight()["ok"])

    def test_disk_failure_is_visible_and_does_not_claim_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            s = server.CameraSession(make_args(), workspace_root=Path(folder))
            with patch.object(s.journal, "save", side_effect=OSError("disk full")), \
                    self.assertLogs("panel", level="WARNING"):
                s.set_slate("2", None, None)
            self.assertIn("not saved", s.status()["workspace"]["warning"])
            self.assertEqual(s.status()["workspace"]["revision"], 0)

    def test_parser_rejects_invalid_http_path_without_replacing_draft(self):
        h = fixtures.TestHandlerInputBoundaries()
        h.setUp()
        try:
            s = server.CameraSession(make_args())
            s.move.name = "keep me"
            for bad in (float("nan"), float("inf")):
                code, result = h._post("/api/move", {"waypoints": [
                    {"pitch": bad, "yaw": 0}]}, s)
                self.assertEqual(code, 400)
                self.assertIn("finite", result["error"])
                self.assertEqual(s.move.name, "keep me")
        finally:
            h.tearDown()

    def test_compare_route_uses_saved_lens_not_current_draft(self):
        h = fixtures.TestHandlerInputBoundaries()
        h.setUp()
        try:
            s = fixtures.TestComparingTakes()._with_takes(0, 0.04)
            for take in s.takes:
                take["setup_fields"] = {"fov_deg": 20}
            s.move.setup["fov_deg"] = 100
            code, result = h._post("/api/takes/compare", {"first": 0, "second": 1}, s)
            self.assertEqual(code, 200)
            actual = result["comparison"]
            self.assertFalse(actual["fov_assumed"])
            self.assertEqual(actual["fov_source"], "shot setup")
            self.assertEqual(actual, s.compare_takes(0, 1))
            self.assertNotEqual(actual["peak_px"], s.compare_takes(0, 1, fov_deg=84)["peak_px"])
        finally:
            h.tearDown()

    def test_aborted_and_truncated_takes_cannot_claim_a_whole_shot_match(self):
        s = fixtures.TestComparingTakes()._with_takes(0, 0)
        for take in s.takes:
            take["duration"] = 30.0
        c = s.compare_takes(0, 1)
        self.assertEqual(c["verdict"], "partial comparison")
        self.assertFalse(c["compositable"])
        s.takes[0]["duration"] = s.takes[1]["duration"] = None
        s.takes[0]["motion"]["aborted"] = True
        self.assertEqual(s.compare_takes(0, 1)["verdict"], "partial comparison")

    def test_missing_decoder_has_actionable_status(self):
        import builtins
        original = builtins.__import__
        def no_av(name, *args, **kwargs):
            if name == "av":
                raise ImportError("test unavailable")
            return original(name, *args, **kwargs)
        view = LiveView()
        with patch("builtins.__import__", side_effect=no_av), self.assertLogs(level="ERROR"):
            view._decode_loop()
        self.assertFalse(view.running)
        self.assertIn("host PyAV", view.stats()["error"])

    def test_long_lens_cap_is_in_preflight(self):
        s = server.CameraSession(make_args())
        s.speed_preset, s.ramp = "fast", "long lens"
        self.assertEqual(s.preflight()["max_dps"], 12)

    def test_nonfinite_setup_cannot_start_or_replace_a_shot(self):
        from unittest.mock import Mock
        s = server.CameraSession(make_args())
        s.runner = fixtures.fake_runner(start=Mock())
        for key in moves.Move.SETUP_FIELDS:
            with self.subTest(key=key), self.assertRaises(ValueError):
                s.set_move({"setup": {key: float("nan")}})
            self.assertEqual(s.move.setup, {})
        s.move.setup = {"fov_deg": float("inf")}
        with self.assertRaises(ValueError):
            s._start_shot_run()
        s.runner.start.assert_not_called()

    def test_nonfinite_retime_preserves_current_draft(self):
        s = server.CameraSession(make_args())
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 90, 0), moves.Waypoint("B", 100, 10)])
        before = s.move.to_dict()
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                s.retime_move(factor=value)
            self.assertEqual(s.move.to_dict(), before)

    def test_record_request_metadata_is_bound_to_the_run(self):
        s = fixtures.TestTakeLog()
        s.setUp()
        session = s._run_shot()
        session.recording = True
        session._start_shot_run()
        session.recording = False
        self.assertTrue(session.log_take()["recording"]["requested"])

    def test_reconnect_during_cancel_is_explicit_not_silently_dropped(self):
        s = server.CameraSession(make_args())
        s._connect_active = True
        s.disconnect()
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            s.connect()
