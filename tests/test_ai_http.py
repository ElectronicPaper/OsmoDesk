"""Real loopback HTTP boundaries for optional AI; no provider or camera IO."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server
from driver.assistant import Assistant
from driver.moves import Move, Waypoint
from tests.test_assistant import FakeProvider
from tests.test_server import make_args


def authored_move(distance=5.0, *, reachable=True):
    pitch, yaw = (90.0, -40.0) if reachable else (0.0, 0.0)
    return Move(name="private framing", setup={"notes": "LOCAL ONLY"},
                waypoints=[
                    Waypoint("PRIVATE START", pitch, yaw, duration=10.0,
                             zoom=0.2, cue=True),
                    Waypoint("PRIVATE END", pitch + distance, yaw + distance,
                             duration=10.0, zoom=0.8, flow=True),
                ])


class AIHTTPBase(unittest.TestCase):
    """Fresh server/session per test without inheriting another suite's tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        args = make_args(ai_lan=False)
        self.session = server.CameraSession(args, workspace_root=Path(self.tmp.name))
        self.source = authored_move()
        self.session.set_move(self.source.to_dict())
        self.provider = FakeProvider(self.source)
        self.session.assistant = Assistant(self.provider, cooldown=0)

        class TestHandler(server.Handler):
            token = None

            def log_message(self, *_):
                pass

        TestHandler.session = self.session
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        # Never inherit proxy settings: every request in this suite stays on
        # the explicitly constructed loopback server.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        self.camera_tripwires = []
        for name in ("connect", "disconnect", "browser_axes", "browser_grab",
                     "browser_release", "camera_set", "camera_resolution",
                     "capture_waypoint", "arm", "disarm", "play", "roll",
                     "stop_move", "goto", "action", "_set_recording"):
            patcher = patch.object(self.session, name,
                                   side_effect=AssertionError(f"AI invoked {name}"))
            self.camera_tripwires.append((name, patcher.start(), patcher))

    def tearDown(self):
        self.provider.release.set()
        deadline = time.monotonic() + 3
        while self.session.assistant.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.005)
        for _, _, patcher in reversed(self.camera_tripwires):
            patcher.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def request(self, method, path, body=None, *, headers=None,
                origin=True, content_type="application/json"):
        request_headers = dict(headers or {})
        data = None
        if method == "POST":
            data = (json.dumps(body or {}).encode("utf-8")
                    if content_type == "application/json" else body or b"brief=x")
            request_headers.setdefault("Content-Type", content_type)
            if origin is True:
                request_headers.setdefault("Origin", self.url)
            elif isinstance(origin, str):
                request_headers.setdefault("Origin", origin)
        request = urllib.request.Request(self.url + path, data=data,
                                         headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = raw.decode("utf-8", errors="replace")
            return response.status, payload

    def generation(self):
        code, body = self.request("GET", "/api/status")
        self.assertEqual(code, 200, body)
        return body["workspace"]["generation"]

    def post(self, path, body, *, client="browser-a", generation=None,
             headers=None, origin=True, content_type="application/json"):
        request_headers = {"X-Osmo-Client": client, **(headers or {})}
        if generation is not None:
            request_headers["X-Osmo-Generation"] = generation
        return self.request("POST", path, body, headers=request_headers,
                            origin=origin, content_type=content_type)

    def prepare(self, *, client="browser-a"):
        generation = self.generation()
        code, manifest = self.post("/api/assistant/prepare", {
            "generation": generation,
            "brief": "Let the reveal breathe",
            "include_labels": False,
            "model": "gpt-5.6-luna",
            "effort": "low",
        }, client=client)
        self.assertEqual(code, 200, manifest)
        return generation, manifest

    def complete(self, *, client="browser-a"):
        generation, manifest = self.prepare(client=client)
        code, sent = self.post("/api/assistant/send", {
            "confirmation": manifest["confirmation"],
            "generation": generation,
            "consent": True,
        }, client=client)
        self.assertEqual(code, 202, sent)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, job = self.request(
                "GET", f"/api/assistant/job?id={sent['job_id']}",
                headers={"X-Osmo-Client": client})
            self.assertEqual(code, 200, job)
            if job["state"] not in ("running", "cancelling"):
                self.assertEqual(job["state"], "completed", job)
                return generation, sent["job_id"], job
            time.sleep(0.005)
        self.fail("AI job did not complete")

    def assert_no_camera_calls(self):
        for name, mocked, _ in self.camera_tripwires:
            self.assertEqual(mocked.call_count, 0, name)
        self.assertEqual(self.session.owner, "none")
        self.assertFalse(self.session.armed)
        self.assertIsNone(self.session.link)


class TestAIHTTP(AIHTTPBase):
    def test_generation_check_and_nonblocking_admission_are_atomic_with_edits(self):
        generation, manifest = self.prepare()
        admission_entered, allow_admission, edit_finished = (threading.Event() for _ in range(3))
        replies = []
        real_send = self.session.assistant.send

        def paused_admission(*args):
            admission_entered.set()
            if not allow_admission.wait(2):
                raise AssertionError("test admission not released")
            return real_send(*args)

        def edit():
            self.session.set_move({**self.source.to_dict(), "name": "newer draft"})
            edit_finished.set()

        with patch.object(self.session.assistant, "send", side_effect=paused_admission):
            sending = threading.Thread(target=lambda: replies.append(self.post("/api/assistant/send", {
                "confirmation": manifest["confirmation"], "generation": generation, "consent": True})))
            sending.start()
            editing = None
            try:
                self.assertTrue(admission_entered.wait(1))
                editing = threading.Thread(target=edit)
                editing.start()
                self.assertFalse(edit_finished.wait(.1), "a draft edit slipped between revision check and admission")
            finally:
                allow_admission.set()
                sending.join(3)
                if editing is not None:
                    editing.join(3)
        self.assertEqual(replies[0][0], 202, replies)
        self.assertTrue(edit_finished.is_set())
        self.assert_no_camera_calls()

    def test_prepare_is_local_and_send_requires_explicit_consent_and_generation(self):
        generation, manifest = self.prepare()
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(manifest["generation"], generation)
        for body in (
                {"confirmation": manifest["confirmation"], "generation": generation},
                {"confirmation": manifest["confirmation"], "generation": "wrong",
                 "consent": True}):
            code, reply = self.post("/api/assistant/send", body)
            self.assertEqual(code, 400, reply)
            self.assertEqual(self.provider.calls, 0)
        code, sent = self.post("/api/assistant/send", {
            "confirmation": manifest["confirmation"], "generation": generation,
            "consent": True})
        self.assertEqual(code, 202, sent)
        deadline = time.monotonic() + 1
        while not self.provider.calls and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.provider.calls, 1)
        self.assert_no_camera_calls()

    def test_cross_origin_form_authentication_and_lan_opt_in_are_enforced(self):
        generation = self.generation()
        body = {"generation": generation, "brief": "Reveal"}
        code, _ = self.post("/api/assistant/prepare", body,
                            origin="https://unrelated.example")
        self.assertEqual(code, 403)
        code, _ = self.post("/api/assistant/prepare", b"brief=reveal",
                            content_type="application/x-www-form-urlencoded")
        self.assertEqual(code, 415)
        self.http.RequestHandlerClass.token = "test-only-token"
        code, _ = self.request("GET", "/api/assistant/status")
        self.assertEqual(code, 401)
        code, _ = self.post("/api/assistant/prepare", body)
        self.assertEqual(code, 401)

        remote_host = f"192.0.2.10:{self.http.server_port}"
        protected = {"Host": remote_host, "X-Osmo-Token": "test-only-token"}
        code, reply = self.request("GET", "/api/assistant/status", headers=protected)
        self.assertEqual(code, 400, reply)
        self.assertIn("host-only", reply["error"])
        self.session.args.ai_lan = True
        code, reply = self.request("GET", "/api/assistant/status", headers=protected)
        self.assertEqual(code, 200, reply)
        self.assertTrue(reply["available"])
        self.assertEqual(self.provider.calls, 0)
        self.assert_no_camera_calls()

    def test_completed_treatment_applies_only_through_move_cas(self):
        original = deepcopy(self.session.move.to_dict())
        generation, job_id, _ = self.complete()
        selection = {"_assistant": {"job_id": job_id, "index": 0}}
        code, reply = self.post("/api/move", selection)
        self.assertEqual(code, 428, reply)
        self.assertEqual(self.session.move.to_dict(), original)
        code, reply = self.post("/api/move", selection, generation=generation)
        self.assertEqual(code, 200, reply)
        applied = self.session.move.to_dict()
        self.assertNotEqual(reply["workspace"]["generation"], generation)
        self.assertEqual(applied["waypoints"][1]["duration"], 12.0)
        self.assertEqual(applied["setup"], original["setup"])
        self.assertEqual({key: applied[key] for key in ("name", "loop", "ping_pong", "route_arcs")},
                         {key: original[key] for key in ("name", "loop", "ping_pong", "route_arcs")})
        for before, after in zip(original["waypoints"], applied["waypoints"]):
            for field in ("pitch", "yaw", "zoom", "zoom_easing", "flow", "cue"):
                self.assertEqual(after[field], before[field])
        self.assertEqual(self.provider.calls, 1)
        self.assert_no_camera_calls()

    def test_selection_rejects_other_browser_malformed_shape_and_stale_generation(self):
        generation, job_id, _ = self.complete()
        selection = {"_assistant": {"job_id": job_id, "index": 0}}
        before = deepcopy(self.session.move.to_dict())
        code, _ = self.post("/api/move", selection, client="browser-b",
                            generation=generation)
        self.assertEqual(code, 400)
        for malformed in (
                {"_assistant": {"job_id": job_id}},
                {"_assistant": {"job_id": job_id, "index": True}},
                {"_assistant": {"job_id": job_id, "index": 0}, "name": "bypass"}):
            code, _ = self.post("/api/move", malformed, generation=generation)
            self.assertEqual(code, 400)
        self.assertEqual(self.session.move.to_dict(), before)

        changed = authored_move()
        changed.name = "concurrent edit"
        self.session.set_move(changed.to_dict())
        current = self.generation()
        code, _ = self.post("/api/move", selection, generation=generation)
        self.assertEqual(code, 409)
        code, reply = self.post("/api/move", selection, generation=current)
        self.assertEqual(code, 400, reply)
        self.assertIn("stale", reply["error"])
        self.assertEqual(self.session.move.name, "concurrent edit")
        self.assert_no_camera_calls()

    def test_changed_rig_cap_is_rechecked_without_a_generation_change(self):
        self.source = authored_move(distance=50.0)
        self.session.set_move(self.source.to_dict())
        self.provider = FakeProvider(self.source)
        self.session.assistant = Assistant(self.provider, cooldown=0)
        before = deepcopy(self.session.move.to_dict())
        generation, job_id, job = self.complete()
        self.assertTrue(job["result"]["treatments"][0]["assessment"]["preflight"]["ok"])
        self.session.speed_preset = "fine"
        self.assertEqual(self.generation(), generation)
        code, reply = self.post("/api/move", {
            "_assistant": {"job_id": job_id, "index": 0}}, generation=generation)
        self.assertEqual(code, 400, reply)
        self.assertIn("current rig", reply["error"])
        self.assertEqual(self.session.move.to_dict(), before)
        self.assert_no_camera_calls()

    def test_failed_preflight_treatment_cannot_apply(self):
        self.source = authored_move(distance=1.0, reachable=False)
        self.session.set_move(self.source.to_dict())
        self.provider = FakeProvider(self.source)
        self.session.assistant = Assistant(self.provider, cooldown=0)
        before = deepcopy(self.session.move.to_dict())
        generation, job_id, job = self.complete()
        self.assertFalse(job["result"]["treatments"][0]["assessment"]["preflight"]["ok"])
        code, reply = self.post("/api/move", {
            "_assistant": {"job_id": job_id, "index": 0}}, generation=generation)
        self.assertEqual(code, 400, reply)
        self.assertIn("preflight", reply["error"])
        self.assertEqual(self.session.move.to_dict(), before)
        self.assert_no_camera_calls()

    def test_configured_key_precedence_and_status_never_return_keys(self):
        captured = []

        class CapturingProvider:
            available = True

            def __init__(self, key):
                captured.append(key)

        cases = (
            ([], {}, {"OPENAI_API_KEY": "config-test-key"}, "config-test-key"),
            (["--ai-env", "synthetic.env"], {}, {"OPENAI_API_KEY": "config-test-key"},
             "file-test-key"),
            (["--ai-env", "synthetic.env"], {"OPENAI_API_KEY": "process-test-key"},
             {"OPENAI_API_KEY": "config-test-key"}, "process-test-key"),
        )
        with patch("server.OpenAIProvider", CapturingProvider), \
                patch.object(Path, "read_text", return_value="OPENAI_API_KEY=file-test-key"):
            for extra, process_env, loaded_env, expected in cases:
                args = server.build_parser().parse_args(["--enable-ai", *extra])
                with patch.dict(os.environ, process_env, clear=True):
                    configured = server.configured_assistant(args, loaded_env)
                self.assertEqual(captured[-1], expected)
                encoded = json.dumps(configured.status())
                for key in ("config-test-key", "file-test-key", "process-test-key"):
                    self.assertNotIn(key, encoded)

        self.session.assistant = configured
        code, status = self.request("GET", "/api/assistant/status")
        self.assertEqual(code, 200, status)
        encoded = json.dumps(status)
        self.assertNotIn("process-test-key", encoded)
        self.assertNotIn("file-test-key", encoded)
        self.assertNotIn("config-test-key", encoded)


class TestAIStartup(unittest.TestCase):
    def test_tokenless_lan_ai_is_rejected_before_key_configuration(self):
        args = server.build_parser().parse_args(
            ["--enable-ai", "--lan", "--no-token"])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "tokenless LAN"):
                server.configured_assistant(args, {})


if __name__ == "__main__":
    unittest.main()
