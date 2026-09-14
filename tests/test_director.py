"""Offline Director preview and checked timing proposals."""

import json
import math
import unittest

from driver import director, moves, response


def at(arc, fraction):
    return arc.at_offset(arc.usable_span * fraction)


def shot(duration=8.0):
    return moves.Move(
        name="Reveal",
        ping_pong=True,
        route_arcs=True,
        setup={"mount": "tripod", "notes": "keep"},
        waypoints=[
            moves.Waypoint("Door", at(moves.PITCH_LIMITS, .4),
                           at(moves.YAW_LIMITS, .4), dwell=1.0, zoom=.2,
                           cue=True),
            moves.Waypoint("Face", at(moves.PITCH_LIMITS, .6),
                           at(moves.YAW_LIMITS, .6), duration=duration,
                           dwell=2.0, easing="linear", zoom=.8,
                           zoom_easing="ease-in", flow=False, cue=True),
        ],
    )


class TestPreviewSchema(unittest.TestCase):
    def test_preview_is_json_shaped_and_bounded(self):
        out = director.preview(shot())
        json.dumps(out, allow_nan=False)
        self.assertLessEqual(len(out["samples"]), director.MAX_VISUAL_SAMPLES)
        self.assertEqual(set(out["samples"][0]),
                         {"time", "pitch", "yaw", "zoom"})
        self.assertIn("preflight", out)
        self.assertIn("approximation", out)

    def test_non_loop_ping_pong_reports_finite_programmed_duration(self):
        move = shot()
        out = director.preview(move)
        self.assertEqual(out["timing"]["forward_duration"], move.total_duration)
        self.assertEqual(out["timing"]["programmed_duration"],
                         2 * move.total_duration)
        self.assertFalse(out["timing"]["loop_uncertain"])
        self.assertTrue(out["timing"]["cue_uncertain"])
        self.assertEqual(out["samples"][-1]["pitch"],
                         out["samples"][0]["pitch"])
        self.assertEqual(out["samples"][-1]["zoom"],
                         out["samples"][0]["zoom"])

    def test_loop_duration_is_open_ended_but_one_cycle_is_sampled(self):
        move = shot()
        move.loop = True
        out = director.preview(move)
        self.assertIsNone(out["timing"]["programmed_duration"])
        self.assertTrue(out["timing"]["loop_uncertain"])
        self.assertEqual(out["samples"][-1]["time"],
                         2 * move.total_duration)

    def test_named_events_include_forward_and_reverse_dwell_and_cues(self):
        move = shot()
        out = director.preview(move)
        reverse = [e for e in out["events"] if e["direction"] == "reverse"]
        self.assertTrue(reverse)
        self.assertTrue(any(e["type"] == "arrival" and e["name"] == "Face"
                            for e in reverse))
        self.assertTrue(any(e["type"] == "dwell_start" for e in reverse))
        self.assertTrue(any(e["type"] == "cue" and e["name"] == "Door"
                            for e in reverse))
        preview_cues = {(e["time"], e["beat_index"]) for e in out["events"]
                        if e["type"] == "cue"}
        self.assertEqual(preview_cues, set(move.playback_cue_points()))


class TestTimingProposal(unittest.TestCase):
    def test_preview_never_mutates_the_move(self):
        move = shot()
        before = move.to_json()
        director.preview(move, target_duration=30.0)
        self.assertEqual(move.to_json(), before)

    def test_requested_duration_is_assessed_and_preserves_the_whole_move(self):
        move = shot(duration=20.0)
        out = director.preview(move, target_duration=60.0)
        proposal = out["proposal"]
        self.assertEqual(proposal["status"], "requested_feasible")
        candidate = moves.Move.from_dict(proposal["move"])
        self.assertAlmostEqual(2 * candidate.total_duration, 60.0)
        self.assertEqual(candidate.name, move.name)
        self.assertEqual(candidate.setup, move.setup)
        self.assertEqual(candidate.ping_pong, move.ping_pong)
        self.assertEqual(candidate.waypoints[1].zoom_easing,
                         move.waypoints[1].zoom_easing)
        self.assertTrue(proposal["preflight"]["ok"])

    def test_leg_floor_returns_truthful_checked_alternative(self):
        pitch = at(moves.PITCH_LIMITS, .5)
        yaw = at(moves.YAW_LIMITS, .5)
        move = moves.Move(
            name="Four stationary beats",
            waypoints=[
                moves.Waypoint(str(index), pitch, yaw, duration=1.0)
                for index in range(4)
            ],
        )

        proposal = director.preview(move, target_duration=.1)["proposal"]

        self.assertEqual(proposal["status"], "suggested")
        self.assertEqual(proposal["requested_duration"], .1)
        self.assertAlmostEqual(proposal["assessed_duration"], .15)
        self.assertTrue(proposal["preflight"]["ok"])
        self.assertIn("minimum leg timing", proposal["reason"])
        candidate = moves.Move.from_dict(proposal["move"])
        self.assertAlmostEqual(candidate.total_duration, .15)

    def test_too_fast_request_gets_a_checked_feasible_suggestion(self):
        move = shot(duration=.05)
        out = director.preview(move, target_duration=.2)
        proposal = out["proposal"]
        self.assertEqual(proposal["status"], "suggested")
        self.assertTrue(proposal["preflight"]["ok"])
        self.assertGreater(proposal["assessed_duration"], .2)
        self.assertIsNotNone(proposal["move"])

    def test_travel_problem_is_not_claimed_fixable_by_timing(self):
        move = shot()
        move.waypoints[1].pitch = moves.wrap180(moves.PITCH_LIMITS.end + 20)
        proposal = director.preview(move, target_duration=30)["proposal"]
        self.assertEqual(proposal["status"], "impossible")
        self.assertIsNone(proposal["move"])
        self.assertIn("travel", proposal["reason"])

    def test_impossible_speed_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            director.preview(shot(), max_dps=response.MIN_DPS / 2,
                             min_dps=response.MIN_DPS)

    def test_extreme_duration_is_rejected_before_sampling(self):
        with self.assertRaises(ValueError):
            director.preview(shot(), target_duration=director.MAX_TARGET_DURATION + 1)

    def test_huge_authored_duration_is_rejected_by_director_envelope(self):
        for duration in (1e308, math.inf):
            with self.subTest(duration=duration):
                move = moves.Move(
                    name="Unrehearsable",
                    waypoints=[
                        moves.Waypoint("A", 0, 0),
                        moves.Waypoint("B", 1, 1, duration=duration),
                    ],
                )

                with self.assertRaisesRegex(ValueError,
                                            "cycle duration.*86400"):
                    director.preview(move)


if __name__ == "__main__":
    unittest.main()
