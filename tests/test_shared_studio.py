"""Real HTTP boundaries and independent browser authors, with no camera IO."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import server
from tests.test_server import make_args
from tests.test_ownership import live_session


class SharedStudioHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = server.CameraSession(make_args(), workspace_root=Path(self.tmp.name))
        class TestHandler(server.Handler):
            token = None
            def log_message(self, *_):
                pass
        TestHandler.session = self.session
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def status(self):
        with urllib.request.urlopen(self.url + "/api/status") as r:
            return json.load(r)

    def post(self, path, body=None, generation=None, origin=None, content_type="application/json", extra_headers=None):
        headers = {"Content-Type": content_type, "Origin": origin or self.url,
                   "X-Osmo-Client": "browser-a"}
        if generation is not None:
            headers["X-Osmo-Generation"] = generation
        headers.update(extra_headers or {})
        req = urllib.request.Request(self.url + path, json.dumps(body or {}).encode(), headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_cross_origin_and_plain_form_cannot_change_slate(self):
        for origin, content_type in (("https://unrelated.example", "text/plain"),
                                     ("null", "application/json"),
                                     (self.url, "text/plain")):
            code, _ = self.post("/api/slate", {"scene": "stolen"},
                                origin=origin, content_type=content_type)
            self.assertIn(code, (403, 415))
            self.assertEqual(self.session.slate["scene"], "1")

    def test_stale_browser_write_is_conflict_without_mutation(self):
        generation = self.status()["workspace"]["generation"]
        code, body = self.post("/api/move", {"name": "A", "waypoints": []}, generation)
        self.assertEqual(code, 200, body)
        self.assertNotEqual(body["workspace"]["generation"], generation)
        code, body = self.post("/api/move", {"name": "stale B", "waypoints": []}, generation)
        self.assertEqual(code, 409, body)
        self.assertEqual(self.session.move.name, "A")
        self.assertEqual(self.session.journal.load()["draft"]["name"], "A")

    def test_browser_write_requires_a_loaded_generation(self):
        code, _ = self.post("/api/move", {"name": "blind", "waypoints": []})
        self.assertEqual(code, 428)
        self.assertEqual(self.session.move.name, "untitled")

    def test_library_delete_advances_generation_and_refuses_stale_followup(self):
        self.session.set_move({"waypoints": [{"pitch": 90, "yaw": 0},
                                             {"pitch": 90, "yaw": 10, "duration": 4}]})
        self.session.save_move("delete-test")
        generation = self.status()["workspace"]["generation"]
        code, body = self.post("/api/moves/delete", {"name": "delete-test"}, generation)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["deleted"])
        self.assertNotEqual(body["workspace"]["generation"], generation)
        self.assertEqual(self.session.list_moves(), [])
        code, _ = self.post("/api/move", {"name": "stale", "waypoints": []}, generation)
        self.assertEqual(code, 409)
        self.assertNotEqual(self.session.move.name, "stale")

    def test_token_cookie_on_lan_host_still_requires_same_origin_json(self):
        self.http.RequestHandlerClass.token = "test-only-token"
        generation = self.session.workspace_info()["generation"]
        headers = {"Host": "192.0.2.1:8722", "Cookie": "osmo_token=test-only-token"}
        for origin, content_type in (("http://unrelated.example", "application/json"),
                                     ("http://192.0.2.1:8722", "text/plain")):
            code, _ = self.post("/api/slate", {"scene": "bad"}, generation,
                                origin, content_type, headers)
            self.assertIn(code, (403, 415))
            self.assertEqual(self.session.slate["scene"], "1")
        code, body = self.post("/api/slate", {"scene": "2"}, generation,
                               origin="http://192.0.2.1:8722", extra_headers=headers)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.session.slate["scene"], "2")

    def test_failed_persistence_reports_memory_only_and_advances_generation(self):
        generation = self.status()["workspace"]["generation"]
        with patch.object(self.session.journal, "save", side_effect=OSError("disk full")):
            code, body = self.post("/api/move", {"name": "in memory", "waypoints": []}, generation)
        self.assertEqual(code, 200, body)
        self.assertFalse(body["workspace"]["durable"])
        self.assertIn("disk full", body["workspace"]["warning"])
        self.assertNotEqual(body["workspace"]["generation"], generation)

    def test_circling_explicit_take_does_not_toggle_the_new_tail(self):
        a = self.session.log_take("first")
        b = self.session.log_take("second")
        generation = self.status()["workspace"]["generation"]
        code, reply = self.post("/api/take/circle", {"id": a["id"]}, generation)
        self.assertEqual(code, 200, reply)
        self.assertTrue(self.session.takes[0]["circled"])
        self.assertFalse(self.session.takes[1]["circled"])

    def test_director_proposal_is_non_actuating_and_does_not_mutate_draft(self):
        self.session.set_move({"name": "reveal", "waypoints": [
            {"name": "start", "pitch": 90, "yaw": 0},
            {"name": "finish", "pitch": 90, "yaw": 15, "duration": 4}]})
        before = self.session.move.to_dict()
        generation = self.status()["workspace"]["generation"]
        code, data = self.post("/api/director/preview", {"target_duration": 8})
        self.assertEqual(code, 200, data)
        self.assertEqual(self.session.move.to_dict(), before)
        self.assertEqual(self.status()["workspace"]["generation"], generation)
        self.assertEqual(self.session.owner, "none")
        self.assertFalse(self.session.armed)
        self.assertIsNone(self.session.link)
        self.assertEqual(data["director"]["proposal"]["move"]["waypoints"][1]["duration"], 8)

    def test_director_rig_metadata_describes_the_assessed_snapshot(self):
        original = server.director.preview
        self.session.speed_preset = "normal"
        before_ramp = self.session.ramp
        def concurrent_change(*args, **kwargs):
            self.session.speed_preset = "fine"
            return original(*args, **kwargs)
        with patch("server.director.preview", side_effect=concurrent_change):
            code, body = self.post("/api/director/preview")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["director"]["rig"]["speed_preset"], "normal")
        self.assertEqual(body["director"]["rig"]["ramp"], before_ramp)


class BrowserMotionLease(unittest.TestCase):
    def setUp(self):
        self.s = live_session()
    def tearDown(self):
        self.s.stop_everything("test complete")

    def test_other_tab_zero_release_and_jog_do_not_touch_holder(self):
        self.s.browser_axes("a", 0.5, 0.0)
        before = self.s.stick.axes
        for name, args in (("browser_axes", ("b", 0, 0)),
                           ("browser_release", ("b",)),
                           ("browser_axes", ("b", 0.1, 0))):
            with self.assertRaises(RuntimeError):
                getattr(self.s, name)(*args)
            self.assertEqual(self.s.stick.axes, before)
            self.assertEqual(self.s.owner, "phone")

    def test_expired_lease_aborts_neutral_without_new_client_packet(self):
        self.s.browser_axes("a", 0.5, 0)
        with patch("server.time.monotonic", return_value=self.s._browser_until + 1):
            self.s._expire_browser()
        self.assertEqual(self.s.owner, "none")
        self.assertEqual(self.s.stick.axes, (0.0, 0.0))

    def test_core2_jog_is_not_relabelled_phone_and_blocks_browser(self):
        self.s._core2_jog(0.4, 0)
        self.assertEqual(self.s.owner, "core2")
        with self.assertRaises(RuntimeError):
            self.s.browser_axes("a", 0.2, 0)
        self.assertEqual(self.s.owner, "core2")

    def test_out_of_order_same_tab_motion_cannot_restart_after_release(self):
        self.s.browser_axes("a", 0.5, 0, sequence=1)
        self.s.browser_axes("a", 0, 0, sequence=3)
        with self.assertRaises(RuntimeError):
            self.s.browser_axes("a", 0.5, 0, sequence=2)
        self.assertEqual(self.s.stick.axes, (0, 0))

    def test_expiry_rejects_delayed_packets_from_the_old_hold(self):
        self.s.browser_axes("a", 0.5, 0, sequence=1, gesture="hold-1")
        with patch("server.time.monotonic", return_value=self.s._browser_until + 1):
            self.s._expire_browser()
        with self.assertRaises(RuntimeError):
            self.s.browser_axes("a", 0.5, 0, sequence=2, gesture="hold-1")
        self.assertEqual(self.s.stick.axes, (0, 0))
        self.s.browser_axes("a", 0.2, 0, sequence=3, gesture="hold-2")
        self.assertEqual(self.s.stick.axes, (0.2, 0))

    def test_stop_is_unconditional_and_old_lease_cannot_stop_new_owner(self):
        self.s.browser_axes("a", 0.5, 0)
        self.s.stop_everything()
        self.assertEqual(self.s.owner, "none")
        self.s._core2_jog(0.2, 0)
        self.s._expire_browser()
        self.assertEqual(self.s.owner, "core2")
        self.assertEqual(self.s.stick.axes, (0.2, 0))

    def test_stop_rejects_continuation_of_held_gesture(self):
        self.s.browser_axes("a", .5, 0, sequence=1, gesture="held")
        self.s.stop_everything()
        with self.assertRaises(RuntimeError):
            self.s.browser_axes("a", .5, 0, sequence=2, gesture="held")
        self.assertEqual(self.s.stick.axes, (0, 0))
        self.s.browser_axes("a", 0, 0, sequence=3, gesture="held")
        self.s.browser_axes("a", .2, 0, sequence=4, gesture="new-touch")
        self.assertEqual(self.s.stick.axes, (.2, 0))

    def test_refused_grab_is_closed_until_a_new_gesture(self):
        with patch.object(self.s, "_start_kinetic"):
            self.s.browser_axes("a", .5, 0, sequence=1, gesture="a-hold")
            with self.assertRaises(RuntimeError):
                self.s.browser_grab("b", sequence=1, gesture="b-hold")
            self.s.browser_axes("a", 0, 0, sequence=2, gesture="a-hold")
            with self.assertRaises(RuntimeError):
                self.s.browser_grab("b", sequence=2, gesture="b-hold")
            self.s.browser_grab("b", sequence=3, gesture="new-hold")
            self.assertEqual(self.s.owner, "phone")

    def test_expired_grab_cannot_reopen_its_gesture(self):
        with patch.object(self.s, "_start_kinetic"):
            self.s.browser_grab("a", sequence=1, gesture="hold")
            with patch("server.time.monotonic", return_value=self.s._browser_until + 1):
                with self.assertRaises(RuntimeError):
                    self.s.browser_grab("a", sequence=2, gesture="hold")
            self.assertEqual(self.s.owner, "none")
