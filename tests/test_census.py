"""The frame census.

The driver subscribes to five camera status streams and decodes none of them,
which is why the monitor cannot prove the camera is recording. This is the
instrument for reading them: it watches what arrives and reports which bytes
move, because a byte that flips exactly when the operator presses record is
the record flag and you do not have to guess the format to find it.
"""

import unittest
from types import SimpleNamespace

from driver import census
from driver.census import FrameCensus, OpcodeRecord


def frame(cmd_set, cmd_id, payload=b""):
    return SimpleNamespace(cmd_set=cmd_set, cmd_id=cmd_id, payload=payload)


class TestCounting(unittest.TestCase):
    def setUp(self):
        self.c = FrameCensus()

    def test_it_counts_each_opcode(self):
        for _ in range(5):
            self.c.note(frame(0x02, 0x10, b"\x00"))
        self.c.note(frame(0x02, 0x11, b"\x00"))
        rows = {r["opcode"]: r for r in self.c.report()["rows"]}
        self.assertEqual(rows["0x02/0x10"]["count"], 5)
        self.assertEqual(rows["0x02/0x11"]["count"], 1)

    def test_busiest_first(self):
        self.c.note(frame(0x01, 0x01))
        for _ in range(9):
            self.c.note(frame(0x02, 0x02))
        self.assertEqual(self.c.report()["rows"][0]["opcode"], "0x02/0x02")

    def test_it_records_the_lengths_seen(self):
        self.c.note(frame(0x02, 0x10, b"\x00" * 4))
        self.c.note(frame(0x02, 0x10, b"\x00" * 9))
        self.assertEqual(self.c.report()["rows"][0]["lengths"], [4, 9])

    def test_what_we_already_decode_is_labelled(self):
        """So the report says what is genuinely unread rather than burying it
        among the things that are fine."""
        self.c.note(frame(0x04, 0x05, b"\x00"))
        self.c.note(frame(0x02, 0x10, b"\x00"))
        rows = {r["opcode"]: r for r in self.c.report()["rows"]}
        self.assertEqual(rows["0x04/0x05"]["known"], "gimbal attitude")
        self.assertIsNone(rows["0x02/0x10"]["known"])

    def test_unknown_only_hides_what_we_read(self):
        self.c.note(frame(0x04, 0x05, b"\x00"))
        self.c.note(frame(0x02, 0x10, b"\x00"))
        rows = self.c.report(unknown_only=True)["rows"]
        self.assertEqual([r["opcode"] for r in rows], ["0x02/0x10"])


class TestByteVolatility(unittest.TestCase):
    """The useful part. Constant bytes are structure; moving bytes carry the
    meaning, and you find them by watching rather than by guessing."""

    def setUp(self):
        self.c = FrameCensus()

    def test_a_byte_that_never_changes_is_reported_constant(self):
        for _ in range(10):
            self.c.note(frame(0x02, 0x10, b"\xAA\x00"))
        row = self.c.report()["rows"][0]
        self.assertEqual(row["constant_bytes"]["0"], 0xAA)
        self.assertNotIn(0, row["volatile_bytes"])

    def test_a_flag_that_flips_is_reported_volatile(self):
        """This is the record flag, found without knowing the format."""
        self.c.note(frame(0x02, 0x10, b"\xAA\x00"))
        self.c.note(frame(0x02, 0x10, b"\xAA\x01"))
        row = self.c.report()["rows"][0]
        self.assertEqual(row["volatile_bytes"], [1])
        self.assertEqual(list(row["constant_bytes"]), ["0"])

    def test_a_counter_is_volatile_even_past_the_value_cap(self):
        """A steadily climbing byte must not look constant once the set of
        remembered values fills up."""
        for i in range(40):
            self.c.note(frame(0x02, 0x10, bytes([0xAA, i])))
        self.assertIn(1, self.c.report()["rows"][0]["volatile_bytes"])

    def test_a_byte_only_present_on_longer_frames_is_never_called_constant(self):
        """It was absent, not unchanging. Reporting it as structure would be
        a lie about a field that may not exist in the short form."""
        self.c.note(frame(0x02, 0x10, b"\xAA"))
        for _ in range(5):
            self.c.note(frame(0x02, 0x10, b"\xAA\x07"))
        row = self.c.report()["rows"][0]
        self.assertIn(1, row["volatile_bytes"])
        self.assertNotIn("1", row["constant_bytes"])

    def test_long_payloads_are_tracked_only_to_the_cap(self):
        self.c.note(frame(0x02, 0x10, bytes(300)))
        row = self.c.report()["rows"][0]
        self.assertLessEqual(len(row["constant_bytes"]) + len(row["volatile_bytes"]),
                             census.MAX_TRACKED_BYTES)


class TestSamples(unittest.TestCase):
    def test_the_first_payload_is_kept(self):
        c = FrameCensus()
        c.note(frame(0x02, 0x10, b"\x01\x02"))
        self.assertEqual(c.report()["rows"][0]["samples"], ["01 02"])

    def test_only_differing_payloads_are_kept(self):
        c = FrameCensus()
        for _ in range(20):
            c.note(frame(0x02, 0x10, b"\x01\x02"))
        c.note(frame(0x02, 0x10, b"\x01\x03"))
        self.assertEqual(c.report()["rows"][0]["samples"], ["01 02", "01 03"])

    def test_samples_are_bounded(self):
        c = FrameCensus()
        for i in range(50):
            c.note(frame(0x02, 0x10, bytes([i])))
        self.assertLessEqual(len(c.report()["rows"][0]["samples"]),
                             census.MAX_SAMPLES)


class TestMarks(unittest.TestCase):
    """Segmenting the capture is what identifies the record flag: press
    record, mark it, stop, mark it, then compare the segments."""

    def test_marks_are_timestamped_and_reported(self):
        c = FrameCensus()
        c.mark("record started")
        c.mark("record stopped")
        marks = c.report()["marks"]
        self.assertEqual([m["label"] for m in marks],
                         ["record started", "record stopped"])
        self.assertGreaterEqual(marks[0]["at"], 0.0)

    def test_a_silly_label_is_truncated_not_refused(self):
        c = FrameCensus()
        c.mark("x" * 500)
        self.assertLessEqual(len(c.report()["marks"][0]["label"]), 60)


class TestItCannotTakeTheLinkDown(unittest.TestCase):
    """note() runs on the datalink read thread. An exception there would drop
    the camera connection over a diagnostic."""

    def test_a_malformed_frame_is_ignored(self):
        c = FrameCensus()
        for bad in (SimpleNamespace(), SimpleNamespace(cmd_set="x", cmd_id=1, payload=b""),
                    SimpleNamespace(cmd_set=1, cmd_id=1, payload=None)):
            c.note(bad)
        self.assertLessEqual(c.report()["opcodes"], 1)

    def test_a_none_payload_counts_as_empty_rather_than_crashing(self):
        c = FrameCensus()
        c.note(SimpleNamespace(cmd_set=2, cmd_id=3, payload=None))
        self.assertEqual(c.report()["rows"][0]["lengths"], [0])

    def test_an_unbounded_stream_of_opcodes_is_capped(self):
        """A garbled link must not grow the report without limit."""
        c = FrameCensus(max_opcodes=8)
        for i in range(50):
            c.note(frame(0x02, i, b"\x00"))
        self.assertEqual(c.report()["opcodes"], 8)
        self.assertGreater(c.report()["dropped_opcodes"], 0)


class TestReset(unittest.TestCase):
    def test_it_clears_everything(self):
        c = FrameCensus()
        c.note(frame(0x02, 0x10, b"\x00"))
        c.mark("something")
        c.reset()
        r = c.report()
        self.assertEqual(r["rows"], [])
        self.assertEqual(r["marks"], [])
        self.assertEqual(r["frames"], 0)


if __name__ == "__main__":
    unittest.main()
