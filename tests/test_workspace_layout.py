"""Offline checks for the vanilla workspace persistence and breakpoint guards."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "web" / "workspace-layout.js"
CSS = ROOT / "web" / "workspace-layout.css"


class TestWorkspaceLayout(unittest.TestCase):
    def test_adapter_contract_and_static_safety(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in ("createWorkspaceLayout", "activate: activate", "refresh: refresh",
                       "destroy: destroy", "OsmoSnapgrid", "localStorage", "ResizeObserver"):
            self.assertIn(needle, text)
        self.assertNotIn("appendChild(panel.element)", text)
        self.assertIn("position: absolute", CSS.read_text(encoding="utf-8"))
        self.assertIn("el.style.left=", text)
        self.assertIn("options.canArrange()", text)

    def test_pure_breakpoints_and_saved_geometry_are_bounded(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        harness = f'''global.window = global;
require({json.dumps(str(SCRIPT))});
const h = global.OsmoWorkspaceLayoutHelpers;
const panels = [{{id:'live', minW:3, minH:2}}, {{id:'form', minW:2, minH:3}}];
const layout = h.normalizeLayout([{{i:'live',x:99,y:-2,w:0,h:0,static:true}},{{i:'unknown',x:0,y:0,w:5,h:5}}], panels, 4);
console.log(JSON.stringify({{bp:[h.breakpointFor(1000),h.breakpointFor(700),h.breakpointFor(200)], layout}}));'''
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
            fh.write(harness)
            path = fh.name
        try:
            done = subprocess.run([node, path], capture_output=True, text=True, timeout=10)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["bp"], ["desktop", "tablet", "mobile"])
        self.assertEqual([entry["i"] for entry in payload["layout"]], ["live", "form"])
        live = payload["layout"][0]
        self.assertEqual((live["x"], live["y"], live["w"], live["h"]), (1, 0, 3, 2))

    def test_javascript_parses(self):
        node = shutil.which("node")
        if node:
            done = subprocess.run([node, "--check", str(SCRIPT)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
