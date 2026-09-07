"""The shot library.

Two things matter here. One is that a move saved this week loads next week
with its rigging notes intact. The other is that the name comes from a text
box and ends up in a filesystem path, which is the shape of a traversal bug.
"""

import json
import tempfile
import unittest
from pathlib import Path

from driver import moves
from driver.library import Entry, Library, LibraryError, safe_stem


def a_move(name="untitled", n=3):
    return moves.Move(
        name=name,
        waypoints=[moves.Waypoint(f"n{i}", pitch=float(i), yaw=float(i * 10),
                                  duration=2.0, flow=(0 < i < n - 1))
                   for i in range(n)],
        setup={"mount": "tripod", "height_cm": 120, "route": "hallway"},
    )


class TestNameSafety(unittest.TestCase):
    """An allow-list, not a blocklist. Anything not known good is dropped."""

    def test_a_traversal_attempt_keeps_only_its_letters(self):
        self.assertEqual(safe_stem("../../etc/passwd"), "etcpasswd")

    def test_a_windows_traversal_attempt_too(self):
        self.assertEqual(safe_stem(r"..\..\Windows\System32"),
                         "WindowsSystem32")

    def test_an_absolute_path_loses_its_root(self):
        self.assertEqual(safe_stem("/etc/shadow"), "etcshadow")

    def test_a_nul_byte_is_dropped(self):
        self.assertEqual(safe_stem("shot\x00.json"), "shot.json")

    def test_ordinary_names_survive_readably(self):
        for given, want in (
            ("Scene 4 - reveal", "Scene 4 - reveal"),
            ("wide_push", "wide push"),
            ("  padded  ", "padded"),
        ):
            with self.subTest(given=given):
                self.assertEqual(safe_stem(given), want)

    def test_a_name_with_nothing_usable_is_refused(self):
        for bad in ("", "   ", "/////", "..", "***"):
            with self.subTest(name=bad):
                with self.assertRaises(LibraryError):
                    safe_stem(bad)

    def test_a_windows_device_name_is_refused(self):
        """CON.move.json cannot be created on Windows. Better a clear refusal
        than a save that works on one machine and not another."""
        for bad in ("con", "CON", "lpt1", "NUL"):
            with self.subTest(name=bad):
                with self.assertRaises(LibraryError):
                    safe_stem(bad)

    def test_an_overlong_name_is_trimmed_not_refused(self):
        stem = safe_stem("x" * 400)
        self.assertEqual(len(stem), 64)

    def test_two_unicode_spellings_of_one_name_collapse(self):
        """Composed and decomposed forms look identical in a list. Two files
        the operator cannot tell apart is worse than one."""
        self.assertEqual(safe_stem("café"), safe_stem("café"))


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lib = Library(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def test_a_saved_move_loads_back_the_same(self):
        self.lib.save("Scene 4", a_move())
        back = self.lib.load("Scene 4")
        self.assertEqual(len(back.waypoints), 3)
        self.assertEqual(back.waypoints[1].yaw, 10.0)

    def test_the_rigging_notes_travel_with_it(self):
        """Angles alone do not recreate a frame."""
        self.lib.save("Scene 4", a_move())
        self.assertEqual(self.lib.load("Scene 4").setup["mount"], "tripod")

    def test_flow_marks_survive(self):
        self.lib.save("Scene 4", a_move())
        self.assertTrue(self.lib.load("Scene 4").uses_flow)

    def test_saving_the_same_name_replaces(self):
        self.lib.save("Scene 4", a_move(n=3))
        self.lib.save("Scene 4", a_move(n=5))
        self.assertEqual(len(self.lib.load("Scene 4").waypoints), 5)
        self.assertEqual(len(self.lib.list()), 1)

    def test_two_spellings_of_a_name_find_the_same_move(self):
        self.lib.save("wide push", a_move())
        self.assertEqual(len(self.lib.load("wide_push").waypoints), 3)

    def test_a_one_waypoint_move_is_refused(self):
        """Nothing to run. Saving it fills the picker with dead entries."""
        with self.assertRaises(LibraryError):
            self.lib.save("stub", moves.Move(waypoints=[
                moves.Waypoint("a", 0.0, 0.0)]))

    def test_loading_something_that_is_not_there(self):
        with self.assertRaises(LibraryError):
            self.lib.load("never saved")


class TestListing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lib = Library(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def test_an_empty_library_lists_nothing_rather_than_failing(self):
        self.assertEqual(Library(self.dir.name + "/nope").list(), [])

    def test_entries_carry_what_the_picker_shows(self):
        self.lib.save("Scene 4", a_move())
        e = self.lib.list()[0]
        self.assertEqual(e.name, "Scene 4")
        self.assertEqual(e.waypoints, 3)
        self.assertGreater(e.duration, 0)
        self.assertIn("tripod", e.setup)

    def test_newest_first(self):
        self.lib.save("older", a_move())
        entry = self.lib.save("newer", a_move())
        self.assertEqual(self.lib.list()[0].name, "newer")
        self.assertGreater(entry.saved_at, 0)

    def test_one_corrupt_file_does_not_hide_the_others(self):
        """The moment an operator most needs the other nine moves is when one
        of them is broken."""
        self.lib.save("good one", a_move())
        Path(self.dir.name, "broken.move.json").write_text("{not json",
                                                           encoding="utf-8")
        names = [e.name for e in self.lib.list()]
        self.assertEqual(names, ["good one"])

    def test_unrelated_files_are_ignored(self):
        Path(self.dir.name, "notes.txt").write_text("hello", encoding="utf-8")
        self.lib.save("real", a_move())
        self.assertEqual([e.name for e in self.lib.list()], ["real"])


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lib = Library(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def test_it_removes_the_move(self):
        self.lib.save("Scene 4", a_move())
        self.assertTrue(self.lib.delete("Scene 4"))
        self.assertEqual(self.lib.list(), [])

    def test_deleting_what_is_not_there_says_so_rather_than_raising(self):
        self.assertFalse(self.lib.delete("never saved"))

    def test_a_traversal_name_cannot_reach_outside(self):
        outside = Path(self.dir.name).parent / "do-not-touch.move.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.lib.delete("../do-not-touch")
        self.assertTrue(outside.exists(), "delete escaped the library folder")


class TestTheSecondLayerHolds(unittest.TestCase):
    """`_path` re-checks containment after safe_stem has already made escape
    impossible. That makes it unreachable through the public API, so nothing
    exercised it -- removing the check broke no test, which is how a guard
    quietly stops working. Reached here by neutering the first layer.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lib = Library(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def test_containment_still_refuses_when_safe_stem_is_bypassed(self):
        from driver import library as lib_mod
        original = lib_mod.safe_stem
        lib_mod.safe_stem = lambda n: n          # first layer disabled
        try:
            with self.assertRaises(LibraryError):
                self.lib._path("../escaped")
        finally:
            lib_mod.safe_stem = original

    def test_an_ordinary_name_still_resolves_inside(self):
        """The guard must not be refusing everything."""
        path = self.lib._path("Scene 4")
        self.assertEqual(Path(self.dir.name).resolve(), path.parent)


class TestAtomicWrite(unittest.TestCase):
    def test_no_temporary_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            lib.save("Scene 4", a_move())
            self.assertEqual([p.name for p in Path(d).glob("*.tmp")], [])

    def test_the_file_on_disk_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(d)
            lib.save("Scene 4", a_move())
            raw = json.loads(next(Path(d).glob("*.move.json"))
                             .read_text(encoding="utf-8"))
            self.assertEqual(raw["name"], "Scene 4")
            self.assertIn("waypoints", raw)


if __name__ == "__main__":
    unittest.main()
