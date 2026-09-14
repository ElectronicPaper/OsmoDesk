# R&D tools

These are optional engineering utilities, not part of normal controller startup or offline tests.

- `python -B tools/verify_vault.py --confirm-test-credential` is an opt-in Windows
  Credential Manager check. It uses a unique temporary state directory and synthetic
  keys, verifies memory-only/reload/rotation/removal/missing-key behavior, and removes
  only its own credential. An existing target is never overwritten or deleted.
  No provider request, real API key, camera or user state is used.

- `verify_studio.py` runs `verify_studio.cjs` in real Chrome through an existing
  Node/Playwright installation. Its temporary loopback host blocks hardware actions,
  isolates authoring files, and is shut down after the check. It does not pair a
  camera, install dependencies, or contact a running OsmoDesk host.
  Add `--ai` to exercise `verify_ai.cjs`: disclosure, two synthetic AI treatments,
  preview/apply/restore, private-context filtering, stale-draft rejection, failure
  without retry, cancellation and responsive layouts. This option uses an explicit
  fake provider; it never reads an API key or makes a paid request.
  Add `--workspace` for pointer/keyboard layout persistence, pin/hide/restore/reset,
  local-only synthetic AI key configuration/removal, crew-cookie revocation, contextual
  help, responsive settings on all surfaces, and selected-take editorial download.

- `node tools/verify_session.cjs` checks per-tab identity, plain-HTTP compatibility,
  stale author conflicts, host-restart generations and held-input STOP semantics
  in a JavaScript VM. No listener, camera, stored credentials or external request is used.

- ble_control_probe.py performs camera operations including motion. Use only with an
  explicitly authorized camera test and a clear gimbal area.
- ble_control_verdict.py interprets that experiment; a historical hypothesis is not a
  supported BLE-control feature.
- probe_rndis.ps1 changes the selected host network adapter configuration and probes
  camera subnets. Read it before use; it is not a connection wizard.
- mutate.sh is a legacy local test-mutation helper. It modifies a source file temporarily
  and assumes a Windows virtual environment. Use a disposable worktree, never a shared
  working tree. Normal verification uses unittest directly.

Do not run any of these automatically during build, startup or continuous integration.
