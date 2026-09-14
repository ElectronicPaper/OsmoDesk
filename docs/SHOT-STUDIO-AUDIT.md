# Shot Studio milestone — 2026-09-14

## Review and scope

Seven independent native Claude audits covered product concept, technical solutions,
UI, UX, users/sessions, platform/operations, and maintainability. Each used verified
`claude-fable-5-1` at `max` effort, followed by an eighth overall synthesis using
the same model/effort. These were consultations, not automatic acceptance.
The accountable Codex root reproduced findings, revised the proposal, reviewed
native Sonnet implementation and Codex-worker changes, and tested the integrated result.

This candidate is based on `2a8c4e0`, on isolated branch `codex/cinema-workspace`.
It has not been committed, merged, pushed, deployed, or verified on a camera.
The review does not certify Pocket 4P protocol support or production readiness.

## Product decision

OsmoDesk's useful distinction is intentional, repeatable Pocket-camera movement:
plan without a connected camera, prepare the rig, run deliberately, and review
evidence tied to the shot that actually ran. Shot Studio makes that the main
workflow instead of a wall of unrelated controls.

| Area | Root decision and implementation |
| --- | --- |
| Concept | Compose / Shoot / Review / Rig, with existing specialist monitor and phone controls retained. No new parallel motion engine. |
| Solutions | Reject non-finite paths; keep one leg-duration floor; respect stops at dwell/cue nodes; include the active ramp cap in preflight. |
| UI | Responsive card layouts, larger editing targets, progressive advanced sections, usable portrait monitor bands, named phone navigation. |
| UX | Offline add/edit/duplicate/reorder; keyboard path preview; library search and destructive-change confirmation; explicit run controls in Shoot; permanent Stop motion separate from recording. |
| Users / sessions | Keep one host authority and the existing owner classes. Fix browser interference with a held Core2 owner. No invented accounts, subscriptions, or browser role-based security. |
| Platform | Persist draft/slate/takes, with bounded schema, atomic replacement, backup and visible recovery failures. Keep credentials and runtime data out of source. No automatic connection or motion restoration. |
| Maintainability | Focused regression tests at the existing parser, owner, runner/session, HTTP and UI contracts; a hardware-blocked real-browser regression path. Broader script extraction is deferred. |

## Findings corrected

- NaN/Infinity could enter a path and appear acceptable to preflight. Invalid
  imports now fail before replacing the current draft or starting a run.
- Waypoint labels could become HTML. Labels and take-comparison output remain text.
- A browser could steal or clear a held Core2 control owner. Existing ownership
  arbitration now covers browser motion and stale releases.
- Opening a panel replayed a saved browser speed setting. Startup now reads host
  state without issuing that configuration command.
- A canceled connection worker could race a retry or publish resources late.
  Cleanup and generation guards retain one connection owner, with an explicit
  retry response while the canceled attempt is still stopping.
- Draft/setup edits and take history did not survive a host restart. The new
  journal restores authoring data only. A missing/corrupt primary with a valid
  backup restores read-only; failed writes remain visible and do not claim saved.
- Logging after a rehearsal or return-to-one could associate the wrong motion
  report with a take. Full-shot report identity, path, setup, fingerprint and
  recording-request metadata are captured at launch. Rehearsal does not erase
  the last full-shot evidence; each run is consumed once. Manual notes have no
  motion proof. Different shot fingerprints cannot be compared as matching takes.
- The compare HTTP route supplied an 84-degree lens even when omitted. It now
  uses saved take lens data or explicitly labelled assumptions/overrides.
- Monitor punch-in discarded desqueeze. Mirror, desqueeze and punch compose
  through one transform. Shutter controls send numeric denominators and display
  the existing host contract, without optimistic readings after failures.
- Host loss left stale recording/countdown/held-input states. All three pages
  now clear them, show the lost connection and require new input after recovery.
  The browser regression specifically holds a stick through loss and recovery.
- Missing HEVC/PyAV initialization was not an actionable live-view failure.
  Status and the monitor now surface the decoder error.

## Advice deliberately qualified or deferred

- Keep the existing embedded monitor; removing useful tools is not simplification.
  Consolidating its image-processing implementation with the specialist monitor
  needs separate frame-by-frame performance/equivalence proof.
- Do not null a valid shot report merely because the operator subsequently moved
  back to P1. Preserve the original full-run identity instead.
- Do not combine Stop motion, Stop recording and Log take into an implicit action.
  Recording remains **requested**, not a camera-confirmed tally.
- Operator seat attribution, authenticated viewer/operator permissions, a complete
  phone-monitor rebuild, packaged-app distribution, acceleration-aware preflight,
  and a guided journal recovery UI remain future work, not claimed features.
- Waypoint zoom and continuous timelapse retain planning/export value. Continuous
  execution is explicitly refused; no unverified camera opcode was invented.

## Verification boundary

Final local candidate: **1,108 offline tests passed, zero skips** (28.947 seconds).
The separate real-browser integration also passed, including the temporary-host
restart check. `git diff --check` found no whitespace errors. These results do
not constitute deployment or user acceptance.

The offline suite exercises the actual parser, session, HTTP contracts, persistence,
ownership and simulated runner. `tools/verify_studio.py` additionally launches an
ephemeral loopback server with an explicit authoring-only POST allowlist, and uses
real Chrome through an existing Playwright installation. It checks authoring,
retiming, route/setup preservation, library round-trip, invalid import, reload,
keyboard preview, 12 workspace/viewport combinations, truthful failed-setting
behavior, host loss/recovery with held input, and phone transitions/reduced motion.
Connected states are browser fixtures; no camera endpoint is allowed through.

Not proven: physical tracking/smoothness, live camera command acceptance, tally,
real BLE/Wi-Fi pairing, Pi decoder performance/restart, actual power-loss recovery,
or user acceptance of the new design. Existing loopback/LAN token boundaries
remain; this R&D HTTP host is not an internet-facing multi-tenant platform.
