"""Byte-level tests for the DUML protocol layer.

Pure byte math, no hardware. These pin the parts that fail *silently* on the
wire -- a wrong receiver byte or routing sequence drops writes while reads keep
flowing, which looks like a working link that ignores you.

    python -m unittest discover -s tests -v
"""

import struct
import unittest

from driver import commands, duml, gimbal, transport
from driver.duml import Frame


class TestChecksums(unittest.TestCase):
    def test_crc8_seed(self):
        # Empty input returns the reflected init value.
        self.assertEqual(duml.crc8(b""), 0x77)

    def test_crc16_seed(self):
        self.assertEqual(duml.crc16(b""), 0x3692)

    def test_crc8_is_byte_wide(self):
        for data in (b"\x00", b"\x55\x17\x04", bytes(range(64))):
            self.assertLessEqual(duml.crc8(data), 0xFF)

    def test_crc16_is_word_wide(self):
        for data in (b"\x00", b"\x55\x17\x04", bytes(range(64))):
            self.assertLessEqual(duml.crc16(data), 0xFFFF)

    def test_crc_detects_single_bit_flip(self):
        base = bytes(range(32))
        flipped = bytearray(base)
        flipped[7] ^= 0x01
        self.assertNotEqual(duml.crc16(base), duml.crc16(bytes(flipped)))


class TestAddressing(unittest.TestCase):
    """Receivers are packed (id << 5) | type. Hardcoding these is how you get
    a transposed nibble that drops every write."""

    def test_rx_packing(self):
        self.assertEqual(duml.rx(0x08, 1), 0x28)
        self.assertEqual(duml.rx(0x08, 2), 0x48)
        self.assertEqual(duml.rx(0x10, 7), 0xF0)
        self.assertEqual(duml.rx(0x04, 0), 0x04)

    def test_named_receivers_match_packing(self):
        self.assertEqual(duml.RX_CAMERA, duml.rx(0x01, 0))
        self.assertEqual(duml.RX_GIMBAL, duml.rx(0x04, 0))
        self.assertEqual(duml.RX_WIFI, duml.rx(0x07, 0))
        self.assertEqual(duml.RX_SESSION, duml.rx(0x10, 7))
        self.assertEqual(duml.RX_WAKE, duml.rx(0x1C, 0))
        self.assertEqual(duml.RX_DM368_1, duml.rx(0x08, 1))
        self.assertEqual(duml.RX_DM368_2, duml.rx(0x08, 2))
        self.assertEqual(duml.RX_GIMBAL_INIT, duml.rx(0x03, 0))

    def test_dm368_literals(self):
        # Pinned explicitly: these two were wrong once.
        self.assertEqual(duml.RX_DM368_1, 0x28)
        self.assertEqual(duml.RX_DM368_2, 0x48)


class TestFrameEncoding(unittest.TestCase):
    def test_exact_layout(self):
        f = Frame(0x02, 0x04, 0x1234, 0x00, 0x04, 0x01, b"\xaa\xbb")
        b = duml.encode(f)
        self.assertEqual(b[0], 0x55)
        self.assertEqual(b[1], 15)  # total = 13 + 2
        self.assertEqual(b[2] >> 2, 1)  # version 1
        self.assertEqual(b[2] & 0x03, 0)  # high length bits
        self.assertEqual(b[3], duml.crc8(b[:3]))
        self.assertEqual(b[4], 0x02)  # sender
        self.assertEqual(b[5], 0x04)  # receiver
        self.assertEqual((b[6], b[7]), (0x34, 0x12))  # seq little-endian
        self.assertEqual(b[8], 0x00)  # flags
        self.assertEqual(b[9], 0x04)  # cmdSet
        self.assertEqual(b[10], 0x01)  # cmdId
        self.assertEqual(b[11:13], b"\xaa\xbb")
        self.assertEqual(b[13] | (b[14] << 8), duml.crc16(b[:13]))

    def test_total_length(self):
        for n in (0, 1, 10, 62, 255):
            b = duml.encode(Frame(0x02, 0x01, 1, 0x40, 0x00, 0x00, bytes(n)))
            self.assertEqual(len(b), 13 + n)
            self.assertEqual(b[1] | ((b[2] & 0x03) << 8), 13 + n)

    def test_high_length_bits_used_past_255(self):
        b = duml.encode(Frame(0x02, 0x01, 1, 0x40, 0x00, 0x00, bytes(300)))
        self.assertEqual(b[1] | ((b[2] & 0x03) << 8), 313)
        self.assertEqual(b[2] >> 2, 1)  # version survives the shared byte

    def test_rejects_oversize(self):
        with self.assertRaises(ValueError):
            duml.encode(Frame(0x02, 0x01, 1, 0x40, 0x00, 0x00, bytes(0x400)))


class TestFrameDecoding(unittest.TestCase):
    def _frame(self, payload=b"\x01\x02\x03"):
        return Frame(0x02, 0x04, 0x00AB, 0x40, 0x07, 0x45, payload)

    def test_roundtrip(self):
        for payload in (b"", b"\x00", b"\x01\x02\x03", bytes(range(64))):
            f = self._frame(payload)
            got = duml.decode(duml.encode(f))
            self.assertIsNotNone(got)
            back, used = got
            self.assertEqual(used, 13 + len(payload))
            self.assertEqual(back.sender, f.sender)
            self.assertEqual(back.receiver, f.receiver)
            self.assertEqual(back.seq, f.seq)
            self.assertEqual(back.flags, f.flags)
            self.assertEqual(back.opcode, f.opcode)
            self.assertEqual(back.payload, payload)

    def test_rejects_bad_magic(self):
        b = bytearray(duml.encode(self._frame()))
        b[0] = 0x56
        self.assertIsNone(duml.decode(bytes(b)))

    def test_rejects_bad_crc8(self):
        b = bytearray(duml.encode(self._frame()))
        b[3] ^= 0xFF
        self.assertIsNone(duml.decode(bytes(b)))

    def test_rejects_bad_crc16(self):
        b = bytearray(duml.encode(self._frame()))
        b[-1] ^= 0xFF
        self.assertIsNone(duml.decode(bytes(b)))

    def test_rejects_corrupt_payload(self):
        b = bytearray(duml.encode(self._frame()))
        b[11] ^= 0xFF
        self.assertIsNone(duml.decode(bytes(b)))

    def test_rejects_truncated(self):
        b = duml.encode(self._frame())
        for cut in (0, 5, 12, len(b) - 1):
            self.assertIsNone(duml.decode(b[:cut]))

    def test_rejects_bad_version(self):
        b = bytearray(duml.encode(self._frame()))
        b[2] = (2 << 2) | (b[2] & 0x03)
        b[3] = duml.crc8(bytes(b[:3]))  # keep crc8 valid so version is the sole fault
        self.assertIsNone(duml.decode(bytes(b)))

    def test_decode_ignores_trailing_bytes(self):
        b = duml.encode(self._frame())
        got = duml.decode(b + b"\xde\xad\xbe\xef")
        self.assertIsNotNone(got)
        self.assertEqual(got[1], len(b))


class TestScanFrames(unittest.TestCase):
    def test_finds_frame_inside_udp_wrapper(self):
        f = commands.gimbal_stick(1574, 474, seq=0x0001)
        body = duml.encode(f)
        wrapped = (
            transport.transport_header(transport.PKT_COMMAND, 12 + len(body), 0xBEEF, 0x0100)
            + transport.routing_header(0x0100, 7)
            + body
        )
        found = duml.scan_frames(wrapped)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].opcode, (0x04, 0x01))
        self.assertEqual(found[0].payload, f.payload)

    def test_finds_multiple_frames_per_datagram(self):
        a = duml.encode(commands.gimbal_recenter(seq=1))
        b = duml.encode(commands.app_presence(seq=2))
        found = duml.scan_frames(b"\x00\x11" + a + b"\xff" + b)
        self.assertEqual([f.opcode for f in found], [(0x04, 0x4C), (0x00, 0x88)])

    def test_ignores_garbage(self):
        self.assertEqual(duml.scan_frames(b""), [])
        self.assertEqual(duml.scan_frames(b"\x55" * 40), [])
        self.assertEqual(duml.scan_frames(bytes(range(200))), [])


class TestStringPacking(unittest.TestCase):
    def test_pack(self):
        self.assertEqual(duml.pack_string("osmo"), b"\x04osmo")
        self.assertEqual(duml.pack_string(""), b"\x00")

    def test_unpack_status_string(self):
        self.assertEqual(duml.unpack_status_string(b"\x00\x04osmo"), "osmo")
        self.assertEqual(duml.unpack_status_string(b"\x00"), "")
        self.assertEqual(duml.unpack_status_string(b""), "")

    def test_unpack_tolerates_truncation(self):
        self.assertEqual(duml.unpack_status_string(b"\x00\x08osm"), "osm")

    def test_roundtrip_through_status_form(self):
        for s in ("osmo", "OsmoPocket4-ABC123", "a"):
            self.assertEqual(duml.unpack_status_string(b"\x00" + duml.pack_string(s)), s)


class TestTransportHeader(unittest.TestCase):
    def test_length_and_flag(self):
        h = transport.transport_header(transport.PKT_HANDSHAKE, 40, 0x1234, 0x0008)
        self.assertEqual(len(h), 8)
        w0 = h[0] | (h[1] << 8)
        self.assertEqual(w0 & 0x3FFF, 48)  # 8 + payload
        self.assertTrue(w0 & 0x8000)

    def test_trailing_xor(self):
        for pkt in (0x00, 0x04, 0x05):
            h = transport.transport_header(pkt, 26, 0xABCD, 0x0130)
            x = 0
            for v in h[:7]:
                x ^= v
            self.assertEqual(h[7], x)

    def test_field_order(self):
        h = transport.transport_header(0x05, 0, 0xABCD, 0x1234)
        self.assertEqual((h[2], h[3]), (0xCD, 0xAB))  # session LE
        self.assertEqual((h[4], h[5]), (0x34, 0x12))  # seq LE
        self.assertEqual(h[6], 0x05)


class TestRoutingHeader(unittest.TestCase):
    def test_layout(self):
        r = transport.routing_header(0x0100, 0x07)
        self.assertEqual(len(r), 12)
        self.assertEqual(r[0] | (r[1] << 8), 0x00F8)  # ack = seq - 8
        self.assertEqual(r[2] | (r[3] << 8), 0x0100)  # seq
        self.assertEqual(r[4:8], b"\x00\x00\x00\x00")
        self.assertEqual(r[8], 0x07)  # counter
        self.assertEqual(r[9], 0x01)
        self.assertEqual(r[10], 0x00)  # not a drone
        self.assertEqual(r[11], 0x00)

    def test_ack_wraps(self):
        r = transport.routing_header(0x0000, 1)
        self.assertEqual(r[0] | (r[1] << 8), 0xFFF8)

    def test_drone_flag(self):
        self.assertEqual(transport.routing_header(8, 1, drone=True)[10], 0x60)


class TestHandshakeAndAck(unittest.TestCase):
    def test_handshake_payload(self):
        p = transport.handshake_payload(0x1238)
        self.assertEqual(len(p), 40)
        self.assertEqual((p[0], p[1]), (0x38, 0x12))  # baseSeq LE
        self.assertEqual(p[2:6], b"\x64\x00\x64\x00")  # window template survives

    def test_ack_payload(self):
        windows = transport.AckWindows(video=0x0110, acked_data=0x0008,
                                       extra=0x2240)
        p = transport.ack_payload(windows)
        self.assertEqual(len(p), 26)
        self.assertEqual(p[0:8], b"\x10\x01\x10\x01\x00\x00\x00\x00")
        self.assertEqual(p[8:16], b"\x08\x00\x08\x00\x00\x00\x00\x00")
        self.assertEqual(p[16:24], b"\x40\x22\x40\x22\x00\x00\x00\x00")
        self.assertEqual(p[24:26], b"\x00\x00")

    def test_ack_windows_start_with_the_handshake_fallbacks(self):
        windows = transport.AckWindows.for_handshake(0x1238)
        self.assertEqual(windows, transport.AckWindows())
        payload = transport.ack_payload(windows, fallback_seq=0x1238)
        self.assertEqual(payload[8:16], b"\x38\x12\x38\x12\x00\x00\x00\x00")
        self.assertEqual(payload[16:24], b"\x38\x12\x38\x12\x00\x00\x00\x00")

    def test_transport_seq(self):
        h = transport.transport_header(0x01, 0, 0x1111, 0x0208)
        self.assertEqual(transport.transport_seq(h), 0x0208)
        self.assertIsNone(transport.transport_seq(b"\x00\x01"))

    def test_is_handshake(self):
        self.assertTrue(transport.is_handshake(transport.transport_header(0x00, 0, 1, 1)))
        self.assertFalse(transport.is_handshake(transport.transport_header(0x05, 0, 1, 1)))
        self.assertFalse(transport.is_handshake(b"\x00" * 4))


class TestRegistrationFrames(unittest.TestCase):
    def test_app_device_info(self):
        f = commands.app_device_info(seq=1)
        self.assertEqual(f.receiver, 0x48)
        self.assertEqual(f.flags, duml.FLAG_ACK80)
        self.assertEqual(f.opcode, (0x00, 0x81))
        self.assertEqual(len(f.payload), 62)
        self.assertEqual(f.payload[1:4], b"APP")
        self.assertEqual(f.payload[41], 0x02)
        self.assertEqual(f.payload[50:52], b"\x02\x08")

    def test_app_presence(self):
        f = commands.app_presence(seq=1)
        self.assertEqual(f.receiver, 0x28)
        self.assertEqual(f.opcode, (0x00, 0x88))
        self.assertEqual(len(f.payload), 14)
        self.assertEqual(f.payload[:2], b"\x17\x00")

    def test_gimbal_init(self):
        f = commands.gimbal_init(seq=1)
        self.assertEqual(f.receiver, 0x03)
        self.assertEqual(f.opcode, (0x03, 0xDA))
        self.assertEqual(f.payload, b"\x05\xff\xff\xff\xff")


class TestSubscriptionPayload(unittest.TestCase):
    """Sizes pinned against the OpenPocketCine capture notes:
    camcap_fov 29 B, cam_storage 30 B, camcap_video_format 38 B."""

    def _payload(self, key):
        return commands.subscribe(key, commands.FIRST_SUB_ID, seq=1).payload

    def test_captured_sizes(self):
        self.assertEqual(len(self._payload("camcap_fov")), 29)
        self.assertEqual(len(self._payload("cam_storage")), 30)
        self.assertEqual(len(self._payload("camcap_video_format")), 38)

    def test_layout(self):
        key = "cam_status"
        p = self._payload(key)
        self.assertEqual(p[0:4], b"\x02\x02\x00\x00")
        self.assertEqual(struct.unpack_from("<I", p, 4)[0], commands.FIRST_SUB_ID)
        self.assertEqual(p[8:11], b"\x00\x00\x00")
        inner = struct.unpack_from("<H", p, 11)[0]
        name_len = struct.unpack_from("<H", p, 13)[0]
        self.assertEqual(name_len, len(key))
        self.assertEqual(inner, name_len + 6)
        self.assertEqual(p[15 : 15 + name_len].decode(), key)
        self.assertEqual(p[15 + name_len :], b"\x00\x00\x00\x00")

    def test_receiver_is_dm368_1(self):
        self.assertEqual(self._subscribe_receiver(), 0x28)

    def _subscribe_receiver(self):
        return commands.subscribe("cam_status", commands.FIRST_SUB_ID, seq=1).receiver


class TestPairingFrames(unittest.TestCase):
    def test_set_pairing_pin(self):
        f = commands.set_pairing_pin("osmo", "abc")
        self.assertEqual(f.receiver, duml.RX_WIFI)
        self.assertEqual(f.opcode, (0x07, 0x45))
        self.assertEqual(f.payload, b"\x03abc\x04osmo")

    def test_approval_ack_is_a_response(self):
        f = commands.pair_approval_ack(seq=0x99)
        self.assertEqual(f.flags, duml.FLAG_RESPONSE)
        self.assertEqual(f.opcode, (0x07, 0x46))
        self.assertEqual(f.seq, 0x99)

    def test_wake_ap(self):
        f = commands.wake_ap()
        self.assertEqual(f.receiver, duml.RX_WAKE)
        self.assertEqual(f.opcode, (0x53, 0x10))
        self.assertEqual(f.payload, b"\x00\x00\x00\x00")

    def test_wifi_queries_are_empty(self):
        self.assertEqual(commands.get_wifi_ssid().payload, b"")
        self.assertEqual(commands.get_wifi_password().payload, b"")
        self.assertEqual(commands.get_wifi_ssid().opcode, (0x07, 0x07))
        self.assertEqual(commands.get_wifi_password().opcode, (0x07, 0x0E))


class TestGimbalFrames(unittest.TestCase):
    def test_stick_layout(self):
        f = commands.gimbal_stick(1574, 474)
        self.assertEqual(f.receiver, duml.RX_GIMBAL)
        self.assertEqual(f.flags, duml.FLAG_NOTIFY)  # no ACK is expected
        self.assertEqual(f.opcode, (0x04, 0x01))
        self.assertEqual(len(f.payload), 10)
        self.assertEqual(struct.unpack_from("<H", f.payload, 0)[0], 1574)  # axis0 tilt
        self.assertEqual(struct.unpack_from("<H", f.payload, 4)[0], 474)  # axis1 pan
        self.assertEqual(f.payload[2:4], b"\x00\x00")
        self.assertEqual(f.payload[6:10], b"\x00\x80\x22\x00")

    def test_stick_centre(self):
        f = commands.gimbal_stick(gimbal.CENTER, gimbal.CENTER)
        self.assertEqual(f.payload[0:2], b"\x00\x04")  # 1024 LE

    def test_mode_frames(self):
        for build, payload in (
            (commands.gimbal_recenter, b"\xfe\x08"),
            (commands.gimbal_flip, b"\xfe\x09"),
            (commands.gimbal_follow, b"\x02\x08"),
            (commands.gimbal_fpv, b"\x01\x08"),
        ):
            f = build()
            self.assertEqual(f.opcode, (0x04, 0x4C))
            self.assertEqual(f.flags, duml.FLAG_REQUEST)
            self.assertEqual(f.payload, payload)

    def test_param_frames(self):
        self.assertEqual(commands.gimbal_params_get().payload, b"\x01\x04\x05")
        self.assertEqual(commands.set_gimbal_speed(2).payload, b"\x00\x05\x01\x02")
        self.assertEqual(commands.set_gimbal_tilt_lock(1).payload, b"\x00\x04\x01\x01")


class TestGimbalTelemetry(unittest.TestCase):
    """0x04/0x05, layout derived from an Osmo Pocket 4 Pro.

    These two payloads are real captures taken while the camera was moved by
    hand. The Pocket 3 notes describe a 12-byte int16 pitch/roll/yaw frame;
    the Pocket 4 Pro sends 50 bytes with a quaternion in it, and trusting the
    inherited layout produced a reading that never changed.
    """

    # Captured 2026-08-25.
    SAMPLE_A = bytes.fromhex(
        "38f9000013ff8200f200000238a45100503d00000900d8ff"
        "938a3c3c50137a3fac7953be43225f3d" + "00" * 10)
    SAMPLE_B = bytes.fromhex(
        "20f90000c801860041fe0002e4fc510047cb000010006e00"
        "5882813cee4a6cbf68dbc3bed2451cbd" + "00" * 10)

    def test_sample_is_the_real_length(self):
        self.assertEqual(len(self.SAMPLE_A), 50)

    def test_parses_angles_as_signed_tenths(self):
        a = commands.parse_gimbal_attitude(self.SAMPLE_A)
        self.assertAlmostEqual(a.pitch, -173.6, places=4)
        self.assertAlmostEqual(a.yaw, -23.7, places=4)
        self.assertAlmostEqual(a.yaw_alt, 24.2, places=4)

    def test_timestamp_is_monotonic_between_samples(self):
        a = commands.parse_gimbal_attitude(self.SAMPLE_A)
        b = commands.parse_gimbal_attitude(self.SAMPLE_B)
        self.assertEqual(a.timestamp, 5350456)
        self.assertGreater(b.timestamp, a.timestamp)

    def test_quaternion_is_a_unit_quaternion(self):
        # The tell that offsets 24..39 are an attitude quaternion and not
        # four unrelated floats.
        for sample in (self.SAMPLE_A, self.SAMPLE_B):
            with self.subTest(sample=sample[:4].hex()):
                att = commands.parse_gimbal_attitude(sample)
                self.assertEqual(len(att.quaternion), 4)
                self.assertAlmostEqual(att.quaternion_norm, 1.0, places=5)

    def test_rotation_delta_is_zero_for_identical_attitudes(self):
        a = commands.parse_gimbal_attitude(self.SAMPLE_A)
        self.assertAlmostEqual(commands.rotation_delta(a, a), 0.0, places=6)

    def test_rotation_delta_detects_real_movement(self):
        a = commands.parse_gimbal_attitude(self.SAMPLE_A)
        b = commands.parse_gimbal_attitude(self.SAMPLE_B)
        self.assertGreater(commands.rotation_delta(a, b), 1.0)

    def test_rotation_delta_ignores_quaternion_double_cover(self):
        # q and -q are the same orientation; a sign flip must not read as motion.
        a = commands.parse_gimbal_attitude(self.SAMPLE_A)
        negated = commands.GimbalAttitude(
            a.pitch, a.yaw, a.yaw_alt, a.timestamp,
            tuple(-c for c in a.quaternion))
        self.assertAlmostEqual(commands.rotation_delta(a, negated), 0.0, places=4)

    def test_rejects_short_payload(self):
        for n in (0, 5, 12, 39):
            with self.subTest(n=n):
                self.assertIsNone(commands.parse_gimbal_attitude(bytes(n)))

    def test_accepts_minimum_length(self):
        self.assertIsNotNone(commands.parse_gimbal_attitude(bytes(40)))

    def test_deprecated_shim_follows_the_new_parser(self):
        got = commands.parse_gimbal_position(self.SAMPLE_A)
        self.assertEqual(got, (-173.6, -23.7, 24.2))
        self.assertIsNone(commands.parse_gimbal_position(bytes(6)))


class TestAxisMapping(unittest.TestCase):
    def test_range_constants(self):
        self.assertEqual(gimbal.CENTER, 1024)
        self.assertEqual(gimbal.AXIS_MIN, 474)
        self.assertEqual(gimbal.AXIS_MAX, 1574)

    def test_centre_and_deadzone(self):
        self.assertEqual(gimbal.axis(0.0), 1024)
        self.assertEqual(gimbal.axis(0.05), 1024)
        self.assertEqual(gimbal.axis(-0.079), 1024)

    def test_just_outside_deadzone_moves(self):
        self.assertNotEqual(gimbal.axis(0.08), 1024)

    def test_full_throw(self):
        self.assertEqual(gimbal.axis(1.0), 1574)
        self.assertEqual(gimbal.axis(-1.0), 474)

    def test_proportional(self):
        self.assertEqual(gimbal.axis(0.5), 1299)
        self.assertEqual(gimbal.axis(-0.5), 749)

    def test_clamps_out_of_range_input(self):
        self.assertEqual(gimbal.axis(5.0), 1574)
        self.assertEqual(gimbal.axis(-5.0), 474)

    def test_gain_cannot_exceed_hardware_range(self):
        self.assertEqual(gimbal.axis(1.0, gain=4.0), 1574)
        self.assertEqual(gimbal.axis(-1.0, gain=4.0), 474)

    def test_gain_scales_within_range(self):
        # 1024 + 0.5 * 550 * 0.4 = 1134, chosen to avoid a rounding tie.
        self.assertEqual(gimbal.axis(0.5, gain=0.4), 1134)
        self.assertEqual(gimbal.axis(-0.5, gain=0.4), 914)

    def test_every_axis_value_is_wire_legal(self):
        for i in range(-20, 21):
            v = gimbal.axis(i / 10.0)
            self.assertGreaterEqual(v, gimbal.AXIS_MIN)
            self.assertLessEqual(v, gimbal.AXIS_MAX)
            # Must survive the u16 pack in gimbal_stick.
            struct.pack("<H", v)


if __name__ == "__main__":
    unittest.main()
