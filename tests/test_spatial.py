"""Focused adversarial tests for pure spatial rehearsal helpers."""
import copy
import math
import unittest
import shutil
import subprocess
from pathlib import Path
from driver import moves, spatial


def move(yaw=20, duration=1.0):
    return moves.Move(waypoints=[moves.Waypoint("A", 0, 0, dwell=.2),
                                 moves.Waypoint("B", 0, yaw, duration=duration, easing="linear")])


def marker(name="front", pitch=0, yaw=0, **extra):
    return {"name": name, "pitch": pitch, "yaw": yaw, "kind": "subject", **extra}


class AnalyzeTests(unittest.TestCase):
    def test_has_preview_and_never_mutates_input(self):
        shot = move(); before = copy.deepcopy(shot.to_dict())
        out = spatial.analyze(shot, {"markers": [marker(not_before=.1)]})
        self.assertIn("preview", out); self.assertIn("display_samples", out)
        self.assertEqual(shot.to_dict(), before)

    def test_rejects_bool_and_nonfinite_nested_values(self):
        for options in ({"hfov": True}, {"aspect": math.inf}, {"markers": [marker(yaw=float("nan"))]}, {"origin": {"pitch": False, "yaw": 0}}):
            with self.subTest(options=options), self.assertRaises(ValueError): spatial.analyze(move(), options)

    def test_narrow_delivery_crop_boundary_is_supported(self):
        self.assertIn("projection", spatial.analyze(move(), {"hfov": 1}) )
        with self.assertRaises(ValueError): spatial.analyze(move(), {"hfov": .99})

    def test_rejects_excess_markers_and_unknown_nested_fields(self):
        with self.assertRaises(ValueError): spatial.analyze(move(), {"markers": [marker(str(i)) for i in range(25)]})
        with self.assertRaises(ValueError): spatial.analyze(move(), {"markers": [{**marker(), "bad": 1}]})

    def test_display_long_arc_is_not_shortened(self):
        low, high = moves.YAW_LIMITS.at_offset(0), moves.YAW_LIMITS.at_offset(260)
        shot = moves.Move(waypoints=[moves.Waypoint("A", 0, low), moves.Waypoint("B", 0, high, duration=.05, easing="linear")])
        samples = spatial.analyze(shot, {})["display_samples"]
        self.assertGreater(samples[-1]["yaw"] - samples[0]["yaw"], 250)
        self.assertLessEqual(max(abs(b["yaw"] - a["yaw"]) for a, b in zip(samples, samples[1:])), 5.01)

    def test_long_timeline_keeps_beats_and_declares_reduced_density(self):
        shot = move(duration=3600); result = spatial.analyze(shot, {})
        times = {row["time"] for row in result["display_samples"]}
        self.assertTrue(result["display_resolution_limited"]); self.assertLessEqual(len(times), 10_000)
        self.assertIn(round(shot.arrival_times()[-1], 4), times)

    def test_visibility_blinks_split_at_hidden_sample(self):
        shot = moves.Move(waypoints=[moves.Waypoint("A", 0, 0), moves.Waypoint("B", 0, 100, duration=.2, easing="linear"), moves.Waypoint("C", 0, 0, duration=.2, easing="linear")])
        intervals = spatial.analyze(shot, {"markers": [marker()]})["projection"]["markers"][0]["intervals"]
        self.assertGreaterEqual(len(intervals), 2)

    def test_behind_camera_is_not_visible(self):
        check = spatial.analyze(move(0), {"markers": [marker(yaw=180, hold=.1)]})["projection"]["markers"][0]
        self.assertIsNone(check["first_visible"]); self.assertTrue(check["violates_reveal"])

    def test_margin_is_applied_to_each_edge(self):
        check = spatial.analyze(move(0), {"hfov": 90, "aspect": 1, "margin": .1, "markers": [marker(pitch=40, hold=.1)]})["projection"]["markers"][0]
        self.assertIsNone(check["first_visible"])

    def test_avoid_marker_visibility_is_violation(self):
        check = spatial.analyze(move(0), {"markers": [{**marker(), "kind": "avoid"}]})["projection"]["markers"][0]
        self.assertTrue(check["violates_reveal"])

    def test_avoid_at_delivery_edge_is_not_hidden_by_subject_safety_margin(self):
        checks = spatial.analyze(move(0), {"hfov": 60, "margin": .2, "markers": [
            marker('Subject', yaw=29), {**marker('Unwanted object', yaw=29), 'kind': 'avoid'}
        ]})['projection']['markers']
        self.assertIsNone(checks[0]['first_visible'])
        self.assertEqual(checks[1]['first_visible'], 0)
        self.assertTrue(checks[1]['violates_reveal'])

    def test_readability_reports_horizontal_and_vertical_rates(self):
        shot = moves.Move(waypoints=[moves.Waypoint("A", 0, 0), moves.Waypoint("B", 20, 0, duration=.2, easing="linear")])
        value = spatial.analyze(shot, {})["projection"]["readability"]
        self.assertEqual(value["horizontal_screen_widths_per_second"], 0.0)
        self.assertGreater(value["vertical_frame_heights_per_second"], 0)


class RecipeTests(unittest.TestCase):
    def test_recipe_preserves_all_non_role_fields(self):
        shot = move(); shot.waypoints[1].zoom = .5; shot.waypoints[1].cue = True
        before = copy.deepcopy(shot.to_dict())
        result = spatial.recipe(shot, {"roles": [{"name": "Open", "duration": .1, "dwell": 0, "easing": "linear"}, {"name": "Close", "duration": 2, "dwell": .5, "easing": "ease-in"}]})
        self.assertEqual(result["waypoints"][1]["zoom"], .5); self.assertTrue(result["waypoints"][1]["cue"]); self.assertEqual(shot.to_dict(), before)

    def test_recipe_rejects_count_bool_and_unknown_fields(self):
        with self.assertRaises(ValueError): spatial.recipe(move(), {"roles": []})
        with self.assertRaises(ValueError): spatial.recipe(move(), {"roles": [{"name": "A", "duration": True, "dwell": 0, "easing": "linear"}] * 2})
        with self.assertRaises(ValueError): spatial.recipe(move(), {"roles": [{"name": "A", "duration": 1, "dwell": 0, "easing": "linear", "x": 1}] * 2})


class SettleTests(unittest.TestCase):
    def test_gap_prevents_late_stable_reading(self):
        out = spatial.settle([[0, 0, 0], [.1, 0, 0], [2, 0, 0], [2.3, 0, 0]], [0])[0]
        self.assertIsNone(out["settled_at"]); self.assertTrue(out["gap_observed"])

    def test_insufficient_and_empty_traces_are_unknown(self):
        self.assertIsNone(spatial.settle([], [0])[0]["settled_at"])
        self.assertIsNone(spatial.settle([[0, 0, 0]], [0])[0]["settled_at"])

    def test_next_arrival_bounds_observation_window(self):
        self.assertIsNone(spatial.settle([[0, 0, 0], [.3, 0, 0]], [0, .2])[0]["settled_at"])

    def test_pitch_and_yaw_wrap_are_observed_continuously(self):
        self.assertEqual(spatial.settle([[0, 179.95, 179.95], [.3, -179.95, -179.95]], [0])[0]["settled_at"], .3)

    def test_trace_and_arrival_bounds_fail_closed(self):
        with self.assertRaises(ValueError): spatial.settle([[0, 0, 0]] * 20_001, [])
        with self.assertRaises(ValueError): spatial.settle([], list(range(201)))

    def test_trace_bool_nonfinite_and_arrival_order_rejected(self):
        for trace, arrivals in (([[0, True, 0]], [0]), ([[0, 0, math.nan]], [0]), ([[0, 0, 0]], [1, 0])):
            with self.subTest(trace=trace, arrivals=arrivals), self.assertRaises(ValueError): spatial.settle(trace, arrivals)


class FrontendCoreTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is not installed')
    def test_local_scene_contracts(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'), 'tests/test_spatial_core.mjs'],
                                cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__": unittest.main()
