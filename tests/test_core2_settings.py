"""What the settings sub-pages on the box need from the host.

The Core2 grew a menu with a Wi-Fi page and two light toggles. Most of that is
firmware and is checked structurally in test_firmware_source, but three things
cross the wire and can break silently from this side:

* the new buttons must be in the accepted-action set, or every press is dropped
  and logged as unknown while the box happily pretends it worked;
* RECONNECT has to actually reach connect();
* the Wi-Fi page can only report what the host tells it. If the SSID is not
  pushed, the page shows "not reported" for ever and looks broken.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import server
from driver import core2


def make_args(**over):
    base = dict(ssid="OsmoPocket4-TEST-CAMERA", password="pw", skip_ble=True,
                skip_wifi_join=True, wifi_interface=None, host="192.168.2.1",
                gain=1.0, pin="osmo", name=None, scan_timeout=1.0,
                kp=2.0, no_live_view=True, no_core2=True, imu_port=None)
    base.update(over)
    return SimpleNamespace(**base)


class TestTheBoxCanBeHeard(unittest.TestCase):
    """core2.ACTIONS is a whitelist. Anything missing is silently discarded."""

    def test_every_new_settings_button_is_accepted(self):
        for name in ("reconnect", "limitleds_0", "limitleds_1",
                     "beacon_0", "beacon_1"):
            with self.subTest(action=name):
                self.assertIn(name, core2.ACTIONS)

    def test_an_unknown_action_is_still_refused(self):
        """The whitelist is only worth having if it still says no."""
        self.assertNotIn("format_the_sd_card", core2.ACTIONS)


class TestReconnectButton(unittest.TestCase):
    def test_it_reaches_connect(self):
        session = server.CameraSession(make_args())
        with mock.patch.object(session, "connect") as connect:
            session._core2_action("reconnect")
        connect.assert_called_once()

    def test_a_light_toggle_does_not_touch_the_camera(self):
        """Those are the box's own lights. Reaching for the camera because
        someone dimmed an LED would be absurd -- and on a failed connect it
        would throw."""
        session = server.CameraSession(make_args())
        with mock.patch.object(session, "connect") as connect:
            for name in ("limitleds_0", "beacon_1"):
                session._core2_action(name)
        connect.assert_not_called()

    def test_a_refused_action_never_escapes(self):
        """_core2_action runs on the serial reader thread. An exception there
        would take the link down over a button press.

        connect() is stubbed to throw rather than left to fail against a real
        camera: the live version spawns a daemon thread that outlives the
        test, so the socket it leaked surfaced as a ResourceWarning charged to
        whatever unrelated line was running when the GC got to it.
        """
        session = server.CameraSession(make_args())
        with mock.patch.object(session, "connect",
                               side_effect=RuntimeError("no camera")):
            session._core2_action("reconnect")


class TestTheWifiPageHasSomethingToShow(unittest.TestCase):
    """The box cannot see the network, so the page is only as good as the feed."""

    def _run_feed_once(self, ssid):
        """Drive the real feed loop for one tick against a fake box."""
        session = server.CameraSession(make_args())
        session.ssid = ssid
        sent = {}

        class Box:
            def send_state(self, **kv):
                sent.update(kv)
                session.core2 = None            # one tick is enough
            def send_attitude(self, *a): pass

        session.core2 = Box()
        with mock.patch.object(server, "CORE2_FEED_PERIOD_S", 0.001):
            session._core2_feed()
        return sent

    def test_the_real_feed_pushes_the_ssid(self):
        sent = self._run_feed_once("OsmoPocket4-TEST-CAMERA")
        self.assertEqual(sent.get("ssid"), "OsmoPocket4-TEST-CAMERA")

    def test_no_ssid_arrives_as_an_empty_string(self):
        """The box prints whatever it is given. "None" on a Wi-Fi page reads as
        a network called None, not as missing information."""
        sent = self._run_feed_once(None)
        self.assertEqual(sent.get("ssid"), "")

    def test_the_per_direction_limits_reach_the_side_bars(self):
        """The LED bars are driven from these four, not from the single worst
        figure -- that is the whole point of using two bars."""
        sent = self._run_feed_once("x")
        for key in ("ptl", "pth", "ywl", "ywh"):
            self.assertIn(key, sent, f"the side bars never see {key}")

    def test_host_owned_control_settings_are_echoed_back_to_the_box(self):
        session = server.CameraSession(make_args())
        session.clutch.set_gain("fast")
        session.speed_preset = "fine"
        session.invert_tilt = True
        session.invert_pan = False
        sent = {}

        class Box:
            def send_state(self, **kv):
                sent.update(kv)
                session.core2 = None

            def send_attitude(self, *a):
                pass

        session.core2 = Box()
        with mock.patch.object(server, "CORE2_FEED_PERIOD_S", 0.001):
            session._core2_feed()
        self.assertEqual(sent["gain"], "fast")
        self.assertEqual(sent["speed"], "fine")
        self.assertTrue(sent["invt"])
        self.assertFalse(sent["invp"])


if __name__ == "__main__":
    unittest.main()


class TestTimelapseReachesTheBox(unittest.TestCase):
    """A timelapse runs unattended for hours. The operator walks over to the
    box to ask whether it is still going -- so the box has to know."""

    def _feed_once(self, tl_state):
        session = server.CameraSession(make_args())
        session.tl_state = tl_state
        sent = {}

        class Box:
            def send_state(self, **kv):
                sent.update(kv)
                session.core2 = None
            def send_attitude(self, *a): pass

        session.core2 = Box()
        with mock.patch.object(server, "CORE2_FEED_PERIOD_S", 0.001):
            session._core2_feed()
        return sent

    def test_a_running_timelapse_sends_its_progress(self):
        sent = self._feed_once({"running": True, "frame": 47, "frames": 200,
                                "error": None, "plan": None})
        self.assertEqual(sent.get("tlf"), 47)
        self.assertEqual(sent.get("tln"), 200)

    def test_nothing_running_sends_zero_not_a_stale_count(self):
        """The box reads zero as 'show the page name'. A leftover count would
        leave TL 200/200 on the screen for the rest of the day."""
        sent = self._feed_once({"running": False, "frame": 200, "frames": 200,
                                "error": None, "plan": None})
        self.assertEqual(sent.get("tlf"), 0)
        self.assertEqual(sent.get("tln"), 0)


class TestRunCues(unittest.TestCase):
    """The crew has to know the head is about to move without looking at a
    screen. A cue that fires on every tick is one they learn to ignore."""

    def _session(self):
        s = server.CameraSession(make_args())
        sent = []

        class Box:
            def send_state(self, **kv):
                sent.append(kv)
            def send_attitude(self, *a): pass

        s.core2 = Box()
        s.sent = sent
        return s

    def _cues(self, s):
        return [kv["rcue"] for kv in s.sent if "rcue" in kv]

    def test_arming_cues_once_not_every_tick(self):
        s = self._session()
        for _ in range(5):
            s._cue_edges(armed=True, moving=False)
        self.assertEqual(self._cues(s), ["arm"])

    def test_a_move_cues_go_then_end(self):
        s = self._session()
        s._cue_edges(True, False)
        for _ in range(4):
            s._cue_edges(True, True)
        s._cue_edges(True, False)
        self.assertEqual(self._cues(s), ["arm", "go", "end"])

    def test_end_only_fires_after_a_move_actually_ran(self):
        """Otherwise disarming without playing anything beeps a finish."""
        s = self._session()
        s._cue_edges(False, False)
        s._cue_edges(True, False)
        self.assertNotIn("end", self._cues(s))

    def test_an_unknown_cue_name_is_not_sent(self):
        """The box ignores it, but sending guesswork down the wire invites
        someone to add a matching guess at the other end."""
        s = self._session()
        s.cue("fanfare")
        self.assertEqual(self._cues(s), [])

    def test_every_documented_cue_is_accepted(self):
        s = self._session()
        for name in ("arm", "count", "go", "end"):
            s.cue(name)
        self.assertEqual(self._cues(s), ["arm", "count", "go", "end"])

    def test_no_box_is_not_an_error(self):
        """Cues are a nicety; losing the Core2 must not break a take."""
        s = server.CameraSession(make_args())
        s.core2 = None
        s.cue("go")

    def test_a_box_that_throws_does_not_kill_the_feed(self):
        s = server.CameraSession(make_args())

        class Angry:
            def send_state(self, **kv):
                raise OSError("cable pulled")

        s.core2 = Angry()
        s.cue("go")
