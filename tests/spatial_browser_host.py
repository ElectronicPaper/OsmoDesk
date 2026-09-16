"""Ephemeral browser fixture; all camera entry points fail closed."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import server
from driver import moves
from tests.test_server import make_args


def main():
    with tempfile.TemporaryDirectory(prefix="osmodesk-spatial-test-") as directory:
        session = server.CameraSession(make_args(ai_lan=False), workspace_root=Path(directory))
        shot = moves.Move(name="Recorded fixture", waypoints=[
            moves.Waypoint("Start", 120, 10, dwell=.3),
            moves.Waypoint("Hero", 121, 12, duration=1, dwell=1),
        ])
        session.takes.append({"scene": "Fixture", "shot": "1", "take": 1,
                              "path": shot.to_dict(), "motion": {"trace": [
                                  [round(i / 10, 2), *shot.sample(i / 10)] for i in range(24)
                              ]}})

        class Handler(server.Handler):
            token = None

            def log_message(self, *_):
                pass

        Handler.session = session
        denied = ("connect", "disconnect", "browser_axes", "browser_grab", "browser_release",
                  "camera_set", "camera_resolution", "arm", "disarm", "play", "roll",
                  "stop_move", "goto", "action", "_set_recording")
        guards = [patch.object(session, name, side_effect=AssertionError("Forbidden camera call: " + name)) for name in denied]
        for guard in guards:
            guard.start()
        try:
            with server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
                print(json.dumps({"url": f"http://127.0.0.1:{http.server_port}"}), flush=True)
                http.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for guard in guards:
                guard.stop()


if __name__ == "__main__":
    main()
