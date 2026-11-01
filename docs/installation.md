# Installation

Use a trusted source checkout or matching wheel/constraints bundle. CheckSI recommends CPython 3.13; [platform evidence](platform-compatibility.md) distinguishes installation targets from verified combinations.

## Source checkout

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) for your platform, then:

```sh
git clone https://github.com/aelashkin/CheckSI checksi
cd checksi
uv sync --locked --python 3.13
uv run --locked checksi config view
```

For the desktop interface, sync with `--extra gui` and run `uv run --locked checksi-gui`. Retain that extra on later syncs; an unselected extra can be removed. The CLI remains headless without Qt. `python -m checksi` is available inside the installed environment. Installing Python/dependencies needs network access or prepared local artifacts; analysis does not install tools.

## Distribution bundle

Use the wheel and `constraints.txt` from the same trusted build. The constraints pin the verified core/GUI dependency closure; they do not install optional GUI packages by themselves.

```sh
uv tool install --python 3.13 --constraints /path/constraints.txt '/path/checksi-0.4.0-py3-none-any.whl[gui]'
checksi-gui
```

Omit `[gui]` for automation. In a manually managed environment:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python --constraint /path/constraints.txt /path/checksi-0.4.0-py3-none-any.whl
```

Windows uses `.venv/Scripts/python.exe`. A wheel installed without matching constraints may select unverified dependencies. Maintainers build wheel, sdist and constraints together using the [development workflow](development.md#build-and-installed-verification). Installation does not create user settings.

## Prerequisites

- **Java:** required only for deep name analysis through local OPSIN. Set `JAVA_HOME` if discovery fails. OPSIN uses the JAR supplied by locked PyOpsin; there is no web fallback or runtime JAR download.
- **LibreOffice:** required only for a DOCX annotated-source PDF. DOCX extraction, findings HTML/PDF, JSON and XLSX work without Office. Configure `conversion.libreoffice_path` if discovery fails. Windows requires a native `soffice.com`/`soffice.exe` and Windows 10+ process ownership; batch scripts are unsupported. Word/COM is unused.
- **Desktop display:** required for normal GUI use. Minimal Linux systems need `libegl1 libopengl0`; offscreen tests use `QT_QPA_PLATFORM=offscreen`.

The application never downloads Java, LibreOffice or model parameters at runtime. External-tool failure is reported at its affected boundary.

## Platform evidence

Targets are Python 3.13 on Intel/Apple Silicon macOS, Linux x86-64 and Windows x86-64. CI targets Linux/Windows Python 3.12/3.13; verification requires receipts. Intel macOS 3.13 has manual source, clean wheel/sdist, GUI and real-converter coverage. Other macOS combinations remain unverified. Consult [coverage and dependency blockers](platform-compatibility.md) before adopting another interpreter or platform.

## Rename and upgrade

CheckSI replaces `data-checker`/`data_checker` with `checksi`, `checksi-gui` and `checksi.*`, without shims. Different distribution names coexist: install CheckSI using the bundle instructions, verify its commands, then remove the old tool:

```sh
checksi --help
checksi config view
checksi dp5 doctor
uv tool uninstall data-checker
```

For a manually managed venv, verify `.venv/bin/python -m checksi --help` and `dp5 doctor`, then `uv pip uninstall --python .venv/bin/python data-checker` ([uv package commands](https://github.com/astral-sh/uv/blob/main/docs/pip/packages.md)). Windows uses `Scripts/python.exe`. Uninstalling old binaries preserves user settings/reports. `--reinstall` repairs an existing same-name tool; it does not remove another distribution.

Existing settings/output folders stay intact. Former defaults were `~/Library/Application Support/Data Checker/settings.toml` (macOS), `%LOCALAPPDATA%/Data Checker/settings.toml` (Windows), and `$XDG_CONFIG_HOME/data-checker/settings.toml` or `~/.config/data-checker/settings.toml` (Linux). CheckSI does not search/copy/rewrite them. Select a previous TOML explicitly:

```sh
checksi --config-file /path/to/previous/settings.toml config view
checksi --config-file /path/to/previous/settings.toml run article.pdf
checksi-gui --config-file /path/to/previous/settings.toml
```

Review the configuration, then use GUI **Save User Defaults** to write the new [default location](configuration.md#paths). **Import Config** loads a draft for review; confirm/save explicitly. Selected old files retain their explicit paths; exporting does not change normal-startup defaults. The default output parent becomes `checksi-output`. Analytical TOML schema is unchanged by renaming; older `paths.report_path`/`paths.error_stat_path` files must use `paths.output_dir`. Qt appearance uses a new CheckSI identity, so select the theme again. JSON consumers must adopt [schema 2 and `provenance.checksi`](reports.md#json-v2). The working GitHub repository URL is retained pending a separate owner-directed cutover.

## Troubleshooting

Check Python 3.13, environment/GUI extra, Java/`JAVA_HOME` for names, and LibreOffice for DOCX annotation. Use retained findings and [workflow recovery](usage.md#recovery); consult the dated [advisory review](security-audit.md).

### Repair bundled DP5q assets

In a source checkout, inspect/preserve intentional edits, including staged changes, before discarding accidental asset damage:

```sh
git diff HEAD -- src/checksi/dp5q/data
git restore -- src/checksi/dp5q/data
uv sync --locked --python 3.13 --extra gui
uv run --locked checksi dp5 doctor
```

Dependency sync alone cannot restore tracked files. For installed wheels, select the intended environment and force-reinstall the complete trusted package:

```sh
uv pip install --python /path/environment/bin/python --constraint /path/constraints.txt --force-reinstall '/path/checksi-0.4.0-py3-none-any.whl[gui]'
uv tool install --force-reinstall --python 3.13 --constraints /path/constraints.txt '/path/checksi-0.4.0-py3-none-any.whl[gui]'
```

Choose pip for a managed venv or tool for a uv tool installation; omit `[gui]`/`--extra gui` for headless use. Windows uses `Scripts/python.exe`. Rerun `checksi dp5 doctor` in that environment. [uv reinstall flags](https://github.com/astral-sh/uv/blob/main/crates/uv-cli/src/lib.rs) replace installed package files.
