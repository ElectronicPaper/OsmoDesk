"""Host wiring and the attitude control law.

No sockets and no threads: `Datalink` is only constructed, never opened, and
the follower is driven through a recording stub. What matters here is that a
wrong target address or a sign error cannot pass unnoticed -- both fail
silently on real hardware.
"""

import socket
import unittest
from unittest import mock

from driver import commands, duml, gimbal, transport
from driver.datalink import Datalink
from driver import response
from driver.gimbal import (AttitudeFollower, GimbalStick,
                           TILT_SIGN, YAW_SIGN)


class TestDatalinkHost(unittest.TestCase):
    def test_defaults_to_the_softap_address(self):
        self.assertEqual(Datalink().host, transport.CAMERA_HOST)
        self.assertEqual(transport.CAMERA_HOST, "192.168.2.1")

    def test_explicit_host_is_kept(self):
        # --host exists so a USB/RNDIS address can be targeted. If this stops
        # propagating, the driver talks to the SoftAP while reporting the
        # address the user asked for.
        self.assertEqual(Datalink(host="192.168.42.1").host, "192.168.42.1")

    def test_attitude_expires_instead_of_becoming_permanent_false_feedback(self):
        from driver import datalink as dl

        link = dl.Datalink()
        frame = duml.Frame(1, 2, 3, 0, 0x04, 0x05, bytes(40))
        with mock.patch.object(dl.time, "monotonic", return_value=10.0):
            link._handle(frame)
        self.assertIsNotNone(link.attitude)
        link._expire_attitude(10.0 + dl.TELEMETRY_STALE_S)
        self.assertIsNotNone(link.attitude)
        link._expire_attitude(10.001 + dl.TELEMETRY_STALE_S)
        self.assertIsNone(link.attitude)
        self.assertIsNone(link.gimbal_pitch)
        self.assertIsNone(link.gimbal_yaw)

    def test_reconnect_clears_the_previous_session_pose(self):
        link = Datalink()
        link.attitude = object()
        link.gimbal_pitch, link.gimbal_yaw = 1.0, 2.0
        link.last_attitude_at = 10.0
        link._reset_session()
        self.assertIsNone(link.attitude)
        self.assertIsNone(link.gimbal_pitch)
        self.assertIsNone(link.gimbal_yaw)
        self.assertEqual(link.last_attitude_at, 0.0)

    def test_tcp_poke_toggle(self):
        self.assertTrue(Datalink().tcp_poke)
        self.assertFalse(Datalink(tcp_poke=False).tcp_poke)

    def test_ports_are_the_documented_ones(self):
        self.assertEqual(transport.CAMERA_UDP_PORT, 9004)
        self.assertEqual(transport.CAMERA_TCP_POKE_PORT, 7001)

    def test_starts_closed(self):
        link = Datalink()
        self.assertIsNone(link.sock)
        self.assertIsNone(link.poke_sock)
        self.assertIsNone(link.gimbal_pitch)


class RecordingLink:
    """Stands in for Datalink: records frames instead of sending them."""

    def __init__(self, pitch=None):
        self.frames = []
        self.gimbal_pitch = pitch
        self.gimbal_roll = None
        self.gimbal_yaw = None

    def send_frame(self, frame):
        self.frames.append(frame)


class RecordingStick:
    """Stands in for GimbalStick. `axes` holds operator-intent calls, `rates`
    holds deg/s calls -- the closed-loop controllers use the latter."""

    def __init__(self):
        self.axes = []
        self.rates = []

    def set_axes(self, tilt, pan):
        self.axes.append((tilt, pan))

    def set_rate(self, tilt_dps, pan_dps):
        self.rates.append((tilt_dps, pan_dps))
        # Record what the real stick would put on the wire, so tests that only
        # look at `axes` still see the controller acting.
        self.axes.append((response.deflection_for_rate(TILT_SIGN * tilt_dps),
                          response.deflection_for_rate(YAW_SIGN * pan_dps)))

    def release(self):
        self.set_rate(0.0, 0.0)

    def abort(self):
        self.rates.append((0.0, 0.0))
        self.axes.append((0.0, 0.0))

    def set_ramp(self, name):
        self.ramp = name

    def set_speed_cap(self, dps):
        self.speed_cap = dps

    def wire_deflections(self, pitch_dps, yaw_dps):
        return (response.deflection_for_rate(TILT_SIGN * pitch_dps),
                response.deflection_for_rate(YAW_SIGN * yaw_dps))


class TestStickCommands(unittest.TestCase):
    """Discrete actions go straight out; no pump thread involved."""

    def setUp(self):
        self.link = RecordingLink()
        self.stick = GimbalStick(self.link)

    def test_recenter(self):
        self.stick.recenter()
        self.assertEqual(self.link.frames[0].opcode, (0x04, 0x4C))
        self.assertEqual(self.link.frames[0].payload, b"\xfe\x08")

    def test_flip(self):
        self.stick.flip()
        self.assertEqual(self.link.frames[0].payload, b"\xfe\x09")

    def test_modes(self):
        self.stick.follow_mode()
        self.stick.fpv_mode()
        self.assertEqual([f.payload for f in self.link.frames], [b"\x02\x08", b"\x01\x08"])

    def test_release_is_recorded_as_centre(self):
        self.stick.release()
        self.assertEqual(self.stick._tilt, 0.0)
        self.assertEqual(self.stick._pan, 0.0)


class TestAttitudeFollower(unittest.TestCase):
    """Closed loop on tilt, open loop on pan.

    Sign convention, measured on hardware with `run.py --map-axes`: a stick
    tilt-up command decreases the reported pitch. So driving the gimbal
    *towards* a higher target means a negative deflection. Inverting this
    turns the loop into positive feedback.
    """

    def _follower(self, measured=None, **kw):
        link = RecordingLink(pitch=measured)
        stick = RecordingStick()
        return stick, AttitudeFollower(stick, link, **kw)

    def test_output_sign_matches_the_measured_convention(self):
        # A stick tilt-up DECREASES reported pitch (measured with --map-axes),
        # so raising the target must command a NEGATIVE stick deflection.
        # Getting this backwards drives away from the target and saturates.
        stick, f = self._follower(measured=0.0)
        f.update(target_pitch_deg=20.0, pan_rate_dps=0.0)
        self.assertLess(stick.axes[-1][0], 0)

    def test_target_below_measured_drives_the_other_way(self):
        stick, f = self._follower(measured=0.0)
        f.update(target_pitch_deg=-20.0, pan_rate_dps=0.0)
        self.assertGreater(stick.axes[-1][0], 0)

    def test_error_wraps_the_short_way(self):
        # Target +170, measured -170: a 20 deg error, not 340.
        stick, f = self._follower(measured=-170.0, kp_tilt=0.01)
        f.update(target_pitch_deg=170.0, pan_rate_dps=0.0)
        self.assertAlmostEqual(abs(stick.rates[-1][0]), 0.2, places=6)

    def test_on_target_is_still(self):
        stick, f = self._follower(measured=15.0)
        f.update(target_pitch_deg=15.0, pan_rate_dps=0.0)
        self.assertEqual(stick.axes[-1][0], 0.0)

    def test_proportional_to_error(self):
        """The gain is deg/s of correction per degree of error.

        It used to produce a raw deflection, which is not proportional to
        anything: on this camera the same deflection step is worth 0.4 deg/s
        near centre and 21 deg/s at full throw.
        """
        stick, f = self._follower(measured=0.0, kp_tilt=0.01)
        f.update(target_pitch_deg=10.0, pan_rate_dps=0.0)
        f.update(target_pitch_deg=20.0, pan_rate_dps=0.0)
        self.assertAlmostEqual(abs(stick.rates[0][0]), 0.1)
        self.assertAlmostEqual(abs(stick.rates[1][0]), 0.2)

    def test_a_speed_below_the_dead_band_commands_a_stop(self):
        """The head cannot creep slower than about a third of a degree a
        second, so asking for less must read as stop rather than silently
        becoming one at some deflection that does nothing."""
        stick, f = self._follower(measured=0.0, kp_tilt=0.001)
        f.update(target_pitch_deg=10.0, pan_rate_dps=0.0)   # 0.01 deg/s
        self.assertEqual(stick.axes[-1][0], 0.0)

    def test_output_is_clamped(self):
        stick, f = self._follower(measured=0.0, kp_tilt=1.0)
        f.update(target_pitch_deg=900.0, pan_rate_dps=0.0)
        f.update(target_pitch_deg=-900.0, pan_rate_dps=0.0)
        self.assertLessEqual(abs(stick.rates[0][0]), response.MAX_DPS)
        self.assertLessEqual(abs(stick.rates[1][0]), response.MAX_DPS)
        self.assertLessEqual(stick.axes[0][0], 1.0)
        self.assertGreaterEqual(stick.axes[1][0], -1.0)

    def test_falls_back_to_rate_without_telemetry(self):
        # No 0x04/0x05 heartbeat yet: still move, just open loop.
        stick, f = self._follower(measured=None)
        f.update(target_pitch_deg=20.0, pan_rate_dps=0.0)
        self.assertNotEqual(stick.axes[-1][0], 0.0)

    def test_pan_follows_gyro_sign(self):
        stick, f = self._follower(measured=0.0)
        f.update(target_pitch_deg=0.0, pan_rate_dps=90.0)
        self.assertGreater(stick.axes[-1][1], 0)
        f.update(target_pitch_deg=0.0, pan_rate_dps=-90.0)
        self.assertLess(stick.axes[-1][1], 0)

    def test_pan_is_clamped(self):
        stick, f = self._follower(measured=0.0)
        f.update(target_pitch_deg=0.0, pan_rate_dps=100000.0)
        self.assertLessEqual(stick.axes[-1][1], 1.0)


class TestEndToEndStickEncoding(unittest.TestCase):
    """A follower output must survive the whole way to legal wire bytes."""

    def test_follower_output_encodes_to_a_valid_frame(self):
        for measured, target, rate in ((0.0, 45.0, 120.0), (30.0, -30.0, -200.0),
                                       (0.0, 0.0, 0.0), (-90.0, 90.0, 1e6)):
            with self.subTest(measured=measured, target=target):
                link = RecordingLink(pitch=measured)
                stick = RecordingStick()
                AttitudeFollower(stick, link).update(target, rate)
                tilt, pan = stick.axes[-1]
                frame = commands.gimbal_stick(gimbal.axis(tilt), gimbal.axis(pan))
                raw = duml.encode(frame)
                decoded = duml.decode(raw)
                self.assertIsNotNone(decoded)
                self.assertEqual(decoded[0].opcode, (0x04, 0x01))
                self.assertEqual(len(decoded[0].payload), 10)


if __name__ == "__main__":
    unittest.main()


class TestDatalinkClosesFailedSockets(unittest.TestCase):
    """The connect path is retried, so a socket leaked per failed attempt
    accumulates file descriptors for as long as the operator keeps trying."""

    def _tracked_socket_factory(self, fail_on):
        """Hand out sockets that record whether they were closed."""
        made = []
        real = socket.socket

        class Tracked(real):
            def connect(self, addr):
                if addr[1] == fail_on:
                    raise TimeoutError("timed out")
                return super().connect(addr)

            def close(self):
                self.closed_by_us = True
                return super().close()

        def factory(*a, **kw):
            s = Tracked(*a, **kw)
            s.closed_by_us = False
            made.append(s)
            return s

        return factory, made

    def test_the_tcp_poke_closes_its_socket_when_connect_times_out(self):
        from driver import datalink as dl

        factory, made = self._tracked_socket_factory(transport.CAMERA_TCP_POKE_PORT)
        link = dl.Datalink(host="192.0.2.1")     # TEST-NET-1, never routable
        with mock.patch.object(dl.socket, "socket", factory):
            with self.assertRaises(TimeoutError):
                link._poke_7001()

        self.assertTrue(made, "the poke never created a socket")
        for s in made:
            self.assertTrue(s.closed_by_us,
                            "a failed poke left its socket open")

    def test_the_udp_open_closes_its_socket_when_connect_fails(self):
        from driver import datalink as dl

        factory, made = self._tracked_socket_factory(transport.CAMERA_UDP_PORT)
        link = dl.Datalink(host="192.0.2.1")
        with mock.patch.object(dl.socket, "socket", factory):
            with self.assertRaises(TimeoutError):
                link._open_udp()

        self.assertTrue(made, "the udp open never created a socket")
        for s in made:
            self.assertTrue(s.closed_by_us,
                            "a failed udp open left its socket open")

    def test_open_closes_every_partial_resource_when_setup_later_fails(self):
        """A bad register must not leak the already-open poke, UDP, or RX."""
        from driver import datalink as dl

        class Resource:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Thread:
            def __init__(self):
                self.joined = False

            def join(self, timeout):
                self.joined = True

        poke, udp, rx = Resource(), Resource(), Thread()
        link = dl.Datalink()

        def send_raw(*_):
            link._handshake_acked.set()
            link._channel_seen.set()

        with (mock.patch.object(link, "_poke_7001", side_effect=lambda: setattr(link, "poke_sock", poke)),
              mock.patch.object(link, "_open_udp", side_effect=lambda: setattr(link, "sock", udp)),
              mock.patch.object(link, "_send_raw", side_effect=send_raw),
              mock.patch.object(link, "send_ack"),
              mock.patch.object(link, "_register", side_effect=RuntimeError("register failed")),
              mock.patch.object(dl, "_spawn", return_value=rx),
              mock.patch.object(dl.time, "sleep")):
            with self.assertRaisesRegex(RuntimeError, "register failed"):
                link.open()

        self.assertTrue(poke.closed)
        self.assertTrue(udp.closed)
        self.assertTrue(rx.joined)
        self.assertEqual(link._threads, [])

    def test_open_refuses_a_receive_only_link_without_a_command_window(self):
        """Telemetry without a transmit window is not control: even STOP
        would be silently discarded, so registration must never begin."""
        from driver import datalink as dl

        class Resource:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Thread:
            def __init__(self):
                self.joined = False

            def join(self, timeout):
                self.joined = True

        udp, rx = Resource(), Thread()
        link = dl.Datalink(tcp_poke=False)

        def handshake_only(*_):
            link._handshake_acked.set()

        with (mock.patch.object(link, "_open_udp",
                                side_effect=lambda: setattr(link, "sock", udp)),
              mock.patch.object(link, "_send_raw", side_effect=handshake_only),
              mock.patch.object(link, "_register") as register,
              mock.patch.object(dl, "_spawn", return_value=rx),
              mock.patch.object(dl, "CHANNEL_WAIT_S", 0.0)):
            with self.assertRaisesRegex(RuntimeError, "command sequence window"):
                link.open()

        register.assert_not_called()
        self.assertTrue(udp.closed)
        self.assertTrue(rx.joined)

    def test_session_reset_clears_the_previous_channel_signal(self):
        link = Datalink()
        link._channel_seen.set()
        link._reset_session()
        self.assertFalse(link._channel_seen.is_set())
