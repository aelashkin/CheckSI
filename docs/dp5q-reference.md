# DP5q development reference

NumPy evaluates the product parameters. TensorFlow, Keras, NFP, HDF5 and legacy pickle loading belong only to this opt-in oracle. Default/installed-wheel tests require neither reference source nor environment.

Use an inspected, immutable DP5 snapshot at
`8f8e22364654bbce71dcb6abb414eee793b00d92`. Its Python requirement `<=3.11`
incorrectly rejects later 3.11 patch releases; import its frozen source directly
without altering upstream metadata or CheckSI pins.

## Isolated environment

`requirements.in` records reviewed direct oracle dependencies;
`requirements.lock` pins the full Intel macOS / CPython 3.11 binary stack and
contains SHA-256 artifact allowlists. Installation requires hashes and binary
wheels; hashes authenticate the pinned artifacts, not their scientific validity.
It is separate from `uv.lock`, and is not a product dependency group. Resolve a
new compatible binary lock explicitly before using another reference platform.

Choose a new ignored report directory and use explicit paths throughout:

```sh
export UV_CACHE_DIR=/private/tmp/checksi-dp5q-uv-cache
export UV_PYTHON_INSTALL_DIR="$reference_root/python"
uv python install --no-bin --no-registry 3.11
uv venv --python 3.11 --no-python-downloads "$reference_root/venv"
uv pip install --python "$reference_root/venv/bin/python" \
  --no-python-downloads --only-binary :all: \
  --require-hashes \
  -r scripts/dp5q-reference/requirements.lock
PYTHONDONTWRITEBYTECODE=1 "$reference_root/venv/bin/python" -P \
  scripts/dp5q_reference.py --source "$frozen_dp5_source" \
  --output "$new_reference_capture"
```

Before installing the oracle or executing upstream code, the product interpreter
can verify the immutable source without TensorFlow or POSIX resource support:

```sh
uv run --locked python -P scripts/dp5q_reference.py \
  --source "$frozen_dp5_source" --verify-only
```

The code-owned [source lock](../scripts/dp5q-reference/source-lock.json) pins every
file in the upstream `dp5` subtree and its license to the exact revision. Its
original model hashes must also match the shipped numeric manifest. Extra files
and bytecode caches under `dp5`, symlinks, changed bytes and changed sizes are refused before any
upstream import or pickle load. Use a clean, inspected, immutable snapshot;
integrity checks do not make executable pickle or custom model layers safe.
The loader exposes only the verified `dp5` package, not the checkout root as a
general import path. The explicit preprocessor load hashes the exact bytes passed
to deserialization, and before/after fingerprints still detect source changes.

The harness caps CPU time at 300 seconds, disables GPU use, requests one native
thread, places Keras state inside the capture directory and disables bytecode
writes. It authenticates frozen source/assets before loading and hashes them again
after execution.
It never trains or writes into the reference snapshot. The scoped parser/assignment
import excludes the unrelated eager raw-FID package initializer. Original frozen
parsing, strict assignment and Gaussian-fit functions execute without edits.

The default S1/S11 run archives exact initial/final SDFs, graph connectivity and
stored distances, RBF values before/after casting, actual Keras semantic weights,
each block's projected atoms/bond residuals/reduced messages/atom residuals,
carbon readout, atomic shift and both output heads. It archives original and
four-decimal emitted `.nmr` bytes, parsed float64 experimental values, strict
assignments, grouped outputs, fitted mu/sigma and legacy scores. It compares an
actual batch of 16 with individual predictions. Public challenge structures are
regression examples; their outputs are not independent chemical truth or evidence
of training-set independence.

## Export and compare

After the semantic and shared-activation gate passes, run from product Python:

```sh
uv run --locked python scripts/export_dp5q_assets.py \
  --reference "$new_reference_capture" \
  --source "$frozen_dp5_source" --output "$new_asset_directory"
```

The exporter defaults to the tracked [49-row tensor identity map](../scripts/dp5q-reference/tensor-map.json),
recovered byte-for-byte from the original inspected tensor-sharing gate. Its
SHA-256 is `76d81ae00ba3cd91f1243fd8df616a0967e1dc41eda8d84c9462ea09c91177a4`,
as recorded by the original numeric manifest. An explicit `--mapping`
path must contain those exact bytes; even a whitespace-only change is refused.
All 49 semantic rows must match the frozen map, requiring
41 byte-identical shared tensors, 57 unique float32 arrays totaling 11,625,924 raw
bytes, exact shared activations on two geometries and the batch comparison. It
writes numeric NPZ, JSON metadata and notices. It verifies the source license
against the pinned size/hash in `source-lock.json` before creating the output.
The package's `NOTICE.txt` is the authoritative modification/attribution notice;
the exporter copies its exact bytes rather than maintaining a second template.
All five notice/license files have size and SHA-256 entries in the manifest.
Use new directories; never overwrite evidence. Numeric assets load with `allow_pickle=False`,
bounded archive/member/header sizes and exact code-owned names, shapes, dtypes,
finiteness and SHA-256 checks.

NumPy comparisons retain the plan's bounds: exact graph/index integers;
block activations `atol=1e-4, rtol=1e-5`; both heads absolute `1e-3 ppm`.
Report each stage and worst error; explain discrepancies without tuning tolerances.
Corrected score arithmetic has separate stable-math tests and intentionally
differs from legacy epsilon-floor aggregation.

The 2026-10-21 Intel macOS gate ran TensorFlow 2.14.0/Keras 2.14.0 on
Python 3.11.17 against the owned NumPy 2.4.2 implementation on Python 3.13.16,
with RDKit 2025.9.3 in both environments. S1/S11 graph arrays were exact;
worst retained block error was 0.0000573 and worst head error was 0.0000458 ppm.
Authored ethanol/toluene software fixtures also passed, with worst head error
0.0000306 ppm. Their [numeric captures](../tests/fixtures/dp5q/reference-network/provenance.json)
allow default tests to compare every retained block without legacy imports.
S1 is explicitly unscored: its original peak list has 14 shifts but 15 required
carbon groups. S11 has complete strict assignment; neither case validates a
general accuracy claim. An actual TensorFlow comparison on another platform
requires its own oracle receipt; product native-wheel CI is a separate check.

## Provenance and notices

The parameters retain [Goodman Lab's MIT license](https://github.com/aelashkin/DP5/blob/8f8e22364654bbce71dcb6abb414eee793b00d92/license.md),
[CASCADE's MIT notice](https://github.com/patonlab/CASCADE/blob/master/LICENSE),
[NFP's BSD-style notice](https://github.com/NatLabRockies/nfp/blob/master/LICENSE)
and [NMRShiftDB2's terms and attribution](https://nmrshiftdb.nmr.uni-koeln.de/nmrshiftdbhtml/nmrshiftdb2datalicense.txt).
Git disables line-ending conversion for the packaged resources, pinned source
lock, tensor map and third-party notice sources, including their JSON manifests.
Checkout and built-wheel tests compare every notice with its manifest pin, and
installed verification also checks each `.dist-info/licenses` copy.
The manifest records original asset hashes, semantic identity, source revision,
transformed archive hash and modification status. No training data are shipped.
The public derived dataset and preparation method are linked at
[the frozen DP5 dataset](https://github.com/aelashkin/DP5/tree/8f8e22364654bbce71dcb6abb414eee793b00d92/nmrdb-dataset).
Cite [CASCADE](https://doi.org/10.1039/D1SC03343C) and
[the DP5q method](https://doi.org/10.1039/D5SC06988B) when using the method.

## Complete legacy CLI benchmark profile

The optional [CLI profile](../scripts/dp5q-reference/requirements-cli.in) and
[locked closure](../scripts/dp5q-reference/requirements-cli.lock) add the frozen
CLI's eager NMR/plotting imports to the numerical oracle environment. The
original numerical pins stay unchanged. This profile is development-only; it
is not part of the product dependencies or offline wheel.
Install it with the same `--require-hashes --only-binary :all:` options, selecting
`requirements-cli.lock`. To refresh hashes with unchanged reviewed versions, use `uv pip compile --no-deps
--generate-hashes --python "$reference_python" --only-binary :all:` with the
existing complete lock as input and a new output file. Review the resulting
hashes and confirm that every version is unchanged before replacing a lock.
Changing dependency versions requires a new compatibility review and full
resolution from the corresponding `.in` file with `--generate-hashes`.

A real full-adapter benchmark must include the old checker's structure
preparation, actual frozen CLI invocation and trusted generated-output parse.
Once-loaded TensorFlow timing omits that path. Run the old
adapter with explicit runtime metadata pointing to the isolated reference
interpreter, and use copied inputs: the frozen CLI rewrites its supplied SDF.
Keep its working directory, output, `KERAS_HOME`, `MPLCONFIGDIR`,
`XDG_CACHE_HOME` and `TMPDIR` under a new owned directory; set
`PYTHONDONTWRITEBYTECODE=1`, `MPLBACKEND=Agg` and native numerical thread
limits to one. Hash frozen code/weights before and after execution. Separate adapter, outer process/import and extraction/report timing; state fresh-process/cache conditions.
