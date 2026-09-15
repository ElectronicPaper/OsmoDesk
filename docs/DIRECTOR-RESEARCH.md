# OsmoDesk Director: research, audit and implementation brief

Research date: 2026-09-14. Scope: desktop/mobile host application, not OsmoPalm firmware. This builds on the uncommitted Shot Studio candidate; it does not replace that work or claim hardware acceptance.

## Product decision

OsmoDesk should turn authored framing into an understandable, rehearsable and inspectable shot. The differentiator is a complete offline-to-set workflow: **compose beats → inspect timing → preview a proposal → apply deliberately → rehearse on the rig → retain honest take evidence**. It is not a general-purpose account platform, an invented camera SDK, or a simulated guarantee of cinematic results.

Three directions were evaluated independently:

1. Rig Reset Passport: useful for remounts, but attitude cannot prove tripod translation or scene continuity. Retain setup notes and start checks; defer automatic reset claims.
2. Director rehearsal and timing fit: strongest immediate outcome. It uses the existing canonical path, works camera-off, and makes travel/speed failures actionable. Selected.
3. Post-ready take proof: valuable supporting work, especially refusing whole-shot matches from partial traces. Full editorial packaging remains a subsequent slice.

## Research findings and design implications

DJI's published Pocket 4P specifications distinguish controllable from mechanical travel, and describe wide and medium-tele lenses. Those specifications do not establish the sign/frame of this reverse-engineered protocol or prove remotely supported commands. Existing measured host limits remain authoritative; no new opcodes or advertised SDK support are inferred.[1]

edelkrone's Keypose workflow makes saved positions a direct creative vocabulary. OsmoDesk can apply that principle as named, selectable beats without copying hold-to-save gestures or implying equivalent hardware.[2] Dragonframe's preroll guidance illustrates why initial velocity and available travel matter before execution; a pleasant-looking curve alone is not feasibility evidence.[3]

Minimum-jerk trajectory literature treats boundary conditions explicitly. OsmoDesk already has easing and continuous path interpolation. The useful next step is making their consequences inspectable, not installing another motion engine or claiming a sampled path mathematically bounds a physical gimbal.[4]

W3C's enhanced target-size guidance uses 44 by 44 CSS pixels, with exceptions. Director controls will use that as a design target, plus text labels, keyboard selection, reduced-motion handling and compact progressive disclosure. This is not a claim of complete WCAG conformance.[5]

## Separate audit lanes

| Lane | Evidence / conclusion | Decision |
|---|---|---|
| Concept | Existing start checks, segment rehearsal, immutable take path/setup support a shot desk, not another generic remote. | Director workflow above. |
| Solutions and motion | `MoveRunner._drive` leaves the prior demand when telemetry disappears; stale measurements are reused. Non-loop ping-pong never finishes. Yaw travel is advisory only. | Fail neutral on telemetry/target faults; finite round trip; preserve measured limit definitions. |
| UI | Fresh Chrome captures show a tall stack of fully expanded waypoint forms and a small preview. | Beat strip, dominant preview, focused adjustments. |
| UX | Existing Compose/Shoot separation is sound; previews and timing warnings do not yet form a useful proposal/apply workflow. | Add offline Director without hiding STOP or triggering motion from preview. |
| Users / shared control | All browsers share `phone`; workspace writes serialize without stale-write detection; local POSTs do not check browser origin. | Same-origin JSON boundary, per-tab motion identity, optimistic authoring concurrency and explicit conflicts. No SaaS accounts. |
| Data / evidence | Take comparison uses overlap only; malformed/nonfinite/unordered samples and insufficient coverage lack a clear gate. | Strict trace validation; overlap/quality evidence; partial or aborted data cannot prove whole-shot matching. Pixel results remain angular estimates, not composite guarantees. |
| Platform / operations | Control-only dependency install omits PyAV; corruption is detected but guided recovery is absent; exports detach setup metadata. | Make capability limitations explicit. Recovery/export expansion remains open; do not destroy preserved journals. |
| Maintainability | One canonical Move and one camera owner are strengths. Inline UI and cross-surface request handling are coupled. | Isolate new Director and browser request modules; avoid broad framework migration. |

## Overall acceptance boundary

The previous passing suite did not exercise the newly reproduced defects. New proof must include regression failures followed by fixes, offline real-HTTP authoring, two-browser conflict/control isolation, responsive interactive Director preview, and the integrated unittest suite. A camera-off pass proves contracts and simulation only. Physical smoothness, command acceptance, lens calibration, real take repeatability, deployment and user acceptance remain unproven.

## Implemented candidate and final evidence

The Director milestone is implemented in the isolated `codex/cinema-workspace` branch,
preserving the prior Shot Studio candidate. The host remains the only canonical authoring
and camera owner; Director adds no transport or alternate motion engine.

- **Rehearsal:** named beat selection, canonical angle-path preview, keyboard scrub,
  next-beat navigation, cue pauses, one-cycle loop disclosure and a reduced-motion stepped cursor.
- **Timing:** requested or suggested duration, proposal preview, explicit Apply and Restore.
  The 50 ms leg floor is disclosed as an alternative, not falsely labeled the requested duration.
  Unsupported travel and assessment bounds cannot yield a passing timing proposal.
- **Motion:** missing/invalid telemetry and invalid targets stop immediately; one-shot
  ping-pong ends after the return. Preflight bounds workload and inspects short legs;
  brief excessive sampled peaks cannot be hidden by violation aggregation.
- **Shared control:** same-origin JSON/auth checks, per-document motion identity, packet
  ordering, lease expiry and closed-gesture STOP. Draft generation conflicts preserve
  local edits for export/reload; late timing responses cannot replace newer same-tab edits.
- **Evidence:** partial coverage, aborted takes and abnormal interpolation gaps are explicit.
  Memory-only saves cannot masquerade as durable saves; circling targets a stable take ID.

Final full suite: **1,175 tests passed, no skips, 34.853 seconds**. The JavaScript session
regression is included in that suite. The separate real-Chrome check passed on the same
application candidate: five workspaces at 1440/768/390 px, real temporary-host authoring
and restart, delayed Apply responses, two real author documents, cue holds, analysis failure
and recovery, slow-frame clock behavior, angle seams, keyboard controls, and simulated
host-loss/recovery on all three browser surfaces. Its allowlist blocked camera actuation;
no hardware endpoint was reached. Desktop/mobile captures were inspected, not AI illustrations.
`git diff --check` passed. A stylesheet guard initially failed because it only read inline CSS;
it now follows local linked stylesheets instead of exempting the Director class.

Independent cold reviews led to the packet-order, plain-HTTP identity, epoch-reset,
held-STOP, timing-floor and Apply-race fixes. Root reviewed and integrated the changes.
The restricted native Claude attempt returned `claude-sonnet-5` but its write tools were
denied; it contributed no code. Codex workers completed the bounded implementation without
permission expansion, API-key use, installation or additional billing configuration.
Total multi-agent cost and savings are unknown, not claimed.

Research shaped the Director choice and canonical-engine constraint; Product Design informed
the actual rendered audits and focused beat editor; Data informed the comparison quality gates;
Superpowers informed failing regressions and final verification. The Creative Production board
was unavailable in this environment; actual application captures were used instead, with no
claim that a board or generated promotional asset was produced.

The software items below were completed in the subsequent modular-workspace pass;
physical acceptance remains open.
No new account/subscription platform or unsupported camera capability was invented. This is
a locally verified product milestone, not an assertion that every repository issue is resolved.
No commit, push, merge, deployment, camera operation or firmware flash was performed.

## Modular workspace and operational completion (2026-09-14)

Implemented the four remaining software areas: guided journal recovery with preserved
originals and stale-fingerprint refusal; pinned optional PyAV 18.1.0 / Python 3.11+
decoder profile and a read-only capability page; selected-take editorial ZIP evidence
packages with relative CSV/CHAN, privacy defaults and content hashes; and expiring,
revocable viewer/editor/operator crew permissions. Local host-owner settings now
manage the OpenAI key and model/effort/session limits. Secret retention defaults to
memory, with explicit optional Windows Credential Manager storage, no plaintext fallback.

Snapgrid core 0.10.0 is bundled locally through a vanilla adapter, not a React rewrite.
Stable panel IDs, breakpoint-specific validated geometry, reversible pin/hide/restore,
keyboard/pointer handles and no live-node replacement reuse the lessons—not source code—
from SignalDeck-Claude's dock ADR and commits e41e07b, 1dd56c4, e5019c3 and c64aed1.
That repository remained read-only and paused; OsmoDesk has no sibling runtime dependency.
Picture/STOP/tactile controls intentionally stay fixed. Studio panels and phone System
adjustments are modular; responsive shared settings and a contextual field guide cover
the Studio, cinema monitor and phone surfaces. Safety states retain visible words.

Native Claude Sonnet 5 supplied a bounded editorial module draft; root revised privacy,
whitelisting, trace budgets and manifest integrity, then integrated and tested it. Root
also reviewed independent layout, recovery/settings and HTTP-test lanes. No API-key
provider request was made in this pass; browser AI settings tests use synthetic keys.
Costs/total multi-agent savings are not established. Remaining ceiling: actual OS-vault
roundtrip, a real camera stream/motion/lens/recording acceptance, deployment and user acceptance.

Upstream: [Snapgrid core API](https://snapgrid.dev/react/docs/api/core/) and
[PyAV 18.1.0](https://pypi.org/project/av/18.1.0/), checked 2026-09-14.

Verification for this pass: **1,257 offline tests passed, no skips, 41.030 seconds**.
The real-Chrome workspace check exercised pointer/keyboard layout editing, resize,
pin/hide/restore/reset, persisted breakpoints, 15 non-overlapping workspace/width
combinations, stable control DOM, host-only synthetic key configuration/removal,
crew cookie identity/revocation, contextual guide, selected-take download and shared
settings on phone/monitor. The AI browser check passed full disclosure, treatment
preview/apply/restore, stale and lost-response handling with a synthetic provider.
It exposed stale pixel offsets during immediate viewport resizing; percentage-based
grid geometry fixed that shared cause without masking overflow or removing features.

## Release completion checks (2026-09-15)

The later live check established BLE pairing, Wi-Fi association, fresh telemetry,
720p preview at approximately 30 fps with 5,349 decoded frames and no decode errors,
and a bounded 0.6-degree pan followed by a stable stop. The operator confirmed
the three-second recording started and stopped. That earlier observation alone
did not establish a camera-reported tally or broad physical smoothness/repeatability.

Native Windows Credential Manager save/reload, rotation, removal and missing-key
behavior passed with disposable synthetic values; the owned entry was removed.
The opt-in probe refuses collisions and cannot report success after cleanup failure.
Native Claude Sonnet 5 supplied the bounded probe draft; root corrected its cleanup
ownership and failure reporting before applying and verifying it.

The cold release review found and corrected missing Pi archive dependencies,
symlinked persistent state incompatible with journal safeguards, and a disabled
Copy button on the second crew issuance. One explicit `--state-dir` now serves
the library, journal, preferences and timelapse state; physical-path validation
precedes deployment staging/seeding. Existing releases are preserved for rollback.
The extracted package passed 1,265 offline tests before the final additional
deployment-ordering regression; all seven focused deployment tests then passed.
All three real-Chrome harnesses passed, including two consecutive crew copy cycles.

Release caveats remain explicit in `RELEASE_NOTES.md`: experimental focus/lens
support, planning-only functions, trusted-LAN scope and the distinction between
software proof, observed camera behavior and user acceptance. The earlier entries
above describe their dated local candidates, not the final release delivery state.

## Camera completion follow-up — 2026-09-15

The working update adds fresh per-field camera readback, a shared Lens panel,
guarded waypoint zoom and finite continuous timelapse through the existing runner.
Named feedback subscriptions extend the host without changing the mirrored v1
Palm/Desk contract. Protocol layouts were checked against OpenPocketCine commit
`9b30b93572797c94db5ad9236fb746410f8d761f` (`Commands.swift`, `CameraStatus.swift`,
`CameraControl.swift`). The zoom SET is four bytes; offset 14 is incoming status,
not a command payload offset. Regression coverage protects this distinction.

On the development Pocket 4P, fresh telemetry confirmed AF-S then AF-C, 1.1x zoom
then 1x, and recording start then stop. A six-second one-degree return path with
zoom peaked at 0.17 degrees tracking error with no telemetry gaps. A three-frame
continuous request sequence finished with 0.23-degree peak error and no gaps.
This does not verify saved still files, optical focus accuracy, manual focus
distance, long-duration filming, or compatibility with another camera model.
Refocus A/B was tested offline only because it also changes spot exposure metering.

The subsequent [camera reliability update](../RELEASE_NOTES.md) adds independent
saved-file proof (four initial photos, then a 20-frame/60-second continuous run),
three repeated takes, disconnect/no-auto-resume checks, and the corrected
recording-confirmation-before-pre-roll contract. It also records the complete
12-combination live AI integration matrix and final 1,340-test software gate.
These later bounded results supersede the saved-file/recording limitations of
the earlier observation; optical focus accuracy and universal reliability are
still not established.

## Focus capability boundary — 2026-09-15

DJI documents AF-S and AF-C (including its autofocus tracking variants) for the
Pocket 4P, not a manual focus-distance interface. The confirmed OpenPocketCine
command set at `9b30b93572797c94db5ad9236fb746410f8d761f` exposes autofocus mode
and screen-region targets, but no calibrated lens-position SET/GET. This is an
evidence gap, not proof that an undiscovered firmware command is impossible.

The documented Mimo-equivalent focus burst explicitly selects spot AE metering
and sets both focus and metering regions. No confirmed previous-metering GET or
restoration SET is available. Therefore OsmoDesk does not probe new lens opcodes,
promise automatic metering restoration, or label autofocus A/B as rack-focus.
The Lens disclosure now says metering must be reviewed/restored on-camera or in
Mimo. At this checkpoint the optical result was unverified; the later physical
check below establishes a bounded AF-S result, not metering restoration or
general-purpose optical acceptance.

The subsequent Refocus reliability closure preserves the captured four-command
burst and correlates every acknowledgment before returning success. One shared
three-second deadline, partial-send cleanup, STOP cancellation, session-loss
guards and late-reply rejection prevent silent partial success or replay. The
shared Lens UI requires an exact four-ACK receipt and warns that even failed
requests may have changed metering. This does not add a metering-restoration opcode.
The software gate passed 1,350 tests. Real Chrome against the real HTTP handler
and a simulated camera socket passed A/B success and rejected-ACK cases on all
three operator pages at 320/390/768/1440 px: 12 layouts, no Lens overflow, and
44 px minimum buttons. An initial reused-tab load lost three script responses;
reload and the final fresh-context cases loaded without script or network errors.
This browser evidence is not physical autofocus or exposure-restoration proof.

### Physical Refocus A/B follow-up — 2026-09-15

Using the deployed `6b0c630` candidate and its sole camera connection, the
development Pocket 4P reported healthy telemetry and live preview while charging.
Two targets in AF-C each returned four acknowledgments, but the preview did not
show a clear near/far focus change. After explicitly selecting AF-S, the real Lens
UI saved A/B and recalled A, B, A: a nearby printed label became sharp, then a
more distant box became sharp while the label blurred, then the label became
sharp again. Three private preview frames preserve this observation; they are
ignored local evidence, not public repository images or saved camera footage.

The three AF-S UI requests received four acknowledgments in 94/66/76 ms; first
matching polled target observations arrived at 359/588/341 ms. Those are host
request/readback timings, not focus-settle or cinematic pull durations. Across
both modes, requested targets `(0.445, 0.640)` and `(0.310, 0.655)` were reported
within 0.0000065 per axis; this measured camera canonicalization is larger than
float32 encoding error alone. Every sampled state remained healthy, disarmed,
unowned and not recording, with unchanged measured pan/tilt.

The original centre target was restored through another four-ACK request, and
fresh telemetry confirmed original AF-C, 1x zoom, HDR, Video and recording off.
Restoring the target does not restore the previous AE metering mode. That manual
camera/Mimo check remains outstanding; manual distance control and repeatable
focus-pull timing remain unsupported. This closes only the basic AF-S A/B optical
check on this device and scene, not professional-focus or user acceptance.

Sources: [DJI Pocket 4P specifications](https://store.dji.com/uk/product/osmo-pocket-4p),
[DJI focus-mode help](https://repair.dji.com/help/content?customId=01700009262&lang=en&paperDocType=ARTICLE&re=US&spaceId=17),
[confirmed focus burst and autofocus commands](https://github.com/erik-sutton95/OpenPocketCine/blob/9b30b93572797c94db5ad9236fb746410f8d761f/Sources/OpenPocketViewCore/Commands.swift#L270-L313).

## Sources

1. DJI, [Osmo Pocket 4P specifications](https://www.dji.com/jp/osmo-pocket-4p/specs), accessed 2026-09-14. Regional pages redirected inconsistently; the Japanese specification page was readable. No Pocket 3 specification was substituted.
2. edelkrone, [Keypose Mode](https://edelkrone.com/blogs/highlights/keypose-mode), accessed 2026-09-14.
3. Dragonframe, [Why am I getting a pre-roll failed message?](https://www.dragonframe.com/ufaqs/why-am-i-getting-a-pre-roll-failed-message/), accessed 2026-09-14.
4. [Minimum Jerk Trajectory Generation for Straight and Curved Movements: Mathematical Analysis](https://arxiv.org/abs/2102.07459), 2021. Mathematical background, not a validated model of this camera.
5. W3C, [Understanding Target Size (Enhanced)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html), accessed 2026-09-14.
