"""Pre-flight: can the rig shoot this move?

Every check here answers a question that is cheap now and expensive on set.
The angles are built from the measured arcs rather than typed in, because
pitch 0 is NOT a reachable angle on this head -- travel runs +64.4 through
+/-180 to -137.1, and a test using level pitch would be testing an impossible
move.
"""

import unittest
from unittest.mock import patch

from driver import limits, moves, preflight, response
from driver.moves import Move, Waypoint
from driver.preflight import check


def pitch_at(fraction: float) -> float:
    """A pitch `fraction` of the way along the usable arc."""
    return moves.wrap180(limits.PITCH_ARC.low
                         + limits.PITCH_ARC.usable * fraction)


def yaw_at(fraction: float) -> float:
    return moves.wrap180(limits.YAW_ARC.low
                         + limits.YAW_ARC.usable * fraction)


def move_between(p0, y0, p1, y1, duration=4.0, flow=False):
    return Move(waypoints=[
        Waypoint("a", pitch=p0, yaw=y0, duration=2.0),
        Waypoint("b", pitch=p1, yaw=y1, duration=duration, flow=flow),
    ])


class TestAMoveTheRigCanDo(unittest.TestCase):
    def test_a_gentle_move_inside_the_arcs_passes(self):
        r = check(move_between(pitch_at(.4), yaw_at(.4),
                               pitch_at(.6), yaw_at(.6), duration=20.0))
        self.assertTrue(r.ok, r.summary())
        self.assertEqual(r.findings, [])

    def test_it_reports_the_peaks_it_measured(self):
        r = check(move_between(pitch_at(.4), yaw_at(.4),
                               pitch_at(.6), yaw_at(.6), duration=20.0))
        self.assertGreater(r.peak_yaw_dps, 0.0)
        self.assertLess(r.peak_yaw_dps, response.MAX_DPS)
        self.assertIn("within sampled planning limits", r.summary())

    def test_an_empty_move_is_not_a_failure(self):
        self.assertTrue(check(Move()).ok)
        self.assertTrue(check(Move(waypoints=[Waypoint("a", 100.0, 0.0)])).ok)

    def test_it_touches_no_hardware(self):
        """The whole point is that it runs with the camera in its case."""
        r = check(move_between(pitch_at(.4), yaw_at(.4),
                               pitch_at(.5), yaw_at(.5), duration=10.0))
        self.assertGreater(r.samples, 100)


class TestTravel(unittest.TestCase):
    def test_a_node_outside_the_arc_is_caught(self):
        bad = moves.wrap180(limits.PITCH_ARC.high + 25.0)
        r = check(move_between(pitch_at(.5), yaw_at(.5), bad, yaw_at(.5),
                               duration=20.0))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.kind == "travel" and f.axis == "pitch"
                            for f in r.findings), r.summary())

    def test_the_finding_says_how_far_out_and_when(self):
        bad = moves.wrap180(limits.PITCH_ARC.high + 25.0)
        r = check(move_between(pitch_at(.5), yaw_at(.5), bad, yaw_at(.5),
                               duration=20.0))
        f = next(f for f in r.findings if f.kind == "travel")
        self.assertGreater(f.worst, 1.0)
        self.assertGreaterEqual(f.worst_at, 0.0)
        self.assertIn("deg", f.detail)

    def test_travel_is_reported_before_speed(self):
        """An axis on a stop is a ruined take; a late one is a fixable take."""
        bad = moves.wrap180(limits.PITCH_ARC.high + 40.0)
        r = check(move_between(pitch_at(.5), yaw_at(.1), bad, yaw_at(.9),
                               duration=0.5))
        kinds = [f.kind for f in r.findings]
        self.assertEqual(kinds[0], "travel", kinds)

    def test_a_move_wholly_inside_reports_no_travel_finding(self):
        """Endpoints chosen so the short way between them also stays inside --
        see the next test for why that is not automatic."""
        r = check(move_between(pitch_at(.1), yaw_at(.2),
                               pitch_at(.9), yaw_at(.8), duration=30.0))
        self.assertFalse([f for f in r.findings if f.kind == "travel"],
                         r.summary())

    def _across_the_wedge(self):
        """Two legal yaw angles either side of the unreachable wedge.

        Yaw travel is 267 degrees usable, leaving 93 degrees behind the camera
        it cannot reach. The short way between these two is straight through
        that wedge.
        """
        return move_between(pitch_at(.5), yaw_at(.05),
                            pitch_at(.5), yaw_at(.95), duration=30.0)

    def test_the_short_way_through_the_wedge_is_caught(self):
        """The pre-flight found this bug in the path planner: every node
        legal, path illegal, and nothing in the authoring UI showed it."""
        move = self._across_the_wedge()
        move.route_arcs = False              # how it behaved before the fix
        r = check(move)
        travel = [f for f in r.findings if f.kind == "travel" and f.axis == "yaw"]
        self.assertTrue(travel, r.summary())
        self.assertGreater(travel[0].worst, 20.0)

    def test_arc_routing_makes_the_same_move_legal(self):
        """Routed along the arc it goes the long way round -- further, but
        inside the travel the whole way."""
        r = check(self._across_the_wedge())
        self.assertTrue(r.ok, r.summary())


class TestSpeed(unittest.TestCase):
    def test_a_leg_faster_than_the_head_is_caught(self):
        r = check(move_between(pitch_at(.5), yaw_at(.05),
                               pitch_at(.5), yaw_at(.95), duration=1.0))
        fast = [f for f in r.findings if f.kind == "too fast"]
        self.assertTrue(fast, r.summary())
        self.assertEqual(fast[0].axis, "yaw")

    def test_the_finding_names_the_rate_and_the_limit(self):
        r = check(move_between(pitch_at(.5), yaw_at(.05),
                               pitch_at(.5), yaw_at(.95), duration=1.0))
        f = next(f for f in r.findings if f.kind == "too fast")
        self.assertGreater(abs(f.worst), response.MAX_DPS)
        self.assertAlmostEqual(f.limit, response.MAX_DPS)

    def test_a_preset_cap_asks_the_narrower_question(self):
        """Not 'can the head do it' but 'can it at the selected sensitivity'."""
        args = (pitch_at(.5), yaw_at(.3), pitch_at(.5), yaw_at(.7))
        gentle = move_between(*args, duration=12.0)
        self.assertTrue(check(gentle).ok)
        capped = check(gentle, max_dps=response.SPEED_CAPS["fine"])
        self.assertFalse(capped.ok)
        self.assertTrue(any(f.kind == "too fast" for f in capped.findings))

    def test_a_fifty_millisecond_leg_is_not_silently_under_sampled(self):
        r = check(move_between(pitch_at(.5), yaw_at(.3),
                               pitch_at(.5), yaw_at(.7), duration=0.05))
        self.assertTrue(any(f.kind == "too fast" for f in r.findings),
                        r.summary())

    def test_a_short_leg_after_a_long_dwell_is_still_inspected(self):
        p, y = pitch_at(.5), yaw_at(.3)
        move = Move(waypoints=[
            Waypoint("hold", p, y, dwell=50_000.0),
            Waypoint("snap", p, yaw_at(.7), duration=0.05),
        ])
        r = check(move)
        self.assertTrue(any(f.kind == "too fast" for f in r.findings),
                        r.summary())


class TestDeadBand(unittest.TestCase):
    def test_an_axis_that_never_clears_the_band_is_caught(self):
        """Judged over the whole move, not per sample. Yaw asked for a third
        of a degree over a minute never gets above the dead band, so it does
        not move at all -- and the operator gets a static frame where they
        asked for a drift."""
        p = pitch_at(.5)
        crawl = Move(waypoints=[
            Waypoint("a", pitch=p, yaw=yaw_at(.5), duration=2.0),
            Waypoint("b", pitch=p, yaw=yaw_at(.5) + 0.3, duration=60.0),
        ])
        r = check(crawl)
        self.assertTrue([f for f in r.findings if f.kind == "dead band"],
                        r.summary())

    def test_an_eased_move_is_not_a_dead_band_finding(self):
        """Every eased move starts and ends at zero velocity and so passes
        through the band twice by construction. Flagging that would fire on
        every well-made move, which is why this is a whole-axis test."""
        p, y = pitch_at(.5), yaw_at(.5)
        held = Move(waypoints=[
            Waypoint("a", pitch=p, yaw=y, duration=2.0, dwell=5.0),
            Waypoint("b", pitch=p, yaw=y, duration=2.0, dwell=5.0),
        ])
        self.assertFalse([f for f in check(held).findings
                          if f.kind == "dead band"], check(held).summary())


class TestAggregation(unittest.TestCase):
    """A hundred findings is a wall of text that hides the other problems."""

    def test_a_long_violation_is_one_finding_not_hundreds(self):
        bad = moves.wrap180(limits.PITCH_ARC.high + 30.0)
        r = check(Move(waypoints=[
            Waypoint("a", pitch=bad, yaw=yaw_at(.5), duration=2.0, dwell=6.0),
            Waypoint("b", pitch=bad, yaw=yaw_at(.5), duration=2.0, dwell=6.0),
        ]))
        travel = [f for f in r.findings if f.kind == "travel"]
        self.assertEqual(len(travel), 1)
        self.assertGreater(travel[0].duration, 1.0)

    def _briefly_outside(self):
        """A move that clips outside the pitch arc for a short span."""
        bad = moves.wrap180(limits.PITCH_ARC.high + 2.0)
        return Move(waypoints=[
            Waypoint("a", pitch=pitch_at(.9), yaw=yaw_at(.5), duration=2.0),
            Waypoint("b", pitch=bad, yaw=yaw_at(.5), duration=2.0),
            Waypoint("c", pitch=pitch_at(.9), yaw=yaw_at(.5), duration=2.0),
        ])

    def test_a_brief_clip_is_reported_at_the_default_threshold(self):
        r = check(self._briefly_outside())
        self.assertTrue([f for f in r.findings if f.kind == "travel"],
                        r.summary())

    def test_raising_the_threshold_suppresses_it(self):
        """Directly exercises the threshold. As a module constant nothing
        touched it -- setting it to zero broke no test, which is how a knob
        stops working without anyone noticing."""
        r = check(self._briefly_outside(), min_duration_s=30.0)
        self.assertFalse([f for f in r.findings if f.kind == "travel"],
                         r.summary())


class TestSerialisation(unittest.TestCase):
    def test_the_report_survives_json_shaping(self):
        bad = moves.wrap180(limits.PITCH_ARC.high + 25.0)
        d = check(move_between(pitch_at(.5), yaw_at(.5), bad, yaw_at(.5),
                               duration=20.0)).to_dict()
        self.assertFalse(d["ok"])
        self.assertIn("findings", d)
        self.assertIn("detail", d["findings"][0])
        self.assertIsInstance(d["duration"], float)


class TestBoundedSampling(unittest.TestCase):
    def test_invalid_canonical_sample_cannot_return_a_passing_check(self):
        move = move_between(pitch_at(.4), yaw_at(.4), pitch_at(.6), yaw_at(.6))
        for point in (None, (float('nan'), 0), (0, float('inf'))):
            with self.subTest(point=point), patch.object(move, 'sample', return_value=point):
                r = check(move)
                self.assertFalse(r.ok)
                self.assertTrue(any(f.kind == 'assessment' for f in r.findings))

    def test_brief_eased_peak_cannot_be_hidden_by_violation_aggregation(self):
        move = move_between(pitch_at(.5), yaw_at(.5),
                            pitch_at(.5), yaw_at(.5) + 1, duration=.05)
        r = check(move, max_dps=25)
        self.assertGreater(r.peak_yaw_dps, 25)
        self.assertFalse(r.ok)
        self.assertTrue(any(f.kind == 'too fast' for f in r.findings))

    def test_huge_duration_has_a_hard_sample_cap(self):
        move = move_between(pitch_at(.4), yaw_at(.4),
                            pitch_at(.6), yaw_at(.6), duration=10_000_000.0)
        r = check(move)
        self.assertLessEqual(r.samples, preflight.MAX_SAMPLES)

    def test_an_unassessably_large_waypoint_list_fails_closed(self):
        move = Move(waypoints=[
            Waypoint(str(i), pitch_at(.5), yaw_at(.5), duration=0.05)
            for i in range(preflight.MAX_WAYPOINTS + 1)
        ])
        r = check(move)
        self.assertFalse(r.ok)
        self.assertTrue(any(f.kind == "assessment" for f in r.findings))

    def test_late_short_travel_jump_is_seen_within_the_budget(self):
        waypoints = [
            Waypoint(str(i), pitch_at(.5), yaw_at(.05),
                     duration=.05, dwell=10_000.0)
            for i in range(preflight.MAX_WAYPOINTS - 1)
        ]
        waypoints.append(Waypoint("jump", pitch_at(.5), yaw_at(.95),
                                  duration=.05))
        move = Move(route_arcs=False, waypoints=waypoints)
        r = check(move)
        self.assertLessEqual(r.samples, preflight.MAX_SAMPLES)
        self.assertTrue(any(f.kind == "travel" and f.axis == "yaw"
                            for f in r.findings), r.summary())


if __name__ == "__main__":
    unittest.main()
