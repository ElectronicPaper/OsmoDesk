# Development handoff — 2026-09-16

## Resume here

Use this repository's `main` branch, not the pre-split controller repository or
an older feature checkout. Read `AGENTS.md`, inspect `git status`, and preserve
other contributors' changes. OsmoPalm remains a separate project.

`CLAUDE.md` points here so Claude Code and Codex share the same baseline. This is
an implementation/evidence snapshot, not a backlog or standing permission to
connect, move, record, change camera settings, spend on AI, or deploy.

## Current implementation

- **Spatial Rehearsal:** browser-local reference sets and panorama import,
  pointing/frame/compare views, static subject/reveal marks, delivery crops,
  A/B timing, measured take ghosts, timing recipes and offline storyboard export.
  Three.js is pinned and bundled locally; 3D is opt-in with a 2D fallback.
- **Axis curves:** separate pan/tilt cubic Bezier profiles per incoming
  transition; independent, pan-leads-tilt and tilt-leads-pan modes; pointer,
  keyboard and numeric editing; position/speed/acceleration graphs; explicit
  Apply/Discard and stale-draft protection. Flow-to-manual conversion is explicit.
- **One path model:** `driver/moves.py` owns sampling and curve/link validation.
  `driver/curves.py` owns curve math, derivatives and conservative speed bounds.
  `driver/curve_preview.py` and `driver/spatial.py` provide non-actuating analysis.
  Playback, Director, 3D and saved shots reuse this model; do not add a second
  motion engine. Legacy paths and legacy timelapse fingerprints stay compatible.
- **UI integration:** `web/axis-curves.*`, `web/spatial*`, `web/director.js`,
  `web/index.html` and the existing workspace layout manager. Preview endpoints
  in `server.py` are non-actuating; normal draft writes retain generation checks.

## Verification baseline

- 1,406 Python unit/integration tests passed on the feature candidate.
- Pure spatial core/renderer checks and real Chrome offline browser gates passed.
  Axis editing covered pointer/keyboard/numeric input, saved reload, linked modes,
  graph metrics, stale drafts, explicit Flow conversion and phone layout.
- The development Pocket 4P completed **10/10 bounded movements**: tilt-only,
  pan-only, independent curves, each linked-axis direction, and five return legs.
  Each outbound move was approximately one degree over two seconds with a hold.
  Peak reported tracking error was 0.30 degrees; maximum settled endpoint error
  was 0.20 degrees. No telemetry gaps, travel-limit hits or aborted runs.
- Original draft and speed preset were restored; no take was added. The rig
  was left stopped/disarmed, with recording off. These tests did not change
  exposure/focus or make recording requests. This is historical test evidence:
  recheck live state and authority before any subsequent hardware operation.

The physical results establish only these small motions on this development
camera. They do not certify recorded-footage smoothness, motor acceleration,
larger/faster moves, long-term repeatability or other camera models/firmware.
Private raw traces and operator state are intentionally excluded from Git.

## Checks to use

From the repository with its Python environment:

```sh
python -B -m unittest discover -s tests -t .
npm run test:spatial
```

To rebuild the shipped static bundles, use the pinned lockfile:

```sh
npm ci --ignore-scripts
npm run build:layout
npm run build:spatial
```

Optional real-browser gates require an existing Playwright module and Chrome:
set `OSMO_PYTHON` to the project interpreter and `OSMO_SPATIAL_TEST_OUTPUT` to
a disposable output directory. Set `OSMO_PLAYWRIGHT_MODULE` if Playwright is
installed outside the project. Leave `OSMO_SPATIAL_TEST_URL` unset so the test
creates its own camera-blocked loopback fixture, never the operator's host.

```sh
node tests/test_axis_curves_browser.mjs
node tests/test_spatial_browser.mjs
```

Recheck only affected layers while iterating, then run the final integration
gate on the actual candidate. Offline tests never authorize hardware actions.

## Boundaries to preserve

- Linked axes map **planned progress**, not measured leader-motor feedback.
  Monotone curves cannot overshoot; manual curves and automatic Flow are separate.
- 3D is rotation-only, with assumed field of view and manually aligned references,
  not a calibrated digital twin. No translation, parallax, moving subjects,
  autonomous panorama capture/stitching or camera-managed roll simulation.
- AI remains an optional timing assistant. It cannot move the camera. Preserve
  key privacy, explicit Send/Apply, spending limits and treatment validation.
- Refocus A/B also changes spot metering. The operator explicitly deferred the
  on-camera/Mimo metering review; do not mark it restored or perform it silently.
  Calibrated manual focus pulls remain unsupported.
- One host owns the camera. Check for an existing host before starting another;
  state lives in the configured physical `--state-dir`, not necessarily beside
  the running source. Never replace or commit local state/credentials.
- There is no new stable-release tag or new camera-compatibility certification.

For machine-specific host paths and private verification artifacts, consult
`moves/CLAUDE-RESUME.local.md` if available; it is intentionally not published.
If absent, identify the listener on port 8722 and inspect its process command
line to resolve the running `server.py` and `--state-dir` before any restart.
A read-only `/api/status` check can establish current connection/motion state,
but not the source checkout. If host ownership or state location is unknown,
continue offline work and ask the operator before replacing that process.
