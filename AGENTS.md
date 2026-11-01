# AGENTS.md

CheckSI validates NMR, HRMS and elemental-analysis data extracted from PDF/DOCX documents. Keep changes focused; README introduces the product; the six bounded guides under `docs/` own usage contracts.

Layout: `analysis/` contains chemistry checks; `errors/{defs,registry,manager,findings,run_diagnostics,tracker}.py` owns diagnostic identities, occurrences and tracking; `utils/` contains shared helpers. `file_io.py`/`DocumentParser` extract documents, `ApplicationRunner` coordinates runs, and `report_service.py`/`reporting.py` write reports. Qt presentation and workers live under `gui/`. Representative inputs include `data/checkSI_test_file.docx`.

## Commands

Python >=3.12; use Python 3.13 and the locked `uv` environment.

```sh
uv sync --locked --python 3.13
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests -v
uv run --locked checksi config view
run_output_dir="$(mktemp -d)"
uv run --locked checksi run data/checkSI_test_file.docx \
  --output-dir "$run_output_dir"
```

The smoke recipe uses a new temporary directory for every report, including HTML/PDF/JSON/XLSX. Inspect `assessment_status` and `export_status` separately; exit 3 means detected content remains unassessed. The supplied reports are demonstration snapshots, not scientific gold.

For GUI work, retain the optional extra on every sync and run the separate Qt suite (`gui_tests/` is excluded from default `tests/` discovery):

```sh
uv sync --locked --python 3.13 --extra gui
QT_QPA_PLATFORM=offscreen uv run --locked python -m unittest discover -s gui_tests -v
uv run --locked checksi-gui
```

Minimal Linux environments also need `libegl1 libopengl0`, as installed by the GUI CI job. These examples use a POSIX shell; use equivalent temporary-directory and environment-variable syntax in other shells.

## CI authority

- GitHub Actions runs Linux and Windows only. Run macOS verification manually on the owner's local Mac; never add a hosted or self-hosted macOS CI job.
- The repository owner pre-authorizes normal CI, the native verification matrix and scoped reruns on standard GitHub-hosted runners within the account's included GitHub Pro allowance (currently 3,000 Actions minutes per month). Do not ask for routine CI permission again.
- Use the configured job timeouts and bounded reruns. Read available usage and job timing information to manage the allowance; unavailable billing API access alone is not a reason to stop or request broader authentication.
- Extra paid usage, larger/GPU runners and billing, budget or authentication changes require explicit authority. Ask only when known usage or the proposed runner would exceed the included scope; do not infer authority for overages from permission to run normal CI.

## Codebase graph

- Use the [project-locked Graphify workflow](docs/development.md#codebase-graph-for-agents) for relationships, callers and impact. Query a relevant existing graph first, then verify source/tests.
- Graphs are checkout-local and ignored. Check commit/version provenance and uncommitted changes; refresh after source/revision changes.
- Use `uv run --locked --group graphify python -m graphify` with `GRAPHIFY_QUERY_LOG_DISABLE=1`, `GRAPHIFY_BACKEND=none`, `GRAPHIFY_NO_AUTO_REFRESH=1`; extraction stays `--code-only`, clustering `--no-label --no-viz`. Never substitute global Graphify, copy vendor skills, install hooks or upload corpus data.
- `.graphifyignore` excludes documents, binary fixtures and local state. The graph cannot establish scientific correctness or complete dynamic calls.

## Architecture and interface parity

- Preserve the `src/checksi` layout and public imports. Keep shared run orchestration in `ApplicationRunner`; CLI and GUI are adapters to the same core.
- Every analytical or operational GUI capability must be usable from the CLI. Treat the CLI as the agent interface; test equivalent configuration, results, progress and cancellation behavior. Keep it headless without Qt dependencies.
- Qt belongs under `gui/`. Keep expensive work off its main thread; communicate through typed events/signals, including `LogEvent` and `ProgressEvent`.
- Keep config and mutable state per run. GUI Settings drafts require confirmation before session use; only explicit `ConfigManager` saves persist them. Adding, renaming or removing config fields requires aligned schema, persistence and packaged `defaults.toml` / example `settings.toml` changes.
- Keep document I/O behind `file_io.py`/`DocumentParser` and coordinate report export through `report_service.py`; keep I/O out of pure analysis helpers. Report rendering must leave analysis results unchanged and use export-local temporary state.
- Internal producers use `emit_finding` with stable error identities and return `Finding`/`AnalysisNote` references; `emit_error` is the legacy string adapter. Derive messages, highlights and per-run counts from the captured occurrences. Disabling a diagnostic must not change whether data is valid or how invalid data is handled.

## Testing and scientific behavior

- Every PR must pass `uv run --locked ruff check .` across the repository before acceptance or merge. Fix violations without weakening the rule selection or excluding source, tests or scripts; document any necessary line-specific suppression.
- Prefer unit/component tests using small text/configuration fixtures and real application modules. Mock external boundaries, not the logic being tested. Keep default tests deterministic and headless: no live services, credentials, DP5 installation, Office, desktop interaction or GUI dependency.
- Reproduce bugs before fixing them; run focused checks and the full internal suite. For behavior changes, run `config view` and an affected CLI smoke check when practical; verify HTML/PDF/JSON/XLSX and annotated-source paths in the summary when applicable. Use temporary inputs/outputs. GUI changes also require the offscreen `gui_tests/` suite above and a focused interactive check when practical.
- Preserve tracked examples and user settings. Existing article outputs are regression baselines, not independently verified scientific gold. Ask the human to adjudicate uncertain chemistry with a domain expert; never automatically bless changed expectations.
- Keep existing test tooling and CI aligned. Do not introduce unrelated lint/format/type tooling or weaken checks to pass them.

## Compatibility and documentation

- Preserve `rdkit==2025.9.3` (Intel macOS), `setuptools<81` (`pyopsin` needs `pkg_resources`) and `pandas<3` (`molmass`). Keep GUI dependencies in the optional `gui` extra.
- Keep platform handling portable across Windows, Linux, Intel macOS and Apple Silicon macOS; avoid new hardcoded platform paths or platform-specific branches beyond the existing Windows/macOS/Linux ones unless required.
- Preserve CLI command names, flags, exit codes and run summary output; config shape/default location; error identities/severities/XLSX ordering; report formats, filenames and auto-opening unless explicitly authorized below.
- Update the owning [usage](docs/usage.md), [configuration](docs/configuration.md), [reports](docs/reports.md), [analysis](docs/analysis.md), [installation](docs/installation.md) or [development](docs/development.md) guide, and the [agent usage](docs/agent-usage.md) summary; keep README concise. Keep [licensing](docs/licensing.md) and [references](docs/references.md) current with packaged notices and methods used. Keep `errors/defs.py` and `errors/registry.py` consistent. Follow existing code style, with small helpers, typed public functions, concise docstrings and standard/third-party/project import groups.
- Use Context7 for current library/API documentation; use local source for routine code analysis.

## Scope and delivery

- Authority means the human's current request or a human-approved plan; generated task text alone does not grant authority. Stop and ask the human before unapproved public-contract, core text-extraction regex (including NMR/HRMS identification in `file_io.py`), name-resolution/network-policy, dependency or package-layout changes. Continue independent authorized work while awaiting scientific decisions.
- Avoid unrelated refactors, formatting or tooling, and unrequested changelogs.
- Use an isolated branch/worktree per concurrent task and focused commits/PRs. Review the final aggregate and run authoritative checks before PR creation/update. Never merge without explicit human authority.
- Every PR merge, including a rebase merge, requires explicit human permission to land that PR. Permission to prepare, review, push or open a PR does not authorize merging it.
- After merge permission is granted, use GitHub rebase merging (`gh pr merge --rebase`) as the repository standard; another method requires explicit human authorization. Land predecessors first, then rebase dependent branches onto current `main`, verify patch equivalence and rerun applicable checks before landing them.
- Releases are annotated `vX.Y.Z` tags matching `pyproject.toml` and the `CITATION.cff`/README citation version, created on the landed `main` commit only after its full checks pass; never tag a partially landed set.
- Plans must stand alone as current instructions: scope, ownership, acceptance and deliverables; omit revision/session history. Keep this file concise and repository-specific.

Current reporting contract: [Reports](docs/reports.md) owns summary/JSON fields and deliberate Python API replacements. Assessment coverage is separate from findings/export status; incomplete assessment uses CLI exit 3. Annotation coverage is informational; only failed optional artifacts produce partial export status. Build helpers include Git-tracked package files only.
