"""Pure continuous-timelapse timing and recovery guards."""

import json
import tempfile
import unittest
from pathlib import Path

from driver import moves, timelapse


def move(*, loop=False, ping_pong=False, cue=False):
    return moves.Move(loop=loop, ping_pong=ping_pong, waypoints=[
        moves.Waypoint("start", 0, 0, duration=3, dwell=1, zoom=.2,
                       zoom_easing="linear"),
        moves.Waypoint("middle", 10, 20, duration=6, dwell=2, zoom=.5,
                       zoom_easing="ease-in", flow=True),
        moves.Waypoint("finish", 20, 40, duration=9, dwell=3, zoom=.8,
                       zoom_easing="ease-out", cue=cue),
    ])


class TestContinuousPlanMove(unittest.TestCase):
    def test_retimes_the_canonical_move_without_mutating_it(self):
        source = move()
        original = source.to_dict()
        plan = timelapse.plan_for(source, frames=11, expose_s=.3, gap_s=.2,
                                  mode="continuous")

        got = timelapse.continuous_plan_move(source, plan)

        interval = .5
        self.assertIsNot(got, source)
        self.assertEqual(source.to_dict(), original)
        self.assertAlmostEqual(got.total_duration, 11 * interval)
        self.assertAlmostEqual(got.total_duration - got.waypoints[-1].dwell,
                               10 * interval)
        self.assertAlmostEqual(got.waypoints[-1].dwell, interval)
        self.assertAlmostEqual(got.waypoints[1].dwell, 2 * (5 / 18))
        self.assertEqual([w.zoom_easing for w in got.waypoints],
                         [w.zoom_easing for w in source.waypoints])
        self.assertEqual([w.flow for w in got.waypoints],
                         [w.flow for w in source.waypoints])

    def test_refuses_ambiguous_or_impossible_continuous_paths(self):
        for kwargs in ({"loop": True}, {"ping_pong": True}, {"cue": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(timelapse.TimelapseError):
                timelapse.plan_for(move(**kwargs), frames=10,
                                   mode="continuous")

        with self.assertRaises(timelapse.TimelapseError):
            timelapse.plan_for(move(), frames=10, expose_s=.1, gap_s=.09,
                               mode="continuous")

    def test_plan_for_has_a_strict_bounded_numeric_boundary(self):
        source = move()
        for frames in (True, 2.0, float("nan"), 1, timelapse.MAX_FRAMES + 1):
            with self.subTest(frames=frames), self.assertRaises(timelapse.TimelapseError):
                timelapse.plan_for(source, frames=frames)
        for field in ("expose_s", "settle_s", "gap_s", "output_fps"):
            with self.subTest(field=field), self.assertRaises(timelapse.TimelapseError):
                timelapse.plan_for(source, frames=10, **{field: float("nan")})
        with self.assertRaises(timelapse.TimelapseError):
            timelapse.plan_for(source, frames=timelapse.MAX_FRAMES,
                               expose_s=10, gap_s=10)


class TestContinuousRecoveryValidation(unittest.TestCase):
    def test_fingerprint_includes_zoom_easing_and_cue(self):
        base = move()
        changed_easing = move()
        changed_easing.waypoints[1].zoom_easing = "ease-out"
        changed_cue = move()
        changed_cue.waypoints[1].cue = True
        self.assertNotEqual(timelapse.fingerprint(base),
                            timelapse.fingerprint(changed_easing))
        self.assertNotEqual(timelapse.fingerprint(base),
                            timelapse.fingerprint(changed_cue))

    def test_malformed_persisted_values_are_not_resume_candidates(self):
        source = move()
        plan = timelapse.plan_for(source, frames=10)
        with tempfile.TemporaryDirectory() as directory:
            path = timelapse.save_progress(directory, source, plan, 2, 5.0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            for key, value in (("frame", True), ("frames", 2.0),
                               ("at", float("nan"))):
                with self.subTest(key=key):
                    corrupted = dict(payload)
                    corrupted[key] = value
                    Path(directory, timelapse.STATE_NAME).write_text(
                        json.dumps(corrupted), encoding="utf-8")
                    self.assertIsNone(timelapse.load_progress(directory, source))


if __name__ == "__main__":
    unittest.main()
