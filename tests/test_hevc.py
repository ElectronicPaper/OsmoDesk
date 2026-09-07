"""HEVC depacketiser and Annex-B helpers.

Pure byte handling, no decoder and no hardware. Reassembly is the part that
fails invisibly: hand a decoder one broken access unit and it stalls, which
looks like "live view doesn't work" rather than "one datagram was lost".
"""

import unittest

from driver import hevc


def pkt(frame_no: int, position: int, body: bytes, pkt_type: int = 0x02) -> bytes:
    """Build a datalink video datagram: pktType @6, frame @16, position @17-18."""
    p = bytearray(20)
    p[6] = pkt_type
    p[16] = frame_no
    p[18] = position // 2
    p[17] = 0x80 if position % 2 else 0x00
    return bytes(p) + body


NAL_SLICE = b"\x00\x00\x01\x40"      # nal type 32 >> vps... see below
START = b"\x00\x00\x01"


class TestNalHelpers(unittest.TestCase):
    def test_nal_type_extraction(self):
        # type = (byte >> 1) & 0x3f
        self.assertEqual(hevc.nal_type(0x40), 32)   # VPS
        self.assertEqual(hevc.nal_type(0x42), 33)   # SPS
        self.assertEqual(hevc.nal_type(0x44), 34)   # PPS
        self.assertEqual(hevc.nal_type(0x28), 20)   # IDR_N_LP
        self.assertEqual(hevc.nal_type(0xFE), 63)   # DJI marker

    def test_vcl_classification(self):
        self.assertTrue(hevc.is_vcl(0))
        self.assertTrue(hevc.is_vcl(31))
        self.assertFalse(hevc.is_vcl(32))

    def test_keyframe_nals(self):
        for t in (hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS, hevc.NAL_IDR):
            self.assertTrue(hevc.is_keyframe_nal(t))
        self.assertFalse(hevc.is_keyframe_nal(1))
        self.assertFalse(hevc.is_keyframe_nal(hevc.NAL_DJI_MARKER))


class TestStripDjiMarker(unittest.TestCase):
    """DJI prefixes each frame with a private NAL type 63. A standard decoder
    rejects the unit unless it is removed."""

    def test_removes_the_marker(self):
        data = START + b"\xfe" + b"junk" + START + b"\x40payload"
        self.assertEqual(hevc.strip_dji_marker(data), START + b"\x40payload")

    def test_leaves_a_clean_unit_alone(self):
        data = START + b"\x40payload"
        self.assertEqual(hevc.strip_dji_marker(data), data)

    def test_returns_input_when_no_start_code(self):
        self.assertEqual(hevc.strip_dji_marker(b"\x01\x02\x03"), b"\x01\x02\x03")

    def test_handles_empty(self):
        self.assertEqual(hevc.strip_dji_marker(b""), b"")


class TestNalSplitting(unittest.TestCase):
    def test_splits_on_start_codes(self):
        data = START + b"\x40aaa" + START + b"\x42bbb"
        self.assertEqual(hevc.nal_units(data), [b"\x40aaa", b"\x42bbb"])

    def test_handles_four_byte_start_codes(self):
        data = b"\x00" + START + b"\x40aaa" + b"\x00" + START + b"\x42bbb"
        self.assertEqual(hevc.nal_units(data), [b"\x40aaa", b"\x42bbb"])

    def test_no_start_codes_yields_nothing(self):
        self.assertEqual(hevc.nal_units(b"nothing here"), [])

    def test_has_keyframe_requires_a_random_access_slice(self):
        """Parameter sets alone must NOT count.

        Starting the decoder on a unit carrying only VPS/SPS/PPS gives it a
        configuration but no reference picture. It then accepts every later
        packet and silently returns no frame -- no error, just a stream that
        decodes nothing. Whether that happened depended on which unit we
        joined on, so the failure looked intermittent on hardware.
        """
        self.assertFalse(hevc.has_keyframe(START + b"@vps"))
        self.assertFalse(hevc.has_keyframe(START + b"Bsps"))
        self.assertFalse(hevc.has_keyframe(START + b"Dpps"))
        self.assertFalse(hevc.has_keyframe(START + b"slice"))

    def test_has_keyframe_accepts_every_random_access_type(self):
        for first, name in ((0x26, "IDR_W_RADL"), (0x28, "IDR_N_LP"), (0x2A, "CRA")):
            with self.subTest(nal=name):
                self.assertTrue(hevc.has_keyframe(START + bytes([first]) + b"slice"))

    def test_parameter_sets_alongside_an_idr_are_accepted(self):
        # The normal case: the camera sends VPS+SPS+PPS+IDR in one unit.
        unit = (START + b"@vps" + START + b"Bsps"
                + START + b"Dpps" + START + b"(idr")
        self.assertTrue(hevc.has_keyframe(unit))

    def test_random_access_slice_classification(self):
        for t in (19, 20, 21):
            self.assertTrue(hevc.is_random_access_slice(t))
        for t in (0, 1, 32, 33, 34, 63):
            self.assertFalse(hevc.is_random_access_slice(t))

class TestDepacketizer(unittest.TestCase):
    def setUp(self):
        self.d = hevc.HevcDepacketizer()

    def test_ignores_non_video_packets(self):
        self.assertIsNone(self.d.feed(pkt(1, 0, b"x", pkt_type=0x01)))
        self.assertIsNone(self.d.feed(pkt(1, 0, b"x", pkt_type=0x05)))

    def test_ignores_short_packets(self):
        self.assertIsNone(self.d.feed(b"\x00" * 12))

    def test_reassembles_fragments_in_order(self):
        self.assertIsNone(self.d.feed(pkt(1, 0, START + b"\x40hello")))
        self.assertIsNone(self.d.feed(pkt(1, 1, b"world")))
        # The frame is only complete once the NEXT frame starts.
        out = self.d.feed(pkt(2, 0, b"next"))
        self.assertEqual(out, START + b"\x40helloworld")

    def test_strips_the_dji_marker_from_the_emitted_unit(self):
        self.d.feed(pkt(1, 0, START + b"\xfemarker" + START + b"\x40real"))
        out = self.d.feed(pkt(2, 0, b"x"))
        self.assertEqual(out, START + b"\x40real")

    def test_one_frame_of_latency_is_inherent(self):
        # Nothing marks the last fragment, so a single frame never completes.
        self.d.feed(pkt(1, 0, START + b"\x40only"))
        self.assertIsNone(self.d.feed(pkt(1, 1, b"more")))
        self.assertEqual(self.d.frames_emitted, 0)

    def test_a_lost_fragment_drops_the_frame(self):
        self.d.feed(pkt(1, 0, b"a"))
        self.d.feed(pkt(1, 2, b"c"))          # position 1 never arrived
        self.assertIsNone(self.d.feed(pkt(2, 0, b"x")))
        self.assertEqual(self.d.dropped_incomplete, 1)
        self.assertEqual(self.d.frames_emitted, 0)

    def test_recovers_after_a_dropped_frame(self):
        self.d.feed(pkt(1, 0, b"a"))
        self.d.feed(pkt(1, 2, b"c"))
        self.d.feed(pkt(2, 0, START + b"\x40good"))
        out = self.d.feed(pkt(3, 0, b"x"))
        self.assertEqual(out, START + b"\x40good")
        self.assertEqual(self.d.dropped_incomplete, 1)
        self.assertEqual(self.d.frames_emitted, 1)

    def test_position_decoding_uses_both_bytes(self):
        # position = byte18 * 2 + byte17 >> 7, so it must increment cleanly
        # across the odd/even boundary rather than reading as a jump.
        self.d.feed(pkt(1, 0, b"a"))
        self.d.feed(pkt(1, 1, b"b"))
        self.d.feed(pkt(1, 2, b"c"))
        self.d.feed(pkt(1, 3, b"d"))
        out = self.d.feed(pkt(2, 0, b"x"))
        self.assertEqual(out, b"abcd")
        self.assertEqual(self.d.dropped_incomplete, 0)

    def test_frame_counter_wraps(self):
        self.d.feed(pkt(255, 0, START + b"\x40wrap"))
        out = self.d.feed(pkt(0, 0, b"x"))
        self.assertEqual(out, START + b"\x40wrap")

    def test_reset_clears_everything(self):
        self.d.feed(pkt(1, 0, b"a"))
        self.d.reset()
        self.assertIsNone(self.d.current_frame)
        self.assertEqual(self.d.dropped_incomplete, 0)
        self.assertEqual(len(self.d.buffer), 0)


if __name__ == "__main__":
    unittest.main()
