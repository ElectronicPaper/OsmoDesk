# Optional AI milestone — 2026-09-14

This extends the locally implemented Shot Studio and Director work. It does not
replace the canonical motion engine, create another controller or claim camera
capabilities that have not been verified. Core2 firmware remains in OsmoPalm.

## Independent audit lanes and decisions

| Lane | Finding and root decision |
| --- | --- |
| Product concept and solutions | Add intent-based shot pacing inside Director, not a generic chatbot or another workspace. Two contrasting treatments provide an actual creative choice beyond uniform retiming. |
| UI and UX | Keep the existing framing plot and rehearsal. Introduce a brief, contextual model/sharing controls, an explicit outbound disclosure, side-by-side treatments, exact changes, preview-gated apply and the established restore action. Phone layouts stack rather than squeeze. |
| Motion and architecture | AI proposes data only. Preserve geometry, beat order/count, zoom, flow, cues, loop and setup; construct candidates from the frozen draft. Assess with the existing Director engine. Never adopt its nested retiming alternative silently. |
| Users and access | This is a trusted local/LAN studio, not SaaS. Browser IDs coordinate tabs; they are not user accounts or authentication. The host owns its key and allowance. LAN cloud access requires explicit host opt-in and authentication. |
| Platform and reliability | One asynchronous request at a time, single-use short-lived confirmations, request count/cooldown, conservative monetary reservations, bounded payload/output, no retries, finite in-memory result lifetime and cancellation with late-result discard. Network work never holds the workspace lock. |
| Security and data | Fixed provider endpoint, redirects refused, no tools, allowlisted models/efforts, server-only credentials, minimum-context disclosure, strict JSON/semantic validation, escaped text rendering and generation-bound application through normal draft CAS. |
| Overall | Ship one complete brief → disclose → generate → locally validate → compare → rehearse → explicitly apply → restore workflow. Defer autonomous camera operation and unsupported scene/footage claims. |

The root reconciled independent product and security/platform audits, reviewed
all generated code, and kept implementation ownership separate. Native Claude
Sonnet 5 drafted the provider adapter through the existing first-party subscription;
Codex tightened, integrated and tested it. No repository secrets were supplied to
the Claude lane. Its generation was not a substitute for code review or testing.

## Why these boundaries matter

The move parser deliberately normalizes some values. That is useful for ordinary
authoring but insufficient for untrusted model output. Copilot checks exact keys,
types, finite values, ranges, beat indices, unchanged first-leg timing, and distinct
effective pacing before constructing a candidate. A failed local check stays a
failure; narrative confidence cannot promote it to a passing treatment.

Preparation is local only. A confirmation binds the source draft, browser ID,
model, effort, context and estimated ceiling. One deliberate Send makes one API
request; a single-use confirmation prevents repeated submission. Results are bound
to the source generation, and application rechecks the current rig speed cap.
AI does not own an arm state, motion lease, camera route or persistent draft store.
The future job ID is disclosed before sending: if the admission response is lost,
the browser retrieves that same job without resubmitting a paid request. Revision
checking and short, nonblocking admission are atomic with draft edits; the worker
does not hold the workspace lock while waiting for the provider.

The host reserves a conservative estimate before dispatch and does not refund
completed, failed or cancelled requests automatically. Input estimates assume
one token per request byte plus a margin, including schema/instructions, and use
the cache-write multiplier. Output is capped at 3,500–6,000 tokens according to
beat count. Published rates can change; this is not an invoice guarantee or an
account-wide spending control. Defaults reset with the server process.

Provider connection/read timeouts and response-size limits bound ordinary network
failure paths. Cancellation is logical, not a promise that OpenAI stopped processing.
The host remains busy until the in-flight adapter returns; cancelled late results
cannot appear or apply. OS DNS resolution is outside urllib's socket deadline.

## Research used

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs): Responses JSON-schema output; local validation still required.
- [Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [Terra model](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra): exact model IDs, supported common reasoning efforts and published text prices checked on 2026-09-14.
- [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data): `store:false` does not establish zero retention. Standard abuse-monitoring logs can remain for up to 30 days, subject to the provider's terms and exceptions.
- [Director research](DIRECTOR-RESEARCH.md): the existing camera capability qualifications, sampled motion checks and professional motion-planning references remain authoritative.

Authenticated model-catalogue lookup confirmed the four advertised IDs. One
authorized Luna/low Responses request used only a synthetic three-position shot:
702 input tokens, 487 output tokens, 1,189 total. The returned model was
`gpt-5.6-luna`. It produced different 9.15 s and 9.8 s treatments, both passing
the local sampled check. This is not live-generation proof for the other models
or all effort levels, and does not prove cinematic quality on physical hardware.

The later 2026-09-15 live matrix exercised all four advertised models at each of
low, medium and high effort through the app's disclosure, explicit send, job and
local validation path. All 12 requests completed with the exact requested model
ID, without retries. Only a synthetic three-position shot was shared: no camera
footage, images, operator notes or real shot labels. The original draft was restored.

All 24 treatments passed structural validation; 23 passed local motion preflight.
One Astra/high treatment failed preflight and remained unavailable for application.
No treatment was applied, and AI never armed, moved or recorded the camera.
Combined usage was 14,471 tokens; the conservative reserved ceiling was $1.207885,
not an invoice. The operator approved a temporary $2 ceiling; the $1 setting was
restored without clearing the process's reservation accounting. These checks
verify provider integration, not deterministic creative quality or billing rates.

## Verification scope

Focused tests cover provider errors, strict schema/type/range validation,
privacy minimization, immutable framing, confirmation expiry/replay, concurrency,
budget admission, cancellation, stale results and safe errors. Real loopback HTTP
tests exercise authentication/origin gates, selected-env/process precedence with
fake keys, current-rig recheck and the ordinary draft-CAS application path.

Real Chrome tests use the actual app and an ephemeral HTTP host with a synthetic
provider. They exercise disclosure, no implicit send, two treatments, exact diffs,
rehearsal/apply/restore, 390/768/1440 layouts, foreign-tab changes, failed provider
responses without retry and cancelled late output. Hardware endpoints are blocked.
Screenshots from that harness show the real UI with synthetic treatment data.

Final local candidate: **1,224 unittest tests passed, no skips, in 37.278 seconds**.
Both the existing Studio Chrome workflow and the Copilot Chrome workflow passed.
The latter also covers lost-admission-response recovery without redispatch and
full-cycle timing for a looping ping-pong shot. The four new AI test modules contain
49 focused provider/contract/job/HTTP tests. No real key is loaded by these suites.

No camera pairing, Wi-Fi change, gimbal movement, recording, firmware flash,
commit, push, merge or deployment is part of this milestone's local proof.

## Deliberately deferred

Selected-still framing critique would need separate image disclosure and cannot
claim depth or clearance from a picture. Take-review assistance should begin from
the existing deterministic evidence comparison, not unsupported “best take” claims.
Neither is silently enabled by adding a key. Internet-facing SaaS accounts,
subscriptions and camera-autonomous agents are outside this local product slice.
