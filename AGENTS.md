# OsmoDesk

This is the authoritative mobile/desktop browser controller and Python camera host repository.
Its sibling is [OsmoPalm](https://github.com/ElectronicPaper/OsmoPalm), normally checked out alongside this repository.

- Read README.md and docs/CROSS_PROJECT.md before changing shared behavior.
- Work only in the requested repository unless the task explicitly spans both.
- Consult sibling source, tests and lessons when available; never silently copy code, secrets, or runtime state. Record relevant source commit and proof when porting a fix.
- Each repository must build and test alone. No imports, symlinks or runtime dependencies on a sibling checkout.
- Preserve user and Claude/Codex changes. Use an isolated worktree for overlapping work.
- One camera connection owner at a time. Preserve neutral STOP, stale-input gates, explicit holds, truthful telemetry and unconfirmed-record/focus states.
- Tests do not authorize camera activation, motion, firmware flashing or deployment.
- Never commit credentials, camera registries, packet captures, operator moves, logs, build caches or personal paths.
- Keep commits scoped and verified. Do not force-push or rewrite published history.
- Do not make this repository public or choose a new project-wide license without owner approval.
- Run python -B -m unittest discover -s tests -t .; do not start a camera connection for offline tests.
- Camera networking belongs in driver/wifi.py and the existing transport/ownership engines, not a new alternate path.
