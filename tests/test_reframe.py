"""Access-unit reframing.

The camera's byte-16 fragment counter advances one NAL late, so every unit the
depacketiser produces ends with the NEXT frame's access unit delimiter. Feeding
those to a decoder gives units one NAL out of step: it decodes sporadically and
stalls, which on hardware looked like an unstable link rather than a framing
error. These pin the correction.
"""

import unittest

from driver import hevc
from driver.hevc import START_CODE as SC


def nal(first_byte: int, body: bytes = b"x") -> bytes:
    return SC + bytes([first_byte]) + body


AUD = nal(0x46)          # type 35
IDR = nal(0x28, b"idr")  # type 20
P = nal(0x02, b"p")      # type 1
SEI = nal(0x50, b"sei")  # type 40
VPS, SPS, PPS = nal(0x40, b"v"), nal(0x42, b"s"), nal(0x44, b"p")


def types(unit: bytes) -> list[int]:
    return [hevc.nal_type(n[0]) for n in hevc.nal_units(unit) if n]


class TestAudReframer(unittest.TestCase):
    def setUp(self):
        self.r = hevc.AudReframer()

    def test_first_partial_unit_yields_nothing(self):
        # No leading delimiter yet, so nothing can be framed.
        self.assertEqual(self.r.feed(IDR + AUD), [])

    def test_every_emitted_unit_starts_with_a_delimiter(self):
        self.r.feed(IDR + AUD)
        out = self.r.feed(P + SEI + AUD) + self.r.feed(P + AUD)
        self.assertTrue(out)
        for au in out:
            self.assertEqual(types(au)[0], hevc.NAL_AUD)

    def test_trailing_delimiter_is_carried_to_the_next_unit(self):
        self.r.feed(IDR + AUD)
        out = self.r.feed(P + SEI + AUD)
        self.assertEqual(len(out), 1)
        self.assertEqual(types(out[0]), [hevc.NAL_AUD, 1, 40])

    def test_slice_and_its_own_delimiter_stay_together(self):
        self.r.feed(AUD)
        out = self.r.feed(IDR + AUD)
        self.assertEqual(types(out[0]), [hevc.NAL_AUD, 20])

    def test_multiple_delimiters_in_one_unit_split_into_several(self):
        self.r.feed(AUD)
        out = self.r.feed(P + AUD + P + AUD + P + AUD)
        self.assertEqual(len(out), 3)
        for au in out:
            self.assertEqual(types(au), [hevc.NAL_AUD, 1])

    def test_units_without_any_delimiter_accumulate(self):
        self.r.feed(AUD)
        self.assertEqual(self.r.feed(P), [])
        out = self.r.feed(SEI + AUD)
        self.assertEqual(types(out[0]), [hevc.NAL_AUD, 1, 40])

    def test_emitted_counter_tracks_output(self):
        self.r.feed(AUD)
        self.r.feed(P + AUD)
        self.r.feed(P + AUD)
        self.assertEqual(self.r.emitted, 2)

    def test_reset_drops_the_carry(self):
        self.r.feed(IDR + AUD)
        self.r.reset()
        self.assertEqual(self.r.carry, b"")
        self.assertEqual(self.r.feed(P + AUD), [])   # nothing to frame against

    def test_parameter_set_unit_survives_reframing(self):
        self.r.feed(AUD)
        out = self.r.feed(VPS + SPS + PPS + AUD)
        self.assertEqual(types(out[0]),
                         [hevc.NAL_AUD, hevc.NAL_VPS, hevc.NAL_SPS, hevc.NAL_PPS])

    def test_aud_positions_finds_only_delimiters(self):
        data = IDR + AUD + P + AUD
        found = list(hevc._aud_positions(data))
        self.assertEqual(len(found), 2)
        for start, header in found:
            self.assertEqual(hevc.nal_type(data[header]), hevc.NAL_AUD)


class TestStripAud(unittest.TestCase):
    def test_removes_delimiters_only(self):
        out = hevc.strip_aud(IDR + SEI + AUD)
        self.assertEqual(types(out), [20, 40])

    def test_leaves_a_unit_without_delimiters_alone(self):
        self.assertEqual(types(hevc.strip_aud(IDR + SEI)), [20, 40])

    def test_empty_result_falls_back_to_the_input(self):
        # A unit that is nothing but a delimiter must not become empty bytes.
        self.assertEqual(hevc.strip_aud(AUD), AUD)


if __name__ == "__main__":
    unittest.main()


class TestCarryIsBounded(unittest.TestCase):
    """The leak that killed a running panel.

    One delimiter arrives, the one that would close the unit never does, and
    the carry absorbs every packet after it. Memory grows linearly; worse, the
    delimiter search rescans the whole carry on every packet, so CPU grows
    quadratically. A panel left streaming for about ninety minutes died with a
    MemoryError, and the decoder starved throughout because the reframer was
    emitting nothing while it swallowed the stream.

    The zero-delimiter case was always handled -- the head is dropped. It is
    exactly-one-delimiter that runs away.
    """

    AUD = hevc.START_CODE + bytes([hevc.NAL_AUD << 1, 1])
    JUNK = hevc.START_CODE + bytes([0x02]) + b"x" * 2000

    def test_one_delimiter_then_silence_does_not_grow_without_limit(self):
        r = hevc.AudReframer()
        r.feed(self.AUD + self.JUNK)          # unit opened, never closed
        for _ in range(3000):
            r.feed(self.JUNK)
        self.assertLessEqual(len(r.carry), hevc.MAX_CARRY)

    def test_the_drop_is_counted_rather_than_silent(self):
        r = hevc.AudReframer()
        r.feed(self.AUD + self.JUNK)
        for _ in range(500):
            r.feed(self.JUNK)
        self.assertGreater(r.overflows, 0)

    def test_no_delimiter_at_all_still_just_drops_the_head(self):
        r = hevc.AudReframer()
        for _ in range(200):
            r.feed(self.JUNK)
        self.assertEqual(len(r.carry), 0)
        self.assertEqual(r.overflows, 0, "this path was never the leak")

    def test_it_recovers_once_delimiters_come_back(self):
        """Dropping the carry must not wedge the reframer permanently."""
        r = hevc.AudReframer()
        r.feed(self.AUD + self.JUNK)
        for _ in range(400):
            r.feed(self.JUNK)                 # force at least one overflow
        self.assertGreater(r.overflows, 0)
        before = r.emitted
        for _ in range(3):                    # a healthy stream resumes
            r.feed(self.AUD + self.JUNK)
        self.assertGreater(r.emitted, before, "reframer never recovered")

    def test_a_normal_stream_is_untouched_by_the_bound(self):
        r = hevc.AudReframer()
        for _ in range(50):
            r.feed(self.AUD + self.JUNK)
        self.assertEqual(r.overflows, 0)
        self.assertLess(len(r.carry), 4000, "a healthy carry is one unit")

    def test_the_ceiling_leaves_room_for_a_real_access_unit(self):
        """Measured units on this camera are about 2.2 KB. The bound has to be
        generous enough that a large intra frame is never mistaken for a fault."""
        self.assertGreater(hevc.MAX_CARRY, 100_000)
        self.assertLess(hevc.MAX_CARRY, hevc.MAX_ACCESS_UNIT)
