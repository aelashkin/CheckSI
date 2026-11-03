# Agent and automation use

For AI agents and scripts running CheckSI headlessly. For a walkthrough, see the [GUI/CLI guide](usage.md). Contributors follow [AGENTS.md](../AGENTS.md); linked guides own each contract.

## Install

Use Python 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/). Choose one route and omit the `gui` extra; in an existing checkout, skip `uv sync`, which removes unselected extras. Wheel routes need the wheel and `constraints.txt` from one trusted build.

```sh
# Source checkout
git clone https://github.com/aelashkin/CheckSI checksi
cd checksi
uv sync --locked --python 3.13
# uv tool
uv tool install --python 3.13 --constraints /path/constraints.txt /path/checksi-0.4.0-py3-none-any.whl
# Managed venv
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python --constraint /path/constraints.txt /path/checksi-0.4.0-py3-none-any.whl
```

In a checkout, prefix commands with `uv run --locked`; in a venv, use `.venv/bin/python -m checksi` (Windows: `.venv/Scripts/python.exe`). Installation needs network access or prepared local artifacts. Java enables deep name analysis; LibreOffice enables DOCX source annotation ([prerequisites](installation.md#prerequisites)).

## Verify and run

```sh
checksi --help
checksi --config-file /abs/checksi.toml config view
checksi --config-file /abs/checksi.toml dp5 doctor
out="$(mktemp -d)"
checksi --config-file /abs/checksi.toml run /abs/article.pdf --output-dir "$out"
```

Without `--config-file`, CheckSI reads the per-user [settings file](configuration.md#paths) when present, otherwise packaged defaults. Put `--config-file` before the subcommand to pin a user-reviewed TOML or the checkout's `settings.toml`, which mirrors packaged defaults; a missing file exits 1. Use absolute paths, a new output parent per run and `--override KEY=VALUE` for one-run changes; `config set` writes the selected or per-user file, so use it only with user approval. Keep `modes.open_reports_on_success` false and never launch `checksi-gui`.

`--deep-name-analysis on` needs Java; without it the run continues, but names report `name_resolution.status: failed`. DP5q scores only resolved structures, so use `--deep-name-analysis on --dp5`; `--dp5` alone skips every compound. `dp5 doctor` exits 0 when ready and 1 on failure ([DP5q](usage.md#dp5q-and-doctor)).

## Read results

Logs and the final `=== Run Summary ===` block of `key: value` lines use UTF-8 stdout; progress uses stderr. Read `results_json` from the [summary](reports.md#cli-summary-and-exits); unavailable artifacts print `not generated`. Its [schema 2](reports.md#json-v2) fields include:

- `analysis.assessment.status` (`complete`, `partial`, `none`) and `unassessed` reasons;
- `export.status` (`complete`, `partial`, `cancelled`; `failed` or `incomplete` means required export failed or was interrupted) and `export.artifact_warnings`;
- `analysis.compounds`, `analysis.error_stats` and, with DP5q, `analysis.dp5_summary`;
- `configuration` and `provenance.checksi` for reproducibility.

Assessment coverage and export completeness are [independent](reports.md#assessment-findings-and-export).

| Exit | Meaning |
|---|---|
| `0` | Complete assessment/export without warning/critical findings |
| `1` | System, configuration, input, usage or required-export failure |
| `2` | Warning/critical findings with complete assessment/export |
| `3` | Incomplete assessment or partial export, such as DOCX without LibreOffice |
| `130` | Cancellation; takes precedence |

## Limits and escalation

Runtime analysis sends no names or documents to a web resolver and downloads no models ([privacy](../README.md#privacy-and-limitations)). `conversion.timeout_seconds` and `dp5.timeout_seconds` bound conversion and each DP5q assessment, but runs have no overall deadline. Enforce one: send Ctrl+C/SIGINT or POSIX SIGTERM, then wait for exit 130; a signal sent during startup ends the process by that signal instead. Killing the process can leave private temporary files ([cancellation](usage.md#cli-run-and-cancellation)).

Findings and scores are advisory. A clean report or high score does not confirm a structure; missing scores or incomplete assessment are not acceptance. Do not disable diagnostics to reach exit 0. Report the run folder and leave chemistry judgements to a human expert ([limits](analysis.md#scientific-limits)).
