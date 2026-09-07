# R&D tools

These are optional engineering utilities, not part of normal controller startup or offline tests.

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
