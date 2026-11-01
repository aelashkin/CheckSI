# Reports and result contracts

This page owns artifact names, coverage/status semantics, CLI summary and JSON contracts. [Analysis](analysis.md) explains applicability; [configuration](configuration.md) owns settings. Rendering must leave analytical results unchanged.

## Artifacts and ownership

`--output-dir` selects a parent; each export atomically reserves a private folder such as `results/article-a1b2c3d4/`. All run artifacts stay together; source, sibling PDFs and prior runs are not overwritten.

| Artifact | Requirement and meaning |
|---|---|
| `findings.html` | Required; self-contained descriptions, findings, molecule images, evidence and DP5q tables |
| `results.json` | Required; structured analysis, effective settings, provenance and export metadata |
| `findings.pdf` | Optional A4 findings rendered from HTML with bundled Noto Sans fonts |
| `statistics.xlsx` | Optional counts and scope/assignment sheets |
| `source-annotated.pdf` | Optional annotated PDF input, or private LibreOffice conversion of DOCX |
| `dp5_artifacts/compound_<n>/` | Optional successful `assessment.json` and `candidate.sdf` when retention is enabled |

JSON/HTML precede optional exports. Failed optional artifacts retain findings and produce actionable warnings; required rendering failure is an error with saved locations logged. Input can be read-only; output/temp storage needs write access. Converter profiles are private and removed afterward. The input stem is capped at 32 characters; prefer short Windows output roots when long-path support is absent.

## Assessment, findings and export

These are independent dimensions:

- Assessment `complete` (**Assessed**): descriptions assessed with no known candidate omissions; `partial` (**Partly assessed**): some assessed with known omissions; `none` (**Nothing assessed**): no supported descriptions assessed.
- Finding counts reflect enabled `critical`, `warning` and `info` identities. Plain operational/analytical notes may be uncounted. Disabled findings do not change validity, coverage or downstream eligibility.
- Export `complete`, `partial` or `cancelled` describes artifacts. Only failed/unavailable optional artifacts make an otherwise finished export partial. Annotation ambiguity/coverage alone is informational.

Coverage appears in HTML/PDF, JSON, CLI and GUI. `analysis.assessment` has `status`, `assessed_descriptions`, `unassessed`. Each omission has `reason`, `excerpt` and `source`; ordinary excerpts are capped at 600 characters, while unsupported-formula evidence retains its complete token. Paragraph coordinates are 1-based paragraph numbers and zero-based half-open normalized-text offsets. DOCX table locations use 1-based `table_path` entries and paragraph number when known; document records invent no coordinates.

Reasons distinguish unsupported/missing proton headers, additional descriptions/sections, unsupported formula notation, DOCX tables/XML/deleted revisions, scan/inspection limits/failure and zero recognition. Deleted text is evidence, not accepted live content; duplicate fallbacks are excluded. This is a conservative detected-candidate inventory, not an exhaustive manuscript denominator or chemistry-validity claim.

## CLI summary and exits

The final summary fields are:

`input_path`, `compounds_detected`, `critical_issues`, `warning_issues`, `informational_notes`, `assessment_status`, `assessed_descriptions`, `unassessed_records`, `report_html`, `report_pdf`, `results_json`, `error_stats_xlsx`, `source_annotated_pdf`, `output_dir`, `export_status`, `elapsed_seconds`.

`report_pdf` means findings PDF; `source_annotated_pdf` means annotated input. Unavailable paths print `not generated`. Repeated `artifact_warning` lines describe optional failures; `annotation_coverage` lines describe source associations. Logs/summary use stdout, progress stderr. No former `report_docx` field remains.

| Exit | Meaning |
|---|---|
| `0` | Complete assessment/export with no warning/critical findings; info alone permits success |
| `1` | System/configuration/input or required-export failure |
| `2` | Warning/critical findings with complete assessment/export |
| `3` | Incomplete assessment or partial export, regardless of finding counts |
| `130` | Cancellation; takes precedence over other statuses |

Cancellation before export may leave no summary artifacts; cancellation during export retains completed paths and suppresses opening. Ctrl+C in other commands also ends as interrupt status 130 without a traceback. [Usage](usage.md#cli-run-and-cancellation) owns signal/cleanup behavior.

## JSON v2

`results.json` has `schema_version: 2`. Top-level `source` retains filename/path/SHA-256; `analysis` retains compounds, diagnostics/statistics, assessment and DP5q summary; `configuration` is effective settings; `finding_severities` captures run severities. `source_associations`, `annotation_ambiguities` and `annotation_warnings` occur once at top level. `export` contains paths, warnings/status and coverage metadata. `provenance` includes renderer, PyMuPDF, font and package version under **`checksi`**, replacing v1 `data_checker`. Missing installed metadata is `null`.

`export.status` begins `incomplete`, becomes `failed` on required rendering failure, and finishes `complete`, `partial` or `cancelled`. An interrupted `incomplete` document is unfinished. Nonfinite analytical values serialize as `null`; `nonfinite_numbers` records exact JSON paths and `NaN`/`Infinity`/`-Infinity`, leaving in-memory values unchanged.

Per-compound evidence includes:

- `formula_sources`: EA declaration, original HRMS ion, recovered parent and accepted name structure; candidates/refusals, parse status/usability, formula/mass and isotope-preserving composition. `hrms_neutral` may retain state `ion` and nonzero charge for a permanent cation. Raw formulas/EA percentages remain available. Accepted isotope-labelled EA retains `formula_sources.ea.percentage_conventions`: `interpretation: pending`, `molar_mass_basis: actual fixed-isotope inventory`, `molar_mass`, `mass_based` and `atom_count_based` vectors.
- `formula_comparisons`: pair status `agree`, `disagree`, `not_comparable` or `unavailable`, with reason. `formula_source_mismatch` compares EA–HRMS parent; `name_formula_mismatch` compares accepted names. Neither identifies the wrong source. `formula_normalization.normalized_description` owns token offsets; `original_text` retains source text. Changed candidates and literal `BArF` retain original evidence. Normalization does not establish neutral state.
- `name_resolution`: status/reason, original/accepted name, identifier and ordered transformations; statuses include `resolved`, `transformed`, `unresolved`, `budget_exhausted`, `refused`, `disabled`, `failed`.
- `carbon_observations`: shift, raw token and offsets within the extracted carbon section, not PDF coordinates. Compatibility key `represented_atoms` stores overlapping-signal count; absent/rejected annotations are `null`.
- `carbon_scope`: full inventory, original observations, represented signals, accepted direct-C–B absence, preparation/partition completeness and reasons. `expected_counts` gives formal carbon configuration counts; `configuration_dependent` concerns carbon grouping, `isomer_scope_dependent` includes C/F/Si/B/P.
- `stereo_assessment`: original structural status, typed descriptor roles, enumeration completeness and configuration status. Preparation `selected_isomer_smiles` is separate from the actual scored geometry's isomer.
- `dp5`: outcome/reason and, when assessed, `score_raw`, `score_percent`, `log_score`; `probability_raw`/`probability_percent` are equal-value fractional/percent aliases. Original-label `atoms` rows retain zero-based `observation_index`, experimental/predicted shifts and `evidence_weight`. Shared observations are not independent measurements. Diagnostics include full/scored counts, original observation count, sharing, `score_scope`, `scope_note`. Rows retain percent `score_percent`/`atom_probability_percent` and fractional `atom_probability_raw`. Provenance records isomer, coordinate verification/reason and stereo-verification limits. `dp5.provenance.component_scope` retains `original_smiles`, `scored_fragment_smiles`, `original_atom_indices`, `ignored_components` for component-scoped scoring. Legacy probability names do not imply calibrated probabilities.

`analysis.dp5_summary` retains `min_score_percent`, `max_score_percent`, `mean_score_percent` and matching `*_probability_percent` transitional aliases. Boron skips have no score/assignment placeholders. Requested count-refused outcomes retain plain `message` with `message_severity: "info"`, also shown in reports/Carbon scope; this note is uncounted. [DP5q protocol](dp5q-architecture.md#structural-inventory-and-observation-contract) owns request capacities and worker validation.

## Source annotations

Annotations require a uniquely located complete source description, using extraction block order and configured PDF margins across pages/columns. The period that analysis adds after a found mass (`found 271.05.`) is optional in the source only when the exact description is not found. Repeated identical, overlapping or uncertain owners remain unassigned; counts alone cannot prove ownership. One note carries only its compound's findings. Confirmed highlights record page, rectangle, phrase, color and compound ID once in JSON. Conversion/fonts can reduce coverage without changing analysis, exits or export completeness.

HTML/PDF retain every assessed description/finding regardless of source matching. Findings PDF verifies ordered visible text, findings, tables, repeated occurrences, headings and images against HTML. Incomplete pagination triggers bounded fragments/table-row splitting and a saved-PDF recheck. If required content cannot be preserved, PDF is unavailable and export partial. Chemical tokens remain intact; PDF-only break opportunities improve layout. Formula evidence tables accompany visible formula findings/unchecked-formula notes; hiding the sole finding hides the table, while JSON retains evidence.

## XLSX

`error_stat` remains first, preserving the original 72 diagnostic rows, appended codes, counts and nonzero highlighting. Aliases add no rows. **Carbon scope** has one row per compound: counts, partition/configuration/meso reasons, sharing, DP5 outcome/scope and preparation/scored isomers. **DP5 assignments** has one row per assessed full environment in source order: labels, experimental/predicted shifts, corrected error, compatibility and evidence weight.

JSON/XLSX observation indices are zero-based; HTML/PDF peak numbers are one-based. Corrected error is the regression-corrected DP4/DP5 error, not predicted minus assigned shift. Below-threshold assignments appear in essential mode; detailed mode also shows successful higher scores. Shared-observation tables remain visible in essential mode above threshold. Tables sort by assigned shift descending, unassigned atoms last; displayed precision preserves threshold inequalities. All assessed workbook rows remain regardless of threshold/output mode; unassessed compounds have no assignment rows. Direct `export_error_stats` stays single-sheet unless keyword-only `compounds` is supplied.

## Python API references

Public imports now use `checksi.*`; see [namespace migration](development.md#python-namespace-and-api-migration). Earlier deliberate report/API replacements remain: lazy `config.user_config_path()` replaces `DEFAULT_CONFIG_PATH`; run-owned destinations replace `get_unique_filename`/`REPORT_FILENAME`; `convert_docx_to_pdf` requires keyword-only `work_dir`; `write_dp5q_artifacts(report_parent, index, request, result)` writes in the unique run folder.

`source_annotations.annotate_source(source, destination, compounds, summary)` replaces the old phrase-map highlighting pipeline. `export_reports` returns `html_path`, `pdf_path`, `analysis_path`, `source_pdf_path`, `error_stats_path` plus status/coverage. No DOCX findings/legacy annotation pipeline remains. Existing helper strings/tuples/dictionaries and tracking adapters remain supported. Direct dictionary export derives severities from supplied config; shared runs use captured severities. Tracking imports avoid workbook/desktop-opening dependencies.
