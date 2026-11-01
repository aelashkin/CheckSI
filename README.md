# checkSI

**Computer-assisted pre-submission checking of characterization data for organic compounds.** checkSI reads PDF or DOCX supporting information, extracts supported compound descriptions, and cross-checks reported NMR, HRMS and elemental analysis. Optional local name conversion enables structure-based checks.

## Why

A wrong HRMS digit, missing carbon signal or formula left behind after a name change is easy to overlook. checkSI puts findings beside the extracted description so authors can review the evidence before submission.

Optional DP5q compares reported ¹³C shifts with predictions for a proposed structure, without DFT calculations. Its compatibility score is advisory.

## What it checks

| Area | Examples |
|---|---|
| Format | Spectral headers, precision, signal order, multiplicity and coupling notation |
| Formula | Name-derived, HRMS and elemental-analysis formula consistency where comparable |
| HRMS / EA | Ion charge, calculated mass, mass accuracy and elemental percentages |
| ¹H NMR | Integrals against the available formula; exchangeable-proton notes |
| Other NMR | Supported nucleus counts, residual solvent peaks and C–F couplings |
| Stereochemistry | Supported structural partitions, meso status and stereodescriptor roles |
| DP5q | Optional ¹³C compatibility scoring with explicit applicability and scope |

Structure checks require **deep name analysis**, successful local OPSIN conversion and supported chemistry. See [analysis boundaries](https://github.com/aelashkin/CheckSI/blob/main/docs/analysis.md).

## Message Guide

The [checkSI Message Guide](https://htmlpreview.github.io/?https://github.com/aelashkin/CheckSI/blob/main/docs/checkSI_message_guide.html) explains when each message is issued and shows example compound descriptions with the selected program output. The test records shown there are generated for demonstration and are not experimental data. To view it offline, download [`docs/checkSI_message_guide.html`](https://github.com/aelashkin/CheckSI/blob/main/docs/checkSI_message_guide.html) and open it in a browser.

## Output

Each exported run has its own folder beneath the selected output parent.

| File | Contents |
|---|---|
| `findings.html` | Required readable findings and assessment coverage |
| `results.json` | Required structured analysis, configuration and export status |
| `findings.pdf` | Optional PDF of the findings |
| `source-annotated.pdf` | Optional source highlights where ownership can be established |
| `statistics.xlsx` | Optional diagnostic counts, carbon scope and DP5q assignments |

Assessment coverage, findings and export completeness are separate. Read [report/status contracts](https://github.com/aelashkin/CheckSI/blob/main/docs/reports.md) before interpreting a clean-looking report.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then use Python 3.13:

```sh
git clone https://github.com/aelashkin/CheckSI checksi
cd checksi
uv sync --locked --python 3.13 --extra gui
uv run --locked checksi-gui
uv run --locked checksi run article.pdf --output-dir results
uv run --locked checksi run article.pdf --output-dir results --deep-name-analysis on --dp5
```

Java enables local OPSIN; LibreOffice enables DOCX source annotation. Headless installation, distribution bundles, platforms and upgrades: [installation guide](https://github.com/aelashkin/CheckSI/blob/main/docs/installation.md). AI agents and scripts: [agent usage](https://github.com/aelashkin/CheckSI/blob/main/docs/agent-usage.md).

## Settings

Deep name analysis and DP5q default to off. Analytical Settings edits form a draft: **Confirm Changes** applies it to the session; **Save User Defaults** retains it for normal startup. **Import Config** and **Export Config** handle chosen TOML files. Analyze options apply immediately; active runs retain their starting settings. Diagnostic search and **Show only changed** help find severity overrides. Disabling a finding changes visibility and counts, while validity and scoring eligibility remain unchanged. See [configuration](https://github.com/aelashkin/CheckSI/blob/main/docs/configuration.md).

## Privacy and limitations

Document analysis, OPSIN conversion, DP5q and report generation run locally. Installation needs downloaded dependencies or a prepared local bundle. Runtime analysis does not send names or documents to a web resolver or download models.

Recognition is bounded: unsupported headers, tables and other detected omissions need manual review; coverage cannot enumerate every missed description. Signal overlap, missing peaks and unresolved stereochemistry can require expert interpretation. DP5q can score a limited structural scope, skips boron compounds, and cannot establish identity. A high score is not confirmation; a low score requests inspection.

## Authors

Pavel Sakharov\* (a), Andrew Elashkin (b), and Mark Gandelman (a, c).

(a) Schulich Faculty of Chemistry, Technion – Israel Institute of Technology, Haifa 3200009, Israel.

(b) Taub Faculty of Computer Science, Technion – Israel Institute of Technology, Haifa 3200009, Israel.

(c) The Resnick Sustainability Center for Catalysis, Technion – Israel Institute of Technology, Haifa 3200009, Israel.

\* Corresponding author: [Pavel Sakharov](mailto:pavelpas@campus.technion.ac.il).

## Citation

If you use checkSI, cite the [ChemRxiv preprint](https://doi.org/10.26434/chemrxiv.15011545/v1), posted November 2, 2026:

```bibtex
@article{sakharov2026checksi,
  author  = {Sakharov, Pavel and Elashkin, Andrew and Gandelman, Mark},
  title   = {{checkSI}: Computer-Assisted Pre-Submission Checking of Characterization Data for Organic Compounds},
  journal = {ChemRxiv},
  year    = {2026},
  doi     = {10.26434/chemrxiv.15011545/v1},
  url     = {https://doi.org/10.26434/chemrxiv.15011545/v1}
}
```

GitHub's **Cite this repository** uses [CITATION.cff](https://github.com/aelashkin/CheckSI/blob/main/CITATION.cff), which also records software version 0.4.0.

## References

checkSI implements or relies on published methods, models, data and software, including DP5q, DP5, CASCADE, nmrshiftdb2, OPSIN, RDKit, ETKDG and MMFF94s. Cite those separately when your results depend on them; see [references](https://github.com/aelashkin/CheckSI/blob/main/docs/references.md).

## License

checkSI code is released under the [MIT License](https://github.com/aelashkin/CheckSI/blob/main/LICENSE). Bundled DP5q model parameters and fonts keep their own terms; [licenses and notices](https://github.com/aelashkin/CheckSI/blob/main/docs/licensing.md) lists the terms and notices for components shipped in the package.

## Further reading

[CLI and desktop workflow](https://github.com/aelashkin/CheckSI/blob/main/docs/usage.md) · [Development](https://github.com/aelashkin/CheckSI/blob/main/docs/development.md) · [Platform evidence](https://github.com/aelashkin/CheckSI/blob/main/docs/platform-compatibility.md) · [Scientific evidence](https://github.com/aelashkin/CheckSI/blob/main/docs/chemistry-verification.md) · [DP5q evaluation](https://github.com/aelashkin/CheckSI/blob/main/docs/dp5q-evaluation.md)
