# AGENTS.md

## Project overview

This repository contains a Windows-only AutoCAD batch print pipeline. The project is split into a thin GUI runner and a runtime core loaded from a configured `CorePath`.

- GUI/launcher: [compile/print_runner.py](compile/print_runner.py)
- Core orchestration: [core/orchestrator.py](core/orchestrator.py)
- Project context and module contracts: [debug/CONTEXT.MD](debug/CONTEXT.MD)
- Build and deployment notes: [compile/deploy.txt](compile/deploy.txt)

## How this codebase is structured

- The compiled executable is a lightweight UI and does not contain business logic.
- `print_runner.py` loads `orchestrator.py` from the configured core directory at runtime via `importlib`.
- The business flow is implemented in the core modules:
  - [core/module1_path_collector.py](core/module1_path_collector.py)
  - [core/module2_version_filter.py](core/module2_version_filter.py)
  - [core/module3_dsd_generator.py](core/module3_dsd_generator.py)
  - [core/module4_print_engine.py](core/module4_print_engine.py)
  - [core/module5_pdf_distributor.py](core/module5_pdf_distributor.py)
- Runtime paths and session state are coordinated by `orchestrator.py` using a `SessionState` object and a `paths` dictionary.

## Important conventions

- This is a Windows/AutoCAD application. Do not assume Unix-style paths or a non-Windows runtime.
- Prefer the existing `Path` and `os.path` patterns used in the project, especially for work directories like `tmp`, `Версии`, and `! ГОТОВО`.
- Keep the runtime contract between GUI and core stable: `orchestrator.run_pipeline(...)` remains the main entry point for the pipeline.
- If a change touches printing, layout mapping, or session flow, validate it against the module flow documented in [debug/CONTEXT.MD](debug/CONTEXT.MD): M2 → M3 → M4 → M5.
- The project intentionally keeps the GUI and core separate so the core can be updated without recompiling the EXE.

## Build and validation commands

Use these commands when working on packaging or runtime setup:

```powershell
pip install customtkinter pywin32 ezdxf pypdf psutil pyinstaller
pyinstaller --onefile --windowed --name print compile/print_runner.py
```

The project also includes a packaged PyInstaller spec in [compile/print.spec](compile/print.spec), which is the canonical packaging target.

## Working rules for agents

- Prefer minimal, surgical edits in the core modules rather than changing the GUI behavior unless the task is specifically about the UI.
- Keep the naming convention `moduleN_...` and `run(...)` functions consistent when adding or modifying modules.
- Preserve the Russian-path directory names and output conventions used by the project unless there is a clear, explicit requirement to change them.
- When fixing bugs, read the orchestrator and related module first; the root cause is often in the pipeline state or path resolution rather than the GUI.
- Treat `config/config.ini` and `CorePath` as deployment configuration, not as business logic.

## Good starting points for AI work

- [core/orchestrator.py](core/orchestrator.py): central state, path resolution, and pipeline orchestration.
- [compile/print_runner.py](compile/print_runner.py): runtime GUI logic and dynamic module loading.
- [debug/CONTEXT.MD](debug/CONTEXT.MD): architecture and module contracts.

## Notes

This repository does not currently include a formal automated test suite. For this reason, validation should focus on code inspection, local runtime checks, and preserving the existing pipeline contract.
