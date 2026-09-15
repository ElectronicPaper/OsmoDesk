"""Focused offline guards for the shared lens card; no server or camera needed."""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "web" / "lens-controls.js"
MOBILE_HTML = ROOT / "web" / "mobile.html"


class LensControlsTests(unittest.TestCase):
    def test_mobile_record_tally_wording_reflects_reported_state(self):
        source = MOBILE_HTML.read_text(encoding="utf-8")
        self.assertNotIn("does not report its tally", source)
        self.assertNotIn("never reports its", source)
        self.assertIn("fresh recording tally", source)
        self.assertIn("REC REQUESTED", source)
        self.assertIn("UNKNOWN", source)

    def test_module_has_no_storage_network_or_dynamic_html_path(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("cannot read or restore the previous metering mode", source)
        self.assertIn("Manual focus-distance control is not available", source)
        for forbidden in ("localStorage", "sessionStorage", "fetch(", "XMLHttpRequest", "eval("):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count(".innerHTML ="), 1)
        self.assertIn("acknowledge_exposure: true", source)
        self.assertIn("Manual focus-distance control is not available.", source)
        self.assertIn("it does not control focus distance or pull speed.", source)
        self.assertIn("min-height:44px", (ROOT / "web" / "lens-controls.css").read_text())

    def test_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        result = subprocess.run([node, "--check", str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deliberate_requests_and_fail_closed_lifecycle(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        # Small DOM contract double. Real layout, focus order and pointer behavior
        # remain part of the integrated browser gate, not claimed by this test.
        harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.nodes = {}; this.events = {}; this.attrs = {}; this.value = ''; this.checked = false; }
  setAttribute(k,v) { this.attrs[k] = v; }
  append(child) { this.child = child; }
  remove() { this.removed = true; }
  set innerHTML(markup) {
    for (const match of markup.matchAll(/data-lens="([^"]+)"[^>]*>/g)) {
      const node = new Element();
      const value = match[0].match(/value="([^"]+)"/);
      node.value = value ? value[1] : '';
      this.nodes[match[1]] = node;
    }
  }
  querySelector(selector) { return this.nodes[selector.match(/data-lens="([^"]+)"/)[1]]; }
  addEventListener(type, fn) { this.events[type] = fn; }
  removeEventListener(type) { delete this.events[type]; }
  fire(type) { this.events[type]?.({target:this}); }
}
let now = 0, tick;
global.window = {};
global.document = {createElement: () => new Element(), activeElement:null};
global.performance = {now: () => now};
global.setInterval = fn => { tick = fn; return 1; };
global.clearInterval = () => { tick = null; };
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const calls = [];
let fail = false, resolvePending = null, pending = false;
const host = new Element();
const api = window.OsmoLens.mount(host, {post: (path, body) => {
  calls.push({path,body});
  if (pending) return new Promise(resolve => { resolvePending = resolve; });
  return fail ? Promise.reject(new Error('<img src=x onerror=secret()>')) : Promise.resolve({ok:true});
}});
const node = name => host.child.nodes[name];
const flush = async () => { for(let i=0;i<6;i++) await Promise.resolve(); };
const report = value => ({value, reported:true, age_s:0});
const status = () => ({connected:true,link_healthy:true,owner:'none',ssid:'OsmoPocket4P-TEST-CAMERA',workspace:{generation:'host1:1'},
  move:{running:false},timelapse:{running:false},preroll:{until:0},recording:{on:false,requested:false}, camera_feedback:{
    zoom:report(3),color:report('normal'),focus_mode:report('single'),focus_point:report({x:0.2,y:0.7}),capture_mode:report('video'),recording:report(false),playback:report(false)
  }});
(async () => {
  assert.equal(calls.length,0);
  assert.equal(node('single').disabled,true);
  api.update(status()); tick();
  assert.equal(calls.length,0);
  assert.match(node('mode').textContent,/AF-S/);
  assert.match(node('capture-reported').textContent,/Video/);
  assert.equal(node('video').attrs['aria-pressed'],'true');
  node('photo').fire('click'); await flush();
  assert.deepEqual(calls.pop(),{path:'/api/camera',body:{set:'capture_mode',value:'photo'}});
  assert.equal(node('video').attrs['aria-pressed'],'true','request cannot rewrite camera-reported capture mode');
  const photoStatus=status(); photoStatus.camera_feedback.capture_mode=report('photo'); api.update(photoStatus);
  assert.equal(calls.length,0,'status rendering cannot change capture mode');
  assert.equal(node('photo').attrs['aria-pressed'],'true');
  for (const mutate of [s=>s.owner='program',s=>s.ssid='OsmoPocket3-TEST-CAMERA',s=>s.recording={on:true,requested:false},
      s=>s.recording={on:false,requested:true},s=>s.camera_feedback.recording.value=true,s=>s.camera_feedback.recording.reported=false,
      s=>s.camera_feedback.playback.value=true,s=>s.camera_feedback.playback.reported=false,s=>s.camera_request_busy=true,
      s=>s.preroll.until=Date.now()/1000+10,s=>s.camera_feedback.capture_mode.age_s=3,s=>s.camera_feedback.capture_mode.reported=false]) {
    const s=status(); mutate(s); api.update(s);
    assert.equal(node('photo').disabled,true);
    node('photo').fire('click'); await flush(); assert.equal(calls.length,0);
  }
  const busyStatus=status(); busyStatus.camera_request_busy=true; api.update(busyStatus);
  assert.equal(node('single').disabled,true,'camera request blocks all lens controls');
  const prerollStatus=status(); prerollStatus.preroll.until=Date.now()/1000+10; api.update(prerollStatus);
  assert.equal(node('range').disabled,true,'active preroll blocks all lens controls');
  api.update(status());
  node('range').value='4'; node('range').fire('input');
  api.update(status());
  assert.equal(node('range').value,'4');
  assert.equal(calls.length,0,'slider input/update cannot send');
  node('range').fire('change'); await flush();
  assert.deepEqual(calls.pop(),{path:'/api/camera',body:{set:'zoom',value:4}});
  node('continuous').fire('click'); await flush();
  assert.deepEqual(calls.pop(),{path:'/api/camera',body:{set:'focus_continuous',value:true}});
  assert.match(node('mode').textContent,/AF-S/,'request cannot rewrite readback');
  node('save-a').fire('click');
  node('x').value='0.1'; node('y').value='0.9'; node('save-b').fire('click');
  node('recall-a').fire('click'); await flush();
  assert.equal(calls.length,0,'no recall without deliberate exposure acknowledgement');
  node('ack').checked=true; node('ack').fire('change');
  node('recall-a').fire('click'); await flush();
  assert.deepEqual(calls.pop(),{path:'/api/camera/focus-target',body:{x:0.5,y:0.5,acknowledge_exposure:true}});
  node('recall-b').fire('click'); await flush();
  assert.equal(calls.pop().body.x,0.1);
  node('x').value=''; node('refocus').fire('click');
  node('x').value='NaN'; node('refocus').fire('click');
  node('x').value='1.01'; node('refocus').fire('click');
  assert.equal(calls.length,0);
  node('use-point').fire('click');
  assert.equal(node('x').value,'0.2'); assert.equal(calls.length,0);
  for (const mutate of [s=>s.camera_feedback.color=report('d-log2'),
      s=>s.camera_feedback.color=report(null),s=>s.camera_feedback.zoom.age_s=3,
      s=>s.camera_feedback.zoom.reported=false,s=>s.move.running=true,
      s=>s.timelapse.running=true,s=>s.link_healthy=false]) {
    const s=status(); mutate(s); api.update(s);
    assert.equal(node('plus').disabled,true);
    node('plus').fire('click'); await flush(); assert.equal(calls.length,0);
  }
  api.update(status()); now=3000; tick();
  assert.equal(node('single').disabled,true); assert.equal(node('range').disabled,true);
  node('single').fire('click'); assert.equal(calls.length,0);
  api.update({...status(),connected:false});
  assert.equal(node('ack').checked,false); assert.match(node('mark-a').textContent,/not saved/);
  api.update(status()); node('save-a').fire('click');
  api.update({...status(),workspace:{generation:'host1:99'}});
  assert.doesNotMatch(node('mark-a').textContent,/not saved/,'draft revisions preserve marks');
  api.update({...status(),workspace:{generation:'host2:1'}});
  assert.match(node('mark-a').textContent,/not saved/);
  fail=true; node('single').fire('click'); await flush(); calls.pop();
  assert.match(node('request').textContent,/Outcome unknown/);
  assert.doesNotMatch(node('request').textContent,/secret|<img/);
  fail=false; pending=true; node('single').fire('click');
  node('continuous').fire('click'); assert.equal(calls.length,1,'one in flight');
  api.update({...status(),connected:false});
  resolvePending({ok:true}); await flush();
  assert.match(node('request').textContent,/cleared/,'old request cannot overwrite new connection state');
  api.destroy(); assert.equal(tick,null); assert.equal(host.child.removed,true);
  api.update(status()); assert.equal(node('single').events.click,undefined);
  console.log('lens control guards passed');
})().catch(error => { console.error(error); process.exitCode=1; });
"""
        result = subprocess.run([node, "-e", harness, str(SCRIPT)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("lens control guards passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
