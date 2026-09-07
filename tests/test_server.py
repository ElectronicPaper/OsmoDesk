"""Control-panel session logic.

No sockets and no HTTP server: `CameraSession` is driven directly with stubs.
The connect worker is called synchronously rather than through its thread so
the failure paths are deterministic.
"""

import unittest
import io
import pathlib
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import server
from driver import core2, library, lut, moves, response
from driver import response, shaping
from driver import commands, duml


def make_args(**over):
    base = dict(ssid="OsmoPocket4-TEST-CAMERA", password="pw", skip_ble=True,
                skip_wifi_join=True, wifi_interface=None, host="192.168.2.1",
                gain=1.0, pin="osmo", name=None, scan_timeout=1.0,
                kp=2.0, no_live_view=True, no_core2=True, imu_port=None)
    base.update(over)
    return SimpleNamespace(**base)


def fake_runner(running=False, **over):
    """A stand-in with every attribute `status()` reads.

    Built once rather than as an ad-hoc SimpleNamespace per test: five
    hand-rolled doubles each miss a different field, and the miss only shows
    up when some unrelated test happens to call status().
    """
    from driver.gimbal import MoveReport
    base = dict(running=running, start=lambda m: None, stop=lambda **k: None,
                elapsed=0.0, progress=0.0, error_pitch=0.0, error_yaw=0.0,
                clamped=False, waiting_cue=None, report=MoveReport())
    base.update(over)
    return SimpleNamespace(**base)


class FakeLink:
    def __init__(self):
        self.frames = []
        self.session_id = 0xABCD
        self.cam_channel = 0x1234
        self.video_packets = 7
        self.attitude = None
        self.power = None

    def send_frame(self, frame):
        self.frames.append(frame)

    def close(self):
        self.closed = True


class FakeStick:
    def __init__(self):
        self.calls = []
        self.axes = None
        self.rates = None
        self.speed_cap = None
        self.ramp = None
        self.axis_stability = None

    def _rec(self, name):
        return lambda: self.calls.append(name)

    def __getattr__(self, name):
        if name in ("recenter", "flip", "follow_mode", "fpv_mode", "stop", "start"):
            return self._rec(name)
        raise AttributeError(name)

    def set_axes(self, tilt, pan):
        self.axes = (tilt, pan)

    def set_rate(self, tilt_dps, pan_dps):
        self.rates = (tilt_dps, pan_dps)
        self.axes = (tilt_dps, pan_dps)

    def set_speed_cap(self, dps):
        self.speed_cap = dps

    def set_ramp(self, name):
        self.ramp = name

    def set_axis_stability(self, tilt="balanced", pan="balanced"):
        self.axis_stability = (tilt, pan)

    def wire_deflections(self, pitch_dps, yaw_dps):
        from driver import response
        from driver.gimbal import TILT_SIGN, YAW_SIGN
        return (response.deflection_for_rate(TILT_SIGN * pitch_dps),
                response.deflection_for_rate(YAW_SIGN * yaw_dps))

    def abort(self):
        # Distinct from release(): no settle, nothing left in the filter. A
        # fake that treated them alike would hide a rig that coasts through
        # an emergency stop.
        self.calls.append("abort")
        self.axes = (0.0, 0.0)
        self.rates = (0.0, 0.0)

    def release(self):
        # The real GimbalStick.release() zeroes the axes; a fake that only
        # records the call would hide a stick left deflected after a STOP.
        self.calls.append("release")
        self.axes = (0.0, 0.0)


class TestNewCameraCommands(unittest.TestCase):
    """Frame shape only. These opcodes are NOT verified on hardware yet; the
    tests exist so the encoding cannot drift before it is."""

    def test_record_start_stop(self):
        start, stop = commands.record(True), commands.record(False)
        for f in (start, stop):
            self.assertEqual(f.opcode, (0x02, 0x02))
            self.assertEqual(f.receiver, duml.RX_CAMERA)
            self.assertEqual(f.flags, duml.FLAG_REQUEST)
        self.assertEqual(start.payload, b"\x01")
        self.assertEqual(stop.payload, b"\x00")

    def test_photo(self):
        f = commands.photo()
        self.assertEqual(f.opcode, (0x02, 0x01))
        self.assertEqual(f.receiver, duml.RX_CAMERA)
        self.assertEqual(f.payload, b"\x01")

    def test_both_encode_and_decode(self):
        for f in (commands.record(True), commands.record(False), commands.photo()):
            got = duml.decode(duml.encode(f))
            self.assertIsNotNone(got)
            self.assertEqual(got[0].opcode, f.opcode)


class TestSessionStatus(unittest.TestCase):
    def test_idle_shape(self):
        s = server.CameraSession(make_args())
        st = s.status()
        self.assertEqual(st["state"], "idle")
        self.assertIsNone(st["error"])
        self.assertEqual(st["host"], "192.168.2.1")
        # No link yet, so no telemetry keys to mislead the UI.
        self.assertNotIn("pitch", st)

    def test_connected_shape_without_telemetry(self):
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        st = s.status()
        self.assertEqual(st["session"], "0xABCD")
        self.assertEqual(st["channel"], "0x1234")
        self.assertEqual(st["video_packets"], 7)
        self.assertFalse(st["telemetry"])
        self.assertIsNone(st["pitch"])

    def test_connected_shape_with_telemetry(self):
        s = server.CameraSession(make_args())
        link = FakeLink()
        link.attitude = commands.GimbalAttitude(
            pitch=-12.5, yaw=3.0, yaw_alt=-3.0, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        s.link, s.stick, s.state = link, FakeStick(), "connected"
        st = s.status()
        self.assertTrue(st["telemetry"])
        self.assertAlmostEqual(st["pitch"], -12.5)
        self.assertEqual(st["quaternion"], [0.0, 1.0, 0.0, 0.0])

    def test_status_is_json_serialisable(self):
        import json
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        json.dumps(s.status())


class TestPowerInStatus(unittest.TestCase):
    def test_power_absent_until_pushed(self):
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        self.assertNotIn("power", s.status())

    def test_power_surfaces_when_present(self):
        from driver import camera
        s = server.CameraSession(make_args())
        link = FakeLink()
        link.power = camera.PowerStatus(percent=80, charging=True, milliamps=-500)
        s.link, s.stick, s.state = link, FakeStick(), "connected"
        st = s.status()
        self.assertEqual(st["power"]["percent"], 80)
        self.assertTrue(st["power"]["charging"])


class TestTakeLog(unittest.TestCase):
    """A take log is the reason a slate exists: what was shot, and which one
    the director wants."""

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_logging_increments_the_take_number(self):
        self.assertEqual(self.s.slate["take"], 1)
        first = self.s.log_take("wide")
        self.assertEqual(first["take"], 1)
        self.assertEqual(self.s.slate["take"], 2)
        self.assertEqual(self.s.log_take("again")["take"], 2)

    def test_entry_carries_slate_and_note(self):
        self.s.set_slate("14", "C", 3)
        e = self.s.log_take("soft focus", circled=True)
        self.assertEqual((e["scene"], e["shot"], e["take"]), ("14", "C", 3))
        self.assertEqual(e["note"], "soft focus")
        self.assertTrue(e["circled"])

    def test_entry_records_the_move_that_was_run(self):
        from driver import moves
        self.s.move = moves.Move(name="slow-reveal", waypoints=[
            moves.Waypoint("A", 0, 0), moves.Waypoint("B", 10, 10, duration=4.0)])
        e = self.s.log_take()
        self.assertEqual(e["move"], "slow-reveal")
        self.assertAlmostEqual(e["duration"], 4.0)

    def test_circle_toggles_the_last_take(self):
        self.s.log_take("one")
        self.assertFalse(self.s.takes[-1]["circled"])
        self.s.circle_last_take()
        self.assertTrue(self.s.takes[-1]["circled"])
        self.s.circle_last_take()
        self.assertFalse(self.s.takes[-1]["circled"])

    def test_circle_with_no_takes_raises(self):
        with self.assertRaises(RuntimeError):
            self.s.circle_last_take()

    def test_slate_take_floors_at_one(self):
        self.s.set_slate(None, None, -4)
        self.assertEqual(self.s.slate["take"], 1)

    def test_status_exposes_recent_takes(self):
        for i in range(20):
            self.s.log_take(f"t{i}")
        self.s.state = "connected"
        st = self.s.status()
        self.assertEqual(len(st["takes"]), 12)   # tail only, keeps status small
        self.assertEqual(st["slate"]["take"], 21)
        # The tail is addressable: every take carries the id compare and
        # export use, so a client holding the tail cannot point at the wrong
        # take by position. 20 takes, tail shows ids 8..19.
        self.assertEqual(st["take_count"], 20)
        self.assertEqual([t["id"] for t in st["takes"]], list(range(8, 20)))
        self.assertEqual([t["id"] for t in self.s.takes], list(range(20)))


class TestCameraDispatch(unittest.TestCase):
    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_each_setting_emits_its_opcode(self):
        for what, value, opcode in (
            ("iso", "400", (0x02, 0x2A)),
            ("shutter", 50, (0x02, 0x28)),
            ("manual", True, (0x02, 0x1E)),
            ("wb", 5600, (0x02, 0x2C)),
            ("color", "d-log", (0x02, 0x42)),
            ("zoom", 3.0, (0x02, 0xB8)),
            ("focus_continuous", True, (0x02, 0x24)),
        ):
            with self.subTest(setting=what):
                self.s.link.frames.clear()
                self.s.camera_set(what, value)
                self.assertEqual(self.s.link.frames[0].opcode, opcode)

    def test_resolution(self):
        self.s.camera_resolution("4K", "50")
        f = self.s.link.frames[0]
        self.assertEqual(f.opcode, (0x02, 0x18))
        self.assertEqual(f.payload[:2], bytes([0x10, 0x05]))

    def test_unknown_setting_raises(self):
        with self.assertRaises(ValueError):
            self.s.camera_set("aperture", 2.8)

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            self.s.camera_set("iso", "999999")
        with self.assertRaises(ValueError):
            self.s.camera_set("color", "sepia")

    def test_camera_boolean_settings_reject_truthy_non_booleans(self):
        for setting in ("manual", "focus_continuous"):
            for value in ("false", "true", 0, 1, None, []):
                with self.subTest(setting=setting, value=value):
                    with self.assertRaisesRegex(ValueError, "must be a boolean"):
                        self.s.camera_set(setting, value)


class TestSessionGuards(unittest.TestCase):
    def test_require_raises_when_not_connected(self):
        s = server.CameraSession(make_args())
        with self.assertRaises(RuntimeError):
            s.require()
        with self.assertRaises(RuntimeError):
            s.set_axes(0.5, 0.0)
        with self.assertRaises(RuntimeError):
            s.action("recenter")

    def test_set_axes_reaches_the_stick(self):
        s = server.CameraSession(make_args())
        s.link, s.stick = FakeLink(), FakeStick()
        s.set_axes(0.25, -0.5)
        self.assertEqual(s.stick.axes, (0.25, -0.5))


class TestActionDispatch(unittest.TestCase):
    """A typo in the dispatch chain silently does nothing, or the wrong thing."""

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_stick_actions(self):
        for name, expect in (("recenter", "recenter"), ("flip", "flip"),
                             ("follow", "follow_mode"), ("fpv", "fpv_mode")):
            with self.subTest(name=name):
                self.s.stick.calls.clear()
                self.s.action(name)
                self.assertEqual(self.s.stick.calls, [expect])

    def test_frame_actions(self):
        for name, opcode, payload in (
            ("record_start", (0x02, 0x02), b"\x01"),
            ("record_stop", (0x02, 0x02), b"\x00"),
            ("photo", (0x02, 0x01), b"\x01"),
            ("live_view", (0x09, 0xA8), None),
            ("params", (0x04, 0x50), b"\x01\x04\x05"),
        ):
            with self.subTest(name=name):
                self.s.link.frames.clear()
                self.s.action(name)
                self.assertEqual(len(self.s.link.frames), 1)
                frame = self.s.link.frames[0]
                self.assertEqual(frame.opcode, opcode)
                if payload is not None:
                    self.assertEqual(frame.payload, payload)

    def test_unknown_action_raises(self):
        with self.assertRaises(ValueError):
            self.s.action("launch_missiles")
        with self.assertRaises(ValueError):
            self.s.action("")

    def test_every_button_in_the_page_has_a_handler(self):
        # The page and the dispatch must not drift apart: a button whose action
        # is not handled looks fine and does nothing.
        import re
        from pathlib import Path
        html = (Path(server.__file__).parent / "web" / "index.html").read_text("utf-8")
        for name in sorted(set(re.findall(r'data-action="([^"]+)"', html))):
            with self.subTest(action=name):
                self.s.action(name)  # must not raise


class TestConnectFailureIsReported(unittest.TestCase):
    """Regression: a failing connect must land in "error", never hang.

    obtain_credentials raises SystemExit when no camera is found. SystemExit
    derives from BaseException, so an `except Exception` here killed the worker
    thread silently and the UI sat on "connecting" indefinitely.
    """

    def _run_with(self, exc):
        s = server.CameraSession(make_args(skip_ble=False))
        original = server.obtain_credentials

        async def boom(_args):
            raise exc

        server.obtain_credentials = boom
        # The worker logs the traceback by design; keep it out of test output.
        server.log.disabled = True
        try:
            s._connect_worker()
        finally:
            server.obtain_credentials = original
            server.log.disabled = False
        return s

    def test_system_exit_is_caught(self):
        s = self._run_with(SystemExit("no DJI Osmo camera found"))
        self.assertEqual(s.state, "error")
        self.assertIn("no DJI Osmo camera found", s.error)

    def test_ordinary_exception_is_caught(self):
        s = self._run_with(TimeoutError("did not associate"))
        self.assertEqual(s.state, "error")
        self.assertIn("TimeoutError", s.error)

    def test_error_state_clears_the_stage(self):
        s = self._run_with(SystemExit("nope"))
        self.assertEqual(s.stage, "")

    def test_failed_connect_leaves_no_half_session(self):
        s = self._run_with(SystemExit("nope"))
        self.assertIsNone(s.link)
        self.assertIsNone(s.stick)
        with self.assertRaises(RuntimeError):
            s.require()


if __name__ == "__main__":
    unittest.main()


class TestSpeedPreset(unittest.TestCase):
    """Top speed is chosen by name and resolved to deg/s on this side.

    The Core2 sends only the name, so the two ends cannot drift apart into
    disagreeing about what "fast" means.
    """

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_each_preset_reaches_the_stick(self):
        for preset, want in response.SPEED_CAPS.items():
            with self.subTest(preset=preset):
                self.s.set_speed(preset)
                self.assertEqual(self.s.stick.speed_cap, want)
                self.assertEqual(self.s.speed_preset, preset)

    def test_an_unknown_preset_falls_back_rather_than_raising(self):
        """A typo from a client must not leave the operator with no control."""
        self.s.set_speed("ludicrous")
        self.assertEqual(self.s.speed_preset, "normal")
        self.assertEqual(self.s.stick.speed_cap, response.SPEED_CAPS["normal"])

    def test_it_survives_having_no_stick_yet(self):
        s = server.CameraSession(make_args())          # not connected
        s.set_speed("fast")
        self.assertEqual(s.speed_preset, "fast")

    def test_a_reconnect_keeps_the_chosen_speed(self):
        """Otherwise every reconnection silently returns the operator to
        normal, which is the sort of thing noticed only mid-take."""
        self.s.set_speed("fine")
        stick = FakeStick()
        stick.set_speed_cap(
            response.SPEED_CAPS.get(self.s.speed_preset,
                                    response.SPEED_CAPS["normal"]))
        self.assertEqual(stick.speed_cap, response.SPEED_CAPS["fine"])


class TestCore2Actions(unittest.TestCase):
    """The box sends short strings; this is the whole vocabulary.

    Glue like this fails silently -- an earlier version matched the remainder
    of a line instead of its first token and read every clutch grab as a
    release -- so each verb is pinned.
    """

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_speed_names_map_to_presets(self):
        for sent, want in (("speed_SLOW", "fine"), ("speed_NORMAL", "normal"),
                           ("speed_FAST", "fast")):
            with self.subTest(sent=sent):
                self.s._core2_action(sent)
                self.assertEqual(self.s.speed_preset, want)

    def test_an_unknown_speed_name_is_survivable(self):
        self.s._core2_action("speed_WARP")
        self.assertEqual(self.s.speed_preset, "normal")

    def test_invert_toggles_carry_their_state(self):
        self.s._core2_action("invert_tilt_1")
        self.assertTrue(self.s.invert_tilt)
        self.s._core2_action("invert_tilt_0")
        self.assertFalse(self.s.invert_tilt)
        self.s._core2_action("invert_pan_1")
        self.assertTrue(self.s.invert_pan)
        self.s._core2_action("invert_pan_0")
        self.assertFalse(self.s.invert_pan)

    def test_tilt_and_pan_inversion_are_independent(self):
        self.s._core2_action("invert_tilt_1")
        self.assertFalse(self.s.invert_pan)

    def test_axis_response_actions_are_independent_and_preserve_legacy_gain(self):
        self.s._core2_action("tilt_response_FINE")
        self.s._core2_action("pan_response_DIRECT")
        self.s.clutch.engage(0.0, 90.0, 0.0)
        pitch, _ = self.s.clutch.target(20.0)
        self.assertAlmostEqual(pitch - 90.0, 5.0, places=3)
        tilt_rate, pan_rate = self.s.clutch.feedforward(20.0, 20.0)
        self.assertAlmostEqual(tilt_rate, 5.0, places=3)
        self.assertAlmostEqual(pan_rate, 20.0, places=3)

    def test_axis_stability_actions_reach_the_host_stick_independently(self):
        self.s._core2_action("tilt_stability_QUIET")
        self.assertEqual(self.s.tilt_stability, "quiet")
        self.assertEqual(self.s.stick.axis_stability, ("quiet", "balanced"))
        self.s._core2_action("pan_stability_RESPONSIVE")
        self.assertEqual(self.s.pan_stability, "responsive")
        self.assertEqual(self.s.stick.axis_stability, ("quiet", "responsive"))

    def test_unknown_axis_tune_action_is_refused_without_changing_the_pair(self):
        self.s._core2_action("tilt_stability_QUIET")
        self.s._core2_action("pan_stability_WARP")
        self.assertEqual((self.s.tilt_stability, self.s.pan_stability),
                         ("quiet", "balanced"))

    def test_profile_selection_still_works(self):
        self.s._core2_action("profile_UPRIGHT")
        self.assertEqual(self.s.imu_profile, "UPRIGHT")

    def test_an_unrecognised_action_is_ignored_not_fatal(self):
        self.s._core2_action("nonsense_verb")      # must not raise

    def test_all_of_it_reaches_the_status_payload(self):
        """The browser panel reads these, so they have to be published."""
        self.s._core2_action("speed_FAST")
        self.s._core2_action("invert_pan_1")
        out = self.s.status()
        self.assertEqual(out["speed_preset"], "fast")
        self.assertEqual(out["invert"], {"tilt": False, "pan": True})
        self.assertIn("axis_tune", out)
        self.assertEqual(out["axis_tune"]["tilt_response"], "normal")
        self.assertEqual(out["axis_tune"]["pan_response"], "normal")

    def test_emitted_settings_actions_cross_the_serial_filter_end_to_end(self):
        """A CameraSession branch is no protection if Core2Link drops B first."""
        link = core2.Core2Link(port="COM-TEST")
        link.on_action = self.s._core2_action
        for action, preset, tilt, pan in (
            ("speed_SLOW", "fine", False, False),
            ("speed_NORMAL", "normal", False, False),
            ("speed_FAST", "fast", False, False),
            ("invert_tilt_1", "fast", True, False),
            ("invert_tilt_0", "fast", False, False),
            ("invert_pan_1", "fast", False, True),
            ("invert_pan_0", "fast", False, False),
        ):
            with self.subTest(action=action):
                link._handle("B " + action)
                self.assertEqual(self.s.speed_preset, preset)
                self.assertEqual(self.s.invert_tilt, tilt)
                self.assertEqual(self.s.invert_pan, pan)

        for action in (
            "tilt_response_FINE", "tilt_response_BALANCED", "tilt_response_DIRECT",
            "pan_response_FINE", "pan_response_BALANCED", "pan_response_DIRECT",
            "tilt_stability_QUIET", "tilt_stability_BALANCED", "tilt_stability_RESPONSIVE",
            "pan_stability_QUIET", "pan_stability_BALANCED", "pan_stability_RESPONSIVE",
        ):
            with self.subTest(action=action):
                link._handle("B " + action)


class TestHandlerInputBoundaries(unittest.TestCase):
    def setUp(self):
        self.old_token = server.Handler.token
        server.Handler.token = "correct-token"

    def tearDown(self):
        server.Handler.token = self.old_token

    def _handler(self, *, path="/api/stick", headers=None, body=b"",
                 address="192.0.2.20"):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.headers = headers or {}
        handler.rfile = io.BytesIO(body)
        handler.client_address = (address, 12345)
        return handler

    def _post(self, path, payload, session):
        raw = server.json.dumps(payload).encode("utf-8")
        handler = self._handler(
            path=path, headers={"Content-Length": str(len(raw)),
                                "X-Osmo-Token": "correct-token"}, body=raw)
        handler.session = session
        replies = []
        handler._json = lambda value, code=200: replies.append((code, value))
        handler.do_POST()
        self.assertTrue(replies, f"{path} produced no JSON response")
        return replies[-1]

    def test_body_is_bounded_and_requires_a_json_object(self):
        oversized = self._handler(headers={"Content-Length": str(server.MAX_JSON_BODY_BYTES + 1)})
        with self.assertRaisesRegex(ValueError, "exceeds"):
            oversized._body()
        for payload in (b"[]", b"true", b'"string"', b"42"):
            handler = self._handler(headers={"Content-Length": str(len(payload))}, body=payload)
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "object"):
                    handler._body()

    def test_body_parses_a_small_json_object(self):
        payload = b'{"tilt": 0.5}'
        handler = self._handler(headers={"Content-Length": str(len(payload))}, body=payload)
        self.assertEqual(handler._body(), {"tilt": 0.5})

    def test_tokens_must_be_exact_query_or_cookie_values(self):
        for path, headers, allowed in (
            ("/api/stick?t=correct-token", {}, True),
            ("/api/stick?t=wrongcorrect-token", {}, False),
            ("/api/stick?note=t=correct-token", {}, False),
            ("/api/stick?t=correct-token-extra", {}, False),
            ("/api/stick", {"Cookie": "osmo_token=correct-token"}, True),
            ("/api/stick", {"Cookie": "other=1; osmo_token=wrongcorrect-token"}, False),
            ("/api/stick", {"Cookie": "note=osmo_token=correct-token"}, False),
        ):
            with self.subTest(path=path, headers=headers):
                self.assertEqual(self._handler(path=path, headers=headers)._authorised(), allowed)

    def test_control_flags_reject_truthy_non_booleans_at_the_routes(self):
        calls = []

        def called(*args, **kwargs):
            calls.append((args, kwargs))
            return {}

        session = SimpleNamespace(
            log_take=called, capture_waypoint=called,
            timelapse_start=called, clear_lut=called, load_lut=called,
            play_segment=called, play=called,
            clutch=SimpleNamespace(set_locks=called),
        )
        cases = (
            ("/api/take", {"circled": "false"}),
            ("/api/waypoint", {"flow": "false"}),
            ("/api/timelapse/start", {"resume": "false"}),
            ("/api/lut", {"clear": "false"}),
            ("/api/clutch/lock", {"tilt": "false"}),
            ("/api/move/segment", {"force": "false"}),
            ("/api/move/play", {"force": "false"}),
        )
        for path, payload in cases:
            with self.subTest(path=path):
                calls.clear()
                code, response = self._post(path, payload, session)
                self.assertEqual(code, 400)
                self.assertIn("must be a boolean", response["error"])
                self.assertEqual(calls, [], "invalid input reached the control layer")


class TestCameraStateIsARequestNotAReading(unittest.TestCase):
    """The monitor shows exposure, and it must not present belief as fact.

    Nothing in the implemented protocol reads the camera's exposure or tally
    back. Every value here is the last thing this panel asked for, and the
    status payload has to say so -- a requested ISO displayed as a measured one
    is how a take gets shot two stops off.
    """

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_nothing_is_claimed_before_anything_is_set(self):
        cam = self.s.status()["camera"]
        self.assertFalse(cam["reported"])
        self.assertEqual({k: v for k, v in cam.items() if k != "reported"}, {})

    def test_a_setting_is_remembered_after_it_is_sent(self):
        self.s.camera_set("iso", 800)
        self.assertEqual(self.s.status()["camera"]["iso"], 800)

    def test_the_payload_always_admits_it_is_unreported(self):
        self.s.camera_set("iso", 800)
        self.s.camera_set("wb", 5500)
        self.assertFalse(self.s.status()["camera"]["reported"])

    def test_a_rejected_setting_is_not_remembered(self):
        """Otherwise the panel would show a value the camera never received."""
        with self.assertRaises(ValueError):
            self.s.camera_set("nonsense", 1)
        self.assertNotIn("nonsense", self.s.status()["camera"])

    def test_resolution_and_fps_are_kept_together(self):
        self.s.camera_resolution("4K", "25")
        cam = self.s.status()["camera"]
        self.assertEqual((cam["resolution"], cam["fps"]), ("4K", "25"))


class TestRecordingIsBelief(unittest.TestCase):
    """The record opcode is unverified on this camera and no message reports
    the tally back. The monitor draws a dashed amber border instead of a solid
    red one on the strength of `reported`, so this flag decides whether the
    operator is told the truth about whether they are rolling."""

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_starts_not_recording_and_not_claiming_to_know(self):
        r = self.s.status()["recording"]
        self.assertFalse(r["on"])
        self.assertFalse(r["reported"])

    def test_a_record_command_sets_belief_only(self):
        self.s.action("record_start")
        r = self.s.status()["recording"]
        self.assertTrue(r["on"])
        self.assertFalse(r["reported"], "nothing confirms the tally")
        self.assertIsNotNone(r["since"])

    def test_stopping_clears_the_start_time(self):
        self.s.action("record_start")
        self.s.action("record_stop")
        r = self.s.status()["recording"]
        self.assertFalse(r["on"])
        self.assertIsNone(r["since"])

    def test_the_command_still_reaches_the_camera(self):
        """The bookkeeping must not have replaced the actual send."""
        before = len(self.s.link.frames)
        self.s.action("record_start")
        self.assertGreater(len(self.s.link.frames), before)


class TestStickDoubleMatchesTheRealThing(unittest.TestCase):
    """Hand-written doubles drift, and the drift is silent until a call lands.

    This has now happened three times in this project: set_rate, set_speed_cap
    and abort were each added to GimbalStick and each broke a suite that had no
    idea it was pretending to be an older class.
    """

    def test_the_fake_implements_every_public_stick_method(self):
        from driver.gimbal import GimbalStick
        real = {n for n in vars(GimbalStick) if not n.startswith("_")
                and callable(getattr(GimbalStick, n))}
        fake = FakeStick()
        missing = sorted(n for n in real if not hasattr(fake, n))
        self.assertEqual(missing, [],
                         f"FakeStick is behind GimbalStick: {missing}")


class TestAxisStabilityPlumbing(unittest.TestCase):
    def test_named_factors_have_balanced_neutral_and_ordered_dynamics(self):
        self.assertEqual(shaping.STABILITY_FACTORS,
                         {"quiet": 0.55, "balanced": 1.0,
                          "responsive": 1.6})
        quiet = shaping.MotionShaper("fluid")
        responsive = shaping.MotionShaper("fluid")
        quiet.set_axis_stability(0.55, 1.6)
        responsive.set_axis_stability(1.6, 0.55)
        for _ in range(2):
            q_pitch, q_yaw = quiet.update(42.0, 42.0, 0.04)
            r_pitch, r_yaw = responsive.update(42.0, 42.0, 0.04)
        self.assertLess(q_pitch, r_pitch)
        self.assertGreater(q_yaw, r_yaw)

    def test_stick_validates_names_and_maps_tilt_pan_to_numeric_shapers(self):
        from driver.gimbal import GimbalStick
        stick = GimbalStick(FakeLink())
        stick.set_axis_stability("quiet", "responsive")
        self.assertAlmostEqual(stick.shaper.pitch.stability_factor, 0.55)
        self.assertAlmostEqual(stick.shaper.yaw.stability_factor, 1.6)
        with self.assertRaises(ValueError):
            stick.set_axis_stability("warp", "balanced")


class TestEasePresetPlumbing(unittest.TestCase):
    """The preset is chosen in the UI and applied on the stick, so the two
    ends have to agree about the names and the payload."""

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_each_preset_reaches_the_stick(self):
        for name in shaping.RAMPS:
            with self.subTest(preset=name):
                self.s.set_ramp(name)
                self.assertEqual(self.s.stick.ramp, name)

    def test_an_unknown_preset_falls_back_rather_than_raising(self):
        """A typo from a client must never leave the operator without a stick."""
        self.s.set_ramp("buttery")
        self.assertEqual(self.s.ramp, shaping.DEFAULT_RAMP)

    def test_it_survives_having_no_stick_yet(self):
        s = server.CameraSession(make_args())
        s.set_ramp("glide")
        self.assertEqual(s.ramp, "glide")

    def test_the_payload_carries_the_list_the_monitor_renders(self):
        out = self.s.status()
        self.assertEqual(out["ramp"], shaping.DEFAULT_RAMP)
        names = [r["name"] for r in out["ramps"]]
        self.assertEqual(names, list(shaping.RAMPS))
        for r in out["ramps"]:
            self.assertTrue(r["blurb"], f"{r['name']} has no explanation")
            self.assertIsInstance(r["stop_time"], float)

    def test_stop_time_is_what_the_operator_needs_to_know(self):
        """How long the head keeps moving after they let go -- and a longer
        preset must actually report a longer number."""
        by = {r["name"]: r["stop_time"] for r in self.s.status()["ramps"]}
        self.assertGreater(by["glide"], by["news"])


class TestEmergencyStopHasNoTail(unittest.TestCase):
    """The whole point of the shaping is that the head keeps moving after a
    release. A stop is the one moment that must not be true."""

    def setUp(self):
        self.s = server.CameraSession(make_args())
        self.s.link, self.s.stick = FakeLink(), FakeStick()

    def test_stop_everything_aborts_rather_than_releasing(self):
        self.s.stop_everything("test")
        self.assertIn("abort", self.s.stick.calls)
        self.assertNotIn("release", self.s.stick.calls)

    def test_the_stick_is_left_at_a_standstill(self):
        self.s.stop_everything("test")
        self.assertEqual(self.s.stick.axes, (0.0, 0.0))

    def test_ownership_is_surrendered_too(self):
        self.s.owner = "phone"
        self.s.stop_everything("test")
        self.assertEqual(self.s.owner, "none")


class TestCaptureByDemonstration(unittest.TestCase):
    """Author a node by driving there and pressing capture.

    The angles are measured off telemetry. The zoom is not -- nothing on this
    camera reports its zoom back over any command implemented here -- so the
    node can only carry what the controller last asked for.
    """

    def _session(self, pitch=-12.5, yaw=3.0):
        s = server.CameraSession(make_args())
        link = FakeLink()
        link.attitude = commands.GimbalAttitude(
            pitch=pitch, yaw=yaw, yaw_alt=-yaw, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        s.link, s.stick, s.state = link, FakeStick(), "connected"
        return s

    def test_it_captures_the_measured_attitude(self):
        wp = self._session(pitch=-12.5, yaw=3.0).capture_waypoint()
        self.assertAlmostEqual(wp["pitch"], -12.5)
        self.assertAlmostEqual(wp["yaw"], 3.0)

    def test_it_refuses_without_telemetry(self):
        """Capturing a position the rig cannot see would write a node that
        looks authoritative and points nowhere."""
        s = self._session()
        s.link.attitude = None
        with self.assertRaises(RuntimeError):
            s.capture_waypoint()

    def test_zoom_is_unset_when_the_controller_never_asked_for_one(self):
        """Not 0.0. An invented wide would rack the lens out between two
        framings that were captured to match."""
        self.assertIsNone(self._session().capture_waypoint()["zoom"])

    def test_zoom_comes_from_the_last_requested_value(self):
        s = self._session()
        s.camera_state["zoom"] = 0.6
        self.assertAlmostEqual(s.capture_waypoint()["zoom"], 0.6)

    def test_an_explicit_zoom_overrides_the_remembered_one(self):
        s = self._session()
        s.camera_state["zoom"] = 0.6
        self.assertAlmostEqual(s.capture_waypoint(zoom=0.25)["zoom"], 0.25)

    def test_a_node_can_be_marked_pass_through(self):
        s = self._session()
        s.capture_waypoint()
        s.capture_waypoint(flow=True)
        self.assertFalse(s.move.waypoints[0].flow)
        self.assertTrue(s.move.waypoints[1].flow)
        self.assertTrue(s.move.uses_flow)

    def test_nodes_are_named_in_order_and_accumulate(self):
        s = self._session()
        first = s.capture_waypoint()
        second = s.capture_waypoint()
        self.assertEqual([first["name"], second["name"]], ["A", "B"])
        self.assertEqual(len(s.move.waypoints), 2)

    def test_a_captured_move_is_immediately_playable(self):
        """Two demonstrated nodes with a flow mark should sample without the
        caller having to fix anything up by hand."""
        s = self._session(pitch=0.0, yaw=0.0)
        s.capture_waypoint()
        s.link.attitude = commands.GimbalAttitude(
            pitch=0.0, yaw=20.0, yaw_alt=-20.0, timestamp=2,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        s.capture_waypoint(flow=True)
        s.link.attitude = commands.GimbalAttitude(
            pitch=0.0, yaw=40.0, yaw_alt=-40.0, timestamp=3,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        s.capture_waypoint()
        mid = s.move.sample(s.move.total_duration / 2)
        self.assertIsNotNone(mid)
        self.assertGreater(mid[1], 0.0)
        self.assertLess(mid[1], 40.0)


class TestMonitorLut(unittest.TestCase):
    """A viewing look. It changes what the operator sees and nothing the
    camera records, and the session has to keep that distinction straight."""

    CUBE = ("TITLE \"test look\"\nLUT_3D_SIZE 2\n"
            + "\n".join(f"{r} {g} {b}"
                        for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n")

    def test_no_lut_is_loaded_to_begin_with(self):
        info = server.CameraSession(make_args()).lut_info()
        self.assertFalse(info["loaded"])
        self.assertEqual(info["size"], 0)

    def test_loading_a_cube_reports_what_was_loaded(self):
        s = server.CameraSession(make_args())
        info = s.load_lut(self.CUBE)
        self.assertTrue(info["loaded"])
        self.assertEqual(info["size"], 2)
        self.assertEqual(info["title"], "test look")

    def test_an_explicit_name_beats_the_files_own_title(self):
        """The operator's name for a look is the one on their shot list."""
        s = server.CameraSession(make_args())
        self.assertEqual(s.load_lut(self.CUBE, name="Look A")["name"], "Look A")

    def test_a_broken_cube_is_refused_and_leaves_the_old_look_alone(self):
        """Half-applying a look is worse than refusing it: the operator would
        be grading against a table that is part one look and part another."""
        s = server.CameraSession(make_args())
        s.load_lut(self.CUBE, name="good")
        with self.assertRaises(lut.LutError):
            s.load_lut("LUT_3D_SIZE 2\n0 0 0\n")
        self.assertEqual(s.lut_info()["name"], "good")

    def test_clearing_returns_to_no_look(self):
        s = server.CameraSession(make_args())
        s.load_lut(self.CUBE)
        self.assertFalse(s.clear_lut()["loaded"])
        self.assertIsNone(s.lut)

    def test_the_table_is_not_carried_by_lut_info(self):
        """A 33-cube is 107k floats. It has no business in a poll response."""
        s = server.CameraSession(make_args())
        s.load_lut(self.CUBE)
        self.assertNotIn("data", s.lut_info())

    def test_the_loaded_table_survives_to_the_viewer_intact(self):
        s = server.CameraSession(make_args())
        s.load_lut(self.CUBE)
        self.assertEqual(len(s.lut.to_dict()["data"]), 2 ** 3 * 3)

    def test_a_look_does_not_touch_the_camera(self):
        """If a viewing LUT ever sent a frame to the camera it would stop
        being a viewing LUT."""
        s = server.CameraSession(make_args())
        link = FakeLink()
        s.link, s.stick, s.state = link, FakeStick(), "connected"
        s.load_lut(self.CUBE)
        s.clear_lut()
        self.assertEqual(link.frames, [])


class TestShutterAngle(unittest.TestCase):
    """The camera takes a speed; the operator thinks in angles. The angle has
    to follow the frame rate or the motion look drifts with it."""

    def _connected(self):
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        return s

    def test_180_degrees_becomes_the_right_speed_for_the_rate(self):
        for fps, denom in (("24", 48), ("25", 50), ("30", 60)):
            with self.subTest(fps=fps):
                s = self._connected()
                s.camera_state["fps"] = fps
                s.set_shutter_angle(180)
                self.assertEqual(s.camera_state["shutter"], denom)

    def test_it_actually_sends_a_frame_to_the_camera(self):
        """An angle the operator sets and the camera never hears is worse than
        no control at all."""
        s = self._connected()
        s.set_shutter_angle(180)
        self.assertEqual(len(s.link.frames), 1)

    def test_the_rate_is_marked_assumed_when_it_was_never_set(self):
        """Nothing reports the frame rate back, so a shutter angle rests on an
        assumption and has to say so."""
        info = self._connected().shutter_info()
        self.assertFalse(info["fps_reported"])
        self.assertEqual(info["fps"], 24.0)

    def test_the_rate_is_marked_reported_once_it_has_been_set(self):
        s = self._connected()
        s.camera_resolution("4K", "25")
        self.assertTrue(s.shutter_info()["fps_reported"])
        self.assertEqual(s.shutter_info()["fps"], 25.0)

    def test_no_shutter_yet_reports_no_denominator_rather_than_guessing(self):
        self.assertIsNone(self._connected().shutter_info()["denominator"])

    def test_the_flicker_verdict_follows_the_supply(self):
        """1/60 is two whole half-cycles of a 60 Hz supply and 1.67 of a 50 Hz
        one. Same shutter, different verdict -- which is the whole point.

        Note 1/48 would NOT work as the example: it bands on both supplies
        (2.083 half-cycles at 50 Hz, 2.5 at 60), which is the trap for anyone
        shooting 24fps at 180 degrees under practicals.
        """
        s = self._connected()
        s.camera_state["fps"] = "30"
        s.set_shutter_angle(180)                 # 1/60 at 30fps
        self.assertEqual(s.camera_state["shutter"], 60)
        self.assertTrue(s.shutter_info()["flickers"])
        s.set_mains(60)
        self.assertFalse(s.shutter_info()["flickers"])

    def test_changing_the_supply_sends_nothing_to_the_camera(self):
        """Which mains the lights are on is not the camera's business."""
        s = self._connected()
        s.set_mains(60)
        self.assertEqual(s.link.frames, [])

    def test_an_impossible_supply_is_refused(self):
        with self.assertRaises(ValueError):
            self._connected().set_mains(400)

    def test_the_safe_list_is_offered_alongside(self):
        """Nothing faster than 1/100 is safe on a 50 Hz supply: below one
        half-cycle there is no whole number of pulses to land on."""
        s = self._connected()
        s.set_mains(50)
        self.assertEqual(s.shutter_info()["safe"], [25, 50, 100])


class TestShotLibrary(unittest.TestCase):
    """A move you can recall by name next week is what separates a
    motion-control rig from a jog box."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.s = server.CameraSession(make_args())
        self.s.library = library.Library(self.dir.name)
        self.s.move = moves.Move(name="untitled", waypoints=[
            moves.Waypoint("a", 0.0, 0.0, duration=2.0),
            moves.Waypoint("b", 5.0, 30.0, duration=2.0, flow=True),
            moves.Waypoint("c", 5.0, 60.0, duration=2.0),
        ], setup={"mount": "tripod"})

    def test_saving_then_listing_finds_it(self):
        self.s.save_move("Scene 4")
        self.assertEqual([m["name"] for m in self.s.list_moves()], ["Scene 4"])

    def test_saving_renames_the_move_being_edited(self):
        """Otherwise the panel keeps saying 'untitled' for a move that has a
        name on disk."""
        self.s.save_move("Scene 4")
        self.assertEqual(self.s.move.name, "Scene 4")

    def test_loading_replaces_the_move_and_returns_it(self):
        self.s.save_move("Scene 4")
        self.s.move = moves.Move(name="scratch")
        out = self.s.load_move("Scene 4")
        self.assertEqual(len(out["waypoints"]), 3)
        self.assertEqual(self.s.move.name, "Scene 4")

    def test_flow_marks_and_setup_survive_the_round_trip(self):
        self.s.save_move("Scene 4")
        self.s.move = moves.Move(name="scratch")
        self.s.load_move("Scene 4")
        self.assertTrue(self.s.move.uses_flow)
        self.assertEqual(self.s.move.setup["mount"], "tripod")

    def test_loading_is_refused_while_a_move_is_running(self):
        """Swapping the path under a running take would have the rig finish a
        move it never started."""
        self.s.runner = fake_runner(running=True)
        with self.assertRaises(RuntimeError):
            self.s.load_move("Scene 4")

    def test_loading_is_allowed_once_it_has_stopped(self):
        self.s.save_move("Scene 4")
        self.s.runner = fake_runner()
        self.s.load_move("Scene 4")

    def test_deleting_reports_whether_anything_went(self):
        self.s.save_move("Scene 4")
        self.assertTrue(self.s.delete_move("Scene 4"))
        self.assertFalse(self.s.delete_move("Scene 4"))

    def test_a_traversal_name_stays_inside_the_library(self):
        self.s.save_move("../escaped")
        listed = [m["stem"] for m in self.s.list_moves()]
        self.assertEqual(listed, ["escaped"])
        self.assertEqual(list(Path(self.dir.name).parent.glob("escaped*")), [])

    def test_saving_a_stub_move_is_refused(self):
        self.s.move = moves.Move(waypoints=[moves.Waypoint("a", 0.0, 0.0)])
        with self.assertRaises(library.LibraryError):
            self.s.save_move("stub")

    def test_the_listing_carries_what_a_picker_needs(self):
        self.s.save_move("Scene 4")
        m = self.s.list_moves()[0]
        for key in ("name", "stem", "waypoints", "duration", "setup", "saved_at"):
            self.assertIn(key, m)


class TestPreflight(unittest.TestCase):
    """Reports, does not block. The runner already clamps pitch, so a move
    outside the arc arrives somewhere other than authored rather than
    crashing -- this says where, before the take."""

    def _session(self):
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        return s

    def _inside(self, frac):
        from driver import limits
        return (moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * frac),
                moves.wrap180(limits.YAW_ARC.low + limits.YAW_ARC.usable * frac))

    def _gentle(self):
        p0, y0 = self._inside(.4)
        p1, y1 = self._inside(.6)
        return moves.Move(waypoints=[
            moves.Waypoint("a", p0, y0, duration=2.0),
            moves.Waypoint("b", p1, y1, duration=30.0)])

    def test_a_gentle_move_passes_at_the_selected_preset(self):
        s = self._session()
        s.move = self._gentle()
        self.assertTrue(s.preflight()["ok"], s.preflight())

    def test_it_checks_against_the_selected_sensitivity(self):
        """'The head could do this at full throw' is not the useful question
        when the operator is shooting on fine."""
        s = self._session()
        s.move = self._gentle()
        s.speed_preset = "fine"
        self.assertEqual(s.preflight()["max_dps"], response.SPEED_CAPS["fine"])
        s.speed_preset = "fast"
        self.assertEqual(s.preflight()["max_dps"], response.SPEED_CAPS["fast"])

    def test_full_throw_can_be_asked_for_explicitly(self):
        s = self._session()
        s.move = self._gentle()
        s.speed_preset = "fine"
        self.assertEqual(s.preflight(at_preset=False)["max_dps"],
                         response.MAX_DPS)

    def test_it_names_the_preset_it_judged_against(self):
        s = self._session()
        s.move = self._gentle()
        s.speed_preset = "fine"
        self.assertEqual(s.preflight()["speed_preset"], "fine")

    def test_a_move_outside_the_arc_is_reported(self):
        from driver import limits
        s = self._session()
        p0, y0 = self._inside(.5)
        s.move = moves.Move(waypoints=[
            moves.Waypoint("a", p0, y0, duration=2.0),
            moves.Waypoint("b", moves.wrap180(limits.PITCH_ARC.high + 25.0),
                           y0, duration=20.0)])
        out = s.preflight()
        self.assertFalse(out["ok"])
        self.assertTrue(any(f["kind"] == "travel" for f in out["findings"]))

    def test_it_never_blocks_play(self):
        """Reporting is the contract. A pre-flight that refused to let an
        operator shoot would be worked around within a day."""
        s = self._session()
        s.armed = True
        s.runner = fake_runner()
        s.move = self._gentle()
        s.play()          # must not raise on a move with findings or without

    def test_an_empty_move_is_not_a_failure(self):
        s = self._session()
        s.move = moves.Move()
        self.assertTrue(s.preflight()["ok"])

    def test_it_sends_nothing_to_the_camera(self):
        s = self._session()
        s.move = self._gentle()
        s.preflight()
        self.assertEqual(s.link.frames, [])


class TestMovePath(unittest.TestCase):
    """The panel draws a map of the move. It asks the host for the samples
    rather than computing them again in JavaScript -- a second implementation
    of easing, dwell and arc routing drifts, and then the map shows a move
    that will not be played."""

    def _session(self, n=3):
        s = server.CameraSession(make_args())
        from driver import limits
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(waypoints=[
            moves.Waypoint(f"n{i}",
                           pitch=p,
                           yaw=moves.wrap180(limits.YAW_ARC.low
                                             + limits.YAW_ARC.usable * (.2 + .2 * i)),
                           duration=2.0)
            for i in range(n)])
        return s

    def test_it_returns_points_along_the_move(self):
        out = self._session().move_path()
        self.assertGreater(len(out["points"]), 100)
        self.assertEqual(len(out["points"][0]), 2)

    def test_the_first_and_last_points_are_the_end_waypoints(self):
        s = self._session()
        out = s.move_path()
        self.assertAlmostEqual(out["points"][0][1], s.move.waypoints[0].yaw, places=1)
        self.assertAlmostEqual(out["points"][-1][1], s.move.waypoints[-1].yaw, places=1)

    def test_an_empty_move_gives_no_points_rather_than_failing(self):
        s = server.CameraSession(make_args())
        s.move = moves.Move()
        self.assertEqual(s.move_path()["points"], [])
        self.assertEqual(s.move_path()["duration"], 0.0)

    def test_the_sample_count_is_bounded(self):
        """An unbounded count from a query string is a way to make the host
        chew CPU on request."""
        s = self._session()
        self.assertLessEqual(len(s.move_path(samples=10 ** 9)["points"]), 2001)
        self.assertGreaterEqual(len(s.move_path(samples=0)["points"]), 3)

    def test_it_follows_the_arc_routing_the_runner_will_use(self):
        """The whole reason for one sampler: routed and unrouted paths differ,
        and the map has to show the one that will actually play."""
        from driver import limits
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        wps = [moves.Waypoint("a", p, moves.wrap180(limits.YAW_ARC.low + 8.0),
                              duration=2.0),
               moves.Waypoint("b", p,
                              moves.wrap180(limits.YAW_ARC.low
                                            + limits.YAW_ARC.usable - 8.0),
                              duration=20.0)]
        s = server.CameraSession(make_args())
        s.move = moves.Move(waypoints=list(wps))
        routed = s.move_path()["points"]
        s.move = moves.Move(waypoints=list(wps), route_arcs=False)
        short = s.move_path()["points"]
        self.assertNotAlmostEqual(routed[len(routed) // 2][1],
                                  short[len(short) // 2][1], places=1)


class TestTimelapseGuards(unittest.TestCase):
    """A timelapse drives the head unattended for hours. Every reason not to
    start one has to be checked before the first hop, not discovered at
    frame 400."""

    def _ready(self):
        from driver import limits
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        s.runner = fake_runner()
        s.armed = True
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(waypoints=[
            moves.Waypoint("a", p, moves.wrap180(limits.YAW_ARC.low + 40), duration=2.0),
            moves.Waypoint("b", p, moves.wrap180(limits.YAW_ARC.low + 80), duration=20.0)])
        return s

    def test_planning_starts_nothing(self):
        """The numbers are the point: an operator has to see the cost before
        committing an afternoon."""
        s = self._ready()
        plan = s.timelapse_plan(frames=100)
        self.assertGreater(plan["shoot_s"], 0)
        self.assertFalse(s.tl_state["running"])
        self.assertEqual(s.link.frames, [])

    def test_it_refuses_when_not_armed(self):
        s = self._ready()
        s.armed = False
        with self.assertRaises(RuntimeError):
            s.timelapse_start(frames=10)

    def test_it_refuses_while_the_clutch_is_held(self):
        s = self._ready()
        s.clutch.state.engaged = True
        with self.assertRaises(RuntimeError):
            s.timelapse_start(frames=10)

    def test_it_refuses_while_a_move_is_running(self):
        s = self._ready()
        s.runner = fake_runner(running=True)
        with self.assertRaises(RuntimeError):
            s.timelapse_start(frames=10)

    def test_it_refuses_a_second_timelapse(self):
        s = self._ready()
        s.tl_state = dict(s.tl_state, running=True)
        with self.assertRaises(RuntimeError):
            s.timelapse_start(frames=10)

    def test_a_bad_plan_is_refused_before_anything_moves(self):
        s = self._ready()
        with self.assertRaises(Exception):
            s.timelapse_start(frames=1)
        self.assertFalse(s.tl_state["running"])

    def test_stop_is_answered_between_frames_not_after_one(self):
        """_sleep_or_stop waits on the event. A plain sleep would make the
        stop button take a whole frame interval to answer, which on a slow
        timelapse is minutes."""
        s = self._ready()
        s._tl_stop.set()
        import time as _t
        began = _t.monotonic()
        self.assertTrue(s._sleep_or_stop(30.0))
        self.assertLess(_t.monotonic() - began, 1.0)

    def test_the_status_carries_progress(self):
        s = self._ready()
        st = s.status()
        self.assertIn("timelapse", st)
        for key in ("running", "frame", "frames"):
            self.assertIn(key, st["timelapse"])


class TestTimelapseArgumentWhitelist(unittest.TestCase):
    """The body is operator input and plan_for takes keyword arguments, so
    forwarding it wholesale would let a caller name any parameter."""

    def test_only_known_fields_pass(self):
        out = server._timelapse_args(
            {"frames": 200, "mode": "continuous", "expose_s": 0.4,
             "nonsense": 1, "move": "hack"})
        self.assertEqual(set(out), {"frames", "mode", "expose_s"})

    def test_a_wanted_playback_length_becomes_frames(self):
        """The direction people think in: 'eight seconds', not '200 frames'."""
        out = server._timelapse_args({"playback_s": 8.0, "output_fps": 25.0})
        self.assertEqual(out["frames"], 200)

    def test_an_explicit_frame_count_wins(self):
        out = server._timelapse_args({"frames": 33, "playback_s": 8.0})
        self.assertEqual(out["frames"], 33)

    def test_types_are_coerced_not_trusted(self):
        out = server._timelapse_args({"frames": "150", "expose_s": "0.25"})
        self.assertIsInstance(out["frames"], int)
        self.assertIsInstance(out["expose_s"], float)

class TestTheFakeRunnerKeepsUp(unittest.TestCase):
    """The same drift that bit FakeStick: a double that misses a field only
    fails when some unrelated test happens to read it."""

    def test_it_carries_every_attribute_status_reads(self):
        import re
        src = pathlib.Path(server.__file__).read_text(encoding="utf-8")
        block = src[src.index('"total_duration"'):src.index('out["limits"]')]
        needed = set(re.findall(r"r\.(\w+)", block))
        fake = fake_runner()
        for name in sorted(needed):
            with self.subTest(attr=name):
                self.assertTrue(hasattr(fake, name),
                                f"fake_runner has no .{name}, which status() reads")



class TestComparingTakes(unittest.TestCase):
    """The question motion control exists to answer, asked of the take log."""

    def _with_takes(self, *drifts):
        s = server.CameraSession(make_args())
        for n, drift in enumerate(drifts, start=1):
            trace = None if drift is None else [
                [i * 0.04, 100.0, i * 0.5 + drift] for i in range(80)]
            s.takes.append({
                "scene": "12", "shot": "A", "take": n, "note": "",
                "circled": False, "move": "push", "duration": 3.2,
                "setup": "", "at": "10:0%d:00" % n,
                "motion": None if trace is None else {"trace": trace},
            })
        return s

    def test_two_matching_takes_are_compositable(self):
        out = self._with_takes(0.0, 0.0).compare_takes(0, 1)
        self.assertEqual(out["verdict"], "matched")
        self.assertTrue(out["compositable"])

    def test_a_drifted_take_is_not(self):
        out = self._with_takes(0.0, 2.0).compare_takes(0, 1)
        self.assertEqual(out["verdict"], "no match")

    def test_the_result_names_the_takes(self):
        out = self._with_takes(0.0, 0.0).compare_takes(0, 1)
        self.assertEqual(out["takes"], ["12A/1", "12A/2"])

    def test_the_lens_can_be_stated(self):
        """The same angular error is a different number of pixels on a
        different lens."""
        s = self._with_takes(0.0, 0.04)
        self.assertEqual(s.compare_takes(0, 1, fov_deg=84.0)["verdict"], "matched")
        self.assertNotEqual(s.compare_takes(0, 1, fov_deg=10.0)["verdict"], "matched")

    def test_the_assumed_field_of_view_is_flagged(self):
        self.assertTrue(self._with_takes(0.0, 0.0).compare_takes(0, 1)["fov_assumed"])

    def test_a_take_with_no_trace_says_why(self):
        s = self._with_takes(0.0, None)
        with self.assertRaises(ValueError) as caught:
            s.compare_takes(0, 1)
        self.assertIn("no motion trace", str(caught.exception))

    def test_comparing_a_take_with_itself_is_refused(self):
        """It always matches, so the answer is worthless and misleading."""
        with self.assertRaises(ValueError):
            self._with_takes(0.0, 0.0).compare_takes(1, 1)

    def test_an_index_off_the_end_is_refused(self):
        with self.assertRaises(ValueError):
            self._with_takes(0.0, 0.0).compare_takes(0, 9)


class TestCensusIsAttached(unittest.TestCase):
    """The census is worthless if it is not actually listening. The driver
    subscribes to five camera status streams and decodes none of them; this
    is the only thing that will ever read them."""

    def test_the_session_has_one(self):
        s = server.CameraSession(make_args())
        self.assertEqual(s.census_report()["frames"], 0)

    def test_it_counts_frames_handed_to_it(self):
        s = server.CameraSession(make_args())
        f = SimpleNamespace(cmd_set=0x02, cmd_id=0x10, payload=b"\x01")
        s.census.note(f)
        self.assertEqual(s.census_report()["frames"], 1)

    def test_the_report_hides_what_we_already_decode(self):
        s = server.CameraSession(make_args())
        s.census.note(SimpleNamespace(cmd_set=0x04, cmd_id=0x05, payload=b"\x00"))
        s.census.note(SimpleNamespace(cmd_set=0x02, cmd_id=0x10, payload=b"\x00"))
        self.assertEqual([r["opcode"] for r in s.census_report()["rows"]],
                         ["0x02/0x10"])
        self.assertEqual(len(s.census_report(unknown_only=False)["rows"]), 2)

    def test_marks_segment_the_capture(self):
        s = server.CameraSession(make_args())
        out = s.census_mark("record started")
        self.assertEqual(out["marks"][0]["label"], "record started")

    def test_reset_clears_it(self):
        s = server.CameraSession(make_args())
        s.census.note(SimpleNamespace(cmd_set=1, cmd_id=1, payload=b""))
        self.assertEqual(s.census_reset()["frames"], 0)

    def test_it_is_hooked_up_before_the_link_opens(self):
        """Attached before open() so the subscription pushes that arrive
        during registration are counted -- those are the frames most likely
        to carry record state and card space."""
        src = pathlib.Path(server.__file__).read_text(encoding="utf-8")
        hook = src.index("link.on_frame = self.census.note")
        opened = src.index("link.open()", src.index("link = Datalink("))
        self.assertLess(hook, opened,
                        "the census is attached after open(); the registration "
                        "pushes are missed")


class TestBetweenTakesApi(unittest.TestCase):
    def _ready(self):
        from driver import limits
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        s.runner = fake_runner()
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(waypoints=[
            moves.Waypoint("a", p, 10.0, duration=2.0, dwell=1.0),
            moves.Waypoint("b", p, 40.0, duration=4.0)])
        s.link.attitude = commands.GimbalAttitude(
            pitch=p, yaw=10.0, yaw_alt=-10.0, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        return s

    def test_retime_by_factor(self):
        s = self._ready()
        before = s.move.total_duration
        out = s.retime_move(factor=2.0)
        self.assertAlmostEqual(out["total_duration"], before * 2, places=2)

    def test_retime_to_a_target(self):
        s = self._ready()
        self.assertAlmostEqual(s.retime_move(total=9.0)["total_duration"], 9.0,
                               places=2)

    def test_offset_shifts_the_stored_move(self):
        s = self._ready()
        before = s.move.waypoints[0].yaw
        s.offset_move(yaw=5.0)
        self.assertAlmostEqual(s.move.waypoints[0].yaw, before + 5.0)

    def test_re_reference_puts_the_start_where_the_head_is(self):
        s = self._ready()
        s.link.attitude = commands.GimbalAttitude(
            pitch=120.0, yaw=-30.0, yaw_alt=30.0, timestamp=2,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        s.reference_move_here()
        self.assertAlmostEqual(s.move.waypoints[0].pitch, 120.0)
        self.assertAlmostEqual(s.move.waypoints[0].yaw, -30.0)

    def test_re_reference_needs_telemetry(self):
        s = self._ready()
        s.link.attitude = None
        with self.assertRaises(RuntimeError):
            s.reference_move_here()

    def test_the_start_check_is_in_the_status(self):
        s = self._ready()
        chk = s.status()["start_check"]
        self.assertTrue(chk["known"])
        self.assertTrue(chk["at_start"])

    def test_the_start_check_reports_the_error_when_off(self):
        s = self._ready()
        s.link.attitude = commands.GimbalAttitude(
            pitch=s.move.waypoints[0].pitch + 3.0, yaw=10.0, yaw_alt=-10.0,
            timestamp=2, quaternion=(0.0, 1.0, 0.0, 0.0))
        chk = s.status()["start_check"]
        self.assertFalse(chk["at_start"])
        self.assertAlmostEqual(chk["tilt_error"], 3.0, places=1)

    def test_back_to_one_needs_a_move(self):
        s = self._ready()
        s.move = moves.Move()
        with self.assertRaises(RuntimeError):
            s.back_to_one()


class TestPathExportApi(unittest.TestCase):
    def _with_take(self, points=60):
        s = server.CameraSession(make_args())
        s.slate = {"scene": "12", "shot": "A", "take": 3}
        s.takes.append({
            "scene": "12", "shot": "A", "take": 3, "note": "", "circled": False,
            "move": "push", "duration": 2.4, "setup": "", "at": "10:00:00",
            "motion": {"trace": [[i * 0.04, 100.0, i * 0.5]
                                 for i in range(points)]},
        })
        return s

    def test_csv_export_of_the_latest_take(self):
        out = self._with_take().export_path()
        self.assertEqual(out["format"], "csv")
        self.assertIn("tilt_deg,pan_deg", out["body"])
        self.assertIn("MEASURED", out["body"])

    def test_chan_export_is_a_bare_table(self):
        out = self._with_take().export_path(fmt="chan")
        first = out["body"].splitlines()[0].split()
        self.assertEqual(first[0], "1")
        self.assertEqual(len(first), 7)

    def test_the_filename_carries_the_slate(self):
        self.assertEqual(self._with_take().export_path()["name"], "12A-3.csv")

    def test_the_summary_says_it_does_not_translate(self):
        """The truth about a pan/tilt head, and the reason this data is cheap
        for a compositor to use."""
        self.assertIn("pan/tilt",
                      self._with_take().export_path()["summary"]["translation"])

    def test_the_frame_rate_is_honoured(self):
        s = self._with_take()
        self.assertGreater(s.export_path(fps=50.0)["summary"]["frames"],
                           s.export_path(fps=25.0)["summary"]["frames"])

    def test_a_take_without_a_trace_says_why(self):
        s = self._with_take()
        s.takes[0]["motion"] = {"trace": []}
        with self.assertRaises(ValueError) as caught:
            s.export_path()
        self.assertIn("no motion trace", str(caught.exception))

    def test_no_takes_at_all_is_refused(self):
        with self.assertRaises(ValueError):
            server.CameraSession(make_args()).export_path()

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            self._with_take().export_path(fmt="fbx")


class TestRollAndLinkHealth(unittest.TestCase):
    """ROLL is record, count, then the move; a disarm cancels the count."""

    def _ready(self):
        from driver import limits
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        s.started = []
        s.runner = fake_runner(start=lambda m: s.started.append(m))
        s.armed = True
        s.actions = []
        s.action = lambda name: s.actions.append(name)
        s.cues = []
        s.cue = lambda name: s.cues.append(name)
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(name="push", waypoints=[
            moves.Waypoint("a", p, 0.0, duration=2.0),
            moves.Waypoint("b", p, 20.0, duration=3.0)])
        s.link.attitude = commands.GimbalAttitude(
            pitch=p, yaw=0.0, yaw_alt=0.0, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        return s

    def test_roll_records_counts_then_starts_after_the_preroll(self):
        s = self._ready()
        out = s.roll(preroll_s=0.05)
        self.assertEqual(out["preroll"], 0.05)
        self.assertEqual(s.actions, ["record_start"])
        self.assertEqual(s.cues, ["count"])
        self.assertEqual(s.started, [])           # nothing moves during the count
        time.sleep(0.2)
        self.assertEqual(len(s.started), 1)
        self.assertEqual(s.owner, "program")

    def test_a_disarm_during_the_count_cancels_the_move(self):
        s = self._ready()
        s.roll(preroll_s=0.1)
        s.disarm("grab")
        time.sleep(0.25)
        self.assertEqual(s.started, [])
        self.assertEqual(s.preroll_until, 0.0)

    def test_roll_runs_the_same_preflight_as_play(self):
        s = self._ready()
        s.armed = False
        with self.assertRaises(RuntimeError):
            s.roll(preroll_s=0.01)
        self.assertEqual(s.actions, [])          # never rolled the camera

    def test_any_disarm_stops_a_timelapse_between_frames(self):
        s = server.CameraSession(make_args())
        s._tl_stop.clear()
        s.disarm("operator")
        self.assertTrue(s._tl_stop.is_set())

    def test_status_reports_link_health_and_preroll(self):
        s = server.CameraSession(make_args())
        st = s.status()
        self.assertIn("link_healthy", st)
        self.assertEqual(st["preroll"]["seconds"], 3.0)


class TestSegmentPlay(unittest.TestCase):
    """"Again from node 3" is how blocking works: the difficult third of a
    shot runs twenty times, the easy first third once."""

    def _ready(self):
        from driver import limits
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        started = []
        s.runner = fake_runner(start=lambda m: started.append(m))
        s.started = started
        s.armed = True
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(name="push", waypoints=[
            moves.Waypoint("a", p, 0.0, duration=2.0, dwell=1.0),
            moves.Waypoint("b", p, 20.0, duration=3.0, dwell=2.0),
            moves.Waypoint("c", p, 40.0, duration=3.0),
            moves.Waypoint("d", p, 60.0, duration=3.0)])
        s.link.attitude = commands.GimbalAttitude(
            pitch=p, yaw=20.0, yaw_alt=-20.0, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))
        return s

    def test_it_plays_only_that_span(self):
        s = self._ready()
        s.play_segment(1, 2)
        played = s.started[-1]
        self.assertEqual([w.name for w in played.waypoints], ["b", "c"])

    def test_the_full_move_is_untouched(self):
        """The next Play is still the shot, not the rehearsal."""
        s = self._ready()
        s.play_segment(1, 2)
        self.assertEqual(len(s.move.waypoints), 4)
        self.assertEqual(s.move.name, "push")

    def test_the_lead_dwell_is_dropped(self):
        """A hold that made sense mid-move is dead air at the top of a
        rehearsal."""
        s = self._ready()
        s.play_segment(1, 2)
        self.assertEqual(s.started[-1].waypoints[0].dwell, 0.0)

    def test_it_checks_the_head_is_at_that_node(self):
        s = self._ready()
        s.link.attitude = commands.GimbalAttitude(
            pitch=s.move.waypoints[0].pitch, yaw=0.0, yaw_alt=0.0,
            timestamp=2, quaternion=(0.0, 1.0, 0.0, 0.0))
        with self.assertRaises(RuntimeError) as caught:
            s.play_segment(1, 2)
        self.assertIn("node 2", str(caught.exception))

    def test_force_runs_it_from_here(self):
        s = self._ready()
        s.link.attitude = commands.GimbalAttitude(
            pitch=s.move.waypoints[0].pitch, yaw=0.0, yaw_alt=0.0,
            timestamp=2, quaternion=(0.0, 1.0, 0.0, 0.0))
        s.play_segment(1, 2, force=True)
        self.assertTrue(s.started)

    def test_it_refuses_when_not_armed(self):
        s = self._ready()
        s.armed = False
        with self.assertRaises(RuntimeError):
            s.play_segment(1, 2)


class TestTheLensIsStatedNotAssumed(unittest.TestCase):
    """Every pixel figure depends on the field of view, and nothing on this
    camera reports it."""

    def _with_takes(self, drift=0.04, fov=None):
        s = server.CameraSession(make_args())
        if fov is not None:
            s.move.setup["fov_deg"] = fov
        for n, d in enumerate((0.0, drift), start=1):
            s.takes.append({
                "scene": "1", "shot": "A", "take": n, "note": "",
                "circled": False, "move": "m", "duration": 1.0, "setup": "",
                "at": "10:00", "motion": {"trace": [
                    [i * 0.04, 100.0, i * 0.5 + d] for i in range(80)]}})
        return s

    def test_without_a_stated_lens_it_says_assumed(self):
        out = self._with_takes().compare_takes(0, 1)
        self.assertTrue(out["fov_assumed"])
        self.assertEqual(out["fov_source"], "house default")

    def test_a_stated_lens_is_used_and_credited(self):
        out = self._with_takes(fov=10.0).compare_takes(0, 1)
        self.assertFalse(out["fov_assumed"])
        self.assertEqual(out["fov_source"], "shot setup")
        self.assertEqual(out["fov_deg"], 10.0)

    def test_the_stated_lens_changes_the_verdict(self):
        """The whole reason it matters: matched wide, no match long."""
        self.assertEqual(self._with_takes(fov=84.0).compare_takes(0, 1)["verdict"],
                         "matched")
        self.assertNotEqual(self._with_takes(fov=10.0).compare_takes(0, 1)["verdict"],
                            "matched")

    def test_an_explicit_request_beats_the_setup(self):
        out = self._with_takes(fov=84.0).compare_takes(0, 1, fov_deg=10.0)
        self.assertEqual(out["fov_source"], "request")

    def test_a_nonsense_setup_value_falls_back_rather_than_lying(self):
        for bad in ("wide-ish", 0.0, 900.0, ""):
            with self.subTest(bad=bad):
                out = self._with_takes(fov=bad).compare_takes(0, 1)
                self.assertTrue(out["fov_assumed"])


class TestTimelapseResume(unittest.TestCase):
    """A four-hour shoot meets a battery swap. Losing it is losing the
    afternoon; resuming it WRONG is worse, because the join does not show
    until playback."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._real = server.MOVES_DIR
        server.MOVES_DIR = Path(self.dir.name)
        self.addCleanup(lambda: setattr(server, "MOVES_DIR", self._real))

    def _ready(self):
        from driver import limits
        s = server.CameraSession(make_args())
        s.link, s.stick, s.state = FakeLink(), FakeStick(), "connected"
        s.runner = fake_runner()
        s.armed = True
        p = moves.wrap180(limits.PITCH_ARC.low + limits.PITCH_ARC.usable * .5)
        s.move = moves.Move(waypoints=[
            moves.Waypoint("a", p, 0.0, duration=2.0),
            moves.Waypoint("b", p, 30.0, duration=20.0)])
        return s

    def _park_at(self, s, pitch, yaw):
        s.link.attitude = commands.GimbalAttitude(
            pitch=pitch, yaw=yaw, yaw_alt=-yaw, timestamp=1,
            quaternion=(0.0, 1.0, 0.0, 0.0))

    def _saved(self, s, frame=40, frames=100):
        from driver import timelapse as tl
        plan = tl.plan_for(s.move, frames=frames)
        tl.save_progress(server.MOVES_DIR, s.move, plan, frame, time.time())
        return plan

    def test_nothing_saved_reports_unavailable(self):
        self.assertFalse(self._ready().timelapse_resume_info()["available"])

    def test_an_interrupted_shoot_is_offered(self):
        s = self._ready()
        self._saved(s, frame=40, frames=100)
        info = s.timelapse_resume_info()
        self.assertTrue(info["available"])
        self.assertEqual(info["frame"], 40)
        self.assertTrue(info["matches"])

    def test_a_finished_shoot_is_not_offered(self):
        s = self._ready()
        self._saved(s, frame=100, frames=100)
        self.assertFalse(s.timelapse_resume_info()["available"])

    def test_resuming_an_edited_move_is_refused_with_the_reason(self):
        s = self._ready()
        self._saved(s)
        s.move.waypoints[1].yaw += 5.0
        with self.assertRaises(RuntimeError) as caught:
            s.timelapse_start(resume=True)
        self.assertIn("edited", str(caught.exception))

    def test_resuming_demands_the_head_is_back_on_the_next_frame(self):
        """After a power cycle the head has re-homed. Enforcing the position
        IS the re-reference discipline."""
        s = self._ready()
        self._saved(s, frame=40, frames=100)
        self._park_at(s, 0.0, 0.0)
        with self.assertRaises(RuntimeError) as caught:
            s.timelapse_start(resume=True)
        self.assertIn("frame 41", str(caught.exception))

    def test_resuming_from_the_right_place_is_allowed(self):
        from driver import timelapse as tl
        s = self._ready()
        plan = self._saved(s, frame=40, frames=100)
        nxt = tl.remaining_poses(s.move, plan, 40)[0]
        self._park_at(s, nxt[0], nxt[1])
        out = s.timelapse_start(resume=True)
        self.assertEqual(out["frame"], 40)
        self.assertEqual(out["frames"], 100)
        self.assertEqual(out["resumed_from"], 40)
        s.timelapse_stop()

    def test_resuming_with_nothing_saved_is_refused(self):
        with self.assertRaises(RuntimeError):
            self._ready().timelapse_start(resume=True)

    def test_a_fresh_start_ignores_a_saved_shoot(self):
        """Resume has to be asked for. Silently continuing someone else's
        abandoned shoot would be astonishing."""
        s = self._ready()
        self._saved(s, frame=40, frames=100)
        self._park_at(s, s.move.waypoints[0].pitch, s.move.waypoints[0].yaw)
        out = s.timelapse_start(frames=10)
        self.assertEqual(out["frame"], 0)
        self.assertEqual(out["frames"], 10)
        s.timelapse_stop()

    def test_a_finished_shoot_clears_its_progress(self):
        """Otherwise it offers to resume itself the morning after, into a
        scene that was struck. Drives the worker directly: the guard lives in
        its finally block, which no other test reaches."""
        from driver import timelapse as tl
        s = self._ready()
        plan = tl.plan_for(s.move, frames=2, settle_s=0.0, expose_s=0.0,
                           gap_s=0.0)
        tl.save_progress(server.MOVES_DIR, s.move, plan, 1, time.time())
        self.assertIsNotNone(tl.load_progress(server.MOVES_DIR))

        poses = tl.frame_poses(s.move, 2)
        s.tl_state = {"running": True, "frame": 0, "frames": 2,
                      "error": None, "plan": plan.to_dict()}
        s._timelapse_worker(poses, plan, done=0)

        self.assertEqual(s.tl_state["frame"], 2)
        self.assertIsNone(tl.load_progress(server.MOVES_DIR),
                          "a completed shoot kept its resume point")

    def test_an_interrupted_shoot_keeps_its_place(self):
        """The other half of the same rule."""
        from driver import timelapse as tl
        s = self._ready()
        plan = tl.plan_for(s.move, frames=6, settle_s=0.0, expose_s=0.0,
                           gap_s=0.0)
        poses = tl.frame_poses(s.move, 6)
        s.tl_state = {"running": True, "frame": 0, "frames": 6,
                      "error": None, "plan": plan.to_dict()}
        s._tl_stop.set()                     # stopped before it can finish
        s._timelapse_worker(poses, plan, done=0)
        self.assertLess(s.tl_state["frame"], 6)
