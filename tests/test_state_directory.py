"""Persistent state is independent of release source paths; safety is unchanged."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server
from driver import timelapse
from tests.test_server import make_args


class TestStateDirectory(unittest.TestCase):
    def test_explicit_state_survives_release_change_for_all_stores(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "state"
            first = server.CameraSession(make_args(), workspace_root=root)
            first.set_move({"name": "retained", "waypoints": [
                {"name": "P1", "pitch": 90, "yaw": 0},
                {"name": "P2", "pitch": 100, "yaw": 10, "duration": 20}]})
            first.set_slate("12", "B", 3)
            first.save_move("saved-shot")
            first.host_settings.update({"model": "gpt-5.6-terra"})
            plan = timelapse.plan_for(first.move, frames=20)
            timelapse.save_progress(root, first.move, plan, 4, time.time())
            # A new release has a different default, but every store must use
            # the explicitly selected existing state directory.
            with mock.patch.object(server, "MOVES_DIR", base / "new-release" / "moves"):
                second = server.CameraSession(make_args(), workspace_root=root)
            self.assertEqual(second.move.name, first.move.name)
            self.assertEqual(second.slate["scene"], "12")
            self.assertEqual(second.library.root, root)
            self.assertEqual(second.journal.root, root)
            self.assertEqual(second.host_settings.root, root)
            self.assertEqual(second.host_settings.load()["model"], "gpt-5.6-terra")
            self.assertTrue(second.timelapse_resume_info()["available"])
            self.assertEqual(second.timelapse_resume_info()["frame"], 4)
            self.assertFalse(second.armed)
            self.assertEqual(second.state, "idle")
            self.assertFalse((base / "new-release").exists())

    def test_cli_exposes_explicit_state_without_changing_default(self):
        self.assertEqual(server.build_parser().parse_args([]).state_dir, server.MOVES_DIR)
        with tempfile.TemporaryDirectory() as folder:
            args = server.build_parser().parse_args(["--state-dir", folder, "--no-core2"])
            self.assertEqual(server.state_directory(args.state_dir), Path(folder))

    def test_unsafe_roots_refused_without_resolving_links(self):
        with self.assertRaises(ValueError):
            server.state_directory(Path("relative"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(ValueError, "symbolic"):
                    server.CameraSession(make_args(), workspace_root=root)
            with self.assertRaises(ValueError):
                server.state_directory(root / ".." / "elsewhere")

    def test_cli_invalid_state_fails_before_loading_secrets_or_hardware(self):
        with mock.patch("sys.argv", ["server.py", "--state-dir", "relative"]), \
             mock.patch.object(server.config, "load") as load, \
             mock.patch.object(server, "CameraSession") as session:
            self.assertEqual(server.main(), 2)
        load.assert_not_called()
        session.assert_not_called()
