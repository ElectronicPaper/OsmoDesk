import copy
import hashlib
import io
import json
import unittest
import zipfile
from driver.editorial import build_package
from tests.test_ai_http import authored_move


class EditorialTests(unittest.TestCase):
    def source(self):
        return [dict(id=0, scene='PRIVATE', shot='PRIVATE', note='PRIVATE', take=1, circled=False,
                     run_id='run-1', fingerprint='proof', recording=dict(requested=True, reported=False),
                     path=authored_move().to_dict(), motion=dict(trace=[[0,90,0],[1,91,2]],
                     aborted=True, telemetry_gaps=3, verdict='incomplete'))]

    def test_private_default_preserves_provenance_and_hashes(self):
        takes = self.source(); before = copy.deepcopy(takes)
        with zipfile.ZipFile(io.BytesIO(build_package(takes, [0]))) as archive:
            content = b''.join(archive.read(n) for n in archive.namelist())
            self.assertNotIn(b'PRIVATE', content)
            self.assertNotIn(b'LOCAL ONLY', content)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(len(manifest['files']), len(archive.namelist()) - 1)
            for entry in manifest['files']:
                self.assertEqual(entry['sha256'], hashlib.sha256(archive.read(entry['file'])).hexdigest())
            evidence = json.loads(archive.read('take_0/evidence.json'))
            self.assertFalse(evidence['recording']['reported'])
            self.assertTrue(evidence['motion']['aborted'])
            self.assertEqual(evidence['motion']['telemetry_gaps'], 3)
        self.assertEqual(takes, before)

    def test_explicit_notes_and_manual_no_trace(self):
        take = self.source()[0]; take['motion'] = None; take['path'] = None
        with zipfile.ZipFile(io.BytesIO(build_package([take], [0], include_notes=True))) as archive:
            self.assertIn(b'PRIVATE', archive.read('take_0/evidence.json'))
            self.assertFalse(any(n.endswith('.chan') for n in archive.namelist()))

    def test_reject_bad_traces_and_budgets(self):
        for trace in ([[0,0,0],[0,1,1]], [[0,0,0],[1,float('nan'),0]], [[False,0,0],[1,1,1]], [[0,0,0],[3601,1,1]], 'invalid'):
            takes = self.source(); takes[0]['motion']['trace'] = trace
            with self.assertRaises(ValueError): build_package(takes, [0])
        for ids in ([True], [0,0], [-1], [], list(range(21))):
            with self.assertRaises(ValueError): build_package(self.source(), ids)
        for fps in (True, float('nan'), 0, 121):
            with self.assertRaises(ValueError): build_package(self.source(), [0], fps=fps)
