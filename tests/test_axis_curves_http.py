"""Loopback-only contracts for authored axis-curve previews and persistence."""

from copy import deepcopy
import unittest

from driver import moves
from tests.test_host_features_http import HostFeaturesHTTPBase


CURVE = [.02, .00, .20, 1.00]


def authored():
    return moves.Move(name="curve fixture", waypoints=[
        moves.Waypoint("Start", 100.0, 10.0),
        moves.Waypoint("End", 120.0, 40.0, duration=4.0, easing="linear",
                       pitch_curve=CURVE, yaw_curve=[.20, .00, .98, .20],
                       axis_link="pan_leads"),
    ])


class AxisCurvesHTTPTests(HostFeaturesHTTPBase):
    def setUp(self):
        super().setUp()
        self.session.set_move(authored().to_dict())

    def test_curve_preview_is_offline_and_does_not_mutate_the_draft(self):
        before = deepcopy(self.session.move.to_dict())
        code, body, _ = self.post("/api/move/curves/preview",
                                  {"move": authored().to_dict(), "leg": 1})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["curves"]["leg"], 1)
        self.assertTrue(body["curves"]["samples"])
        self.assertEqual(self.session.move.to_dict(), before)
        self.assert_no_hardware()

    def test_editor_can_preview_but_viewer_cannot(self):
        viewer, editor = self.issue("viewer"), self.issue("editor")
        payload = {"move": authored().to_dict(), "leg": 1}
        code, body, _ = self.post("/api/move/curves/preview", payload,
                                  credential=viewer["token"], client="viewer")
        self.assertEqual(code, 403, body)
        code, body, _ = self.post("/api/move/curves/preview", payload,
                                  credential=editor["token"], client="editor")
        self.assertEqual(code, 200, body)
        self.assert_no_hardware()

    def test_invalid_link_nonfinite_flow_and_leg_fail_without_mutation(self):
        before = deepcopy(self.session.move.to_dict())
        cases = []
        bad_link = authored().to_dict()
        bad_link["waypoints"][1]["axis_link"] = "both_lead"
        cases.append(({"move": bad_link, "leg": 1}, "link"))
        nonfinite = authored().to_dict()
        nonfinite["waypoints"][1]["pitch_curve"][0] = float("nan")
        cases.append(({"move": nonfinite, "leg": 1}, "nonfinite"))
        flowing = authored().to_dict()
        flowing["waypoints"][1]["flow"] = True
        cases.append(({"move": flowing, "leg": 1}, "flow"))
        cases.append(({"move": authored().to_dict(), "leg": 2}, "leg"))
        for payload, name in cases:
            with self.subTest(name=name):
                code, body, _ = self.post("/api/move/curves/preview", payload)
                self.assertEqual(code, 400, body)
                self.assertEqual(self.session.move.to_dict(), before)
        self.assert_no_hardware()

    def test_move_endpoint_round_trips_curve_fields_and_preview_consumers_keep_them(self):
        source = authored().to_dict()
        generation = self.generation()
        code, body, _ = self.post("/api/move", source, generation=generation)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.session.move.to_dict()["waypoints"][1]["pitch_curve"], CURVE)
        self.assertEqual(self.session.move.to_dict()["waypoints"][1]["axis_link"], "pan_leads")

        code, body, _ = self.post("/api/director/preview", {"move": source})
        self.assertEqual(code, 200, body)
        samples = body["director"]["samples"]
        midpoint = min(samples, key=lambda sample: abs(sample["time"] - 2.0))
        expected = authored().sample(midpoint["time"])
        self.assertAlmostEqual(midpoint["pitch"], expected[0], places=5)
        self.assertAlmostEqual(midpoint["yaw"], expected[1], places=5)

        code, body, _ = self.post("/api/spatial/preview", {"move": source})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["move"]["waypoints"][1]["yaw_curve"], [.20, .00, .98, .20])
        self.assertEqual(body["move"]["waypoints"][1]["axis_link"], "pan_leads")
        self.assert_no_hardware()


if __name__ == "__main__":
    unittest.main()
