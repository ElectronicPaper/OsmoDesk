"""Motion-control maths: easing, timed sampling, soft limits.

A programmed move is only worth anything if it is repeatable and lands where
it says. These pin the timeline arithmetic, the short-way angle handling, and
the limit clamp that keeps a move off the mechanical stop.
"""

import unittest

from driver import moves
from driver.moves import Move, SoftLimits, Waypoint


class TestAngleHelpers(unittest.TestCase):
    def test_wrap180(self):
        self.assertAlmostEqual(moves.wrap180(0), 0)
        self.assertAlmostEqual(moves.wrap180(190), -170)
        self.assertAlmostEqual(moves.wrap180(-190), 170)
        self.assertAlmostEqual(moves.wrap180(540), 180)

    def test_lerp_takes_the_short_way(self):
        # 170 -> -170 is 20 degrees across the wrap, not 340 back through zero.
        mid = moves.lerp_angle(170.0, -170.0, 0.5)
        self.assertAlmostEqual(moves.wrap180(mid - 170.0), 10.0, places=6)

    def test_lerp_endpoints(self):
        self.assertAlmostEqual(moves.lerp_angle(-20, 40, 0.0), -20)
        self.assertAlmostEqual(moves.wrap180(moves.lerp_angle(-20, 40, 1.0) - 40), 0)


class TestEasing(unittest.TestCase):
    def test_all_easings_are_normalised(self):
        for name, fn in moves.EASINGS.items():
            with self.subTest(easing=name):
                self.assertAlmostEqual(fn(0.0), 0.0, places=6)
                self.assertAlmostEqual(fn(1.0), 1.0, places=6)

    def test_all_easings_are_monotonic(self):
        # A curve that dips backwards would make the camera reverse mid-move.
        for name, fn in moves.EASINGS.items():
            with self.subTest(easing=name):
                prev = -1e-9
                for i in range(101):
                    v = fn(i / 100)
                    self.assertGreaterEqual(v + 1e-9, prev)
                    prev = v

    def test_all_easings_clamp_out_of_range(self):
        for name, fn in moves.EASINGS.items():
            with self.subTest(easing=name):
                self.assertAlmostEqual(fn(-5.0), 0.0, places=6)
                self.assertAlmostEqual(fn(5.0), 1.0, places=6)

    def test_s_curves_pass_through_the_middle(self):
        for name in ("ease-in-out", "ease-in-out-cubic", "ease-in-out-sine"):
            with self.subTest(easing=name):
                self.assertAlmostEqual(moves.EASINGS[name](0.5), 0.5, places=6)

    def test_ease_in_starts_slow(self):
        self.assertLess(moves.ease_in(0.25), 0.25)

    def test_ease_out_starts_fast(self):
        self.assertGreater(moves.ease_out(0.25), 0.25)

    def test_unknown_easing_raises(self):
        with self.assertRaises(ValueError):
            moves.get_easing("swoosh")


class TestSoftLimits(unittest.TestCase):
    """Pitch travel is an arc crossing +/-180, measured +64.5 -> -136.9.

    A min/max range gets this wrong twice over: it rejects legal angles past
    the wrap and permits the entire forbidden side. That bug clamped a whole
    programmed move on hardware.
    """

    def test_angles_inside_the_measured_arc_are_allowed(self):
        lim = SoftLimits()
        for p in (70.0, 120.0, 179.0, -179.0, -150.0, -140.0):
            with self.subTest(pitch=p):
                self.assertTrue(lim.contains(p), f"{p} should be reachable")

    def test_angles_on_the_forbidden_side_are_rejected(self):
        lim = SoftLimits()
        for p in (0.0, 30.0, -45.0, -90.0, -120.0, 60.0):
            with self.subTest(pitch=p):
                self.assertFalse(lim.contains(p), f"{p} should be blocked")

    def test_arc_wraps_through_180(self):
        lim = SoftLimits()
        self.assertTrue(lim.contains(180.0))
        self.assertTrue(lim.contains(-180.0))

    def test_margin_keeps_targets_off_the_stops(self):
        lim = SoftLimits()
        self.assertFalse(lim.contains(-136.9))  # the measured stop itself
        self.assertFalse(lim.contains(64.5))

    def test_clamp_returns_reachable_angles(self):
        lim = SoftLimits()
        for p in (0.0, -90.0, 45.0, -130.0, 200.0, -400.0):
            with self.subTest(pitch=p):
                self.assertTrue(lim.contains(lim.clamp_pitch(p)))

    def test_clamp_leaves_legal_angles_alone(self):
        lim = SoftLimits()
        for p in (100.0, 179.0, -150.0):
            self.assertEqual(lim.clamp_pitch(p), p)

    def test_clamp_picks_the_nearer_end(self):
        lim = SoftLimits()
        # Just past the -137 stop clamps to that end, not the far one.
        self.assertAlmostEqual(lim.clamp_pitch(-134.0), lim.end, places=6)
        # Just below the +64.5 stop clamps to the start end.
        self.assertAlmostEqual(lim.clamp_pitch(60.0), lim.start, places=6)

    def test_usable_span_is_the_arc_less_both_margins(self):
        lim = SoftLimits(arc_from=0.0, arc_span=100.0, margin=5.0)
        self.assertAlmostEqual(lim.usable_span, 90.0)
        self.assertAlmostEqual(lim.start, 5.0)
        self.assertAlmostEqual(lim.end, 95.0)

    def test_blocked_is_the_inverse_of_contains(self):
        lim = SoftLimits()
        for p in range(-180, 180, 7):
            self.assertEqual(lim.pitch_blocked(float(p)), not lim.contains(float(p)))


class TestMoveTiming(unittest.TestCase):
    def build(self):
        return Move(name="t", waypoints=[
            Waypoint("A", pitch=-170, yaw=0, dwell=1.0),
            Waypoint("B", pitch=-150, yaw=30, duration=4.0, dwell=0.5),
            Waypoint("C", pitch=-160, yaw=-20, duration=3.0),
        ])

    def test_total_duration_counts_dwells(self):
        self.assertAlmostEqual(self.build().total_duration, 1.0 + 4.0 + 0.5 + 3.0)

    def test_holds_on_the_opening_dwell(self):
        m = self.build()
        for t in (0.0, 0.5, 0.99):
            self.assertEqual(m.sample(t), (-170, 0))

    def test_arrives_on_time(self):
        m = self.build()
        p, y = m.sample(5.0)
        self.assertAlmostEqual(p, -150, places=3)
        self.assertAlmostEqual(y, 30, places=3)

    def test_dwell_holds_position(self):
        m = self.build()
        self.assertEqual(m.sample(5.25), (-150, 30))

    def test_midpoint_of_an_s_curve_is_halfway(self):
        m = self.build()
        p, y = m.sample(1.0 + 2.0)  # halfway through the 4s leg
        self.assertAlmostEqual(p, -160, places=3)
        self.assertAlmostEqual(y, 15, places=3)

    def test_ends_at_the_last_waypoint(self):
        m = self.build()
        self.assertEqual(m.sample(m.total_duration), (-160, -20))
        self.assertEqual(m.sample(999), (-160, -20))

    def test_finished_flag(self):
        m = self.build()
        self.assertFalse(m.finished(m.total_duration - 0.01))
        self.assertTrue(m.finished(m.total_duration))

    def test_too_few_waypoints_samples_none(self):
        self.assertIsNone(Move(waypoints=[]).sample(0))
        self.assertIsNone(Move(waypoints=[Waypoint("A", 0, 0)]).sample(0))
        self.assertEqual(Move(waypoints=[Waypoint("A", 0, 0)]).total_duration, 0.0)

    def test_negative_time_is_the_start(self):
        self.assertEqual(self.build().sample(-3), (-170, 0))

    def test_easing_is_per_leg(self):
        m = Move(waypoints=[
            Waypoint("A", 0, 0),
            Waypoint("B", 100, 0, duration=2.0, easing="linear"),
        ])
        self.assertAlmostEqual(m.sample(1.0)[0], 50.0, places=6)

    def test_loop_never_finishes(self):
        m = self.build()
        m.loop = True
        self.assertFalse(m.finished(9999))
        self.assertEqual(m.sample(0.2), m.sample(m.total_duration + 0.2))

    def test_ping_pong_reverses(self):
        m = self.build()
        m.ping_pong = True
        total = m.total_duration
        # Just past the end it is on the way back, not parked at the last point.
        self.assertEqual(m.sample(total + 0.5), m.sample(total - 0.5))


class TestMoveSerialisation(unittest.TestCase):
    def test_roundtrip(self):
        m = Move(name="dolly", loop=True, ping_pong=False, waypoints=[
            Waypoint("A", -170, 0, dwell=1.0),
            Waypoint("B", -150, 30, duration=4.0, easing="linear"),
        ])
        back = Move.from_json(m.to_json())
        self.assertEqual(back.name, "dolly")
        self.assertTrue(back.loop)
        self.assertEqual(len(back.waypoints), 2)
        self.assertEqual(back.waypoints[1].easing, "linear")
        self.assertAlmostEqual(back.total_duration, m.total_duration)

    def test_from_dict_applies_floors(self):
        w = Waypoint.from_dict({"name": "x", "pitch": 1, "yaw": 2,
                                "duration": -5, "dwell": -5})
        self.assertGreaterEqual(w.duration, 0.1)
        self.assertEqual(w.dwell, 0.0)

    def test_from_dict_defaults(self):
        w = Waypoint.from_dict({"pitch": 1, "yaw": 2})
        self.assertEqual(w.easing, moves.DEFAULT_EASING)
        self.assertIn(w.easing, moves.EASINGS)


class TestPresets(unittest.TestCase):
    def test_presets_reference_real_easings(self):
        for name, cfg in moves.PRESETS.items():
            with self.subTest(preset=name):
                self.assertIn(cfg["easing"], moves.EASINGS)
                self.assertGreater(cfg["duration"], 0)

    def test_presets_span_a_useful_range(self):
        durations = [c["duration"] for c in moves.PRESETS.values()]
        self.assertLess(min(durations), 1.0)   # something snappy
        self.assertGreater(max(durations), 30) # something very slow


def _velocity(move, t, axis=1, h=0.01):
    """Numerical dy/dt of a sampled axis. 1 = yaw, 0 = pitch."""
    return (move.sample(t + h)[axis] - move.sample(t - h)[axis]) / (2 * h)


def _pan(flow, span=90.0, legs=3, duration=3.0):
    """A constant-rate pan across `legs` equal legs."""
    step = span / legs
    wps = [Waypoint("n0", pitch=0.0, yaw=0.0, duration=duration)]
    for i in range(1, legs + 1):
        wps.append(Waypoint(f"n{i}", pitch=0.0, yaw=step * i,
                            duration=duration, flow=flow and i < legs))
    return Move(name="pan", waypoints=wps)


class TestFlowNodes(unittest.TestCase):
    """A node is either a mark to stop on or a point to pass through.

    Every easing curve in the table has zero velocity at both ends, so a move
    built from legs eases to a dead stop at every interior node -- five nodes,
    five stutters, whatever the dwell says. That is right for a mark and wrong
    for a continuous move, and no amount of jog-side ramp tuning fixes it
    because the stop is in the path.
    """

    def test_a_stop_node_still_comes_to_rest(self):
        """The old behaviour has to survive: moves already cut depend on it."""
        move = _pan(flow=False)
        for t in move.arrival_times()[1:-1]:
            self.assertLess(abs(_velocity(move, t)), 0.5,
                            f"a stop node at t={t} is not stopping")

    def test_a_flow_node_passes_through_at_speed(self):
        move = _pan(flow=True)
        for t in move.arrival_times()[1:-1]:
            self.assertGreater(_velocity(move, t), 5.0,
                               f"a flow node at t={t} stalled")

    def test_a_constant_rate_pan_flows_at_the_constant_rate(self):
        """90 degrees over 9 seconds is 10 deg/s, at the nodes as well as
        between them. If the nodes are slower than the legs, it stutters."""
        move = _pan(flow=True, span=90.0, legs=3, duration=3.0)
        for t in move.arrival_times()[1:-1]:
            self.assertAlmostEqual(_velocity(move, t), 10.0, delta=0.2)

    def test_the_ends_are_at_rest_even_when_marked_flow(self):
        """A move that starts or ends mid-swing cannot be repeated."""
        move = Move(waypoints=[
            Waypoint("a", 0.0, 0.0, duration=2.0, flow=True),
            Waypoint("b", 0.0, 20.0, duration=2.0, flow=True),
            Waypoint("c", 0.0, 40.0, duration=2.0, flow=True),
        ])
        self.assertLess(abs(_velocity(move, 0.02)), 1.0)
        self.assertLess(abs(_velocity(move, move.total_duration - 0.02)), 1.0)

    def test_a_move_with_no_flow_nodes_is_left_on_the_old_path(self):
        """Not a style preference -- it is the compatibility guarantee."""
        self.assertFalse(_pan(flow=False).uses_flow)
        self.assertTrue(_pan(flow=True).uses_flow)

    def test_flow_never_overshoots_a_node(self):
        """The reason this is monotone cubic and not Catmull-Rom.

        A pass-through spline that overshoots means a move whose nodes all sit
        inside the travel arc can still swing past a hard stop between them.
        The shape below is the one that provokes it: a fast leg running into a
        slow one.
        """
        move = Move(waypoints=[
            Waypoint("a", 0.0, 0.0, duration=1.0),
            Waypoint("b", 0.0, 80.0, duration=1.0, flow=True),
            Waypoint("c", 0.0, 85.0, duration=4.0, flow=True),
            Waypoint("d", 0.0, 86.0, duration=4.0),
        ])
        seen = [move.sample(i * 0.01)[1]
                for i in range(int(move.total_duration * 100))]
        self.assertGreaterEqual(min(seen), -0.01, "undershot below the first node")
        self.assertLessEqual(max(seen), 86.01, "overshot past the last node")

    def test_flow_takes_the_short_way_across_the_seam(self):
        """Tangents computed on wrapped angles are wrong by 360 deg/s."""
        move = Move(waypoints=[
            Waypoint("a", 0.0, 170.0, duration=2.0),
            Waypoint("b", 0.0, -170.0, duration=2.0, flow=True),
            Waypoint("c", 0.0, -150.0, duration=2.0),
        ])
        # 170 -> -170 is +20 the short way, so nothing should exceed ~20 deg/s.
        for i in range(1, int(move.total_duration * 100)):
            self.assertLess(abs(_velocity(move, i * 0.01)), 40.0,
                            "unwound the long way round")


class TestZoomTrack(unittest.TestCase):
    """Zoom rides the same nodes but is its own track.

    It is authored with the framing -- you set the shot, you capture it -- but
    executed separately, because the gimbal takes a continuous rate and the
    lens takes steps.
    """

    def _move(self):
        return Move(waypoints=[
            Waypoint("a", 0.0, 0.0, duration=2.0, zoom=0.0),
            Waypoint("b", 0.0, 30.0, duration=2.0),           # no zoom set
            Waypoint("c", 0.0, 60.0, duration=2.0, zoom=1.0,
                     zoom_easing="linear"),
        ])

    def test_a_move_without_zoom_reports_none(self):
        move = Move(waypoints=[Waypoint("a", 0.0, 0.0),
                               Waypoint("b", 0.0, 10.0)])
        self.assertFalse(move.has_zoom)
        self.assertIsNone(move.sample_zoom(0.0))

    def test_unset_nodes_are_skipped_not_read_as_wide(self):
        """Reading an unset node as 0.0 would rack the lens out between two
        matched framings."""
        self.assertEqual([n[0] for n in self._move().zoom_nodes()], [0.0, 4.0])

    def test_zoom_is_held_before_the_first_and_after_the_last_node(self):
        move = self._move()
        self.assertEqual(move.sample_zoom(-1.0), 0.0)
        self.assertEqual(move.sample_zoom(0.0), 0.0)
        self.assertEqual(move.sample_zoom(99.0), 1.0)

    def test_zoom_interpolates_between_its_own_nodes(self):
        move = self._move()
        self.assertAlmostEqual(move.sample_zoom(2.0), 0.5, delta=0.01)

    def test_zoom_easing_is_independent_of_the_move_easing(self):
        """A push that lags the pan and catches up is a different shot from
        one that moves with it."""
        wps = lambda e: [
            Waypoint("a", 0.0, 0.0, duration=2.0, zoom=0.0, zoom_easing=e),
            Waypoint("b", 0.0, 60.0, duration=4.0, zoom=1.0, zoom_easing=e),
        ]
        linear = Move(waypoints=wps("linear")).sample_zoom(1.0)
        lagged = Move(waypoints=wps("ease-in")).sample_zoom(1.0)
        self.assertLess(lagged, linear, "ease-in should lag a linear push")

    def test_a_single_zoom_node_holds_that_value(self):
        move = Move(waypoints=[
            Waypoint("a", 0.0, 0.0, duration=2.0),
            Waypoint("b", 0.0, 30.0, duration=2.0, zoom=0.7),
        ])
        self.assertEqual(move.sample_zoom(0.0), 0.7)
        self.assertEqual(move.sample_zoom(10.0), 0.7)


class TestNewFieldsRoundTrip(unittest.TestCase):
    def test_flow_and_zoom_survive_a_save_and_load(self):
        move = Move(name="push", waypoints=[
            Waypoint("a", 10.0, 0.0, duration=2.0, zoom=0.2, flow=False),
            Waypoint("b", 12.0, 45.0, duration=3.0, zoom=0.9,
                     zoom_easing="linear", flow=True),
        ])
        back = Move.from_json(move.to_json())
        self.assertTrue(back.waypoints[1].flow)
        self.assertEqual(back.waypoints[1].zoom, 0.9)
        self.assertEqual(back.waypoints[1].zoom_easing, "linear")
        self.assertEqual(back.waypoints[0].zoom, 0.2)

    def test_an_old_file_without_the_new_fields_still_loads(self):
        """Moves cut before this existed have to keep working."""
        old = ('{"name":"x","waypoints":['
               '{"name":"a","pitch":0,"yaw":0,"duration":2},'
               '{"name":"b","pitch":0,"yaw":30,"duration":2}]}')
        back = Move.from_json(old)
        self.assertFalse(back.uses_flow)
        self.assertFalse(back.has_zoom)
        self.assertIsNone(back.waypoints[0].zoom)

    def test_a_zoom_out_of_range_is_clamped_not_accepted(self):
        back = Move.from_dict({"waypoints": [
            {"name": "a", "pitch": 0, "yaw": 0, "zoom": 4.5},
            {"name": "b", "pitch": 0, "yaw": 1, "zoom": -2.0}]})
        self.assertEqual(back.waypoints[0].zoom, 1.0)
        self.assertEqual(back.waypoints[1].zoom, 0.0)


class TestArcRouting(unittest.TestCase):
    """The path has to stay inside the travel, not merely start and end there.

    Yaw has 267 degrees of travel, leaving a 93 degree wedge behind the camera
    it cannot reach. Interpolating the geometrically short way between two
    legal angles either side of that wedge routes straight through it.
    """

    def _yaw(self, fraction):
        y = moves.YAW_LIMITS
        return moves.wrap180(y.start + y.usable_span * fraction)

    def test_the_short_way_and_the_arc_way_disagree_across_the_wedge(self):
        a, b = self._yaw(0.03), self._yaw(0.97)
        short = moves.wrap180(b - a)
        along = moves.arc_delta(moves.YAW_LIMITS, a, b)
        self.assertLess(abs(short), 180.0)
        self.assertGreater(abs(along), 180.0)
        self.assertNotAlmostEqual(short, along, places=1)

    def test_they_agree_when_the_short_way_is_already_inside(self):
        """The fix must not lengthen a move that was already correct."""
        a, b = self._yaw(0.4), self._yaw(0.6)
        self.assertAlmostEqual(moves.wrap180(b - a),
                               moves.arc_delta(moves.YAW_LIMITS, a, b),
                               places=6)

    def test_an_endpoint_outside_the_arc_falls_back_to_the_short_way(self):
        """A move authored outside the travel cannot be routed inside it, and
        silently relocating it would hide the real problem."""
        outside = moves.wrap180(moves.YAW_LIMITS.end + 30.0)
        self.assertAlmostEqual(
            moves.arc_delta(moves.YAW_LIMITS, self._yaw(0.5), outside),
            moves.wrap180(outside - self._yaw(0.5)), places=6)

    def test_a_routed_move_never_leaves_the_arc(self):
        move = Move(waypoints=[
            Waypoint("a", 100.0, self._yaw(0.03), duration=2.0),
            Waypoint("b", 100.0, self._yaw(0.97), duration=20.0)])
        for i in range(int(move.total_duration * 20)):
            yaw = move.sample(i * 0.05)[1]
            with self.subTest(t=i * 0.05):
                self.assertTrue(moves.YAW_LIMITS.contains(yaw),
                                f"yaw {yaw:.1f} left the arc")

    def test_the_unrouted_move_does_leave_it(self):
        """Proves the previous test is testing the routing, not the geometry."""
        move = Move(route_arcs=False, waypoints=[
            Waypoint("a", 100.0, self._yaw(0.03), duration=2.0),
            Waypoint("b", 100.0, self._yaw(0.97), duration=20.0)])
        outside = [move.sample(i * 0.05)[1] for i in range(int(20 * 20))]
        self.assertTrue(any(not moves.YAW_LIMITS.contains(y) for y in outside))

    def test_flow_nodes_are_routed_too(self):
        """The spline builds its own unwrapped chain, so it needed the same
        fix -- fixing only the leg path would leave the bug in every move that
        used a pass-through node."""
        move = Move(waypoints=[
            Waypoint("a", 100.0, self._yaw(0.03), duration=2.0),
            Waypoint("b", 100.0, self._yaw(0.50), duration=10.0, flow=True),
            Waypoint("c", 100.0, self._yaw(0.97), duration=10.0)])
        self.assertTrue(move.uses_flow)
        for i in range(int(move.total_duration * 20)):
            yaw = move.sample(i * 0.05)[1]
            with self.subTest(t=i * 0.05):
                self.assertTrue(moves.YAW_LIMITS.contains(yaw),
                                f"yaw {yaw:.1f} left the arc on the spline path")

    def test_routing_survives_a_save_and_load(self):
        move = Move(route_arcs=False, waypoints=[
            Waypoint("a", 100.0, 0.0, duration=2.0),
            Waypoint("b", 100.0, 30.0, duration=2.0)])
        self.assertFalse(Move.from_json(move.to_json()).route_arcs)

    def test_an_old_file_gets_routing_switched_on(self):
        """An old move that crossed the wedge was already playing wrong;
        reproducing the wrong path faithfully is not a kindness."""
        old = ('{"waypoints":[{"name":"a","pitch":100,"yaw":0,"duration":2},'
               '{"name":"b","pitch":100,"yaw":30,"duration":2}]}')
        self.assertTrue(Move.from_json(old).route_arcs)


class TestOneSourceOfTravelTruth(unittest.TestCase):
    def test_the_limit_monitor_uses_the_same_arcs_as_the_planner(self):
        """Written out twice, the path planner and the limit monitor can
        disagree about where the stops are, which is the worst way for this to
        go wrong."""
        from driver import limits as lim
        self.assertAlmostEqual(lim.PITCH_ARC.usable,
                               moves.PITCH_LIMITS.usable_span)
        self.assertAlmostEqual(lim.YAW_ARC.usable, moves.YAW_LIMITS.usable_span)
        self.assertAlmostEqual(lim.PITCH_ARC.start, moves.PITCH_LIMITS.arc_from)
        self.assertAlmostEqual(lim.YAW_ARC.start, moves.YAW_LIMITS.arc_from)


class TestBetweenTakes(unittest.TestCase):
    """The loop a set actually runs: back to one, the director asks for an
    adjustment, the operator applies it, go again. The adjustment is almost
    never "reprogram the move"."""

    def _move(self):
        return Move(waypoints=[
            Waypoint("a", 100.0, 0.0, duration=2.0, dwell=1.0),
            Waypoint("b", 110.0, 30.0, duration=4.0, dwell=0.5),
            Waypoint("c", 105.0, 60.0, duration=3.0)])

    # -- retime ---------------------------------------------------------------

    def test_a_factor_scales_the_whole_move(self):
        self.assertAlmostEqual(self._move().retimed(factor=2).total_duration,
                               self._move().total_duration * 2)

    def test_a_target_duration_is_hit_exactly(self):
        """'Make it eight seconds' is how the request arrives."""
        self.assertAlmostEqual(self._move().retimed(total=8.0).total_duration,
                               8.0, places=6)

    def test_dwells_scale_too_so_the_rhythm_survives(self):
        """Scaling only the legs is the tempting shortcut and it changes the
        timing of the shot, not just its length."""
        slow = self._move().retimed(factor=2)
        self.assertAlmostEqual(slow.waypoints[0].dwell, 2.0)
        self.assertAlmostEqual(slow.waypoints[1].dwell, 1.0)

    def test_the_shape_is_unchanged(self):
        """Same positions, same easing, same flow marks -- only the clock."""
        original, slow = self._move(), self._move().retimed(factor=3)
        for a, b in zip(original.waypoints, slow.waypoints):
            self.assertEqual((a.pitch, a.yaw, a.easing, a.flow),
                             (b.pitch, b.yaw, b.easing, b.flow))

    def test_a_move_retimed_to_nothing_still_has_usable_legs(self):
        """The sampler divides by duration; a zero leg is a crash mid-take."""
        tiny = self._move().retimed(factor=0.00001)
        for w in tiny.waypoints:
            self.assertGreaterEqual(w.duration, moves.MIN_LEG_S)

    def test_it_refuses_nonsense(self):
        m = self._move()
        for kw in ({}, {"factor": 2, "total": 8}, {"factor": 0}, {"total": -1}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    m.retimed(**kw)

    # -- trim and re-reference -------------------------------------------------

    def test_an_offset_shifts_every_node_equally(self):
        moved = self._move().offset(pitch=5.0, yaw=-3.0)
        for a, b in zip(self._move().waypoints, moved.waypoints):
            self.assertAlmostEqual(b.pitch, a.pitch + 5.0)
            self.assertAlmostEqual(b.yaw, a.yaw - 3.0)

    def test_an_offset_wraps_rather_than_running_past_180(self):
        edged = Move(waypoints=[Waypoint("a", 100.0, 175.0, duration=1.0),
                                Waypoint("b", 100.0, 178.0, duration=1.0)])
        self.assertLessEqual(abs(edged.offset(yaw=10.0).waypoints[0].yaw), 180.0)

    def test_re_reference_puts_the_first_node_where_the_head_is(self):
        """The routine after a remount or a battery swap: aim at the frame you
        want and re-reference."""
        moved = self._move().referenced_to(120.0, 45.0)
        self.assertAlmostEqual(moved.waypoints[0].pitch, 120.0)
        self.assertAlmostEqual(moved.waypoints[0].yaw, 45.0)

    def test_re_reference_preserves_the_relative_motion(self):
        """Otherwise it is not the same shot from a new place, it is a
        different shot."""
        original, moved = self._move(), self._move().referenced_to(120.0, 45.0)
        for a, b in zip(original.waypoints, moved.waypoints):
            self.assertAlmostEqual(
                moves.wrap180(b.pitch - moved.waypoints[0].pitch),
                moves.wrap180(a.pitch - original.waypoints[0].pitch))
            self.assertAlmostEqual(
                moves.wrap180(b.yaw - moved.waypoints[0].yaw),
                moves.wrap180(a.yaw - original.waypoints[0].yaw))

    def test_re_referencing_to_where_it_already_is_changes_nothing(self):
        m = self._move()
        same = m.referenced_to(m.waypoints[0].pitch, m.waypoints[0].yaw)
        for a, b in zip(m.waypoints, same.waypoints):
            self.assertAlmostEqual(a.pitch, b.pitch)
            self.assertAlmostEqual(a.yaw, b.yaw)

    def test_none_of_these_mutate_the_original(self):
        """An operator trying a slower version must be able to go back."""
        m = self._move()
        before = m.to_json()
        m.retimed(factor=2); m.offset(pitch=9.0); m.referenced_to(1.0, 2.0)
        self.assertEqual(m.to_json(), before)

    # -- the bumped tripod -----------------------------------------------------

    def test_a_head_parked_at_the_start_passes(self):
        self.assertTrue(self._move().at_start(100.4, -0.3))

    def test_a_knocked_tripod_fails(self):
        """Somebody will knock it between takes and nobody will admit it. The
        move then runs perfectly from the wrong place."""
        self.assertFalse(self._move().at_start(103.0, 0.0))

    def test_the_error_says_which_axis_and_by_how_much(self):
        dp, dy = self._move().start_error(102.0, -5.0)
        self.assertAlmostEqual(dp, 2.0)
        self.assertAlmostEqual(dy, -5.0)

    def test_the_check_measures_the_short_way_round(self):
        seam = Move(waypoints=[Waypoint("a", 100.0, 179.0, duration=1.0),
                               Waypoint("b", 100.0, 170.0, duration=1.0)])
        dp, dy = seam.start_error(100.0, -179.0)
        self.assertAlmostEqual(dy, 2.0, places=6)

    def test_an_empty_move_reports_no_error_rather_than_crashing(self):
        self.assertEqual(Move().start_error(1.0, 2.0), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()


class TestSegment(unittest.TestCase):
    """Rehearsing part of a shot. Covered through the server already, but the
    method's own refusals were not, and those are what an operator hits when
    they fat-finger a node number."""

    def _move(self, n=4):
        return Move(name="push", waypoints=[
            Waypoint(chr(97 + i), 100.0 + i, i * 20.0, duration=3.0, dwell=1.0,
                     flow=(0 < i < n - 1))
            for i in range(n)])

    def test_it_takes_the_span_between_two_nodes(self):
        seg = self._move().segment(1, 2)
        self.assertEqual([w.name for w in seg.waypoints], ["b", "c"])

    def test_the_order_of_the_arguments_does_not_matter(self):
        """An operator dragging a selection backwards means the same span."""
        self.assertEqual([w.name for w in self._move().segment(2, 1).waypoints],
                         [w.name for w in self._move().segment(1, 2).waypoints])

    def test_the_name_says_which_part(self):
        self.assertEqual(self._move().segment(1, 2).name, "push [2-3]")

    def test_the_lead_dwell_goes_but_the_others_stay(self):
        seg = self._move().segment(0, 2)
        self.assertEqual(seg.waypoints[0].dwell, 0.0)
        self.assertEqual(seg.waypoints[1].dwell, 1.0)

    def test_flow_marks_and_easing_survive(self):
        seg = self._move().segment(1, 3)
        self.assertTrue(seg.waypoints[0].flow)
        self.assertEqual(seg.waypoints[0].easing, "ease-in-out-sine")

    def test_looping_is_not_inherited(self):
        """Looping the whole shot and looping a fragment of it are different
        intentions; inheriting the flag surprises people."""
        m = self._move()
        m.loop = m.ping_pong = True
        seg = m.segment(1, 2)
        self.assertFalse(seg.loop)
        self.assertFalse(seg.ping_pong)

    def test_the_original_is_untouched(self):
        m = self._move()
        before = m.to_json()
        m.segment(1, 2)
        self.assertEqual(m.to_json(), before)

    def test_a_segment_of_one_node_is_refused(self):
        with self.assertRaises(ValueError):
            self._move().segment(2, 2)

    def test_an_index_off_the_end_is_refused(self):
        for bad in ((0, 9), (-1, 2), (9, 0)):
            with self.subTest(nodes=bad):
                with self.assertRaises(ValueError):
                    self._move().segment(*bad)

    def test_a_move_too_short_to_segment_is_refused(self):
        with self.assertRaises(ValueError):
            Move(waypoints=[Waypoint("a", 100.0, 0.0)]).segment(0, 0)


class TestStatedFieldOfView(unittest.TestCase):
    """Every pixel figure this rig produces depends on the lens, and nothing
    on the camera reports it. Stated once, it travels with the shot."""

    def _with(self, value):
        m = Move()
        m.setup["fov_deg"] = value
        return m

    def test_absent_is_none_so_the_caller_can_say_assumed(self):
        self.assertIsNone(Move().fov_deg())

    def test_a_stated_lens_comes_back(self):
        self.assertEqual(self._with(63.5).fov_deg(), 63.5)

    def test_a_number_typed_as_text_is_accepted(self):
        """It arrives from a text field."""
        self.assertEqual(self._with("42").fov_deg(), 42.0)

    def test_nonsense_falls_back_rather_than_lying(self):
        """A wrong field of view produces a confident, wrong pixel verdict --
        worse than admitting the assumption."""
        for bad in ("wide", "", None, 0.0, -5.0, 900.0, [84]):
            with self.subTest(bad=bad):
                self.assertIsNone(self._with(bad).fov_deg())

    def test_the_bounds_are_inclusive_at_sane_values(self):
        self.assertEqual(self._with(1.0).fov_deg(), 1.0)
        self.assertEqual(self._with(180.0).fov_deg(), 180.0)

    def test_it_survives_a_save_and_load(self):
        """It is only useful if it travels with the shot into the library."""
        back = Move.from_json(self._with(63.5).to_json())
        self.assertEqual(back.fov_deg(), 63.5)


class TestPersistedBooleanBoundaries(unittest.TestCase):
    def test_move_flags_do_not_use_python_truthiness(self):
        for field in ("loop", "ping_pong", "route_arcs"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"{field} must be a boolean"):
                    Move.from_dict({field: "false"})

    def test_waypoint_flags_do_not_use_python_truthiness(self):
        base = {"pitch": 90.0, "yaw": 0.0}
        for field in ("flow", "cue"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"{field} must be a boolean"):
                    Waypoint.from_dict({**base, field: "false"})
