"""Cube LUT parsing.

A LUT pipeline fails quietly: a transposed cube or a short table produces a
picture that looks graded rather than broken, and the operator works against
it for an hour. These pin the failures that do not announce themselves.
"""

import unittest

from driver import lut
from driver.lut import Lut, LutError, identity, parse_cube


def cube_3d(size=2, rows=None, header="", title=None):
    """Build a minimal valid .cube, or a deliberately broken one."""
    lines = []
    if title is not None:
        lines.append(f'TITLE "{title}"')
    lines.append(f"LUT_3D_SIZE {size}")
    if header:
        lines.append(header)
    if rows is None:
        rows = []
        last = size - 1
        for b in range(size):
            for g in range(size):
                for r in range(size):
                    rows.append(f"{r/last:.6f} {g/last:.6f} {b/last:.6f}")
    lines.extend(rows)
    return "\n".join(lines) + "\n"


class TestParsingAValidFile(unittest.TestCase):
    def test_a_minimal_cube_parses(self):
        table = parse_cube(cube_3d(size=2))
        self.assertEqual(table.size, 2)
        self.assertEqual(table.dimensions, 3)
        self.assertEqual(len(table.data), 2 ** 3 * 3)

    def test_the_title_survives_spaces_and_quotes(self):
        self.assertEqual(parse_cube(cube_3d(title="Kodak 2383 D65")).title,
                         "Kodak 2383 D65")

    def test_comments_and_blank_lines_are_ignored(self):
        text = "# a comment\n\n" + cube_3d(size=2) + "\n# trailing\n"
        self.assertEqual(parse_cube(text).size, 2)

    def test_a_comment_after_a_row_is_stripped(self):
        rows = ["0 0 0  # black"] + ["1 1 1"] * 7
        self.assertEqual(len(parse_cube(cube_3d(size=2, rows=rows)).data), 24)

    def test_the_domain_defaults_to_zero_one(self):
        table = parse_cube(cube_3d())
        self.assertEqual(table.domain_min, (0.0, 0.0, 0.0))
        self.assertEqual(table.domain_max, (1.0, 1.0, 1.0))

    def test_an_explicit_domain_is_kept(self):
        table = parse_cube(cube_3d(header="DOMAIN_MAX 4.0 4.0 4.0"))
        self.assertEqual(table.domain_max, (4.0, 4.0, 4.0))

    def test_a_one_dimensional_lut_parses(self):
        text = "LUT_1D_SIZE 4\n" + "\n".join(["0 0 0"] * 4) + "\n"
        table = parse_cube(text)
        self.assertEqual(table.dimensions, 1)
        self.assertEqual(table.entries, 4)


class TestRedVariesFastest(unittest.TestCase):
    """Get the axis order wrong and the cube is transposed. That swaps the
    colour axes, which reads as a look rather than as a bug."""

    def test_the_second_row_is_the_red_axis_not_the_blue(self):
        table = parse_cube(cube_3d(size=2))
        # Row index 1 is (r=1, g=0, b=0) if red varies fastest.
        self.assertEqual(table.data[3:6], [1.0, 0.0, 0.0])

    def test_identity_maps_each_corner_to_itself(self):
        table = identity(size=5)
        for r, g, b in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)):
            with self.subTest(corner=(r, g, b)):
                out = table.sample_nearest(float(r), float(g), float(b))
                self.assertEqual([round(v, 6) for v in out], [r, g, b])

    def test_a_parsed_identity_round_trips_through_the_text_format(self):
        """The parser and the generator have to agree, or every look is
        shifted by whatever they disagree about."""
        original = identity(size=4)
        rows = [f"{original.data[i]} {original.data[i+1]} {original.data[i+2]}"
                for i in range(0, len(original.data), 3)]
        back = parse_cube(cube_3d(size=4, rows=rows))
        self.assertEqual(back.data, original.data)


class TestRejectingBadFiles(unittest.TestCase):
    """Every one of these produces a plausible-looking picture if accepted."""

    def assert_rejects(self, text, needle):
        with self.assertRaises(LutError) as caught:
            parse_cube(text)
        self.assertIn(needle, str(caught.exception).lower())

    def test_a_file_with_no_size_directive(self):
        self.assert_rejects("0 0 0\n1 1 1\n", "not a .cube file")

    def test_a_short_table(self):
        self.assert_rejects(cube_3d(size=2, rows=["0 0 0"] * 7), "found 7")

    def test_a_long_table(self):
        self.assert_rejects(cube_3d(size=2, rows=["0 0 0"] * 9), "found 9")

    def test_a_row_with_two_numbers(self):
        rows = ["0 0"] + ["0 0 0"] * 7
        self.assert_rejects(cube_3d(size=2, rows=rows), "needs 3 numbers")

    def test_a_row_that_is_not_numeric(self):
        rows = ["nan_but_text 0 0"] + ["0 0 0"] * 7
        self.assert_rejects(cube_3d(size=2, rows=rows), "not a number")

    def test_a_non_finite_value(self):
        rows = ["inf 0 0"] + ["0 0 0"] * 7
        self.assert_rejects(cube_3d(size=2, rows=rows), "not finite")

    def test_a_size_of_one(self):
        self.assert_rejects("LUT_3D_SIZE 1\n0 0 0\n", "outside")

    def test_a_size_beyond_what_a_monitor_needs(self):
        self.assert_rejects(f"LUT_3D_SIZE {lut.MAX_3D_SIZE + 1}\n", "outside")

    def test_a_size_declared_twice(self):
        self.assert_rejects("LUT_3D_SIZE 2\nLUT_3D_SIZE 4\n", "twice")

    def test_an_inverted_domain(self):
        self.assert_rejects(cube_3d(header="DOMAIN_MAX 0.0 1.0 1.0"),
                            "greater than")

    def test_an_unknown_lut_directive_is_refused_not_ignored(self):
        """A dialect this parser does not understand is not something to guess
        at -- it is a different colour pipeline."""
        self.assert_rejects("LUT_3D_SIZE 2\nLUT_3D_INPUT_RANGE 0 1\n",
                            "unsupported directive")


class TestIdentity(unittest.TestCase):
    def test_it_has_the_right_number_of_entries(self):
        self.assertEqual(len(identity(size=8).data), 8 ** 3 * 3)

    def test_it_refuses_a_size_it_cannot_represent(self):
        with self.assertRaises(LutError):
            identity(size=1)

    def test_it_is_serialisable_for_the_viewer(self):
        d = identity(size=2).to_dict()
        self.assertEqual(d["size"], 2)
        self.assertEqual(d["dimensions"], 3)
        self.assertEqual(len(d["data"]), 24)


class TestDomainScaling(unittest.TestCase):
    def test_a_wide_domain_moves_where_a_value_lands(self):
        """A log LUT with DOMAIN_MAX above 1 indexes differently. Ignoring the
        domain silently crushes the top of the curve."""
        rows = [f"{i/7:.6f} 0 0" for i in range(8)]
        wide = parse_cube(cube_3d(size=2, rows=rows,
                                  header="DOMAIN_MAX 2.0 2.0 2.0"))
        narrow = parse_cube(cube_3d(size=2, rows=rows))
        self.assertNotEqual(wide.sample_nearest(1.0, 0.0, 0.0),
                            narrow.sample_nearest(1.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
