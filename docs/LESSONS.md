# Host-controller lessons carried into OsmoDesk

- BLE provisioning and Wi-Fi gimbal control are different stages; do not claim ready from
  a BLE session, TCP socket or Wi-Fi association without fresh camera evidence.
- ACK windows belong to individual camera streams. Mixing video and command sequence
  spaces can silently break commands while telemetry still flows.
- Optional Core2 USB commands are a trust boundary. Every emitted symbolic action must
  be accepted deliberately or fail closed; record_start/record_stop were once omitted.
- Commands, camera acknowledgement, recording intent and actual recording tally are not
  interchangeable. Preserve the UI's unconfirmed states.
- Motion has one owner. STOP/deadman and stale-link behavior take priority over cosmetic
  smoothing, programmed moves, live view and camera controls.
- Route platform Wi-Fi work through driver/wifi.py. Windows-only direct imports break Pi
  deployments even when a Windows test suite passes.
- Browser pages need syntax and DOM/style contract checks as well as server tests.
  A missing style block or undefined JavaScript variable can disable a whole control page.
- HEVC support is browser/platform dependent; a network link alone does not prove live view.
- Stage and test a Pi release before switching its active symlink. Preserve operator moves
  outside release directories and retain rollback. Packaging is not deployment acceptance.

OsmoPalm's IMU, deadman and per-axis feel lessons are relevant, but the host scheduler is
not the ESP32 scheduler. Consult its tests before adapting a fix and validate locally.
