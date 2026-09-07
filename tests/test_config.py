"""Credentials-file parsing, merging and masking.

The merge path rewrites a file whose contents must not be displayed, so
"unrelated keys and comments survive" is a correctness requirement, not a nicety.
"""

import tempfile
import unittest
from pathlib import Path

from driver import config


class TestParseEnv(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(config.parse_env("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_ignores_blanks_and_comments(self):
        text = "\n# a comment\n\nA=1\n   # indented comment\nB=2\n"
        self.assertEqual(config.parse_env(text), {"A": "1", "B": "2"})

    def test_strips_export_prefix(self):
        self.assertEqual(config.parse_env("export A=1\n"), {"A": "1"})

    def test_strips_matching_quotes(self):
        self.assertEqual(config.parse_env('A="hunter2"\n'), {"A": "hunter2"})
        self.assertEqual(config.parse_env("A='hunter2'\n"), {"A": "hunter2"})

    def test_keeps_mismatched_quotes(self):
        self.assertEqual(config.parse_env("A=\"oops'\n"), {"A": "\"oops'"})

    def test_value_may_contain_equals(self):
        self.assertEqual(config.parse_env("A=a=b=c\n"), {"A": "a=b=c"})

    def test_preserves_inner_spaces(self):
        self.assertEqual(config.parse_env('A="two words"\n'), {"A": "two words"})

    def test_skips_malformed_lines(self):
        self.assertEqual(config.parse_env("no_equals_here\n=novalue\n9BAD=x\nA=1\n"), {"A": "1"})

    def test_empty_value(self):
        self.assertEqual(config.parse_env("A=\n"), {"A": ""})

    def test_last_wins_on_duplicate(self):
        self.assertEqual(config.parse_env("A=1\nA=2\n"), {"A": "2"})


class TestMergeEnv(unittest.TestCase):
    def test_replaces_in_place(self):
        out = config.merge_env("A=1\nB=2\n", {"A": "9"})
        self.assertEqual(config.parse_env(out), {"A": "9", "B": "2"})
        self.assertLess(out.index("A="), out.index("B="))  # order held

    def test_appends_new_keys(self):
        out = config.merge_env("A=1\n", {"B": "2"})
        self.assertEqual(config.parse_env(out), {"A": "1", "B": "2"})

    def test_preserves_comments(self):
        out = config.merge_env("# keep me\nA=1\n", {"A": "9"})
        self.assertIn("# keep me", out)

    def test_does_not_touch_unrelated_keys(self):
        # The whole point: we rewrite a file we are not allowed to read back.
        original = "# notes\nOTHER_SECRET=untouched\nOSMO_SSID=old\n"
        out = config.merge_env(original, {"OSMO_SSID": "new"})
        parsed = config.parse_env(out)
        self.assertEqual(parsed["OTHER_SECRET"], "untouched")
        self.assertEqual(parsed["OSMO_SSID"], "new")
        self.assertIn("# notes", out)

    def test_replaces_exported_key(self):
        out = config.merge_env("export A=1\n", {"A": "9"})
        self.assertEqual(config.parse_env(out), {"A": "9"})

    def test_does_not_match_inside_a_comment(self):
        out = config.merge_env("# A=1\nB=2\n", {"A": "9"})
        self.assertIn("# A=1", out)
        self.assertEqual(config.parse_env(out), {"B": "2", "A": "9"})

    def test_from_empty(self):
        self.assertEqual(config.parse_env(config.merge_env("", {"A": "1"})), {"A": "1"})

    def test_quotes_values_needing_it(self):
        for value in ("two words", "has#hash", 'has"quote', ""):
            with self.subTest(value=value):
                out = config.merge_env("", {"A": value})
                self.assertEqual(config.parse_env(out).get("A"), value)

    def test_output_ends_with_newline(self):
        self.assertTrue(config.merge_env("A=1", {"B": "2"}).endswith("\n"))

    def test_roundtrip_is_stable(self):
        once = config.merge_env("A=1\n", {"B": "two words"})
        twice = config.merge_env(once, {"B": "two words"})
        self.assertEqual(config.parse_env(once), config.parse_env(twice))


class TestResolve(unittest.TestCase):
    def test_canonical_key(self):
        self.assertEqual(config.resolve({"OSMO_SSID": "x"}, "ssid"), "x")

    def test_alias_fallback(self):
        self.assertEqual(config.resolve({"WIFI_SSID": "x"}, "ssid"), "x")
        self.assertEqual(config.resolve({"SSID": "x"}, "ssid"), "x")

    def test_precedence_is_most_specific_first(self):
        env = {"SSID": "generic", "WIFI_SSID": "middle", "OSMO_SSID": "specific"}
        self.assertEqual(config.resolve(env, "ssid"), "specific")

    def test_empty_value_is_not_a_hit(self):
        self.assertEqual(config.resolve({"OSMO_SSID": "", "SSID": "x"}, "ssid"), "x")

    def test_missing_returns_none(self):
        self.assertIsNone(config.resolve({}, "ssid"))
        self.assertIsNone(config.resolve({"UNRELATED": "x"}, "ssid"))

    def test_password_aliases(self):
        for key in ("DJI_OSMO_POCKET_WIFI_PASS", "OSMO_PASSWORD", "OSMO_PASS",
                    "WIFI_PASSWORD", "PASSWORD"):
            with self.subTest(key=key):
                self.assertEqual(config.resolve({key: "p"}, "password"), "p")

    def test_dji_prefixed_spelling(self):
        env = {"DJI_OSMO_POCKET_WIFI_SSID": "s", "DJI_OSMO_POCKET_WIFI_PASS": "p"}
        self.assertEqual(config.resolve(env, "ssid"), "s")
        self.assertEqual(config.resolve(env, "password"), "p")

    def test_dji_prefixed_wins_over_generic(self):
        env = {"SSID": "generic", "DJI_OSMO_POCKET_WIFI_SSID": "specific"}
        self.assertEqual(config.resolve(env, "ssid"), "specific")

    def test_canonical_names_are_real_aliases(self):
        for field, key in config.CANONICAL.items():
            self.assertIn(key, config.ALIASES[field])


class TestMask(unittest.TestCase):
    def test_hides_content(self):
        masked = config.mask("hunter2hunter2")
        self.assertNotIn("hunter", masked)
        self.assertEqual(set(masked), {"*"})

    def test_caps_length_so_it_does_not_leak_size(self):
        self.assertEqual(len(config.mask("x" * 200)), 12)

    def test_unset(self):
        self.assertEqual(config.mask(None), "(unset)")
        self.assertEqual(config.mask(""), "(unset)")


class TestFileIO(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            config.save({"OSMO_SSID": "OsmoPocket4-ABC", "OSMO_PASSWORD": "pw pw"}, root=td)
            env = config.load(root=td)
            self.assertEqual(config.resolve(env, "ssid"), "OsmoPocket4-ABC")
            self.assertEqual(config.resolve(env, "password"), "pw pw")

    def test_save_creates_env_local(self):
        with tempfile.TemporaryDirectory() as td:
            path = config.save({"OSMO_SSID": "x"}, root=td)
            self.assertEqual(path.name, config.FILENAMES[0])

    def test_save_preserves_existing_unrelated_content(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / config.FILENAMES[0]
            target.write_text("# mine\nKEEP=yes\n", encoding="utf-8")
            config.save({"OSMO_SSID": "x"}, root=td)
            text = target.read_text(encoding="utf-8")
            self.assertIn("# mine", text)
            self.assertEqual(config.parse_env(text)["KEEP"], "yes")

    def test_local_overrides_plain(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".env").write_text("OSMO_SSID=plain\n", encoding="utf-8")
            (Path(td) / ".env.local").write_text("OSMO_SSID=local\n", encoding="utf-8")
            self.assertEqual(config.resolve(config.load(root=td), "ssid"), "local")

    def test_find_file_prefers_local(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".env").write_text("A=1\n", encoding="utf-8")
            self.assertEqual(config.find_file(td).name, ".env")
            (Path(td) / ".env.local").write_text("A=2\n", encoding="utf-8")
            self.assertEqual(config.find_file(td).name, ".env.local")

    def test_missing_files_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(config.load(root=td), {})
            self.assertIsNone(config.find_file(td))


if __name__ == "__main__":
    unittest.main()
