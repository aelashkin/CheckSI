# Usage

CLI and GUI use the same analysis/reporting runner. [Configuration](configuration.md) owns defaults and overrides; [reports](reports.md) owns artifacts, statuses and exit codes.

## CLI run and cancellation

```sh
uv run --locked checksi run article.pdf --output-dir results
uv run --locked checksi run article.docx --override modes.output_mode=detailed
```

Use `--config-file FILE` before the subcommand to select TOML. Run flags include `--output-dir`, `--deep-name-analysis on|off`, `--output-mode essential|detailed`, `--dp5`/`--no-dp5` and repeatable `--override KEY=VALUE`; consult `checksi run --help`. Overrides affect one run without saving.

Ctrl+C requests cancellation; POSIX `SIGTERM`/`SIGHUP` do likewise, preserving previously ignored signals. Further signals repeat the cooperative request. Only completed compounds contribute findings. Cancellation before export suppresses reports; during export, completed artifacts remain and automatic opening is suppressed. Extraction/rendering may finish their current operation. DOCX conversion and model workers have bounded termination/cleanup; abrupt OS or supervisor death and filesystem failures can leave private temporary files.

Both streams use UTF-8. Progress/phase counts go to stderr; logs/summary go to stdout. Counts are phase-local, not an overall percentage. Configuration-load errors/notices use stderr so `config view` stdout remains JSON.

## GUI Analyze

Launch `checksi-gui` after installing the GUI extra. Choose PDF/DOCX and an output parent, adjust **Run options**, then select **Run Analysis**. Compact options apply immediately; the runner copies confirmed settings at start. A prior result remains identified while another input is selected or run.

**Open Findings** opens HTML; **Other formats** contains available exports. The result shows input, assessment, findings and export warnings. Progress, elapsed time and **Cancel** remain visible in Settings. Activity details preserve manual scrolling and offer **Jump to latest**/**Copy details**. Ctrl+O (Command+O on macOS) selects a document; Tab/Space operate controls. Closing during work requests cancellation and waits for the worker; status checks also finish before close.

## Settings and diagnostic search

Settings edits remain a draft. **Confirm Changes** validates and applies the draft to subsequent runs without writing a file. Leaving for Analyze with unconfirmed edits offers **Confirm Changes**, **Discard Changes** or **Keep Editing**. Discard restores the last confirmed session, including after invalid edits. An active check always retains its starting settings.

**Save User Defaults** confirms and saves settings for normal startup. **Export Config** confirms/writes a chosen TOML; **Import Config** loads a draft for review. Neither changes normal-startup defaults. **Restore Factory Settings** replaces the draft. Import/restore protects existing edits; closing with unsaved changes offers save defaults, close without saving or keep editing. File conflicts preserve edits and require explicit recovery. See [persistence](configuration.md#persistence-and-import).

Under **Diagnostic Settings → Show Per-Error Overrides**, search matches codes and all message templates, ignoring case (`13C` also finds `c13`). **Show only changed** combines with search and compares per-code choices with factory settings, including removed factory overrides. Hidden rows retain their values; clear both filters to see all diagnostics. Hover over codes for templates. Edit choices with one click or Enter; Escape cancels. Dropdowns open screen-bounded and never move afterwards.

**Advanced Configuration** shows editable validated JSON and a read-only factory tab. **Update Settings** updates the draft; confirmation applies it. **Copy JSON** copies the selected tab. Untouched values beyond a compact control's range/precision remain exact until that control changes. Blank Default Input retains its confirmed value. Appearance choices apply immediately and save automatically, separately from analytical drafts.

## DP5q and doctor

```sh
uv run --locked checksi run article.pdf --deep-name-analysis on --dp5
uv run --locked checksi dp5 doctor
```

`dp5 doctor` exits **0** when ready, **1** for package/asset/self-check failure (cancellation: 130). Its isolated full check has a 120-second startup deadline. [Repair instructions](installation.md#repair-bundled-dp5q-assets) distinguish source and installed-package recovery.

Java and a resolvable name are required for structure generation; `--dp5` does not turn deep name analysis on. `--no-dp5` disables scoring for one run. Eligibility is prepared after extraction/core analysis. Only eligible requests need model readiness; weights load in a run-owned child on its first request. Disabled, boron-only and other no-eligible runs need no model worker. See [applicability](analysis.md#dp5q-eligibility).

The fresh low-score threshold is **20%**; saved thresholds remain unchanged. It affects warnings/table visibility, not scores. [Configuration](configuration.md#schema-and-defaults) describes deliberate threshold changes.

GUI **Refresh Status** is a lightweight metadata check. **Run Full Self-check** uses the same bounded isolated diagnostics as CLI `dp5 doctor`, in the background. Status-check requests queue during analysis/status work; duplicate analyses are refused. Analysis requested during a full self-check must be started afterward. Status is advisory and does not change the DP5q toggle, chemistry eligibility or configuration. Closing waits for an active check. Missing assets require package repair/reinstallation, not a separate DP5 environment.

## Recovery

Validation reveals the relevant field or solvent cell. Analysis validates the selected document independently of unused Default Input. Unavailable exports stay disabled and warnings explain why. If desktop opening fails, use **Copy Path** or **Open Folder**. Set `modes.open_reports_on_success=false` for headless use; opening defaults off. Keep the unique run folder when reporting a problem: HTML/JSON preserve findings even if an optional artifact failed. Do not interpret missing scores or incomplete assessment as acceptance.
