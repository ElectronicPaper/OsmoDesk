"""Loopback HTTP boundaries for the offline-only spatial endpoints."""
from copy import deepcopy
from types import SimpleNamespace
import math
import time
import unittest

from driver import moves
from tests.test_host_features_http import HostFeaturesHTTPBase


def authored(*, pitch=120.0, yaw=20.0):
    return moves.Move(name="spatial fixture", setup={"notes": "local"},
                      waypoints=[moves.Waypoint("Start", pitch, yaw, dwell=.2,
                                                 zoom=.2, cue=True),
                                 moves.Waypoint("End", pitch + 1, yaw + 2,
                                                 duration=1.0, zoom=.8,
                                                 easing="linear")])


class SpatialHTTPTests(HostFeaturesHTTPBase):
    def setUp(self):
        super().setUp()
        self.session.set_move(authored().to_dict())

    def test_preview_with_real_pitch_120_is_offline_and_does_not_mutate_draft(self):
        before = deepcopy(self.session.move.to_dict())
        code, body, _ = self.post("/api/spatial/preview", {
            "options": {"hfov": 1, "aspect": 16 / 9,
                        "markers": [{"name": "subject", "pitch": 120,
                                     "yaw": 20, "kind": "subject"}]},
        })
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["move"], before)
        self.assertEqual(self.session.move.to_dict(), before)
        self.assertIn("display_samples", body["spatial"])
        self.assert_no_hardware()

    def test_recipe_preview_returns_only_a_proposal_and_preserves_angles(self):
        before = deepcopy(self.session.move.to_dict())
        roles = [{"name": "Opening", "duration": .2, "dwell": .1, "easing": "linear"},
                 {"name": "Close", "duration": 2, "dwell": .3, "easing": "ease-in"}]
        code, body, _ = self.post("/api/spatial/preview", {"recipe": {"roles": roles}})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.session.move.to_dict(), before)
        for old, proposed in zip(before["waypoints"], body["move"]["waypoints"]):
            self.assertEqual(old["pitch"], proposed["pitch"])
            self.assertEqual(old["yaw"], proposed["yaw"])
            self.assertEqual(old["zoom"], proposed["zoom"])
            self.assertEqual(old["cue"], proposed["cue"])
        self.assert_no_hardware()

    def test_invalid_options_and_required_oversize_fail_bounded_without_mutation(self):
        before = deepcopy(self.session.move.to_dict())
        code, body, _ = self.post("/api/spatial/preview", {"options": {"hfov": .99}})
        self.assertEqual(code, 400, body)
        self.assertEqual(self.session.move.to_dict(), before)

        low, high = moves.YAW_LIMITS.at_offset(0), moves.YAW_LIMITS.at_offset(260)
        points = [moves.Waypoint("0", 120, low)]
        for index in range(1, 200):
            points.append(moves.Waypoint(str(index), 120, high if index % 2 else low,
                                         duration=.05, easing="linear"))
        oversized = moves.Move(waypoints=points).to_dict()
        code, body, _ = self.post("/api/spatial/preview", {"move": oversized})
        self.assertEqual(code, 400, body)
        self.assertIn("exceed 10000", body["error"])
        self.assertEqual(self.session.move.to_dict(), before)
        self.assert_no_hardware()

    def test_viewer_is_denied_editor_is_allowed(self):
        viewer, editor = self.issue("viewer"), self.issue("editor")
        code, body, _ = self.post("/api/spatial/preview", {}, credential=viewer["token"], client="viewer")
        self.assertEqual(code, 403, body)
        code, body, _ = self.post("/api/spatial/preview", {}, credential=editor["token"], client="editor")
        self.assertEqual(code, 200, body)
        self.assert_no_hardware()

    def test_settle_permissions_and_missing_snapshot_fail_closed(self):
        viewer, editor = self.issue("viewer"), self.issue("editor")
        self.session.takes.append({"path": authored().to_dict(),
                                   "motion": {"trace": [[0, 120, 20], [.3, 120, 20]]}})
        code, body, _ = self.post("/api/spatial/settle", {"take": 0},
                                  credential=viewer["token"], client="viewer")
        self.assertEqual(code, 403, body)
        code, body, _ = self.post("/api/spatial/settle", {"take": 0},
                                  credential=editor["token"], client="editor")
        self.assertEqual(code, 200, body)
        self.assertIn("observations", body)
        self.assert_no_hardware()

    def test_settle_refuses_looping_and_ping_pong_take_timing(self):
        editor = self.issue("editor")
        for field in ("loop", "ping_pong"):
            shot = authored().to_dict()
            shot[field] = True
            self.session.takes = [{"path": shot,
                                   "motion": {"trace": [[0, 120, 20], [.3, 120, 20]]}}]
            code, body, _ = self.post("/api/spatial/settle", {"take": 0},
                                      credential=editor["token"], client="editor")
            self.assertEqual(code, 400, (field, body))
            self.assertIn("single forward", body["error"])
        self.assert_no_hardware()

    def test_snapshot_requires_existing_fresh_sources_and_rejects_bad_frame(self):
        code, body, _ = self.request("GET", "/api/spatial/snapshot", headers=self.headers())
        self.assertEqual(code, 400, body)
        self.assertIn("Nothing was connected or started", body["error"])

        old_live, old_link = self.session.live, self.session.link
        try:
            self.session.link = SimpleNamespace(attitude=SimpleNamespace(pitch=120, yaw=20),
                                                last_attitude_at=time.monotonic() - 1)
            self.session.live = SimpleNamespace(running=True, last_frame_at=time.time(),
                                                snapshot=lambda: b"")
            code, body, _ = self.request("GET", "/api/spatial/snapshot", headers=self.headers())
            self.assertEqual(code, 400, body)
            self.assertIn("fresh", body["error"])
            self.session.link.last_attitude_at = time.monotonic()
            code, body, _ = self.request("GET", "/api/spatial/snapshot", headers=self.headers())
            self.assertEqual(code, 400, body)
            self.assertIn("bounded preview frame", body["error"])
        finally:
            self.session.live, self.session.link = old_live, old_link
        self.assert_no_hardware()

    def test_snapshot_returns_fresh_bounded_image_without_actuation(self):
        old_live, old_link = self.session.live, self.session.link
        try:
            self.session.link = SimpleNamespace(attitude=SimpleNamespace(pitch=120, yaw=20),
                                                last_attitude_at=time.monotonic())
            self.session.live = SimpleNamespace(running=True, last_frame_at=time.time(),
                                                snapshot=lambda: b"\xff\xd8fixture\xff\xd9")
            code, body, _ = self.request("GET", "/api/spatial/snapshot", headers=self.headers())
            self.assertEqual(code, 200, body)
            self.assertTrue(body["image"].startswith("data:image/jpeg;base64,"))
            self.assertEqual((body["pitch"], body["yaw"]), (120, 20))
        finally:
            self.session.live, self.session.link = old_live, old_link
        self.assert_no_hardware()

    def test_snapshot_rejects_future_and_nonfinite_freshness_or_attitude(self):
        old_live, old_link = self.session.live, self.session.link
        try:
            self.session.live = SimpleNamespace(running=True, last_frame_at=time.time(),
                                                snapshot=lambda: b"fixture")
            cases = (
                (SimpleNamespace(pitch=120, yaw=20), time.monotonic() + 1, time.time()),
                (SimpleNamespace(pitch=120, yaw=20), time.monotonic(), time.time() + 1),
                (SimpleNamespace(pitch=math.nan, yaw=20), time.monotonic(), time.time()),
                (SimpleNamespace(pitch=120, yaw=math.inf), time.monotonic(), time.time()),
            )
            for attitude, attitude_at, frame_at in cases:
                self.session.link = SimpleNamespace(attitude=attitude,
                                                    last_attitude_at=attitude_at)
                self.session.live.last_frame_at = frame_at
                code, body, _ = self.request("GET", "/api/spatial/snapshot", headers=self.headers())
                self.assertEqual(code, 400, body)
                self.assertIn("required", body["error"].lower())
        finally:
            self.session.live, self.session.link = old_live, old_link
        self.assert_no_hardware()


if __name__ == "__main__":
    unittest.main()
