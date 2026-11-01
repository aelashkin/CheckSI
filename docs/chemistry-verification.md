# Chemistry changes and expert verification

**Current scope:** Pavel’s supplied 2026-10-26/27 decisions establish the
structural partitions, typed meso/descriptor policy, overlapping-observation
semantics and boron skip described below. Historical measurements remain
labelled as software comparisons; neither those outputs nor the full legacy
63-case catalogue are scientific gold. This record does not authorize a merge
or claim experimental validation, model-domain expansion or score calibration.

## Historical implementation acceptance

The maintainer accepted these implementation changes on 2026-10-21.

1. At that revision, a malformed EA record such as `.1H26N2: C, 81.77` receives one precise `ea_number_format_invalid` message instead of separate misleading non-numeric and precision messages. The record remains invalid and excluded from comparisons; later valid entries remain available. This is diagnostic consolidation, not automatic correction of the source formula.
2. The mass-discrepancy explanation table uses most-abundant **11B (11.00930536 Da)** instead of the first isotope-table entry **10B (10.01293695 Da)** for an unspecified B contribution. This matches the main formula-mass convention. Explicit isotope formulas retain their specified isotope. [NIST boron data](https://physics.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=B) supplies independent reference values.

## Carbon observations and structural status — 2026-10-26/27

An accepted `(nC)` annotation contributes n overlapping non-equivalent signals
to the carbon count and region totals; equivalent carbons remain unannotated.
Pavel confirmed the eight real-article examples **4e, 4j, 4n, 4o, 5i, 12h, 12n
and 12p** as genuine overlaps. In 4e, thirteen listed peaks including
`131.0 (2C)` represent fourteen environments. The claim requires spectral verification.

Every listed peak remains one original observation. Full chemical environments
are not merged to match a lower peak count. Where sharing is justified, distinct
environments retain separate means, quantile fits and result rows referring to
the same observation. Each of k contributors has weight 1/k, preserving one observation's evidence. An explicit `(nC)` fixes capacity
in formal environments, not raw atoms; unannotated sharing requires an accepted
structural coalition. A manual lower count alone cannot supply that coalition.
No environment is omitted from an assessed result.

The earlier equivalent-carbon fraction and “likely/unlikely meso” hint do not
control current meso decisions. `stereo_assessment` is structural and separates
`confirmed_meso`, `confirmed_non_meso`, `meso_possible`, `no_meso_form`,
`achiral_no_centres` and `unassessed`. It retains enumeration completeness,
reason, configuration status and typed atom/bond inventory. Supported S/P and
quaternary-N atom centres, relative ring/coupled tetrahedral roles and E/Z bonds
have distinct descriptor roles. An unspecified possible meso completion is not
a claim that the sample is meso; no-centre achirality is not meso. Changing an
overlap annotation or a finding severity never changes this structural status.

## Boron policy — 2026-10-27

Pavel’s instruction is to **skip DP5q for every boron-containing
compound**, with outcome `not_applicable`, reason `boron_not_supported`, and:
“DP5q not calculated: boron-containing compounds are not supported (no validated
geometry/prediction for boron)”. The application exposes this as a skipped
compound and `dp5_boron_not_supported` (warning by default), with no score or
assignment rows. Applicability precedes model readiness and geometry/inference;
a boron-only run can explain its skips without model assets or a child worker.
Disabling the warning does not change the skip. No UFF fallback, xTB path,
boron partial score or public omission API is supported.

The independent diagnostic retains the full expected carbon inventory. An
accepted absent directly B-bound environment gives
`c13_boron_signal_not_observed` (INFO by default), with no low-count finding for
that deficit. The allowance uses actual direct sites and full environment
identity, without remote-carbon, duplicate-bond or symmetry inflation. An
observed C–B signal remains counted. **1.147 has 18 full environments and 17
original observations**; the accepted absent site is C6/original atom 5 directly
bonded to B at atom 4. Ring CH2 environments and the separate 34.26/34.00 and
26.65/26.21 observations remain distinct. Formula, NMR and stereo findings remain independent. Structural correspondence evidence does not permit
a boron score. Future support requires separate training-domain, suitable
geometry and known-spectrum validation; none is claimed here.

## Verification reference

Compare base `d843d0d003789e2f16c35b100d10c4be7c49aa86` with chemistry source `ac6abfe8050eb2a733f9754de8fa73ee87534a41` (identical chemistry files at reviewed revision `05607ff7053ed8dd7dd04c88a810d19a90e51b3f`). The complete per-compound line comparison is retained in PR #13's repair response and description; raw CLI evidence SHA-256 is `1ee70b413b4e35323ae4cf1683ad5c6a27211913b5bdd2acbf7964e65d03b10b`.

At that historical boundary, all nine shipped inputs retained compound counts, CLI exit codes and PDF annotation counts/colors. Eighteen copied-input CLI runs produced DOCX/PDF/XLSX. The following counter changes were recorded; boron suggestion text also changes in affected compounds. Detailed mode additionally shows an independently valid found mass when calculated is absent.

| Input | Compounds | Critical findings base → accepted | EA format count base → accepted |
|---|---:|---:|---:|
| test_all_errors.docx | 87 | 200 → 199 | 8 → 7 |
| test_all_errors.pdf | 87 | 201 → 200 | 8 → 7 |
| test_pdf_online.pdf | 96 | 199 → 198 | 8 → 7 |
| test_pdf_offline.pdf | 96 | 199 → 198 | 8 → 7 |

The malformed record occurs in compound 27 of the all-errors inputs and compound 25 of the online/offline inputs. Other sample counters are unchanged. These values characterize the accepted implementation; they are not expert-approved chemistry expectations.

### Input fingerprints

| Input | SHA-256 |
|---|---|
| test_all_errors.docx | `efc62712bc3421b430ed4f126792be23cffc9a40dc665c397662e67972eed5ce` |
| test_all_errors.pdf | `089479bf05832763ff2b7e855f70b895b24cb33c86ccaf2956e621232ad6cc17` |
| test_pdf_online.pdf | `6bf021765b11cfb7e9536d8a1ee4759cc4d0bf4adae9a494cdff3a5d7c170d27` |
| test_pdf_offline.pdf | `0d2a3cac5da61b4347a341c19b77a41a09d1ca8325728a604a1fda6f332e710d` |

## Historical repaired-upstream software comparison

The historical comparison parent was `322ff1a3397f69204636c8add3fb5b92e1574a18`. Compare it with
the measured scientific layer using the same nine inputs, packaged settings, deep name analysis off
and DP5 disabled. These are direct extraction/analysis runs: no live resolver,
conversion, scoring or report campaign was performed. Usable names below mean
context-accepted names, not resolved structures; their counts are identical
between parent and layer.

| Input | Compounds | Critical: parent → layer | Informational: parent → layer | Usable names in each run | Changed warning lists |
|---|---:|---:|---:|---:|---:|
| `test_all_errors.docx` | 87 | 216 → 210 | 62 → 62 | 69 | 8 |
| `test_all_errors.pdf` | 87 | 229 → 218 | 47 → 47 | 50 | 9 |
| `test_correct_description.docx` | 1 | 0 → 0 | 1 → 1 | 1 | 0 |
| `test_correct_description.pdf` | 1 | 0 → 0 | 1 → 1 | 1 | 0 |
| `test_dp5_integration.docx` | 2 | 0 → 0 | 2 → 2 | 2 | 0 |
| `test_parsing_header.pdf` | 25 | 80 → 80 | 1 → 1 | 20 | 0 |
| `test_pdf_offline.pdf` | 96 | 198 → 192 | 92 → 92 | 96 | 6 |
| `test_pdf_online.pdf` | 96 | 199 → 193 | 91 → 91 | 95 | 6 |
| `test_real_article.pdf` | 46 | 142 → 142 | 0 → 0 | 45 | 14 |

These were the only nonzero per-code differences at that boundary; the tables
are historical measurements and do not describe the current implementation.

| Finding | all-errors DOCX | all-errors PDF | offline PDF | online PDF |
|---|---:|---:|---:|---:|
| `c13_count_exceeds_formula` | 2 → 1 | 2 → 1 | 2 → 1 | 2 → 1 |
| `calcd_formula_mass_delta` | — | 6 → 5 | — | — |
| `calcd_formula_mass_mismatch` | — | 6 → 5 | — | — |
| `ea_calcd_found_element_mismatch` | 10 → 9 | 10 → 9 | 10 → 9 | 10 → 9 |
| `ea_formula_calcd_mismatch` | 13 → 11 | 13 → 11 | 13 → 11 | 13 → 11 |
| `ea_formula_element_missing_in_calcd` | 5 → 4 | 5 → 4 | 5 → 4 | 5 → 4 |
| `ea_number_format_invalid` | 7 → 6 | 7 → 6 | 7 → 6 | 7 → 6 |
| `halogen_isotope_unspecified` | — | 2 → 1 | — | — |
| `hrms_formula_charge_missing` | — | 2 → 1 | — | — |
| `isotope_format_invalid` | — | 1 → 0 | — | — |

The parent checked the EA source `C2.1H26N2` as `C2`. The measured layer refused the
complete token. The current policy, adjudicated by Pavel on 2026-10-26, classifies
`C2.1H26N2` as a definite author typo (`formula_invalid_format`); it never
percentage-checks a partial `C2` formula. The PDF-only HRMS token `C19H23Cl35 2N2` is also not
checked: its mass, charge and isotope checks are removed and a coverage record is
added. Unassessed records change 15 → 16 (all-errors DOCX), 15 → 17 (all-errors
PDF), 56 → 57 (offline PDF) and 119 → 120 (online PDF). Experts must assess these
source strings against the original documents rather than infer a corrected
formula.

Warning-list changes with unchanged counters, including 14 compounds in
`test_real_article.pdf`, included observation counts and annotation explanations.
Pavel subsequently adjudicated the overlapping-signal count as described below;
these historical values are not the accepted count policy. In the offline PDF, compound 39
now shows a `stereo_descriptor_invalid` message; the run-wide counter is 3 on
both sides.

These measurements record software behavior for adjudication. Neither the
upstream name handling nor changed carbon/formula outputs are automatically
accepted as scientific truth, and sample expectations were not changed to pass.

## Structural partitions adjudicated — 2026-10-26/27

The settled named cases replace the former #36 activation gate and pending
adjudication. The following full partitions use fast permitted conformational
exchange in an achiral medium, with achiral R for the prochiral series and
nonrotating C=C distinctions retained. **C labels here are one-based original
SMILES atom indices**, including gaps for non-carbon atoms; they are not
sequential carbon numbers or wire group indices. Exact SMILES, hand-bound
chemical labels and reference frames are in the
[structural oracle](../tests/fixtures/chemistry/structure_oracle.json).
Tartrate/prochiral methyl identities use those bound frames; atom permutations
must transport the full sets explicitly.

| Case | Full count | Manual-only lower count | Full environments (semicolon-separated) |
|---|---:|---:|---|
| Ribitol | 3 | — | C2+C9; C3+C7; C5 |
| myo-Inositol | 4 | — | C3+C7; C2+C9; C5; C11 |
| Diisopropyl L-tartrate | 5 | 4 | C5+C11; C7+C9; C2+C14; C1+C16; C3+C15 |
| S-Isopropyl methyl sulfoxide | 4 | 3 | C1; C4; C5; C6 |
| L-Valine | 5 | 4 | C1; C2; C3; C4; C6 |
| 1.142 | 17 | 13 | C1; C2; C3; C4; C5; C6; C7; C8; C9+C14; C10+C13; C11; C12; C15; C16; C17; C18; C19 |
| 1.145 | 13 | 10 | C1; C2; C3+C17; C4+C16; C5; C6; C7; C8; C9; C10+C11; C12; C13+C14; C15 |
| 1.147 | 18 | 16 | C1+C3+C24+C25; C2+C23; C6; C7; C8; C9; C10; C11; C12; C13; C14; C15; C16; C17; C18; C19; C20; C21 |
| (iPr)2CH–OH | 4 | — | C1+C8; C3+C7; C2+C6; C4 |
| (iPr)2CH–OEt | 6 | — | C1+C10; C3+C9; C2+C8; C4; C6; C7 |
| (iPr)2CH–OAc | 6 | — | C1+C11; C3+C10; C2+C9; C4; C6; C7 |
| (iPr)2CH–O-allyl | 7 | — | C1+C11; C3+C10; C2+C9; C4; C6; C7; C8 |
| Alpha-pinene | 10 | 9 | C1; C2; C3; C4; C5; C6; C7; C8; C9; C10 |

Ribitol and myo-inositol are confirmed meso; the configured tartrate, sulfoxide
and valine controls are confirmed non-meso. The prochiral OR cases and the
repeated-substituent sources 1.145/1.147 have no permanent atom R/S centre from
those repeated groups. For 1.142 the source configuration remains unspecified;
all four accepted physical forms have full count 17, and the structural status
must retain that scope. Its manual 13 does not create a sharing coalition.
Alpha-pinene admits the topology-constrained physical enantiomer pair, with
`no_meso_form` and unspecified sample configuration; its manual 9 is not a
merged prediction partition. The settled OR counts are 4/6/6/7, independent of
whether a remote C=O or C=C happens to be present.

The named-control structural counts do not invent measured spectra. The literal
catalogue sources 1.142/1.145 each supply only one original carbon observation;
manual lower counts cannot complete their coverage. Valine 5/4, tartrate 5/4
and sulfoxide 4/3 shared-observation controls test accepted comparison scenarios;
synthetic shifts remain engineering fixtures, not supplied measured data.

At the adjudication boundary, prediction geometries had to reproduce exact original-atom groups on existing coordinates; matching counts were insufficient. This implementation admission gate is historical and superseded. Current scoring retains the original partition, records mismatch as postselection evidence (`coordinate_partition_verified=false`) and can continue scoring; see [analysis](analysis.md#stereochemistry) and [architecture](dp5q-architecture.md#calculation-and-applicability). Genuine isotope-created ring-CH2 E/Z beside exocyclic C=C in 1.142/1.145/1.147 uses actual 3D geometry; conditional bookkeeping candidates do not become permanent source descriptor requirements. No arbitrary relabelling/new embedding repairs failed correspondence. Incomplete, unsupported, inadmissible or exhausted work remains explicit; legacy/manual alternatives cannot certify completeness. These accepted cases do not blanket-approve the older 63-molecule catalogue.

Verification is anchored by the hand-bound oracle and its original-index,
renumbering, physical-isomer and coordinate fixtures in
[`test_structure_expert_oracle.py`](../tests/test_structure_expert_oracle.py),
[`test_structure_conditional_stereo.py`](../tests/test_structure_conditional_stereo.py),
[`test_structural_meso.py`](../tests/test_structural_meso.py) and
[`test_stereo_structural_descriptors.py`](../tests/test_stereo_structural_descriptors.py).
Shared observation, worker/host and boron diagnostic controls are maintained in
[`test_observation_policy.py`](../tests/test_observation_policy.py) and
[`test_boron_policy.py`](../tests/test_boron_policy.py), with the complete
[DP5q contract](dp5q-architecture.md#structural-inventory-and-observation-contract)
described separately. These establish implementation invariants; they do not
establish experimental calibration or universal model coverage.

## Source-specific diagnostic decisions — 2026-10-27

Multiplicity, descriptor ordering and integral ownership remain independent.
For 1.62, `(J = 8.0 Hz, 4H)` is missing multiplicity only and has no order finding;
other malformed brackets keep their own findings. For 1.64, a misplaced valid
`d` produces an order finding without false missing/wrong multiplicity.
Outside-bracket 4H/2H/1H tokens in 1.75 are malformed integral evidence, not ppm
shifts, and do not create precision/order/duplicate-shift cascades. The truncated HRMS source is 1.57 before the independent 1.58 heading; its formula
tail and genuine precision findings remain its own. It is not relabelled 1.75
or completed with a neighbouring compound's name/data.

For 1.119, `[M+H]` with unsigned `C21H29N2` establishes arithmetic charge +1,
neutral parent `C21H28N2` and calculated m/z 309.2325. It receives both
`hrms_formula_charge_missing` and `hrms_ion_charge_missing` because the written
sign is independently absent from each location. Each code can be disabled
without changing arithmetic or the other finding. Neither finding claims
arithmetic charge is unknown or triggers an electron-mass/adduct cascade. Other findings/severities remain independent. Source-specific controls live in
[`test_chemistry_diagnostic_acceptance.py`](../tests/test_chemistry_diagnostic_acceptance.py),
[`test_chemistry_charge_acceptance.py`](../tests/test_chemistry_charge_acceptance.py)
and [`test_chemistry_source_ownership.py`](../tests/test_chemistry_source_ownership.py).

## Future expert checklist

- Inspect the original garbled formula/EA text and confirm rejection and one-record diagnostic consolidation are appropriate; do not infer a corrected formula from OCR residue.
- Confirm the unspecified-boron isotope convention and the changed per-compound explanation combinations using independent mass arithmetic. Distinguish unspecified B from explicitly labelled isotope inputs.
- Review the preserved CHN/oxygen, ppm and typo conventions with representative valid and invalid examples. The existing software tests establish implementation behavior, not universal scientific correctness.
- Record the expert, date, evidence, conclusion and any remaining uncertainty in a follow-up to PR #13 or this note. If expectations need correction, add an independently justified regression and change the owning chemistry policy explicitly; do not silently overwrite sample baselines.

## Adjudicated policy and pending conventions

Pavel adjudicated the wave-three carbon and formula policies on 2026-10-26.
A `(2C)` carbon peak represents two non-equivalent overlapping signals for the
count diagnostic. The subsequent 2026-10-27 structural policy above replaces
meso-fraction decisions. The eight article overlap examples were confirmed as
genuine overlaps. These decisions do not approve every historical sample
expectation.

EA–parent differences of whole listed acid, water or solvent units are
informational. Explicit dot formulas use the parent for comparison and the full
formula for percentages. Folded partial solvates are accepted when an integer
parent plus fractional listed solvent explains them; percentages still use all
fractional atoms. `C10H13O2.5` has several possible decompositions from formula
alone: a reference may establish the matching parent, and the checker does not
invent one. `C2.1H26N2` stays critical: an exact invariant excludes an integer
parent plus fractional listed solvents. Unproved multi-solvent fractional
inventories remain unsupported/informational, rather than being called typos
because a narrower solver did not find them. Whole-inventory relationships
above the accepted two-kind folded bound, and exhausted proof budgets, remain
pending; a listed component's atom subset alone never establishes agreement.
Permanent cations compare with charge-balanced listed counter-ions and are
labelled as ions. Uncovered component/state cases remain informational and pending.

Natural instrument-peak labels follow the most-abundant-isotope or at-least-10%
abundance rule; common enrichment labels remain distinct. The locked mass table
supplies abundances, with 198Hg eligibility updated to the
[CIAAW 2024 representative abundance](https://www.ciaaw.org/isotopic-abundances.htm)
of 10.04%. Masses are unchanged. Isotope labels in accepted names denote isotopic
modification and remain not comparable to a merely natural instrument peak.
Formula charge controls m/z and conventional adducts can supply arithmetic
charge. Missing written notation remains separately reportable at each location
as in 1.119 above, with two notation findings and no false mass cascade. H-only protonation
differences remain pending
only when the hydrogen difference equals the charge difference and all other
atoms agree. Balanced mixed counter-ions and unadjudicated rare-isotope adduct
bases remain pending; definite inventory or charge contradictions stay errors.

For EA, Pavel agreed that D and labelled carbon are counted under H and C and
the molar mass uses actual isotope masses. The checker accepts either complete
calculated-percentage vector: actual-isotope mass numerators, or ordinary
element atomic weight times atom count numerators with the same actual molar
mass denominator. A match to either is accepted; no match gives the pending
`ea_isotope_calcd_mismatch` warning in packaged settings. Element identity,
precision and calculated–found tolerance remain independent checks. This does
not settle a universal publication convention.

Contiguous leading/bracketed isotope formulas are identified in EA/HRMS; uncovered cases remain pending. [Analysis acceptance criteria](analysis.md#acceptance-criteria-and-notation) owns current grammar/limits; [configuration](configuration.md) owns settings. Never infer corrections to garbled source strings.
