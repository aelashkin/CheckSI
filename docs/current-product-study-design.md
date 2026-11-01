# Current-product scientific study design — proposed, not executed

The checker is advisory. Software regression tests, historical SDF-order results and agreement with a reference implementation do not establish current-product scientific validity. This design authorizes no campaign and makes no accuracy claim.

## Questions and product freeze

Measure whether the actual document → extraction → recognition → resolution → structure/NMR/EA/HRMS → optional geometry/scoring → report path identifies independently adjudicated inconsistencies, and which cases it cannot assess. Freeze exact code/lock/configuration, supported grammar, normalization rules, resolver provenance and DP5 assets before execution. A changed relevant ancestor invalidates affected evidence. Freeze thresholds before evaluating the held-out cohort; do not tune acceptance on its results.

## Cases and independent labels

An identified chemistry expert must own source-level labels, expected identity/composition, atom/environment assignments, supported/refused status and error categories. A second adjudicator resolves disagreements; unresolved cases remain a reported stratum. Review original PDF/DOCX text, not merely extracted strings. Track rights and permissible use separately with the provenance owner.

Include prospective independent manuscripts and controlled expert-authored perturbations. Stratify PDF/body-DOCX/table-DOCX, accepted and unsupported header forms, zero/mixed recognition, symmetry with/without carbon annotations, coincident shifts, EA/HRMS/name agreement and copied-block disagreement, supported/unsupported isotopes, salts/solvates/charge, name transformations and missing data. Report natural and synthetic cohorts separately. Include both correct identities and independently justified alternative identities; compatibility scores alone cannot define correctness.

Prespecify grouped development/holdout allocation, admission/exclusion rules and primary endpoints. Keep each source document, its perturbations, related spectra and intended candidate families in one split. Adjudicate and lock labels blind to product outcomes; record unavoidable exposure. Audit source, identity, stereochemistry and model/development overlap, with explicit known/unknown exposure strata. A held-out-result-informed change consumes that cohort for development; subsequent results are exploratory or need a fresh untouched cohort. Choose sample size and uncertainty methods accounting for document/family clustering. Historical examples are regression baselines, not untouched holdouts.

## Execution and denominators

Use the real current-product document/resolver/geometry/scoring paths. A deterministic fake resolver is appropriate for engineering regressions but cannot establish live identity-resolution performance. Any network, cost, privacy and compute campaign needs its own authority. Log exact original/accepted names, transformation provenance, source locations, complete formulas, scientific sources, comparisons/refusals, settings and artifact status.

Link document, source-description, expected-check and intended candidate/spectrum-family IDs. Freeze these endpoint definitions and partial-description/document aggregation rules with the expert before execution:

| Endpoint | Unit and full denominator | Assessed-only result |
| --- | --- | --- |
| Extraction/recognition | Every expert-annotated source description | Fraction correctly extracted/recognized; omissions remain in full denominator |
| Ordinary inconsistency detection | Every prespecified expert-labelled expected check, by modality and inconsistency category | Detection/false-alarm table among assessed checks, plus full-cohort missed/refused/failed counts |
| Optional DP5 compatibility discrimination | Every intended candidate–spectrum request in its prespecified family and identity class, including those that never reach the worker | Compatible/flagged rates among scored requests, plus full-cohort compatible/flagged/unassessed counts for each identity class |

Deliberately disabled scoring is a separate arm, not a refusal. A missed/refused check is neither a true negative nor a correct rejection. DP5 outcomes describe incorrect-compatible or correct-flagged candidates under the frozen threshold; they do not certify identity. Do not count several candidate requests as independent descriptions.

Report detected, assessed, unsupported, unrecognized, cancelled and failed counts; record scoring eligibility, attempts and completion separately from export completeness.

For each endpoint, report confusion/compatibility tables and uncertainty intervals conditional on assessment alongside the full-cohort outcomes above. Report false acceptance and false rejection by stage/stratum. Keep missing reference truth distinct from product refusal. Evaluate cancellation, operational failure and report fidelity separately from chemistry accuracy.

## Expert decisions required before execution

- Appoint adjudicators and document their qualifications, independence and disagreement process.
- Choose the intended users, input domain, study claims, sample source/rights, cohort size and acceptable error/coverage thresholds with statistical justification.
- Approve atom/environment/coincidence mapping and DP5 eligibility, complete formula grammar, neutral/adduct normalization and treatment of salts, solvates, isotope labels and charge.
- Validate source-carbon-to-product-SMILES correspondence for fixed labels/equivalence groups against the exact resolved structure and solution/stereochemical state. Never assume source-SDF atom ordinals are product indices. Retain unresolved maps as mapping uncertainty/refusal. Prespecify a bounded atom-order-sensitivity assessment and its claim rule; keep historical/source-SDF comparator arms separate.
- Carry forward the unresolved expert checklist in `docs/chemistry-verification.md`: malformed EA rejection/diagnostics, unspecified versus explicit boron mass arithmetic, CHN/oxygen, ppm and typo conventions. Maintainer implementation acceptance is not scientific adjudication.
- Decide whether transformed-name results are scientifically assessable and how unresolved identity affects comparisons.
- Define expected findings and severity without treating current software output as truth.
- Approve a frozen product revision, evaluation budget, independent analysis and publication wording. A failed or inconclusive gate remains a valid study result.

Deliver a versioned protocol, adjudicated case manifest, execution receipts and an independent results interpretation. State every unsupported/unassessed category and remaining limitation. No scientific gate is established by this document.
