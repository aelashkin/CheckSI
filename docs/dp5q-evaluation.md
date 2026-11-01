# DP5q regression and geometry evaluation

## Document-derived acceptance regression

Opt-in [harness](../scripts/check_dp5q_acceptance.py): 32 actual DOCX-to-OPSIN SMILES requests through serial product geometry/prediction/scoring and parent validation. [Fixture](../tests/fixtures/dp5q/acceptance-requests.json) pins `checkSI_dp5q_test_v2.docx` SHA-256 `5c6d8828be4c198ad358a02fdfb1bee44c203fedeefa9df7209b3378c38da242`, extraction revision/command, original names/observations, parser/DOCX-paragraph indices, result/assessment hashes, original atom order/groups/capacities. **3.9 revised/3.10 original differ from historical SDFs.**

Requires locked environment/bundled weights; no Java, OPSIN execution, network, original DOCX or corpus. Requires fresh output directory; retains requests/results/`acceptance.json` and all per-case scores; inference stays outside default discovery. All 32 must assess; 14 revised scores strictly exceed originals; Part B **1.1 > 1.3 > 1.4 > 1.2**, 1.2 below **20%**. Missing/duplicate/failed/skipped/nonfinite results fail. Default synthetic gate tests neither run inference nor establish scientific validity. Comparative scores are not fixed targets/calibrated correctness probabilities or general accuracy evidence.

```sh
acceptance_parent="$(mktemp -d)"
uv run --locked python scripts/check_dp5q_acceptance.py \
  --output "$acceptance_parent/acceptance"
```

## Historical development campaign

Frozen [DP5 manifest](../tests/fixtures/dp5q/manifest.json), all source files matching `8f8e22364654bbce71dcb6abb414eee793b00d92`: **370 candidates**, **48/24 revision structures/families**, **322/42 numbered stereo structures/families**, **67 spectra**, **338 exact structures +32 extra duplicates**. Preserved labels/bytes are development regression evidence, not untouched accuracy data.

[Audit](../tests/fixtures/dp5q/development-audit.json): 94/370 constitutions overlap provided train/valid/test union (78/0/50; nonadditive, not training-membership proof). Exact canonical isomeric SMILES identity removes explicit H, retains specified tetrahedral/double-bond stereo; different/unspecified stereo is nonexact: four matches, two source-correct (S14b/S8b). JB13A/B's eight candidates share proposed correct structure/different spectra; aggregate labels exclude them pending adjudication.

[Paper §5.3](https://pubs.rsc.org/en/content/articlehtml/2026/sc/d5sc06988b): a initial/b revised. [Stereo README](https://github.com/aelashkin/DP5/blob/8f8e22364654bbce71dcb6abb414eee793b00d92/stereo_examples/README.md): candidate 1 correct; AT3/TS3A/TS4/NL1A excluded, missing carbon; unnumbered JB7 excluded. S13 submitted/unscorable: differing peak lists, no missing equivalence inferred. Historical tie-refusals differ from current first-optimum policy.

## Controlled arms

- **F:** exact provided pose after the legacy AddHs/writer/final-parser boundary.
  This separates fixed-geometry TensorFlow/NumPy parity from assessment policy.
- **A:** frozen checker ETKDG (v1), 30 conformers, seed `0xD05`, MMFF94s/500
  iterations, status other than -1 accepted if energy is finite, lowest-energy
  selection and the original first-conformer fallback. Both SDF roundtrips are
  archived. This development baseline preserves the source atom order.
- **B:** converged lowest-energy ETKDGv3/MMFF94s single geometry. Current `converged-single-v2` uses 30 requested conformers, seed `0xD05`, one native
  thread, at most 1,000 embedding attempts and a 10-second native embedding
  timeout per conformer/fragment. The parent request deadline remains the
  total bound. MMFF94s first permits 500 iterations; only unfinished conformers
  continue from their current coordinates for up to 1,500 more. Converged poses
  are unchanged and unfinished poses are never scored. These engineering budgets are not calibrated chemistry limits. A complete
  explicit-H structure must fit the 512-atom graph domain; size alone cannot
  guarantee convergence. Unsupported trained elements prevent prediction.
  Three-coordinate N/P stereo remains outside complete 3D verification; current
  product scores retain that limitation rather than refusing for it.
- **C:** the bounded ensemble from the same generated/optimized candidates as B:
  converged finite stereochemistry-preserving geometries, 10 kJ/mol window,
  fixed-heavy-atom 0.5 Å RMSD pruning without reflection, cap ten, 298.15 K.
  `bounded-ensemble-v3` additionally retains only the perceivable tetrahedral
  and double-bond stereoconfiguration of the lowest-energy member. These
  perceived configurations are separated even when the input leaves a centre
  unspecified. Axial atropisomerism is not identified: distinct hindered axis
  configurations can still be mixed. C is a development-only arm, and this
  limitation blocks a general claim that it separates all stereoisomers. It
  does not establish the correct configuration of an unspecified centre.

B is the first retained lowest-energy member of C's shared candidate set. The
engine averages mean predictions for assignment and mixes per-conformer group
compatibilities with Boltzmann populations. It does not average Gaussian
parameters or choose conformers from observed scores. Geometry audits retain
all candidate statuses, energies, rejection reasons and stage timings.

## Execute explicitly

Product/default tests need no corpus/legacy environment. This campaign supplies source SDF atom order through the development Mol boundary, **not** product SMILES. Seeded ETKDG depends on atom order; these results cannot establish product coverage. A future product campaign needs validated source-carbon-to-SMILES mapping for groups/fixed labels, disclosed refusals and atom-order sensitivity. No current-policy campaign has run.
From an authorized frozen source checkout, use a new output directory per run:

```sh
uv run --locked python scripts/evaluate_dp5q.py run \
  --manifest tests/fixtures/dp5q/manifest.json --source "$frozen_dp5_source" \
  --output "$new_pilot_output"
uv run --locked python scripts/evaluate_dp5q.py run --full \
  --manifest tests/fixtures/dp5q/manifest.json --source "$frozen_dp5_source" \
  --output "$new_full_output"
PYTHONDONTWRITEBYTECODE=1 "$reference_python" \
  scripts/dp5q-reference/campaign.py --source "$frozen_dp5_source" \
  --manifest tests/fixtures/dp5q/manifest.json --owned "$new_full_output" \
  --output "$new_reference_output"
```

Prepare the explicit reference environment using [the reference instructions](dp5q-reference.md).
The eight-case pilot precedes the full run. One numerical child loads parameters once for serial cases. The supervisor reaps a child after
600 seconds without a completed case; an execution failure leaves an incomplete
campaign, never a fabricated chemistry refusal. CPU limits reserve ten core-hours
for owned evaluation and one for reference diagnostics within the approved total
budget. Inputs, result sizes and retained artifacts have explicit bounds.
Code/manifest hashes identify execution.

Reference preflight runs before TensorFlow, the frozen oracle, or POSIX resource
modules are imported. It requires a successful complete owned execution for
exactly the declared eight-case pilot or all 370 manifest cases. Manifest,
run/execution metadata, case-directory mappings, result identities, and every
declared file hash must agree. Assessed F/A arms require their declared heads
and geometry; an unscored arm can be skipped only for an explicit validated
applicability reason. Missing artifacts fail execution, never chemistry. A reference PASS additionally requires every planned
comparison to succeed and at least one actual comparison. It records explicit
skip/comparison counts and an evidence seal, rechecked before publishing PASS.
The final reference receipt also binds every per-case `reference.json` and
compared F/A `*-legacy.npz` by relative path, byte count, and SHA256, with exact
JSON/array/total counts. Its `owned_preflight_file` separately identifies the
sealed preflight JSON. Reporting and follow-up refits must verify these bindings
before consuming the arrays or publishing a new report.
The owned directory remains read-only, including evidence from a completed
older run; validating its final snapshot does not claim that older code used
the new live reservation mechanism.

Owned supervision checks live output usage throughout execution and terminates
and reaps a child that exhausts the reserved budget. Before each case computes,
the child reserves 36 MiB for the nine possible case files (including the
temporary/final result, which is atomically renamed), each still subject to the
4-MiB kernel file limit. It also keeps 16 MiB for bounded control records and
log growth. Admission therefore requires at least 52 MiB of free campaign
budget; this deliberately conservative rule can stop a run whose next actual
case might have been smaller. Polling alone does not prevent transient growth:
the full-case reservation and hard per-file/count bounds provide that margin
for this frozen harness. Successful new case results inventory artifact sizes
and hashes. The original CPU and 600-second per-case wall bounds remain.

`execution.json` receives PASS only after completion, identity, source-hash,
CPU, and final output-budget gates. Budget failure persists `FAILED_BUDGET`;
timeout, interruption, stale code, or incomplete results persist `INCOMPLETE`.
Neither is a scientific refusal or a valid input to reference parity. Pure
preflight and tiny supervisor tests import on Windows without `resource`;
actual resource-limited campaign execution remains an explicit POSIX task.

The reference runner uses actual frozen semantic models, strict assignment and
Gaussian fitting. It compares exact graph integers/stored distances and both
heads at absolute 0.001 ppm. It archives reference-chosen assignments and fits,
even when the owned ambiguity policy refuses an assessment. Those legacy scores
are diagnostic evidence and never replace an owned refusal.

The carbon-only adapter transcript retains four-decimal rounding and dummy
proton input. Across all 67 spectra, its parsed carbon values/labels/constraints
match the frozen parser exactly. One original proton section is malformed for
the strict parser; it is recorded separately. The largest actual carbon rounding
change is 0.0000497856 ppm. Original and emitted inputs remain distinguishable.
N1 compares old epsilon aggregation and stable tails on the **same** reference
fit/assignment; N8 compares original and rounded observations without silently
mixing in an optimizer or geometry change.

## Decision rules and limits

Report assessed-compatible (`score >= threshold`), assessed-flagged and
unassessed separately at 0.10 and 0.20. Keep class-specific submitted and assessed
denominators, correct-compatible yield, failures/refusals, family ranks/ties and
every threshold/coverage reversal. Refusal is never acceptance. A source-family
ranking needs the complete selected family with assessed candidates.

The preregistered C gate requires no increased incorrect-compatible or
correct-flagged counts at 0.10, no reduced assessment coverage in either class,
improved aggregate correct-candidate rank, no lost successful revision family,
at most 2x warm B article time and 1.5x B peak RSS. Harm or an unresolved gate
selects B; no threshold tuning or post-hoc tradeoff is permitted. Cumulative
same-process F/A/B/C high-water RSS does not establish independent B/C RSS.
Separate native runtime evidence must support that gate before it can pass.

## Resource boundary

The graph limit is 512 explicit atoms and 32,768 directed edges, with an additional
100-neighbor constraint and explicit refusals. A bounded 512-atom synthetic graph
with 49,632 edges passes an explicit 50,000-edge probe cap and refuses the shipped
cap. Page-touched fixed-network live-array shapes at 50,000 edges use 425,904,140
bytes and measured 632,037,376-byte peak RSS on the local Intel Mac; the shipped
edge count uses 284,394,956 bytes and 387,915,776-byte measured peak RSS. The
conservative cap preserves headroom below the 1-GiB child benchmark ceiling.
These are allocation/refusal probes, without large matrix multiplication or any
chemical-accuracy claim; native worker benchmarks remain separate evidence.

## External applicability and mapping cases

The [BMRB fixture manifest](../tests/fixtures/dp5q/bmrb/manifest.json) preserves
four public records, their eight exact input hashes/source URLs, raw carbon
assignment rows, shift-set/sample/condition/reference join keys and bounded
heavy-atom topology maps. It describes eight same-deposition or same-formula
swapped candidate requests in two formula families. These cases stay separate
from the 370 source-labeled regression candidates.

A topology map matches elements and bonds and retains every graph isomorphism;
it does not establish solution protonation, stereochemistry or assignment
truth. The BMRB atom ordinal is not a MOL atom index. Leucine has two topology
maps for its methyl carbons and eight raw assignment rows describing six
distinct peak values with alternative methyl assignments. No equivalence
group, averaged peak or correctness label is invented. All eight requests are
marked scientifically unscorable pending domain adjudication, with no model
assessment executed. The original small public files remain in the authorized
private evidence; only the concise provenance-bearing JSON is shipped.

To reproduce the extraction from those already pinned files, run
`scripts/dp5q-reference/external.py --source <pinned-directory>
--receipt <download-receipt.json> --output <new-directory>`. It verifies the
exact eight hashes with a 256-KiB per-file/1-MiB total bound and writes copies
only to that new directory. All four pinned BMRB depictions have constitutional matches in the union of
provided dataset splits; three match train and beta-alanine matches test only.
This identity observation does not adjudicate solution state or exact training
membership. These four records do not establish independent accuracy or calibration.

## Derived report and isolated fitter comparison

After both campaigns pass, `scripts/dp5q-reference/report.py` reuses
the reference runner's authoritative owned preflight. It verifies every
reference JSON/array hash, exact case/comparison coverage and both evidence
seals before publishing refusal-aware counts, ranks, threshold reversals and
separate N1/N8 ledgers. Partial/empty execution cannot PASS.
Selection also checks the actual recorded B/C geometry-method identifiers.
Missing, mixed or stale methods cannot satisfy a current-method gate; historical
results are never relabeled with the current implementation's version.

`scripts/dp5q-reference/analyze_fits.py` optionally isolates N2 on the exact saved
TensorFlow quantile vectors, original percentile levels and reference-chosen
observed assignment. It compares valid positive legacy fits with the owned
constrained fit, retaining conditioning, residuals, crossings, width/location
differences and threshold changes. A failed refit stays an explicit diagnostic
refusal; it never fills an owned assessment. Nontrivial unresolved group
mapping is reported without an invented comparison. Supply its output through
`report.py --fit-analysis <same-input-refit.json>`. N3 crossing/residual and N4
optional regression diagnostics remain separate from compatibility decisions.

The F/A reference comparison establishes fixed-input prediction parity.
Legacy finite scores that become owned ambiguity refusals are a separate
coverage limitation. A-to-B score changes also combine a geometry-policy
change with removal of SDF coordinate rounding; no isolated causal claim is
made from that aggregate comparison. B's selected conformer count is one even
when its explicitly shared C candidate audit reports multiple retained poses.

## Completed development study

The historical 370-candidate study has 66 families and 67 spectra.
It used the **source SDF atom-order development path**, `converged-single-v1`
and historical C `bounded-ensemble-v1`. Its raw B execution assessed 21/370
candidates; this is not a coverage result for product SMILES or current B v2.
The [committed historical ledger](../tests/fixtures/dp5q/historical-sdf-results.json)
preserves these unpublished local observations. Original snapshots remain unpublished; the ledger permits recounting, not independent inference reproduction. Current-policy inference remains pending.

After excluding the eight unadjudicated JB13A/B candidates, labeled summaries
have **362 candidates: 64 source-correct and 298 source-incorrect**. B assesses
17 of them (7 correct, 10 incorrect); 345 remain unassessed. The original
370-candidate B refusal categories were 291 ambiguous assignments, 44
unsupported-element inputs, **12 coverage mismatches** (seven too few carbon
observations, five too many), and two S13 candidates without a common
authenticated spectrum. S13a independently has too few observations but is
categorized by its earlier shared-spectrum refusal; it is not a thirteenth
coverage-refusal result. Refusal is never acceptance or correct rejection.

| Historical SDF-order arm | Correct assessed / submitted | Incorrect assessed / submitted | Incorrect compatible at 0.10 / 0.20 | Correct flagged at 0.10 / 0.20 |
|---|---:|---:|---:|---:|
| F: provided pose | 8/64 | 12/298 | 11 / 9 | 0 / 1 |
| A: frozen checker geometry | 6/64 | 9/298 | 8 / 8 | 0 / 0 |
| B: converged single v1 | 7/64 | 10/298 | 9 / 9 | 0 / 0 |
| C: bounded-ensemble v1 | 8/64 | 13/298 | 12 / 12 | 0 / 0 |

The [audit](../tests/fixtures/dp5q/development-audit.json) retains split/overlap counts at both thresholds. Within B's
label-eligible cohort, the 94 candidates whose constitutions overlap any
provided split include five assessed candidates (one correct, four incorrect;
all compatible at both thresholds). The remaining 268 candidates include
12 assessed candidates (six correct, six incorrect; five incorrect compatible
at both thresholds). Train-only overlap counts are separately recorded. Lack
of a measured identity match does not establish an untouched holdout.

Historical C v1 fails the registered no-increase rule: the label-eligible
incorrect-compatible count rises from nine to twelve. Historical family-rank
comparisons are not reasserted after excluding JB13A/B. Corrected C v3/current B v2 lack full campaigns; independent C/B full-article/RSS gates remain unresolved. The product uses B as an engineering choice;
the current scientific selection gate is `NOT_ESTABLISHED`. No improved accuracy, generalization or current-policy coverage is claimed. Nine of B's ten assessed
source-incorrect candidates remain compatible at 0.10 and 0.20.

Frozen-geometry tests pin scores, nonidentity assignments, row compatibilities and signed regression errors. Product-SMILES tests cover parent acceptance/display underflow/unrepresentable tails; TensorFlow fixtures cover every trained element token without experimental labels. An additional fit
guard compares the least-squares CDF objective with its exact infinite-width
constant-CDF boundary. No numerically resolvable improvement means refusal,
independent of optimizer stopping width. This is a floating-point objective
check; calibrated scientific sigma/residual limits remain unavailable and are
not invented. Modest quantile crossings remain diagnostics.

These numerical/timing figures are **unpublished local historical evidence**, not current-policy gates. The ledger supports class/threshold recounts without complete reference arrays/original receipts.

Actual frozen TensorFlow comparison passed all 638 applicable F/A checks,
with 102 explicit skips and no changed frozen source/weights. Graph integers,
stored distances and all RBF hashes matched. The worst head difference was
0.000106812 ppm against the fixed 0.001-ppm limit. Same-reference-input N2
controls compared 11,553 valid fitted groups: zero location/width exceedances
at absolute 0.0001 plus relative 0.0001 tolerance, maximum absolute location
difference 0.000000406 ppm and width difference 0.000001520 ppm, with zero
N2 threshold reversals. Fifteen other reference arms retain their explicit
parser/assignment failure instead of a fabricated fit comparison.

Historical N5 controls on identical reference predictions and legacy-parsed observations
found 42 uniquely assigned case-arms, all matching reference observed values
and objectives, and 581 explicit case-arm ambiguity refusals. These finite legacy scores
never replace owned refusals. N1's isolated formula repair produces 41
threshold reversals at 0.10 and eight at 0.20 on fixed reference fits; these
are diagnostic-only because the affected owned cases refuse assignment.
Historical N8's 623 same-fit score and same-prediction assignment controls show no
threshold or assignment/status reversal in this corpus. A deterministic near-tie fixture shows four-decimal rounding could cause earlier-policy ambiguity refusal from a unique assignment.
Current squared-cost assignment retains the first optimum, including ties; historical refusals are not current behavior. No general rounding
invariance is claimed.

Applicability differs: frozen parsing refuses
`stereo/JB8_2` F's invalid noncarbon `OMIT Cl22` label, while the owned
carbon-only request ignores that noncarbon directive and assesses 0.390615.
Its source label is incorrect; the extra assessment invents neither a reference score nor inference failure.

Matched complete-adapter timing on homogeneous authored ethanol requests
includes geometry, actual process/model startup, transport, output parsing
and cleanup. N=1 legacy/owned enrichment times were 41.317/3.437 seconds;
N=10 were 214.533/3.836 seconds. These are fresh parent processes with warm
shared OS caches and private per-article writable caches, using unmodified
historical baseline and owned source snapshots that were not published as
durable refs. Both arms completed every request with unchanged source/weight hashes. The measurement supports
the adapter timing gate on these small synthetic requests; full enabled
complex-article extraction/report timing is not established. Independent
C/B full-article/RSS evidence is not supplied by these measurements.

Original receipts, arrays and N1/N2/N5/N8 controls remain unpublished. Public input pins, ledger and audit tool support the coverage recount without private paths or unreachable commits. The historical
owned full run's 20,728,673 retained bytes, maximum 24.915-second case, 2,973.435 CPU seconds
and cumulative F/A/B/C 464,420,864-byte child RSS were postverified. The
stronger live output monitor added later did **not** supervise that historical
run. The guarded reference run used 183.862 CPU seconds and a 188.191-second
outer wall time with both evidence seals rechecked before reporting.

Reproduce the identity audit/recount without pickle reconstruction or inference:

```sh
uv run --locked python scripts/audit_dp5q_corpus.py \
  --source "$frozen_dp5_source" \
  --historical-results tests/fixtures/dp5q/historical-sdf-results.json \
  --output "$new_audit_json"
```

The checker verifies the three compressed public dataset SHA-256 pins and sizes
before bounded gzip decoding. It scans inert pickle opcodes and parses only
bounded RDKit molecular binary literals; it never unpickles DataFrames, imports
pickle globals or executes reconstruction instructions. Per-candidate identities,
exposures, duplicate groups and label exclusions are retained in the JSON.
