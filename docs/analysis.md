# Analysis boundaries

CheckSI checks consistency within supported descriptions, not chemical identity. [Reports](reports.md) owns coverage/results; [scientific decisions](chemistry-verification.md) retains adjudications and pending conventions.

## Recognition and formula

Admission requires a supported proton header, e.g. `1H NMR (400 MHz, CDCl3): δ`, also with decimal MHz. Compact `400MHz` and standalone carbon/HRMS/EA remain unsupported. Symbol-font PDF Greek is read. Complementary EA/HRMS continuations cannot cross another name/section. DOCX tables/XML/revisions and additional sections produce detected omissions; PDF requires extractable text, without OCR.

Names use conservative boundaries/identifiers. Mixed context, unproven wraps and work refusals stay explicit. Local deep analysis tries eight unique candidates within 30 seconds, originals/mechanical edits before speculative hyphen removal; successful transformations need review. Initialization failure has no web fallback. [Acceptance](#acceptance-criteria-and-notation) below defines implemented thresholds, not independently validated publication standards.

## Acceptance criteria and notation

EA requires `XX.XX` percentages. CHN calculated–found differences are rounded to two decimals and flagged above **0.40 percentage points**; formula–calculated differences allow **0.01** after rounding. Missing/duplicate/out-of-range/nonfinite values are excluded. Finite misprecision values can still enter formula comparisons. Isotope percentage conventions remain [pending](chemistry-verification.md#adjudicated-policy-and-pending-conventions).

HRMS requires positive four-decimal masses. Calculated/found fields are independent; exact-decimal error strictly **>10 ppm** is flagged. A formula-mass discrepancy surviving four-decimal equality is “possible typo” at **≤0.0002 Da**; larger differences get mismatch/delta explanations. Zero/negative/malformed values remain missing; later unrelated numbers cannot repair them. Found accepts :/=/comma/spaced-dash separators, m/z/ion labels and parentheses; observed/measured/obsd/exp./experimental never substitute for found.

Complete inventories accept omitted counts of one, contiguous `[13C]C5H13O2`/internal isotopes and `[C3H10O]2+`. `[13C] C5H13O2` and condensed `CH3CH2OH` remain unsupported whole tokens, never partial formulas. Unknown elements, zero counts, fractional HRMS counts and unclosed internal brackets are invalid; ambiguous unsupported EA notation remains unassessed, not automatically a typo.

Headers include `HREIMS`, `HR-EI-MS`, `EI-HRMS`, `Q-TOF MS`. Adducts include H/Li/Na/K/NH4/Cs/Ag addition, H loss, Cl/formate/acetate addition, `[M+H−H2O]+`, `[M+2H]2+`, `[M+H+Na]2+`, molecular/radical ions and listed counter-ion losses. Adduct `2H` is a coefficient; use D/`[2H]` for deuterium. Inline 13C/15N/18O/halogen labels denote isotopes. Formula charge controls m/z `(atom mass − charge × electron mass)/|charge|`; otherwise written/conventional adduct charge supplies it. Missing written charge is diagnosed independently.

| Comparison catalogue | Supported components |
|---|---|
| Neutral acids/solvents | HCl, HBr, HI, TFA, water, MeOH, EtOH, MeCN, CH2Cl2, CHCl3, EtOAc, DMSO, Et2O, acetone, THF, toluene |
| Counter-ions | Cl, Br, I, BF4, PF6, OTf, NTf2, ClO4, SbF6, BArF, OTs, OAc |

EA dot/folded solvates (`C21H28N2·2HCl`/`C10H13O2.5`) retain full percentage inventories. Whole-unit explanations are informational; folded inference establishes at most two component kinds. Permanent cations require charge-balanced listed anions. Balanced mixed counter-ions, ambiguity/exhausted proof and unhandled states remain pending; atom accounting does not prove physical identity. Natural-peak comparison normalizes most-abundant or ≥10%-abundance labels; D/2H, 13C, 15N, 17O, 18O remain distinct. Name isotopic modification cannot be certified by natural-peak normalization. [Policy source](../src/checksi/analysis/formula_source_policy.py) owns exact catalogue formulas.

## NMR

Checks cover solvent/frequency, residual peaks, shift order/precision/rounding, duplicates, multiplicity/J/integrals and proton totals. Multiplicity precedes assignment tails; misplaced valid fields receive order findings without invented missing fields. Assignment labels cannot supply multiplicities. Required-spectrum conventions remain configurable. Compensating integral errors can go unnoticed.

Supported nuclei: ¹H/¹³C/¹⁹F/¹¹B/³¹P/²⁹Si/²H/¹⁵N; structure counts cover C/F/Si/B/P where supported, not equivalent ²H/¹⁵N coverage. `(nC)` denotes overlapping non-equivalent signals, not duplicated observations/equivalent atoms. [Structural rules](dp5q-architecture.md#structural-inventory-and-observation-contract) retain equivalence, justified sharing and direct-C–B absence separately.

The **10,000 represented-carbon-shift** limit includes validated repetitions and excludes coupling labels. Oversized counts, including malformed/nested annotations, are refused before allocation. Refusal persists with `nmr_signal_format` or DP5q disabled; requested DP5q uses `carbon_signal_limit` without an additional finding.

## Stereochemistry

Structural stereoisomers/typed elements determine meso status, not peak ratios/manual lower counts: confirmed meso/non-meso, meso-possible/no-meso-form for unspecified configuration, achiral/no relevant centres, or unassessed. Atom R/S, relative ring/coupled tetrahedral and bond E/Z roles differ, including supported S/P/quaternary-N centres.

Carbon grouping can vary by configuration. Incomplete source partitions can receive warned, scoped embedded-isomer scores; coordinate/stereo verification is postselection evidence, not an admission gate. Axial/atropisomer, three-coordinate N/P, non-tetrahedral and cumulene stereo retain limits. [Adjudicated partitions](chemistry-verification.md#structural-partitions-adjudicated--2026-10-2627) remain source-specific, not universal validation.

## DP5q eligibility

DP5q needs a structure, compatible original carbon observations/capacities and computable geometry/predictions. Enable deep name analysis independently. Missing/malformed/unconsumed sections and unresolved coverage remain unscored; unsupported elements/graphs/MMFF94s parameters can prevent prediction. Invalid numerical fits/calculation failures publish no score. Ties retain the first optimum; incomplete stereo alone does not refuse scoring.

Every boron structure skips before readiness/geometry, with `boron_not_supported`/`dp5_boron_not_supported` and no assignments/score. Carbon diagnostics independently permit justified absent direct-C–B sites.

For disconnected non-boron sources, exactly one carbon-bearing component is scored with original charge/protonation/observations; the warning explains ignored counter-ion/solvate and possible salt shifts. Multiple carbon-bearing components remain unscored; no peak allocation occurs. [Architecture](dp5q-architecture.md) owns deterministic ETKDGv3/MMFF94s, bundled CPU prediction and protocol limits. There is no DFT, runtime model download, external DP5 install or user-facing backend selector. Thresholds change findings/table visibility, not scores.

## Scientific limits

Compatibility is not calibrated correctness: high scores cannot confirm identity; low scores request inspection. Embedded isomers, salt/solvent effects, overlap, exchange and weak/unobserved peaks need expert interpretation. Tests/reference outputs establish engineering contracts, not independent experimental truth, training independence or general accuracy. Refused/unassessed/failed cases are not correct rejections. [Current-product validation](current-product-study-design.md) remains proposed, not executed.
