// Pure renderer math checks: bundle to CJS so Node never needs a DOM/WebGL context.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import * as THREE from 'three';

const temp = mkdtempSync(join(tmpdir(), 'osmodesk-spatial-'));
const output = join(temp, 'renderer.cjs');
try {
  execFileSync(process.platform === 'win32' ? 'cmd.exe' : 'npx', process.platform === 'win32'
    ? ['/c', 'npx', 'esbuild', 'web/spatial-view-entry.js', '--bundle', '--platform=node', '--format=cjs', `--outfile=${output}`]
    : ['esbuild', 'web/spatial-view-entry.js', '--bundle', '--platform=node', '--format=cjs', `--outfile=${output}`],
  { cwd: resolve('.'), stdio: 'inherit' });
  const mod = createRequire(import.meta.url)(output);
  const near = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-8, `${message}: ${actual} !== ${expected}`);
  let v = mod.directionFromPitchYaw(0, 0);
  near(v.x, 0, 'forward x'); near(v.y, 0, 'forward y'); near(v.z, -1, 'forward z');
  v = mod.directionFromPitchYaw(0, 90);
  near(v.x, 1, 'right x'); near(v.z, 0, 'right z');
  assert.deepEqual(mod.pitchYawFromDirection(v).yaw, 90);
  near(mod.hfovToVfovDeg(90, 1), 90, 'square FOV');
  const center = mod.panoramaDirectionForUv(.5, .5), top = mod.panoramaDirectionForUv(.5, 1), right = mod.panoramaDirectionForUv(.75, .5);
  near(center.z, -1, 'pano center is forward'); near(top.y, 1, 'pano image top is up'); near(right.x, 1, 'pano image right is positive yaw');
  const ref = { pitch: 0, yaw: 0, hfov: 90, aspect: 1 };
  const refRight = mod.referenceDirectionForScreen(ref, 1, 0), refUp = mod.referenceDirectionForScreen(ref, 0, 1);
  assert.ok(refRight.x > 0 && refRight.z < 0, 'reference screen-right projects right/forward');
  assert.ok(refUp.y > 0 && refUp.z < 0, 'reference screen-up projects up/forward');
  assert.ok(mod.isValidSpatialPose({ pitch: 120, yaw: 721 }), 'unwrapped camera display angles are valid');
  const picked=mod.pitchYawFromDirection(mod.directionFromPitchYaw(120,721),{pitch:121,yaw:722});
  near(picked.pitch,120,'overview picking keeps the nearby encoder pitch branch');
  near(picked.yaw,721,'overview picking keeps the nearby yaw turn');
  assert.ok(!mod.isValidSpatialPose({ pitch: 1_000_001, yaw: 0 }), 'absurd angles fail closed');
  const sample = { pitch: 120, yaw: 721 };
  const tenThousand = Array.from({ length: 10_000 }, () => sample);
  const twentyThousand = Array.from({ length: 20_000 }, () => sample);
  const data = mod.cleanSpatialData({ samples: tenThousand, ghostSegments: [twentyThousand] });
  assert.equal(data.samples.length, 10_000, 'canonical sample capacity');
  assert.equal(data.ghostSegments[0].length, 20_000, 'ghost segment capacity');
  assert.equal(mod.cleanSpatialData({ghostSegments:Array.from({length:100},()=>[sample,sample])}).ghostSegments.length,100,'many gaps are retained');
  assert.equal(mod.cleanSpatialData({samples:[{pitch:NaN,yaw:0}]}),null,'invalid points are rejected rather than silently hidden');
  assert.equal(mod.cleanSpatialData({ghostSegments:[twentyThousand,[sample]]}),null,'aggregate ghost budget is bounded');
  const group = new THREE.Group();
  const child = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial());
  let disposed = 0;
  child.geometry.dispose = () => { disposed += 1; };
  group.add(child);
  mod.disposeGroupChildren(group);
  assert.equal(group.children.length, 0, 'disposed child detached');
  assert.equal(disposed, 1, 'detached child geometry disposed');
  assert.equal(mod.markerColor('avoid'), 0xff4444, 'avoid markers remain red');
  assert.equal(mod.markerColor('issue'), 0xffffff, 'legacy issue kind is not mistaken for avoid');
  console.log('spatial renderer pure math passed');
} finally {
  rmSync(temp, { recursive: true, force: true });
}
