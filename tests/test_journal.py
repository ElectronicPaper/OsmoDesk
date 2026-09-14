"""Focused persistence proof for the Shot Studio workspace journal."""

import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from driver.journal import WorkspaceJournal


def draft():
    return {"name": "reveal", "waypoints": [
        {"name": "A", "pitch": 0, "yaw": 0},
        {"name": "B", "pitch": 10, "yaw": 20, "duration": 2},
    ]}


def slate():
    return {"scene": "12", "shot": "A", "take": 3}


def take(index=0, motion=None):
    return {"id": index, "scene": "12", "shot": "A", "take": index + 1,
            "note": "wide", "circled": False, "motion": motion,
            "move": "reveal", "duration": 2.0, "setup": "", "at": "10:00"}


class TestWorkspaceJournal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_restores_detached_canonical_snapshot(self):
        one = WorkspaceJournal(self.root)
        takes = [take()]
        one.save(draft=draft(), slate=slate(), takes=takes)
        got = WorkspaceJournal(self.root).load()
        self.assertEqual(got["draft"]["name"], "reveal")
        self.assertEqual(got["slate"], slate())
        got["takes"][0]["note"] = "changed"
        self.assertEqual(WorkspaceJournal(self.root).load()["takes"][0]["note"], "wide")

    def test_atomic_failure_preserves_last_good_current_file(self):
        journal = WorkspaceJournal(self.root)
        journal.save(draft=draft(), slate=slate(), takes=[])
        before = journal.path.read_bytes()
        original_replace = os.replace
        def reject_current(source, target):
            if Path(target) == journal.path:
                raise OSError("disk full")
            original_replace(source, target)
        with mock.patch("driver.journal.os.replace", side_effect=reject_current):
            with self.assertRaises(OSError):
                journal.save(draft=draft(), slate=slate(), takes=[take()])
        self.assertEqual(journal.path.read_bytes(), before)
        self.assertEqual(journal.revision, 1)

    def test_corrupt_current_recovers_backup_and_blocks_save(self):
        journal = WorkspaceJournal(self.root)
        journal.save(draft=draft(), slate=slate(), takes=[take()])
        journal.save(draft=draft(), slate=slate(), takes=[take()])
        journal.path.write_text("{broken", encoding="utf-8")
        recovered = WorkspaceJournal(self.root)
        self.assertEqual(recovered.load()["takes"], [take()])
        self.assertIn("read-only", recovered.warning)
        with self.assertRaises(RuntimeError):
            recovered.save(draft=draft(), slate=slate(), takes=[])

    def test_missing_current_recovers_backup_read_only(self):
        journal = WorkspaceJournal(self.root)
        journal.save(draft=draft(), slate=slate(), takes=[take()])
        journal.save(draft=draft(), slate=slate(), takes=[])
        journal.path.unlink()
        recovered = WorkspaceJournal(self.root)
        self.assertEqual(recovered.load()["takes"], [take()])
        self.assertIn("missing", recovered.warning)
        self.assertTrue(recovered.backup_path.exists())
        with self.assertRaises(RuntimeError):
            recovered.save(draft=draft(), slate=slate(), takes=[])

    def test_missing_current_with_malformed_backup_is_blocked(self):
        journal = WorkspaceJournal(self.root)
        journal.backup_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(journal.load())
        self.assertIn("backup needs recovery", journal.warning)
        with self.assertRaises(RuntimeError):
            journal.save(draft=draft(), slate=slate(), takes=[])

    def test_recovery_info_is_read_only_for_a_clean_or_corrupt_workspace(self):
        journal = WorkspaceJournal(self.root)
        self.assertFalse(journal.recovery_info()["needed"])
        journal.path.write_text("{broken", encoding="utf-8")
        info = journal.recovery_info()
        self.assertTrue(info["needed"])
        self.assertEqual(info["current"], "invalid")
        self.assertIsNone(journal.warning)
        self.assertFalse(journal._blocked)

    def test_restore_backup_preserves_both_originals(self):
        journal = WorkspaceJournal(self.root)
        journal.save(draft=draft(), slate=slate(), takes=[take()])
        journal.save(draft=draft(), slate=slate(), takes=[])
        current = b"{broken"
        backup = journal.backup_path.read_bytes()
        journal.path.write_bytes(current)
        info = journal.recovery_info()
        self.assertTrue(info["can_restore_backup"])
        restored = journal.recover("restore_backup", expected_fingerprint=info["fingerprint"])
        self.assertEqual(restored["takes"], [take()])
        copies = list(self.root.glob("workspace.recovery-*.json"))
        self.assertEqual(len(copies), 2)
        self.assertEqual({copy.read_bytes() for copy in copies}, {current, backup})
        self.assertFalse(journal.recovery_info()["needed"])
        self.assertIsNone(journal.warning)

    def test_recovery_rejects_a_stale_inspection_and_keeps_blocked(self):
        journal = WorkspaceJournal(self.root)
        journal.path.write_text("{broken", encoding="utf-8")
        info = journal.recovery_info()
        journal.path.write_text("{changed", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            journal.recover("keep_current", expected_fingerprint=info["fingerprint"],
                            draft=draft(), slate=slate(), takes=[])
        self.assertEqual(journal.path.read_text(encoding="utf-8"), "{changed")
        self.assertTrue(journal._blocked)

    def test_keep_current_validates_and_preserves_corrupt_primary(self):
        journal = WorkspaceJournal(self.root)
        journal.path.write_text("{broken", encoding="utf-8")
        info = journal.recovery_info()
        self.assertTrue(info["can_keep_current"])
        kept = journal.recover("keep_current", expected_fingerprint=info["fingerprint"],
                               draft=draft(), slate=slate(), takes=[take()])
        self.assertEqual(kept["takes"], [take()])
        self.assertEqual(journal.load()["takes"], [take()])
        copies = list(self.root.glob("workspace.recovery-*.current.json"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), "{broken")

    def test_recovery_refuses_missing_or_invalid_backup(self):
        journal = WorkspaceJournal(self.root)
        journal.path.write_text("{broken", encoding="utf-8")
        info = journal.recovery_info()
        self.assertFalse(info["can_restore_backup"])
        with self.assertRaises(RuntimeError):
            journal.recover("restore_backup", expected_fingerprint=info["fingerprint"])
        journal = WorkspaceJournal(self.root)
        journal.backup_path.write_text("{broken too", encoding="utf-8")
        info = journal.recovery_info()
        self.assertFalse(info["can_restore_backup"])

    def test_recovery_write_failure_preserves_originals_and_stays_blocked(self):
        journal = WorkspaceJournal(self.root)
        journal.save(draft=draft(), slate=slate(), takes=[])
        journal.save(draft=draft(), slate=slate(), takes=[take()])
        journal.path.write_text("{broken", encoding="utf-8")
        current, backup = journal.path.read_bytes(), journal.backup_path.read_bytes()
        info = journal.recovery_info()
        original_write = journal._write_atomic
        def fail_current(path, raw):
            if path == journal.path:
                raise OSError("disk full")
            original_write(path, raw)
        with mock.patch.object(journal, "_write_atomic", side_effect=fail_current):
            with self.assertRaises(OSError):
                journal.recover("restore_backup", expected_fingerprint=info["fingerprint"])
        self.assertEqual(journal.path.read_bytes(), current)
        self.assertEqual(journal.backup_path.read_bytes(), backup)
        self.assertEqual(len(list(self.root.glob("workspace.recovery-*.json"))), 2)
        self.assertTrue(journal._blocked)

    def test_unknown_version_is_not_overwritten(self):
        bad = {"version": 2, "revision": 1, "draft": draft(), "slate": slate(), "takes": []}
        path = self.root / "workspace.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        journal = WorkspaceJournal(self.root)
        with self.assertRaises(RuntimeError):
            journal.save(draft=draft(), slate=slate(), takes=[])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)

    def test_non_finite_and_unsafe_fields_are_rejected(self):
        journal = WorkspaceJournal(self.root)
        with self.assertRaises(ValueError):
            journal.save(draft=draft(), slate=slate(), takes=[{"at": math.nan}])
        unsafe = {"version": 1, "revision": 1, "draft": draft(), "slate": slate(),
                  "takes": [], "armed": True}
        (self.root / "workspace.json").write_text(json.dumps(unsafe), encoding="utf-8")
        self.assertIsNone(WorkspaceJournal(self.root).load())

    def test_strict_version_and_required_waypoints_are_rejected(self):
        journal = WorkspaceJournal(self.root)
        with self.assertRaises(ValueError):
            journal._validated_payload({"version": True, "revision": 0,
                                        "draft": draft(), "slate": slate(), "takes": []})
        with self.assertRaises(ValueError):
            journal.save(draft={"name": "missing"}, slate=slate(), takes=[])

    def test_invalid_take_shape_and_trace_are_rejected(self):
        journal = WorkspaceJournal(self.root)
        with self.assertRaises(ValueError):
            journal.save(draft=draft(), slate=slate(), takes=[None])
        with self.assertRaises(ValueError):
            journal.save(draft=draft(), slate=slate(),
                         takes=[take(motion={"trace": [[0, 1]]})])
        with self.assertRaises(ValueError):
            journal.save(draft=draft(), slate=slate(),
                         takes=[take(motion={"trace": [[0, 1, math.inf]]})])

    def test_unknown_draft_fields_are_not_restored(self):
        journal = WorkspaceJournal(self.root)
        unsafe_draft = {**draft(), "armed": True, "owner": "someone"}
        journal.save(draft=unsafe_draft, slate=slate(), takes=[])
        self.assertNotIn("armed", journal.load()["draft"])

    def test_current_host_take_provenance_is_accepted(self):
        journal = WorkspaceJournal(self.root)
        current = take()
        current.update({"path": draft(), "setup_fields": {"fov_deg": "84"},
                        "run_id": None, "source": "manual note — no shot run attached",
                        "fingerprint": None,
                        "recording": {"requested": False, "reported": False}})
        journal.save(draft=draft(), slate=slate(), takes=[current])
        self.assertEqual(journal.load()["takes"][0]["path"]["name"], "reveal")

    def test_same_instance_writers_serialize(self):
        journal = WorkspaceJournal(self.root)
        errors = []
        def writer(i):
            try:
                journal.save(draft=draft(), slate=slate(), takes=[take()])
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(journal.revision, 12)
        self.assertEqual(WorkspaceJournal(self.root).load()["draft"]["name"], "reveal")


if __name__ == "__main__":
    unittest.main()
