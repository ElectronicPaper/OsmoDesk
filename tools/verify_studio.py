"""Real-browser Shot Studio check against an ephemeral, hardware-blocked host.

Requires an existing Node + Playwright installation (NODE_PATH may locate it).
Nothing is installed; all authoring files live in TemporaryDirectory.
"""
import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from tests.test_server import make_args
from driver.ai_provider import ProviderError
from driver.assistant import Assistant


class OfflineCopilotProvider:
    """Synthetic responses only, never reads env or connects to a provider."""
    available = True
    calls = 0

    def generate(self, context, schema, *, model, effort, max_output_tokens):
        self.calls += 1
        time.sleep(0.7)
        if context["brief"].startswith("TEST FAILURE"):
            raise ProviderError("Synthetic provider unavailable. No retry was made.")
        treatments = []
        for index, (title, duration, hold) in enumerate([
                ("The quiet reveal", 8.0, 1.2), ("A decisive arrival", 6.0, 0.6)]):
            beats = [{key: beat[key] for key in ("index", "name", "duration", "dwell", "easing")}
                     for beat in context["beats"]]
            beats[0]["dwell"] = hold
            for beat in beats[1:]:
                beat["duration"] = duration
            treatments.append({"title": title, "intent": "Build anticipation, let the reveal land, then leave an editorial tail.",
                               "cautions": ["Synthetic browser fixture, not a provider judgment."], "beats": beats})
        return {"document": {"treatments": treatments}, "model": model,
                "usage": {"input_tokens": 123, "output_tokens": 456, "total_tokens": 579}}


class OfflineHandler(server.Handler):
    writes = []
    blocked = []
    allowed = {"/api/move", "/api/moves/save", "/api/moves/load",
               "/api/move/retime", "/api/slate", "/api/take", "/api/setup",
               "/api/director/preview"}

    def do_POST(self):
        self.writes.append(self.route)
        if self.route not in self.allowed:
            self.blocked.append(self.route)
            self._json({"ok": False, "error": "Hardware actions blocked in browser test"}, 403)
            return
        super().do_POST()

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--ai", action="store_true", help="exercise optional AI with a synthetic provider; never loads a key")
    parser.add_argument('--workspace', action='store_true', help='exercise layouts, local AI settings, crew and editorial handoff; no provider or hardware')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="osmodesk-studio-") as folder:
        OfflineHandler.session = server.CameraSession(make_args(), workspace_root=Path(folder))
        if args.ai:
            OfflineHandler.allowed = OfflineHandler.allowed | {"/api/assistant/prepare", "/api/assistant/send", "/api/assistant/cancel"}
            OfflineHandler.session.assistant = Assistant(OfflineCopilotProvider(), cooldown=0)
        if args.workspace:
            OfflineHandler.token = 'browser-fixture-only'
            OfflineHandler.allowed = OfflineHandler.allowed | {'/api/settings/ai', '/api/crew', '/api/crew/revoke', '/api/workspace/recovery', '/api/editorial/export'}
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), OfflineHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            command = [args.node, str(ROOT / "tools" / ('verify_workspace.cjs' if args.workspace else "verify_ai.cjs" if args.ai else "verify_studio.cjs")),
                       f"http://127.0.0.1:{http.server_port}"]
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                command.append(str(args.screenshots.resolve()))
            subprocess.run(command, check=True, timeout=180)
            assert not OfflineHandler.blocked, OfflineHandler.blocked
            restored = server.CameraSession(make_args(), workspace_root=Path(folder))
            if not args.workspace:
                assert restored.move.name == "Window reveal"
                assert len(restored.move.waypoints) == 3
                assert float(restored.move.setup["fov_deg"]) == 65
            assert restored.state == "idle" and not restored.armed and restored.owner == "none"
            print("PASS: real HTTP draft persistence/restart; no hardware endpoint reached")
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
