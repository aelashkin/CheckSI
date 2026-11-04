# Development

Use repository instructions in [AGENTS.md](../AGENTS.md). Keep dependencies locked, scientific expectations explicit, and user settings/source documents unchanged. [Installation](installation.md) owns user setup; this page owns contributor workflows.

## Architecture

`src/checksi` contains the shared core. `ApplicationRunner` copies effective configuration, parses documents, runs `CheckSIController`, prepares/enriches optional DP5q and coordinates report export. CLI and GUI are adapters to this runner; the CLI remains headless without Qt.

`file_io.py`/`DocumentParser` own extraction; `analysis/` owns chemistry; `errors/{defs,registry,manager,findings,run_diagnostics,tracker}.py` owns diagnostic identities, occurrences and tracking. Producers use `emit_finding`; `emit_error` is the legacy string adapter. `report_service.py` coordinates export and `report_rendering.py`/`reporting.py` render using export-local state. Rendering cannot mutate analysis.

Qt stays under `gui/`; expensive work uses background workers and typed `LogEvent`/`ProgressEvent`. Model calculation uses a bounded isolated process. Configuration/mutable state belongs to a run; confirmed GUI session state is separate from persisted TOML. Analytical/operational GUI features need CLI parity. Schema changes require matching defaults/example/persistence/docs. [Reports](reports.md) and [DP5q architecture](dp5q-architecture.md) own detailed contracts.

## Lint and tests

```sh
uv sync --locked --python 3.13
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests -v
uv run --locked checksi config view
run_output_dir="$(mktemp -d)"
uv run --locked checksi run data/checkSI_test_file.docx --output-dir "$run_output_dir"
```

Inspect assessment and export separately; exit 3 means incomplete assessment. The `data/checkSI_*` inputs and report ZIPs are demonstration snapshots, not scientific gold; the [asset manifest](publication-assets.tsv) records original/published hashes. Report source hashes refer to original inputs; publication copies normalize file paths and embedded document-name metadata. Each smoke uses a fresh temporary parent. Default tests are deterministic/headless, use real application modules with small fixtures, and mock external boundaries; no credentials/live services, Office or separate DP5 install. Software parity is not scientific validation.

For GUI work retain the extra on every sync:

```sh
uv sync --locked --python 3.13 --extra gui
QT_QPA_PLATFORM=offscreen uv run --locked python -m unittest discover -s gui_tests -v
uv run --locked checksi-gui
```

The Qt suite is separate from default discovery. Minimal Linux needs `libegl1 libopengl0`. Run focused interactive checks when practical. Repository-wide Ruff must pass without weakening selection/exclusions. Documentation budgets/links are checked through the existing unittest suite against [the manifest](_budgets.toml); new Markdown needs bounded ownership, not an unbounded appendix.

## README media

`docs/assets/readme/` contains real GUI/report captures from `data/checkSI_test_file.docx`, using packaged defaults with deep name analysis and DP5q off. The source application revision is `15cb9fecf2d5c064236ea67f9e3d8455f57bbe3b`; [sample provenance](publication-assets.tsv) records the input hash.

To regenerate, prepare a locked Python 3.13 GUI environment. Isolate config, appearance, file-picker history and all inputs/outputs in neutral temporary folders. Show only the demo shortcut and test documents in the picker. Capture actual file selection, **Run Analysis**, progress and results, then the same run's HTML findings in isolated Chromium. Preserve actual statuses and messages. Assemble continuous frames with editorial cursor motion, camera moves and smooth transitions; describe test data and shortened waits in the image's alternative text. Loop back smoothly to the opening. Inspect every picker state and scan all frames for private paths, usernames and unrelated files. Record source/configuration, dimensions, timing, privacy audit and artifact status with the PR.

## Build and installed verification

```sh
uv run --locked python scripts/portability_check.py build --out-dir /path/new-build
```

The helper stages Git-tracked package files/metadata only, builds wheel/sdist and exports matching locked `constraints.txt`. Stage intentional new runtime files first; untracked scratch files stay excluded. Clean staging prevents stale `build/lib` resurrection. Plain `uv build` needs a separately exported matching constraint file. Archives ship README/package resources, not the repository guide corpus; distribution README guide URLs must resolve to real repository files.

For the native DP5q verifier, choose new wheelhouse/build/receipt destinations:

```sh
uv run --locked python scripts/dp5q_packaging_check.py prepare --wheelhouse /path/new-wheelhouse --max-mib 600
wheel_path="$(uv run --locked python scripts/dp5q_packaging_check.py build --out-dir /path/new-wheel-output)"
uv run --locked python scripts/dp5q_packaging_check.py verify --wheel "$wheel_path" --wheelhouse /path/new-wheelhouse --receipt /path/new-receipt.json
```

Preparation authenticates bounded public downloads; build/verification use local artifacts offline. Portable verification installs both artifact types outside the checkout, separately for base/GUI closures, checks entry points/resources/licenses and compares analytical projections. DP5q's bounded wheelhouse/build/verifier checks real model execution, warm reuse, cancellation/reaping, zero-worker disabled/no-eligible runs and network/import guards. See [native procedure](platform-compatibility.md#native-installed-artifact-verification) and [reference tools](dp5q-reference.md); legacy TensorFlow/NFP/HDF5/pickle tooling stays opt-in and outside product dependencies.

Preserve `rdkit==2025.9.3`, `setuptools<81`, `pandas<3`, GUI optionality, parameter/notice hashes and pinned bytes. Build helpers reject symlink inputs; do not bypass hooks or scientific gates to pass a build.

## Codebase graph for agents

Graphify is optional, locked as `graphifyy==0.9.93` in the `graphify` group. Use this checkout's environment, never a global replacement:

```sh
uv sync --locked --python 3.13 --group graphify
export GRAPHIFY_QUERY_LOG_DISABLE=1
export GRAPHIFY_BACKEND=none
export GRAPHIFY_NO_AUTO_REFRESH=1
uv run --locked --group graphify python -m graphify --version
uv run --locked --group graphify python -m graphify extract . --code-only
uv run --locked --group graphify python -m graphify cluster-only . --no-label --no-viz
uv run --locked --group graphify python -m graphify query 'How does ApplicationRunner coordinate export?' --seed ApplicationRunner --budget 1500
uv run --locked --group graphify python -m graphify explain ApplicationRunner
```

Retain `--extra gui` during sync if needed. These settings disable logging, model backends and automatic workstation refresh. `.graphifyignore` limits source/tests/scripts/packaging/supported CI metadata; documents, binaries, images and local state are excluded. Read unsupported configuration formats directly.

Reuse relevant `graphify-out/graph.json` only after checking commit/version provenance and uncommitted changes. Refresh extraction/clustering after source/revision changes; add `--force` after corpus-policy/version changes. Outputs/caches are ignored and checkout-local. Query first, verify source/tests afterward; inferred edges and dynamic Qt/Python calls may be incomplete. Exit 3 means ambiguous names, 4 no match. No global skill install, copied vendor skill, hook, remote service or corpus upload is required. Graphs do not validate chemistry.

## Native checks

Ordinary CI runs Linux internal tests and a separate GUI suite. Dispatch the opt-in **Internal tests** matrix with `dp5q_native=true` on the reviewed SHA: Linux/Windows x64, Python 3.12/3.13. It checks installed wheel/sdist behavior and native model/worker paths; Python 3.13 also verifies GUI launch. Linux 3.13 requires pinned real LibreOffice; other lanes exercise explicit converter-unavailable results. A skipped manual job does not replace native receipts.

Run macOS verification only on the owner's local Mac; no hosted/self-hosted macOS jobs. [Platform evidence](platform-compatibility.md) owns tested combinations, exact comparison tolerances and remaining limitations. Retain receipts with exact revisions, model/method IDs, hashes, guard activation, actual interpreter exit and native timing/RSS; cold initialization and warm reuse are separate measurements.

Normal CI and bounded standard-runner reruns within the owner's included Pro allowance are pre-authorized by [CI authority](../AGENTS.md#ci-authority). Read timing/usage, preserve timeouts, and get separate authority for paid overages/nonstandard runners or billing/authentication changes. Never present unavailable hosted checks as passed.

## Python namespace and API migration

The canonical package is `checksi`, with `CheckSIController`, `CheckSIArgumentParser` and `CheckSIMainWindow`; update imports, patch targets, resource lookups and worker module strings. No `data_checker` shim remains. Distribution/commands/default-path and JSON-consumer changes are documented in [upgrade instructions](installation.md#rename-and-upgrade); exact earlier report-helper replacements remain in [report API references](reports.md#python-api-references).

Deep-name integrations use `DeepNameAnalysis.ON`/`.OFF` and `config.modes.deep_name_analysis`; old `OperatingMode`/attribute and `--operating-mode` are removed. The resolver retains its positional five-tuple plus optional cancellation/details. Tracking adapters remain supported without importing workbook/desktop-opening dependencies. Keep public functions typed, helpers small and docstrings concise.
