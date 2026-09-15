# Camera reliability update — 2026-09-15

Fresh camera-reported recording, autofocus, AF target, zoom and color are separate
from setter requests. All operator views have a shared Lens panel with accessible
controls and exposure-acknowledged Refocus A/B. These are autofocus targets, not
manual focus distance or a calibrated rack-focus mechanism.

Waypoint zoom now executes in the canonical runner at no more than 20 Hz, using
the corrected four-byte SET format (status offset 14 is not a SET offset).
Continuous timelapse executes a finite retimed path, aborting on missed deadlines,
STOP, tracking failure, lost standby readback or recovery-write failure. Capture
counts are acknowledged shutters, not confirmed files; interrupted continuous time cannot be resumed.

Photo and Video now have explicit controls in Lens. Switching requires a supported
Pocket 4-family identity, standby, an accepted command and fresh post-command mode
readback. A rejected, cancelled or timed-out shutter never advances timelapse
progress, and no shutter is retried automatically. STOP stays responsive while
waiting for the camera. Review exposure and framing after a mode switch: camera
profiles may retain different settings.

On 2026-09-15, the development Pocket 4P saved one stationary photo and all three
frames of a nine-second continuous timelapse. The four new SD-card originals were
independently downloaded and decoded at 3840×2160; this proves those files, not
unlimited timelapse reliability or optical sharpness. Telemetry stayed healthy
through 35 samples, with at most 0.6° pan and 0.2° tilt from the starting pose.
The original draft and Video mode were restored, with motion stopped/disarmed.

Mode switches now have a separate three-second acknowledgment window; shutter
timing remains separately bounded. Twelve successive real Photo/Video switches
with preview passed after one earlier transition timed out despite changing mode.
No acknowledgment-format defect was established and no automatic retry was added.

A later 60-second continuous timelapse saved all 20 originals. Every file was
downloaded and decoded at 3840×2160. The canonical runner recorded 1,943 samples,
zero telemetry gaps and 0.2° peak tracking error. Three short repeated moves also
completed, with fresh recording start/stop tally and readable saved MP4s. Those
clips exposed a real short-pre-roll issue: motion could start before recording.

Roll now requires freshly reported Video standby, waits up to five seconds for
recording confirmation, then starts the full chosen pre-roll. It rechecks tally,
link, ownership and start position before motion. STOP, Stop move, Stop recording,
disarm and replaced connection state cancel the pending start; old timer callbacks
cannot start a new transaction. Timeouts never retry recording automatically.
In the corrected physical test, tally arrived at 1.17 s, motion began at 1.75 s,
and the six-second path completed with zero gaps. Its saved 4K/59.94 fps clip is
6.273 seconds; container, first frame and final GOP decoded successfully (not
an exhaustive decode or frame-to-telemetry alignment). Stop move also cancelled a
confirmed countdown without implicitly stopping recording. Mid-timelapse disconnect
left the host stopped/disarmed; reconnect did not resume motion or recording.

All 12 live model/effort combinations (Luna, Terra, Sol, Astra × low/medium/high)
returned the requested model. Of 24 synthetic treatments, 23 passed local
preflight and one was blocked. No treatment was applied. The temporarily approved
$2 verification ceiling was restored to $1; no footage or real shot data was sent.

Final software checks: 1,340 tests passed; real Chrome workspace interaction and
15 responsive layout checks passed. Lens and recording-wait layouts passed at
320/390/768/1440 pixels on desktop, mobile and cinema views, with 44-pixel Lens
controls and no JavaScript errors. These are bounded checks, not certification.

Manual focus-distance/rack-focus remains unsupported by the confirmed protocol.
Refocus A/B also changes spot exposure metering; OsmoDesk cannot read or restore
the previous metering mode. The UI now explicitly directs the operator to review
and restore metering on-camera or in Mimo. Its optical result remains unverified.

The dated preview notes below describe the earlier shipped release. Its tally,
waypoint-zoom and continuous-execution limitations are superseded by this update;
other camera models, long-term reliability and optical calibration remain unproven.

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
