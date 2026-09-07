"""Motion timelapse planning.

Everything here is knowable before a four-hour shoot rather than after it,
which is the entire point. A plan that is wrong costs the whole session.
"""

import unittest

from driver import limits, moves, timelapse
from driver.moves import Move, Waypoint
from driver.timelapse import (Plan, TimelapseError, frame_poses,
                              frames_for_playback, plan_for)


def pitch_at(f):
    return moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * f)


def yaw_at(f):
    return moves.wrap180(limits.YAW_ARC.low + limits.YAW_ARC.usable * f)


def a_move(y0=0.30, y1=0.40, easing="linear"):
    return Move(waypoints=[
        Waypoint("a", pitch_at(.5), yaw_at(y0), duration=2.0),
        Waypoint("b", pitch_at(.5), yaw_at(y1), duration=20.0, easing=easing)])


class TestTheArithmetic(unittest.TestCase):
    def test_playback_length_is_frames_over_rate(self):
        p = plan_for(a_move(), frames=250, output_fps=25.0)
        self.assertAlmostEqual(p.playback_s, 10.0)

    def test_shoot_time_counts_every_phase(self):
        p = plan_for(a_move(), frames=100, expose_s=0.5, settle_s=0.3,
                     gap_s=0.2, mode="sms")
        self.assertAlmostEqual(p.per_frame_s, 1.0)
        self.assertAlmostEqual(p.shoot_s, 100.0)

    def test_continuous_pays_no_settle(self):
        """Nothing stops, so there is nothing to wait for."""
        args = dict(frames=100, expose_s=0.5, settle_s=0.3, gap_s=0.2)
        sms = plan_for(a_move(), mode="sms", **args)
        cont = plan_for(a_move(), mode="continuous", **args)
        self.assertAlmostEqual(cont.per_frame_s, 0.7)
        self.assertLess(cont.shoot_s, sms.shoot_s)

    def test_the_speed_ratio_is_reality_over_playback(self):
        p = plan_for(a_move(), frames=250, expose_s=0.5, settle_s=0.3,
                     gap_s=0.2, output_fps=25.0)
        self.assertAlmostEqual(p.speed_ratio, 250.0 / 10.0)

    def test_frames_for_a_wanted_playback_length(self):
        """The direction people actually think in."""
        self.assertEqual(frames_for_playback(8.0, 25.0), 200)
        self.assertEqual(frames_for_playback(10.0, 24.0), 240)

    def test_a_nonsense_playback_length_is_refused(self):
        with self.assertRaises(TimelapseError):
            frames_for_playback(0)


class TestFramePoses(unittest.TestCase):
    def test_there_is_one_pose_per_frame(self):
        self.assertEqual(len(frame_poses(a_move(), 60)), 60)

    def test_the_ends_are_the_ends_of_the_move(self):
        poses = frame_poses(a_move(), 40)
        self.assertAlmostEqual(poses[0][1], yaw_at(.30), places=2)
        self.assertAlmostEqual(poses[-1][1], yaw_at(.40), places=2)

    def test_easing_is_preserved_rather_than_flattened(self):
        """Dividing the ANGLE evenly is the obvious implementation and it
        throws the easing away, turning an eased move into a constant-rate
        glide -- a different shot."""
        eased = frame_poses(a_move(easing="ease-in-out-sine"), 41)
        linear = frame_poses(a_move(easing="linear"), 41)
        mid = 20
        # The eased move is at the same midpoint but got there differently.
        self.assertAlmostEqual(eased[mid][1], linear[mid][1], places=1)
        quarter = 10
        self.assertNotAlmostEqual(eased[quarter][1], linear[quarter][1], places=1)

    def test_a_move_with_one_waypoint_gives_nothing(self):
        self.assertEqual(frame_poses(Move(waypoints=[Waypoint("a", 100.0, 0.0)]), 10), [])

    def test_it_follows_arc_routing(self):
        """A timelapse across the forbidden wedge must step the long way
        round like everything else."""
        m = Move(waypoints=[
            Waypoint("a", pitch_at(.5), yaw_at(.03), duration=2.0),
            Waypoint("b", pitch_at(.5), yaw_at(.97), duration=20.0)])
        for _, yaw in frame_poses(m, 80):
            self.assertTrue(moves.YAW_LIMITS.contains(yaw),
                            f"frame at yaw {yaw:.1f} is outside the travel")


class TestTheWarningsThatSaveAShoot(unittest.TestCase):
    def test_too_few_frames_for_the_travel_will_judder(self):
        """Past about a degree a frame the subject jumps further between
        frames than the eye merges."""
        wide = Move(waypoints=[
            Waypoint("a", pitch_at(.5), yaw_at(.05), duration=2.0),
            Waypoint("b", pitch_at(.5), yaw_at(.95), duration=20.0)])
        p = plan_for(wide, frames=30)
        self.assertGreater(p.worst_per_frame, timelapse.JUDDER_DEG_PER_FRAME)
        self.assertTrue(any("judder" in w for w in p.warnings()), p.warnings())

    def test_enough_frames_does_not_warn_about_judder(self):
        wide = Move(waypoints=[
            Waypoint("a", pitch_at(.5), yaw_at(.05), duration=2.0),
            Waypoint("b", pitch_at(.5), yaw_at(.95), duration=20.0)])
        p = plan_for(wide, frames=600)
        self.assertFalse(any("judder" in w for w in p.warnings()), p.warnings())

    def test_a_crawl_below_the_dead_band_is_flagged(self):
        tiny = Move(waypoints=[
            Waypoint("a", pitch_at(.5), yaw_at(.5), duration=2.0),
            Waypoint("b", pitch_at(.5), yaw_at(.5) + 0.5, duration=20.0)])
        p = plan_for(tiny, frames=2000)
        self.assertTrue(any("dead band" in w for w in p.warnings()), p.warnings())

    def test_a_very_long_shoot_says_so(self):
        p = plan_for(a_move(), frames=5000, expose_s=1.0, settle_s=0.5, gap_s=0.5)
        self.assertTrue(any("battery" in w for w in p.warnings()), p.warnings())

    def test_sms_warns_about_having_no_motion_blur(self):
        self.assertTrue(any("strobe" in w
                            for w in plan_for(a_move(), frames=300).warnings()))

    def test_continuous_does_not(self):
        p = plan_for(a_move(), frames=300, mode="continuous")
        self.assertFalse(any("strobe" in w for w in p.warnings()))

    def test_a_clip_too_short_to_cut_says_so(self):
        p = plan_for(a_move(), frames=20, output_fps=25.0)
        self.assertTrue(any("shorter than most cuts" in w for w in p.warnings()))

    def test_a_sensible_plan_warns_only_about_the_mode(self):
        wide = Move(waypoints=[
            Waypoint("a", pitch_at(.5), yaw_at(.35), duration=2.0),
            Waypoint("b", pitch_at(.5), yaw_at(.55), duration=20.0)])
        p = plan_for(wide, frames=250, mode="continuous", output_fps=25.0)
        self.assertEqual(p.warnings(), [], p.warnings())


class TestRefusals(unittest.TestCase):
    def test_an_unknown_mode(self):
        with self.assertRaises(TimelapseError):
            plan_for(a_move(), frames=10, mode="bracketing")

    def test_too_few_frames(self):
        with self.assertRaises(TimelapseError):
            plan_for(a_move(), frames=1)

    def test_an_absurd_frame_count(self):
        with self.assertRaises(TimelapseError):
            plan_for(a_move(), frames=10 ** 7)

    def test_a_negative_time(self):
        for kw in ("expose_s", "settle_s", "gap_s"):
            with self.subTest(kw=kw):
                with self.assertRaises(TimelapseError):
                    plan_for(a_move(), frames=10, **{kw: -1.0})

    def test_a_move_with_nothing_to_shoot(self):
        with self.assertRaises(TimelapseError):
            plan_for(Move(), frames=10)


class TestSerialisation(unittest.TestCase):
    def test_the_plan_survives_json_shaping(self):
        d = plan_for(a_move(), frames=250).to_dict()
        for key in ("frames", "shoot_s", "playback_s", "speed_ratio",
                    "yaw_per_frame", "warnings"):
            self.assertIn(key, d)
        self.assertIsInstance(d["warnings"], list)


if __name__ == "__main__":
    unittest.main()


class TestSurvivingTheShoot(unittest.TestCase):
    """A four-hour timelapse meets a battery swap or a dropped access point.
    Losing the shoot to either is losing the afternoon."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.move = a_move()
        self.plan = plan_for(self.move, frames=200)

    def _save(self, frame=80, at=1000.0):
        return timelapse.save_progress(self.dir.name, self.move, self.plan,
                                       frame, at)

    def test_progress_round_trips(self):
        self._save(frame=80)
        got = timelapse.load_progress(self.dir.name, self.move)
        self.assertEqual(got["frame"], 80)
        self.assertEqual(got["frames"], 200)
        self.assertTrue(got["matches"])

    def test_nothing_saved_is_none_not_an_error(self):
        self.assertIsNone(timelapse.load_progress(self.dir.name))

    def test_an_unreadable_file_is_none_not_a_crash(self):
        """A startup that fails because of a leftover diagnostic is worse than
        the thing it guards."""
        from pathlib import Path
        Path(self.dir.name, timelapse.STATE_NAME).write_text("{broken",
                                                             encoding="utf-8")
        self.assertIsNone(timelapse.load_progress(self.dir.name))

    def test_an_edited_move_does_not_match(self):
        """Resuming frame 812 of a move that has since been edited steps the
        head somewhere the earlier frames never went, and the join is
        invisible until playback."""
        self._save()
        edited = a_move(y1=0.60)
        self.assertFalse(timelapse.load_progress(self.dir.name, edited)["matches"])

    def test_renaming_a_move_still_matches(self):
        """The fingerprint is of the PATH. A rename must not cost the resume."""
        self._save()
        renamed = a_move()
        renamed.name = "something else"
        renamed.setup = {"mount": "changed"}
        self.assertTrue(timelapse.load_progress(self.dir.name, renamed)["matches"])

    def test_moving_a_waypoint_does_not_match(self):
        self._save()
        nudged = a_move()
        nudged.waypoints[1].pitch += 0.5
        self.assertFalse(timelapse.load_progress(self.dir.name, nudged)["matches"])

    def test_a_stale_resume_is_flagged(self):
        """Twelve hours on, it is a forgotten shoot, not an interrupted one --
        and the scene was struck."""
        self._save(at=0.0)
        fresh = timelapse.load_progress(self.dir.name, self.move, now=60.0)
        old = timelapse.load_progress(self.dir.name, self.move,
                                      now=timelapse.STALE_AFTER_S + 60.0)
        self.assertFalse(fresh["stale"])
        self.assertTrue(old["stale"])

    def test_a_completed_shoot_is_flagged_finished(self):
        """A finished shoot offering to resume itself is a trap the morning
        after."""
        self._save(frame=200)
        self.assertTrue(timelapse.load_progress(self.dir.name)["finished"])

    def test_clearing_removes_it(self):
        self._save()
        timelapse.clear_progress(self.dir.name)
        self.assertIsNone(timelapse.load_progress(self.dir.name))

    def test_clearing_nothing_is_not_an_error(self):
        timelapse.clear_progress(self.dir.name)

    def test_the_write_leaves_no_temporary_behind(self):
        from pathlib import Path
        self._save()
        self.assertEqual(list(Path(self.dir.name).glob("*.tmp")), [])


class TestResumingWhereItStopped(unittest.TestCase):
    def test_the_remainder_is_the_tail_of_the_original_plan(self):
        """Re-sampling over what is left would redistribute the frames and
        every remaining position would land somewhere the plan never intended
        -- on playback, a speed change halfway through."""
        move, plan = a_move(), plan_for(a_move(), frames=100)
        full = timelapse.frame_poses(move, plan.frames)
        rest = timelapse.remaining_poses(move, plan, done=40)
        self.assertEqual(len(rest), 60)
        for a, b in zip(rest, full[40:]):
            self.assertAlmostEqual(a[1], b[1], places=9)

    def test_resuming_from_zero_is_the_whole_move(self):
        move, plan = a_move(), plan_for(a_move(), frames=50)
        self.assertEqual(len(timelapse.remaining_poses(move, plan, 0)), 50)

    def test_resuming_past_the_end_is_empty_not_negative(self):
        move, plan = a_move(), plan_for(a_move(), frames=50)
        self.assertEqual(timelapse.remaining_poses(move, plan, 999), [])
