"""Live view pipeline: what happens between a reassembled unit and the decoder.

Every stall this project has had lived in these few branches, and none of them
raised. The decoder just quietly returned nothing, which on screen is
indistinguishable from a bad Wi-Fi link -- so it was misdiagnosed as one for a
long time. These tests pin the recovery behaviour instead.
"""

import unittest

from driver import hevc
from driver.liveview import LiveView, STARVATION_LIMIT

START = hevc.START_CODE
VPS = START + bytes([0x40, 1])
SPS = START + bytes([0x42, 1])
PPS = START + bytes([0x44, 1])
IDR = START + bytes([0x28]) + b"idr"      # nal type 20, random access
SLICE = START + bytes([0x02]) + b"p"      # nal type 1, predicted


class TestKeyframeGate(unittest.TestCase):
    def setUp(self):
        self.lv = LiveView()
        self.asked = 0
        self.lv.on_need_keyframe = self._ask

    def _ask(self):
        self.asked += 1

    def drain(self):
        out = []
        while not self.lv._queue.empty():
            out.append(self.lv._queue.get_nowait())
        return out

    def test_waits_for_parameter_sets_and_a_random_access_slice(self):
        self.lv._offer(SLICE)
        self.assertEqual(self.drain(), [], "started on a P-frame")
        self.lv._offer(VPS + SPS + PPS)
        self.assertEqual(self.drain(), [], "started on parameter sets alone")

    def test_starts_on_an_idr_with_the_cached_parameter_sets_prepended(self):
        self.lv._offer(VPS + SPS + PPS)     # own access unit, as this camera sends
        self.lv._offer(IDR)
        out = self.drain()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(VPS + SPS + PPS))
        self.assertTrue(out[0].endswith(IDR))

    def test_asks_the_camera_for_a_keyframe_while_stuck_waiting(self):
        """Joining mid-stream there may be no IDR for minutes.

        The camera emits roughly two IDRs in twelve thousand frames, so simply
        waiting is not a recovery strategy -- it is the stall.
        """
        self.lv._offer(VPS + SPS + PPS)
        for _ in range(STARVATION_LIMIT):
            self.lv._offer(SLICE)
        self.assertEqual(self.asked, 1)
        self.assertEqual(self.drain(), [])


class TestRecoveryDoesNotStrand(unittest.TestCase):
    """The regression that mattered.

    A one-second starvation used to reset started_keyframe, and from then on
    every unit was discarded while the pipeline waited for a random-access
    slice that this camera sends about twice a session. Recovery was the thing
    that broke it, and the counters showed a hard stall rather than the
    unstable link everyone assumed.
    """

    def setUp(self):
        self.lv = LiveView()
        self.lv._offer(VPS + SPS + PPS)
        self.lv._offer(IDR)
        while not self.lv._queue.empty():
            self.lv._queue.get_nowait()
        self.assertTrue(self.lv.started_keyframe)

    def test_a_rebuild_keeps_the_pipeline_fed(self):
        self.lv._replay_params = True       # what the decode thread sets
        self.lv._offer(SLICE)
        out = self.lv._queue.get_nowait()
        self.assertTrue(out.startswith(VPS + SPS + PPS),
                        "the fresh decoder was not reconfigured")
        self.assertTrue(self.lv.started_keyframe,
                        "went back to waiting for an IDR that never comes")

    def test_configuration_is_replayed_once_not_on_every_unit(self):
        self.lv._replay_params = True
        self.lv._offer(SLICE)
        self.lv._queue.get_nowait()
        self.lv._offer(SLICE)
        self.assertEqual(self.lv._queue.get_nowait(), SLICE)

    def test_predicted_frames_keep_flowing_without_any_idr(self):
        for _ in range(200):
            self.lv._offer(SLICE)
        self.assertEqual(self.lv._queue.qsize(),
                         min(200, self.lv._queue.maxsize))


class TestKeyframeRequestPolicy(unittest.TestCase):
    def setUp(self):
        self.lv = LiveView()
        self.asked = 0
        self.lv.on_need_keyframe = lambda: setattr(
            self, "asked", self.asked + 1)

    def test_rate_limited(self):
        self.lv._ask_for_keyframe()
        self.lv._ask_for_keyframe()
        self.assertEqual(self.asked, 1, "a burst of requests would stutter the encoder")

    def test_force_bypasses_the_cooldown(self):
        self.lv._ask_for_keyframe()
        self.lv._ask_for_keyframe(force=True)
        self.assertEqual(self.asked, 2)

    def test_a_raising_callback_never_kills_the_decode_thread(self):
        def boom():
            raise RuntimeError("link is down")
        self.lv.on_need_keyframe = boom
        self.lv._ask_for_keyframe()          # must not propagate
        self.assertEqual(self.lv.keyframe_requests, 1)

    def test_no_callback_is_harmless(self):
        self.lv.on_need_keyframe = None
        self.lv._ask_for_keyframe()
        self.assertEqual(self.lv.keyframe_requests, 0)


if __name__ == "__main__":
    unittest.main()
