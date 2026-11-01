# Licenses and notices

CheckSI code is released under the [MIT License](../LICENSE). Some components shipped in the package keep their upstream terms. This page lists them and where their notices are installed. It is a summary; the license files themselves govern.

CheckSI builds on the [original upstream project](https://github.com/Paivel/Data-checker/tree/12a9e75acbfe5bea864e7c6a2eff60dc11a42e5b).

## Package contents

| Component | Path in the wheel | Terms | Notice files |
|---|---|---|---|
| CheckSI code and defaults | `checksi/` | MIT | [LICENSE](../LICENSE) |
| GUI icon | `checksi/gui/assets/checksi_logo.png` | MIT | [LICENSE](../LICENSE) |
| DP5q parameters (`weights.npz`, `manifest.json`) | `checksi/dp5q/data/` | DP5 models: MIT, Goodman Lab; CASCADE: MIT, Paton Lab; NFP layers: BSD-style; nmrshiftdb2 Database License | [NOTICE](../src/checksi/dp5q/data/NOTICE.txt), [DP5](../src/checksi/dp5q/data/LICENSE.dp5.txt), [CASCADE](../src/checksi/dp5q/data/LICENSE.cascade.txt), [NFP](../src/checksi/dp5q/data/LICENSE.nfp.txt), [nmrshiftdb2](../src/checksi/dp5q/data/LICENSE.nmrshiftdb2.txt) |
| Noto Sans Regular and Bold | `checksi/resources/fonts/` | SIL Open Font License 1.1 | [LICENSE](../src/checksi/resources/fonts/LICENSE.txt), [SOURCE](../src/checksi/resources/fonts/SOURCE.txt) |

The notices sit next to their assets. The wheel also copies all seven license and notice files to `checksi-<version>.dist-info/licenses/`, keeping their source paths. Its metadata declares `License-Expression: MIT AND LicenseRef-NFP AND LicenseRef-NMRShiftDB2 AND OFL-1.1`. The `LicenseRef` identifiers point to the supplied NFP and nmrshiftdb2 texts; they do not relicense upstream assets. The source distribution built by the maintainer workflow contains `LICENSE`, `README.md`, `pyproject.toml` and `src/`; a plain `uv build` from a full checkout also adds the `tests/test_*.py` modules. Sample documents and test fixtures are not packaged.

## Model parameters

The float32 parameters were exported without retraining from the DP5q models in the frozen [DP5 fork at `8f8e223`](https://github.com/aelashkin/DP5/tree/8f8e22364654bbce71dcb6abb414eee793b00d92). [NOTICE](../src/checksi/dp5q/data/NOTICE.txt) records the source, the modifications (NumPy implementation, safe numeric container, JSON metadata) and the attribution chain. No training data, optimizer state, executable model, pickle or NFP code ships.

The training data derive from nmrshiftdb2. The notice keeps the database attribution and links the public derived dataset and preparation method. When redistributing the parameters, keep `NOTICE.txt` and all four license files with them.

## Fonts

The fonts are unmodified copies of the [pinned Noto Sans source](../src/checksi/resources/fonts/SOURCE.txt). PDF reports embed them; embedding does not place generated reports under the OFL.

## Dependencies and external tools

Dependencies are installed separately and keep their own licenses; the MIT license does not relicense them. Notably, required PyMuPDF offers AGPL-3.0 or commercial terms, and the optional `gui` extra installs Qt for Python with LGPL/GPL/commercial routes. Java and LibreOffice are external tools and are not distributed.
