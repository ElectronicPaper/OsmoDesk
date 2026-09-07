"""Offline packaging checks for the Pi deployment script."""

from __future__ import annotations

import subprocess
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "deploy-pi.sh"


class TestDeployPackage(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "deployment package smoke requires Bash")
    def test_package_only_includes_desk_without_palm_or_build_output(self):
        """The archive is inspectable locally and never opens an SSH connection."""
        # Use a repository-relative output path: Git Bash converts Windows
        # absolute arguments before handing them to GNU tar.
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            archive = Path(temporary) / "osmo-rig.tgz"
            completed = subprocess.run(
                ["bash", "deploy/deploy-pi.sh", "--package-only", archive.relative_to(ROOT).as_posix()],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.stderr, "")
            self.assertTrue(archive.exists(), completed.stdout)

            with tarfile.open(archive, "r:gz") as package:
                names = set(package.getnames())

        self.assertTrue(
            {
                "server.py", "run.py", "web/index.html",
                "contracts/camera-control-v1.json",
            }.issubset(names)
        )
        self.assertFalse(any(name.startswith("firmware/") for name in names))
        self.assertFalse(any("/.pio/" in name or name.endswith("/.pio") for name in names))
        self.assertFalse(any(name.endswith(".log") for name in names))

    def test_candidate_is_proved_before_activation_and_legacy_migration(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('tar xzf "$ARCHIVE_PATH" -C "$INCOMING_DIR"', source)
        self.assertIn('mv "$INCOMING_DIR" "$RELEASE_DIR"', source)
        self.assertLess(source.index('unittest discover -s tests'), source.index('mv -Tf "$APP_LINK.next" "$APP_LINK"'))
        self.assertLess(source.index('unittest discover -s tests'), source.index('mv "$APP_LINK" "$LEGACY_DIR"'))

    def test_activation_errors_restore_the_previous_release_and_service(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("trap 'rollback_activation $?' ERR", source)
        self.assertIn('ACTIVATED=1', source)
        self.assertIn('LEGACY_MOVED=1', source)
        self.assertIn('activation failed; restoring previous release', source)
        self.assertIn('systemctl --user restart osmo-rig || true', source)
        self.assertIn('mv -Tf "$UNIT_PATH.next" "$UNIT_PATH"', source)
        self.assertIn('cp -p "$UNIT_BACKUP" "$UNIT_PATH.rollback" || true', source)
        self.assertIn('mv -Tf "$UNIT_PATH.rollback" "$UNIT_PATH" || true', source)

    def test_release_identity_is_content_addressed_and_process_stop_is_scoped(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('RELEASE_ID="release-$ARCHIVE_SHA"', source)
        self.assertIn('if [ "$PREVIOUS_RELEASE" = "$RELEASE_DIR" ]; then', source)
        self.assertLess(source.index('release already active'), source.index('python3 -m venv .venv'))
        self.assertNotIn("pkill", source)
        self.assertNotIn("\nsystemctl --user stop", source)
        self.assertIn('touch -m "$RELEASE_DIR"', source)

    def test_retention_never_sorts_hash_names_or_deletes_the_active_target(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('ACTIVE_RELEASE="$(readlink -f "$APP_LINK")"', source)
        self.assertIn('[ "$(readlink -f "$candidate_release")" = "$ACTIVE_RELEASE" ] && continue', source)
        self.assertIn("sort -z -nr", source)
        self.assertIn('[ "$kept_other" -le 2 ] && continue', source)
        self.assertNotIn("sort -r", source)
        self.assertNotIn("tail -n +4", source)

    def test_moves_are_persistent_seeded_and_linked_before_candidate_proof(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('PERSISTENT_MOVES="$STATE_DIR/moves"', source)
        self.assertIn('cp -a -n "$PREVIOUS_RELEASE/moves/." "$PERSISTENT_MOVES/"', source)
        self.assertIn('ln -s "$PERSISTENT_MOVES" "$RELEASE_DIR/moves"', source)
        self.assertLess(source.index('ln -s "$PERSISTENT_MOVES" "$RELEASE_DIR/moves"'), source.index('unittest discover -s tests'))


if __name__ == "__main__":
    unittest.main()
