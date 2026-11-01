# Platform compatibility

Targets are CPython 3.13 on Intel macOS, Apple Silicon macOS,
Linux x86-64 and Windows x86-64. The opt-in native matrix is configured for
Linux/Windows on Python 3.12/3.13. See [local macOS verification](#local-macos-verification)
for manual coverage and unverified combinations. Resolution alone is
not runtime evidence: installed-package receipts and chemistry tests must pass
before claiming a new platform/Python combination.

## Python 3.15 dependency assessment

The 2026-10-22 runtime `uv.lock` audit at
`dfddc4b5d4eca080aa0c553648df972512e14e90`, covered base and `gui` separately. Targets were ordinary CPython (not free-threaded), macOS 15 on
x86-64/ARM64, Ubuntu 24.04-compatible glibc 2.39 on x86-64, and Windows AMD64.
Public PyPI JSON supplied wheel tags, `Requires-Python`, yanks and source availability. After removal of the unused
Word/COM dependency, the base closure contains 33 distributions on every target;
GUI adds seven distributions (40 total). This inventory records that revision;
the current lock uses certifi 2026.10.19 and urllib3 2.8.0 following the
[HTTP advisory remediation](security-audit.md).
Developer/Graphify groups were excluded. Java and document converters are
external runtime prerequisites and are not Python wheels.

Python 3.15 remains blocked; chemistry/GUI dependency pins and the Python metadata lower bound
are unchanged:

- [RDKit 2026.9.1](https://pypi.org/pypi/rdkit/2026.9.1/json) provides `cp315`
  wheels for Apple Silicon, Linux x86-64 and Windows AMD64, but no Intel macOS
  wheel or source distribution. Intel macOS must retain
  [RDKit 2025.9.3](https://pypi.org/pypi/rdkit/2025.9.3/json), whose wheels cover
  CPython 3.12–3.14, not 3.15.
- The newest permitted [pandas 2.3.3](https://pypi.org/pypi/pandas/2.3.3/json)
  has no `cp315` wheel on any target. `pandas<3` remains required for `molmass`.
- The newest [JPype1 1.7.1](https://pypi.org/pypi/jpype1/1.7.1/json), required by
  `pyopsin`, has no `cp315` wheel on any target. Source availability proves neither builds nor runtime.
- Locked [PySide6 6.11.0](https://pypi.org/pypi/PySide6/6.11.0/json) and its
  native components require Python `<3.15`. PySide6 6.12 permits `<3.16`, but
  changes its closure; upgrading remains untested.
- Other locked native extensions also lack 3.15 wheels (table below).

These gaps block Python 3.15; conditional RDKit upgrades alone cannot deliver it. Keep `setuptools<81` for
`pyopsin`'s `pkg_resources`, `pandas<3`, and the Intel RDKit pin. Source builds,
chemistry-library replacements, and algorithm changes are outside this
verification. CPython 3.14 is not part of the supported native matrix; in
particular the locked JPype1 release has no 3.14 wheels.

Before adopting a newer RDKit/NumPy/SciPy, run the existing graph, numerical,
structure and real model fixtures on every affected native target. Molecular
sanitization, aromaticity, atom ordering, geometry/conformer generation and
floating-point fitting can change scientific results. Compare the same
fixtures with the existing locked environment; a changed expectation requires
scientific review, not automatic replacement of the baseline.

## Complete locked wheel inventory

Each row applies to all four audit targets unless indicated. `wheel` means at
least one compatible wheel and an allowed Python version; it does not mean the
package was imported or executed on that target. `sdist only` means no
compatible wheel; `excluded` means release metadata rejects that Python.
`pyopsin` is a pure-Python source distribution built
by the existing bounded native wheelhouse preparation.

Release evidence is available at `https://pypi.org/pypi/<name>/<version>/json`.

| Distribution | Locked version | Scope | CPython 3.13 | CPython 3.15 |
| --- | --- | --- | --- | --- |
| annotated-types | 0.7.0 | core | wheel | wheel |
| certifi | 2026.1.4 | core | wheel | wheel |
| charset-normalizer | 3.4.4 | core | wheel | wheel |
| et-xmlfile | 2.0.0 | core | wheel | wheel |
| idna | 3.11 | core | wheel | wheel |
| jinja2 | 3.1.6 | GUI | wheel | wheel |
| jpype1 | 1.6.0 | core | wheel | sdist only |
| lxml | 6.0.2 | core | wheel | sdist only |
| markupsafe | 3.0.3 | GUI | wheel | sdist only |
| molmass | 2026.1.8 | core | wheel | wheel |
| numpy | 2.4.2 | core | wheel | sdist only |
| openpyxl | 3.1.5 | core | wheel | wheel |
| packaging | 26.0 | core | wheel | wheel |
| pandas | 2.3.3 | core | wheel | sdist only |
| periodictable | 2.0.2 | core | wheel | wheel |
| pillow | 12.1.0 | core | wheel | sdist only |
| pydantic | 2.12.5 | core | wheel | wheel |
| pydantic-core | 2.41.5 | core | wheel | sdist only |
| pymupdf | 1.26.7 | core | wheel | wheel |
| pyopsin | 0.2.4 | core | sdist only | sdist only |
| pyparsing | 3.3.2 | core | wheel | wheel |
| pyside6 | 6.11.0 | GUI | wheel | excluded |
| pyside6-addons | 6.11.0 | GUI | wheel | excluded |
| pyside6-essentials | 6.11.0 | GUI | wheel | excluded |
| python-dateutil | 2.9.0.post0 | core | wheel | wheel |
| python-docx | 1.2.0 | core | wheel | wheel |
| pytz | 2025.2 | core | wheel | wheel |
| qt-material | 2.17 | GUI | wheel | wheel |
| rdkit | 2025.9.3 | core | wheel | none |
| regex | 2026.1.15 | core | wheel | sdist only |
| requests | 2.32.5 | core | wheel | wheel |
| scipy | 1.18.1 | core | wheel | wheel |
| setuptools | 80.10.2 | core | wheel | wheel |
| shiboken6 | 6.11.0 | GUI | wheel | excluded |
| six | 1.17.0 | core | wheel | wheel |
| tomlkit | 0.14.0 | core | wheel | wheel |
| typing-extensions | 4.15.0 | core | wheel | wheel |
| typing-inspection | 0.4.2 | core | wheel | wheel |
| tzdata | 2025.3 | core | wheel | wheel |
| urllib3 | 2.6.3 | core | wheel | wheel |

The inventory deliberately distinguishes a metadata/wheel audit from native
runtime verification. See the workflow receipts for the revision actually
tested. uv documents the limits of alternate-platform resolution in its
[official resolution documentation](https://docs.astral.sh/uv/concepts/resolution/).

## Native installed-artifact verification

Dispatch **Internal tests** with `dp5q_native=true` on the reviewed SHA: four standard-runner Linux/Windows Python 3.12/3.13 jobs. Python 3.13 also checks GUI lifecycle/clean installed launch. Linux 3.13 requires successful real LibreOffice conversion; empty/missing executables or missing conversion receipts fail aggregation. Other lanes verify explicit converter-unavailable partial export. Real Windows LibreOffice remains unverified; implementation requires Windows 10+ Job Object creation attributes and native `soffice.com`/`soffice.exe`. Unsupported ownership fails optional conversion closed, retaining findings.

Linux 3.13 uses LibreOffice 26.8.1 from the official [Linux DEB](https://download.documentfoundation.org/libreoffice/stable/26.8.1/deb/x86_64/LibreOffice_26.8.1_Linux_x86-64_deb.tar.gz.mirrorlist), SHA-256 pinned in the workflow, cached by version/OS/architecture/digest and reverified after restore. DEBs unpack beneath runner temp; receipts retain executable/version. OS libraries come from runner/package repositories.

Always-on tests inspect portable resources/both entry points, reject missing-font/entrypoint mutations and run installed factory view/empty-PDF export offline outside the checkout, sharing existing dependency files. The dispatch matrix instead installs complete locked closures in fresh base/GUI environments.

`scripts/portability_check.py` builds clean sdist then wheel through `uv build`; resources/defaults/icon/fonts/license/provenance must be byte-identical. It exports portable conditional `constraints.txt`, including optional GUI pins but no developer groups. Constraints do not install Qt in base; unconstrained upgrades remain unverified. Bounded wheelhouse preparation authenticates locked artifacts; installations are offline/outside checkout. Base CLI runs from wheel and sdist, whose isolated backend comes from the wheelhouse. Base must contain no Qt. Isolated homes/config locations and factory view must create no settings. Inputs/package files have write bits removed and before/after inventories. Helpers stage tracked inputs only; stage new files first, reject symlinks/untracked scratch modules.

Generated PDF/DOCX fixtures contain two distinct NMR descriptions with HRMS/EA. Installed CLI exercises real chemistry/all exporters, Unicode and Noto Sans PDF text; correct status/exits are required. Missing converters retain identical findings. Paths contain spaces/Unicode. Installed GUI launches offscreen with packaged icon, closes through Qt and retains a screenshot.

Receipts preserve every analytical value, severities, settings, source associations, tool/font/distribution versions and resource/artifact hashes. Aggregate compares full analytical projections per format across all four lanes, including exact original text/numbers. Only separately retained source identity/settings, export paths/layout and tool provenance are excluded. PDF annotations compare compound IDs/phrases/colors; DOCX mapping is required for Linux 3.13 wheel/sdist. Rectangles/layout can differ. No finding normalization or scientific-expectation updates conceal differences. Real model/native-process verification is separate within this matrix. Duplicate wheel/sdist receipts cannot replace a missing OS/Python lane. These are regression checks, not scientific validation.

## Local macOS verification

Run macOS checks only on the owner's local Mac; never hosted/self-hosted macOS Actions. Follow [source checks](../AGENTS.md#commands) and [installed verification](development.md#build-and-installed-verification), retaining revision, interpreter, architecture and receipts. Keep `--extra gui` on syncs; smoke with deep name analysis and automatic opening off.

Local CheckSI receipts on Intel macOS 26.7.1/Python 3.13.16 cover source CLI/core/Qt checks, clean headless wheel/sdist installs, installed GUI startup, read-only inputs/package resources, Unicode paths and HTML/PDF/JSON/XLSX/source annotation. Real DOCX conversion uses LibreOffice 25.2.4.3; explicit converter absence retains partial findings.

Apple Silicon and macOS/Python 3.12 remain unverified.

## Updated extraction boundary

An earlier verification fixture split HRMS and trailing `Anal. Calcd` into separate blocks: one compound, two `hrms_ppm_too_large`, zero `ea_tolerance_exceeded`. That dated omission is superseded: [complementary-continuation grouping](../src/checksi/file_io.py) rejoins supported EA/HRMS without crossing another compound/section.

An in-memory probe of `1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 20.0.` followed by HRMS `C2H7O+ [M+H]+ 47.0491, found 47.0501` and EA `C2H6O: C, 52.14; H, 13.13. Found: C, 51.73; H, 13.13` now yields one paragraph/compound, complete assessment, two HRMS findings and one EA-tolerance finding.

The installed fixture retains two compounds/two HRMS/one EA findings. Its DOCX uses Arial to avoid split-delta fallback streams; both source PDFs associate four NMR phrases with two compound IDs. PDF original text retains a trailing space absent from DOCX; per-format projections preserve it. Converter presence cannot change analysis.

## Numerical comparison boundaries

The installed PDF/DOCX projections compare complete analytical values exactly
per input format. The same DOCX also compares exactly with/without conversion.
Existing DP5q tests retain their independently captured reference bounds:

- Network intermediates: `atol=1e-4`, `rtol=1e-5`; final float32 means and
  quantiles: `atol=1e-3 ppm`, `rtol=0` (`tests/test_dp5q_network.py`).
- Frozen-geometry end-to-end raw score: absolute `2e-5`; corrected errors:
  `0.0002 ppm`; atom compatibility scores: `0.003 percentage points`
  (`tests/test_dp5q_engine.py`).
- Algebraic scoring/fitting checks retain their tighter existing double-precision
  bounds, typically 8–12 decimal places (`tests/test_dp5q_numerics.py`).

These bounds cover known float32/backend arithmetic, not a permission to change
analytical meaning, assignments, statuses, refusal/advisory reasons or threshold
decisions. No numerical dependency, algorithm, reference fixture or test bound
was changed in this delivery. All platform receipts identify the exact dependency
closure; adopting new numerical dependencies requires fresh parity evidence.
