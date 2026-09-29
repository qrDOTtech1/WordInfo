# SAF Engine — Desktop migration

## Status
First isolated desktop foundation. No trading integration, executable release, background supervision or automatic GitHub reporting is implemented yet. Existing code is unchanged. Profitability is not established.

## Product contract
Windows desktop application without a required browser, Python installation or development venv for the final user. Simulation is the default when the engine is integrated. Reference strategy remains versioned separately from challengers. No automatic promotion based on a short performance window.

## Architecture target
PySide6 presentation, separate supervised engine process, versioned IPC messages, persisted order lifecycle and reconciliation before resuming after a crash. UI failure must not imply that orders have stopped. Real trading requires explicit authorization and platform eligibility.

## Migration stages
1. Read and audit existing entry points, dependency manifests, exchange adapters and tests. Run the existing tests before modifying execution paths.
2. Map the engine to typed state/events without importing trading modules into UI startup.
3. Connect read-only state, then a recorded-data simulation with realistic fees and execution assumptions.
4. Add bounded capital allocation, stale-feed guard, partial-fill handling, reconciliation and emergency controls.
5. Validate Windows packaging and Rust dependency compatibility on a clean Windows machine. Signing requires separate credentials; no signed binary is promised.
6. Add opt-in sanitized GitHub reports and release checking. Never silently restart with open positions.
7. Consider eligible live trading only after simulation, integration tests and explicit approval.

## Diagnostics
Local minimal JSON reports exclude exception messages, source lines, full paths, environment values and local variables. Reports are best-effort, not a complete crash handler. Thread, asyncio, engine-process and native crashes need additional handling. GitHub transmission must use a repository-scoped credential, preview/consent, deduplication and retries. Do not commit operational reports to main. A dedicated private diagnostics repository is preferred.

## Model configuration
Only inspect allowlisted environment variables and explicitly authorized configuration files. Never scan the entire disk or extract secrets from password managers. Cloud validation has an explicit budget; providers use separate adapters. Local inference is optional and requires available model weights and sufficient resources.

## Development
Use a local venv, do not commit it. From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe -m saf_desktop
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_saf_diagnostics.py
```

Dependencies are provisional; reproducible release builds need pinned validated versions. This scaffold has not been tested on Windows. Engine integration is intentionally absent.
