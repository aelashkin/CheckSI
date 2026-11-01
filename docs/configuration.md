# Configuration

CLI and GUI share schema-validated TOML. Factory settings are package resources; the repository's `settings.toml` is an explicitly selected example. [Usage](usage.md#settings-and-diagnostic-search) explains desktop controls.

## Precedence

With no `--config-file`, load user settings if present, otherwise immutable factory defaults. A missing explicitly selected file is an error. Reading, first launch and upgrades never create or save settings.

Precedence is selected settings, dedicated run flags, then `--override KEY=VALUE` in command order; the last value wins. Each run copies its effective settings. Overrides do not persist. GUI startup accepts the CLI run-layer session options; the source label does not imply startup flags were saved.

```sh
checksi config view
checksi config view --factory
checksi --config-file settings.toml run article.pdf --override dp5.enabled=false
checksi config set dp5.score_threshold_percent 20.0
```

`config view` prints JSON; factory view does not reset a session. Validation names dotted field locations. Unknown keys fail except the documented legacy inputs below.

## Schema and defaults

The [schema](../src/checksi/config_schema.py) defines types/constraints; [packaged defaults](../src/checksi/defaults.toml) owns initial values. This table lists all active fields. `paths`, `modes`, `solvents` and `errors` are required sections; `modes.output_mode` is required. Factory loading supplies them; `dp5`/`conversion` have schema defaults.

| Field | Packaged default; constraint/meaning |
|---|---|
| `paths.input_path` | `.`; input PDF/DOCX |
| `paths.output_dir` | `checksi-output`; parent for unique run folders |
| `conversion.libreoffice_path` | Unset; empty string means automatic executable discovery |
| `conversion.timeout_seconds` | `60`; integer 1–3600 |
| `modes.deep_name_analysis` | `off`; `on` resolves names locally using OPSIN/Java |
| `modes.output_mode` | `essential`; `detailed` adds analytical explanations/tables |
| `modes.open_reports_on_success` | `false`; explicitly enable desktop artifact opening |
| `modes.header_margin`, `modes.footer_margin` | Each `0.05`; page-height fractions 0–0.45 ignored during PDF extraction |
| `dp5.enabled` | `false`; optional bundled scoring |
| `dp5.score_threshold_percent` | `20.0`; 0–100; low-score finding/table threshold, not a score calculation parameter |
| `dp5.timeout_seconds` | `600`; positive integer deadline per assessment |
| `dp5.keep_artifacts` | `false`; retain successful request/result JSON and SDF |
| `errors.default_severity` | `critical`; fallback for codes without overrides |
| `errors.overrides` | Per-code `disabled`, `info`, `warning` or `critical`; initial exceptions are in packaged defaults |
| `solvents.entries` | Packaged residual-peak catalogue; each entry has name, aliases, proton shifts and carbon shifts |

Essential mode retains visible findings and necessary scope/refusal information; it does not mean “critical only.” Detailed mode adds successful comparisons and explanatory output. All structured results remain available regardless of display threshold.

Saved **10%** thresholds remain 10%. To adopt 20%, explicitly `config set dp5.score_threshold_percent 20.0`, use a run override, or edit/confirm/save in Settings. For an explicit file, put `--config-file FILE` before `config`. Reading does not change saved thresholds.

## Paths

| System | User settings destination |
|---|---|
| macOS | `~/Library/Application Support/CheckSI/settings.toml` |
| Linux | `$XDG_CONFIG_HOME/checksi/settings.toml`, falling back to `~/.config/checksi/settings.toml` |
| Windows | `%LOCALAPPDATA%/CheckSI/settings.toml`, with the existing home/AppData fallback |

Relative file paths in selected TOML resolve against that file's directory. Factory paths, positional inputs, run flags, GUI-entered relative paths and overrides resolve against invocation CWD. `~` expands. Resolution copies settings; viewing/saving TOML preserves its raw paths. GUI imports display resolved paths, making later export unambiguous.

The first user `config set` materializes factory settings with absolute input/output paths pinned to that invocation directory, and prints them. Later invocations retain those paths. GUI saving follows the same resolution model. Explicit files work when home discovery fails; user-default actions then report a path error. Converter bare names use PATH; directory-containing values follow the same relative-path rules. Blank clears the selection.

## Persistence and import

`config set KEY VALUE` updates the latest file atomically under a transaction lock. GUI saves compare the source revision and preserve edits on external modification; explicit reload/recreation handles changed/deleted sources. Export confirms replacement before overwriting another destination. Settings in read-only directories remain viewable.

**Confirm Changes** only applies the draft to the session. **Save User Defaults** writes the default destination; **Export Config** writes a chosen file. Import/restore stays a draft and preserves the selected Analyze input. [Settings workflow](usage.md#settings-and-diagnostic-search) owns navigation/close prompts.

Saves preserve unrelated comments, POSIX permissions and supported dotted-key formatting. Solvents/overrides and inline tables may become section tables. Symlinked files are saved through their target with shared locking. Explicit saves canonicalize accepted legacy keys; backups remain advisable before opening settings in an older version. [Upgrade guidance](installation.md#rename-and-upgrade) covers the CheckSI cutover.

## Diagnostics and solvents

Severities control finding visibility/counts; they never make invalid input usable or enable refused scoring. Stable identities and templates live in [definitions](../src/checksi/errors/defs.py) and [registry](../src/checksi/errors/registry.py); avoid copying a second catalogue. `formula_source_review_required` is informational in factory settings; a saved critical default without that override still applies critical severity.

The alias `nmr_solvent_and_frequency_missing` becomes canonical `nmr_solvent_or_frequency_missing`; if both are explicit, canonical wins, including `disabled`. It adds no statistics/GUI row. `hrms_formula_missing` still round-trips but has no active emitting check. Invalid-atom diagnostics remain independently configurable. `ea_include_oxygen` requires formula oxygen in calculated EA at `warning`/`critical` (factory fallback: critical); `info`/`disabled` removes that requirement. It emits no finding itself.

Solvent entries contain `name`, string-list `aliases`, and numeric-list `proton_shifts`/`carbon_shifts`; empty lists are allowed. Editing one GUI cell changes only that cell; unedited alias lists/precision remain exact. Solvent and diagnostic visibility settings are shared by CLI/GUI.

## Legacy keys

TOML `modes.operating_mode` maps `offline`→`off`, `online`→`on`; conflicting canonical values fail. Replace removed `--operating-mode` with `--deep-name-analysis`. Resolution is local in either migrated case. Python uses `DeepNameAnalysis`, not removed `OperatingMode`.

`dp5.probability_threshold_percent` is a load/dotted-CLI alias; unequal old/new values within one layer fail. Repeated same-spelling CLI values use normal precedence. Obsolete `managed`, `install_root`, `expected_rev` load with notices and no effect. Loads do not rewrite; explicit save removes obsolete keys. Notices use CLI stderr and GUI activity/status.
