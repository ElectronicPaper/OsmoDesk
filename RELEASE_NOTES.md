# Cinema Workspace preview — v0.2.0-preview.1

This is an experimental OsmoDesk milestone, not a stable v1.0 or certification of
every camera command. It preserves the separate OsmoPalm/Core2 project.

## Included

- Compose, Director, Shoot, Review and Rig: offline framing beats, canonical path
  rehearsal, cue pauses and timing proposals with explicit Apply/Restore.
- Optional OpenAI Shot Copilot: disclose a brief and existing shot context, compare
  two locally validated treatments, preview and apply deliberately. AI cannot
  connect, move, arm or record the camera.
- Locally bundled Snapgrid layouts with pointer/keyboard move and resize,
  pin/hide/restore/reset, separate responsive arrangements and stable control nodes.
- Shared host settings, labelled icons, focus-aware tooltips and contextual guides
  across Studio, cinema monitor and phone controls.
- Memory-only API keys by default; opt-in Windows Credential Manager retention,
  model/effort settings, per-process request and estimated-cost limits.
- Expiring viewer/editor/operator crew credentials, separate AI permission,
  revocation, draft conflict protection and ordered, short-lived motion holds.
- Restart-safe journal, guided recovery preserving originals and selected-take
  editorial ZIP export with evidence, privacy defaults and hash manifests.
- Neutral motion on telemetry/target faults; finite non-loop ping-pong;
  bounded preflight and honest partial/aborted take comparisons.
- Explicit physical state directory for versioned installations. Pi packaging now
  carries the live decoder profile, browser test tools and layout build manifests.

## Verified boundaries

Real Chrome exercises desktop, tablet and phone layouts, authoring/restart,
conflicts, timing/AI proposal workflows, host-loss handling, settings, crew access,
repeated credential copying, recovery and editorial handoff. Browser AI tests use
synthetic providers; a configured key alone does not verify provider access.

On the development Pocket 4P, BLE/Wi-Fi connection, telemetry and decoded 720p
preview at about 30 fps passed. A bounded pan moved 0.6 degrees and settled.
The operator confirmed recording started and stopped during a three-second test.
Native Windows credential save/reload, rotation, removal and missing-key handling
passed; the disposable synthetic credential was removed afterward.

## Important limitations

- Sustained physical smoothness, take repeatability and lens calibration are not
  established by a small pan test. Focus and other reverse-engineered commands
  remain experimental. Pocket 3 and Pocket 4 have not been tested by us.
- Recording status is still request tracking, not a camera-reported tally.
- Waypoint zoom and continuous timelapse are planning-only; continuous execution
  is explicitly refused. Take comparisons are angular estimates, not footage or
  camera-translation tracking.
- This is a browser app plus Python host, not a standalone phone-to-camera app.
  A local computer or supported rig host must remain running. The default listener
  is loopback-only and disconnected; use explicit trusted-LAN sharing for crew.
- No internet-facing service, SaaS account system, automatic camera operation,
  cloud footage analysis, native installer or background autostart is implied.
- Keys persist only with explicit opt-in on Windows. Crew credentials and AI
  proposals expire on restart. AI cost estimates are not an account-wide billing cap.
- Original-code licensing remains unchanged; included third-party licenses apply.

## Run and upgrade

Use Python 3.10+ for control, or Python 3.11+ with the optional pinned PyAV profile
for live preview. Install `requirements.txt`; for live view also install
`requirements-live.txt`. Run `python server.py --no-core2`, then open
http://127.0.0.1:8722. Node is not required to run the bundled application.

Keep existing `.env.local` and `moves/` private. For separate versioned deployments,
use `--state-dir /absolute/physical/directory` so shots, journal, preferences and
timelapse progress survive a source upgrade. Do not run two camera-owning hosts.
Pi deployment requires an explicitly selected target; the release is not evidence
that a Raspberry Pi was deployed or physically verified.
