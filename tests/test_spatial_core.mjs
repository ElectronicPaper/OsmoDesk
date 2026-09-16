import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const core = createRequire(import.meta.url)('../web/spatial-core.js');
const image = 'data:image/png;base64,iVBORw0KGgo=';

function validLocation(overrides = {}) {
  return {
    schema: 1,
    name: 'Studio',
    hfov: 84,
    aspect: 9 / 16,
    margin: 0.1,
    references: [],
    markers: [],
    recipes: [],
    ...overrides,
  };
}

function throws(fn, expected) {
  assert.throws(fn, expected);
}

const validated = core.validate(validLocation({
  unknown: 'discard this',
  captureAspect: 16 / 9,
  references: [{ name: 'Door', url: image, pitch: 120, yaw: 721, hfov: 84, aspect: 16 / 9, attacker: true }],
}));
assert.equal(validated.unknown, undefined, 'top-level unknown field stripped');
assert.equal(validated.references[0].attacker, undefined, 'reference unknown field stripped');
assert.equal(validated.panorama, null, 'image omission defaults panorama to null');
assert.equal(validated.captureAspect, 16 / 9, 'capture aspect preserved');

throws(
  () => core.validate(validLocation({ panorama: { url: 'https://example.invalid/track.png', pitch: 0, yaw: 0 } })),
  /Invalid panorama/,
);
throws(
  () => core.validate(validLocation({ references: Array.from({ length: 25 }, () => ({
    name: 'still', url: image, pitch: 0, yaw: 0, hfov: 84, aspect: 1,
  })) })),
  /at most 24 reference stills/,
);
throws(
  () => core.validate(validLocation({ markers: Array.from({ length: 25 }, () => ({
    name: 'mark', pitch: 0, yaw: 0, kind: 'subject', not_before: null, hold: 0,
  })) })),
  /at most 24 framing marks/,
);

const html = core.storyboard({
  name: '<img src=x onerror=alert(1)>',
  scope: '<script>fetch(\'https://example.invalid\')</script>',
  beats: [{ name: '<b>attack</b>', arrival: 1, dwell_start: 1, dwell_end: 2 }],
});
assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'storyboard escapes title');
assert.ok(html.includes('&lt;script&gt;fetch'), 'storyboard escapes scope');
assert.ok(!html.includes('<img '), 'images are omitted by default');
assert.ok(!html.includes('src="https://example.invalid"'), 'escaped content cannot create an external request');
assert.match(html, /default-src 'none'; img-src data:/, 'CSP permits only data images');

const trace = core.ghostSegments([[0, 0, 0], [0.5, 1, 2], [2, 3, 4], [2.4, 5, 6]]);
assert.deepEqual(trace.map((segment) => segment.map((point) => point.time)), [[0, 0.5], [2, 2.4]], 'gaps split ghost trace while preserving order');
throws(() => core.ghostSegments([[1, 0, 0], [1, 1, 1]]), /Unordered/);
throws(() => core.ghostSegments(Array.from({ length: 20_001 }, (_, time) => [time, 0, 0])), /Invalid/);

const halfway = core.sampleAt([{ time: 0, pitch: 0, yaw: 0 }, { time: 10, pitch: 120, yaw: 720 }], 5);
assert.deepEqual(halfway, { time: 5, pitch: 60, yaw: 360 }, 'long turns retain host-selected unwrapped route');

const frame = core.framing({ hfov: 84, captureAspect: 16 / 9, aspect: 9 / 16 });
const verticalFov = (horizontal, aspect) => 2 * Math.atan(Math.tan(horizontal * Math.PI / 360) / aspect) * 180 / Math.PI;
assert.ok(frame.hfov < 84, 'portrait delivery is a centered crop');
assert.ok(Math.abs(verticalFov(frame.hfov, 9 / 16) - verticalFov(84, 16 / 9)) < 1e-10, '16:9 to 9:16 maintains vertical FOV');

const cues = core.cueEvents(
  { preview: { events: [{ type: 'cue', time: 5, beat_index: 1 }, { type: 'cue', time: 2, beat_index: 0 }, { type: 'cue', time: 2, beat_index: 0 }] } },
  { preview: { events: [{ type: 'cue', time: 2, beat_index: 3 }] } },
);
assert.deepEqual(cues.map((cue) => cue.time), [2, 2, 5], 'A/B cues are sorted and same-lane duplicates collapse');
assert.equal(new Set(cues.map((cue) => cue.key)).size, cues.length, 'A/B cue events have unique keys');

console.log('spatial core browser-neutral checks passed');
