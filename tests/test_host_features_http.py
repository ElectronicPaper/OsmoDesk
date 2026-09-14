"""Loopback-only HTTP security regressions for host settings, crew and recovery."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server
from tests.test_server import make_args


class HostFeaturesHTTPBase(unittest.TestCase):
    """Fresh, token-protected host; no request may reach camera/provider IO."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = server.CameraSession(make_args(ai_lan=False), workspace_root=Path(self.tmp.name))

        class TestHandler(server.Handler):
            token = "master-fixture"

            def log_message(self, *_):
                pass

        TestHandler.session = self.session
        self.handler = TestHandler
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.host = f"localhost:{self.http.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.trips = []
        for name in ("connect", "disconnect", "browser_axes", "browser_grab", "browser_release",
                     "camera_set", "camera_resolution", "arm", "disarm", "play", "roll",
                     "stop_move", "goto", "action", "_set_recording"):
            mocked = patch.object(self.session, name, side_effect=AssertionError(f"HTTP invoked {name}")).start()
            self.trips.append((name, mocked))

    def tearDown(self):
        for _, mocked in reversed(self.trips):
            mocked.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def request(self, method, path, body=None, *, headers=None):
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        supplied = dict(headers or {})
        if method == "POST":
            supplied.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.url + path, data=data, headers=supplied, method=method)
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
            return response.status, payload, dict(response.headers.items())

    def headers(self, credential="master-fixture", *, client=None, remote=False):
        host = (f"192.0.2.10:{self.http.server_port}" if remote else self.host)
        result = {"Host": host, "X-Osmo-Token": credential}
        if client:
            result["X-Osmo-Client"] = client
        return result

    def generation(self):
        code, body, _ = self.request("GET", "/api/status", headers=self.headers())
        self.assertEqual(code, 200, body)
        return body["workspace"]["generation"]

    def post(self, path, body, *, credential="master-fixture", client=None, generation=None, remote=False):
        headers = self.headers(credential, client=client, remote=remote)
        if generation is not None:
            headers["X-Osmo-Generation"] = generation
        return self.request("POST", path, body, headers=headers)

    def issue(self, role, *, allow_ai=False):
        return self.session.crew.issue(label=f"{role} fixture", role=role, hours=1, allow_ai=allow_ai)

    def assert_no_hardware(self):
        for name, mocked in self.trips:
            self.assertEqual(mocked.call_count, 0, name)
        self.assertIsNone(self.session.link)
        self.assertEqual(self.session.owner, "none")
        self.assertFalse(self.session.armed)


class TestHostFeaturesHTTP(HostFeaturesHTTPBase):
    def test_access_identity_uses_explicit_credential_and_never_falls_back_to_crew_cookie(self):
        crew = self.issue("viewer")
        code, body, _ = self.request("GET", "/api/access", headers=self.headers())
        self.assertEqual(code, 200, body)
        self.assertEqual(body["identity"]["role"], "owner")

        # An invalid explicit header wins over a valid older cookie; it cannot
        # inherit the crew's authority or loopback ownership.
        bad = self.headers("not-a-credential")
        bad["Cookie"] = f"osmo_token={crew['token']}"
        code, _, _ = self.request("GET", "/api/access", headers=bad)
        self.assertEqual(code, 401)
        code, body, _ = self.request("GET", "/api/access", headers=self.headers(crew["token"]))
        self.assertEqual(code, 200, body)
        self.assertEqual(body["identity"]["id"], crew["id"])
        self.assertEqual(body["identity"]["role"], "viewer")
        self.assert_no_hardware()

    def test_crew_cookie_is_the_presented_crew_token_not_master_and_is_httponly(self):
        crew = self.issue("editor")
        code, _, headers = self.request("GET", f"/?t={crew['token']}", headers={"Host": self.host})
        self.assertEqual(code, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"osmo_token={crew['token']}", cookie)
        self.assertNotIn("master-fixture", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assert_no_hardware()

    def test_viewer_and_editor_cannot_mutate_camera_but_editor_can_author_with_cas(self):
        viewer, editor = self.issue("viewer"), self.issue("editor")
        for credential in (viewer["token"], editor["token"]):
            code, body, _ = self.post("/api/connect", {}, credential=credential, client="crew-browser")
            self.assertEqual(code, 403, body)

        original = self.session.move.to_dict()
        authored = {**original, "name": "editor-authored"}
        generation = self.generation()
        code, body, _ = self.post("/api/move", authored, credential=editor["token"],
                                  client="crew-browser", generation=generation)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.session.move.name, "editor-authored")
        code, body, _ = self.post("/api/move", original, credential=editor["token"],
                                  client="crew-browser", generation=generation)
        self.assertEqual(code, 409, body)
        self.assert_no_hardware()

    def test_settings_are_local_owner_only_and_never_return_or_persist_memory_key(self):
        code, body, _ = self.request("GET", "/api/settings/ai", headers=self.headers())
        self.assertEqual(code, 200, body)
        public = json.dumps(body)
        self.assertNotIn("provider_key", public)
        self.assertNotIn("master-fixture", public)
        self.assertFalse((Path(self.tmp.name) / "host-settings.json").exists())

        # A master credential from a non-local Host is only an operator.
        code, body, _ = self.request("GET", "/api/settings/ai", headers=self.headers(remote=True))
        self.assertEqual(code, 403, body)
        self.assert_no_hardware()

    def test_key_removal_disables_ai_preserves_accounting_and_invalidates_confirmations(self):
        self.session.assistant._requests_used = 3
        self.session.assistant._reserved = 0.125
        self.session.assistant._confirmations["stale"] = {"expires_at": time.monotonic() + 30}
        with patch.object(server, "OpenAIProvider", return_value=object()) as provider:
            code, body, _ = self.post("/api/settings/ai", {
                "provider_key": "memory-only-fixture", "enabled": True,
                "remember_host": False,
            })
            self.assertEqual(code, 200, body)
            provider.assert_called_once_with("memory-only-fixture")
        self.assertFalse("memory-only-fixture" in (Path(self.tmp.name) / "host-settings.json").read_text(encoding="utf-8"))
        code, body, _ = self.post("/api/settings/ai", {"remove_key": True})
        self.assertEqual(code, 200, body)
        self.assertFalse(body["settings"]["enabled"])
        self.assertFalse(body["settings"]["key_configured"])
        self.assertEqual(body["usage"]["requests_used"], 3)
        self.assertEqual(body["usage"]["reserved_usd"], 0.125)
        self.assertEqual(body["usage"]["pending_confirmations"], 0)
        self.assertNotIn("memory-only-fixture", json.dumps(body))
        self.assert_no_hardware()

    def test_revoked_crew_credential_is_immediately_unauthorised(self):
        crew = self.issue("editor")
        code, body, _ = self.post("/api/crew/revoke", {"id": crew["id"]})
        self.assertEqual(code, 200, body)
        code, _, _ = self.request("GET", "/api/access", headers=self.headers(crew["token"]))
        self.assertEqual(code, 401)
        self.assert_no_hardware()

    def test_recovery_requires_info_confirmation_and_current_fingerprint_without_hardware(self):
        journal = self.session.journal
        journal.save(draft=self.session.move.to_dict(), slate=self.session.slate, takes=[])
        journal.save(draft=self.session.move.to_dict(), slate=self.session.slate, takes=[])
        journal.path.write_text("not json", encoding="utf-8")
        code, body, _ = self.request("GET", "/api/workspace/recovery", headers=self.headers())
        self.assertEqual(code, 200, body)
        info = body["recovery"]
        self.assertTrue(info["needed"])
        self.assertTrue(info["can_restore_backup"])
        code, body, _ = self.post("/api/workspace/recovery", {"action": "restore_backup", "fingerprint": info["fingerprint"]})
        self.assertEqual(code, 400, body)
        code, body, _ = self.post("/api/workspace/recovery", {"confirm": True, "action": "restore_backup", "fingerprint": "stale"})
        self.assertEqual(code, 400, body)
        code, body, _ = self.post("/api/workspace/recovery", {"confirm": True, "action": "restore_backup", "fingerprint": info["fingerprint"]})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assert_no_hardware()
