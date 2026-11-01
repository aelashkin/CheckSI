# DP5q fixture scope

`acceptance-requests.json` contains the 32 actual DOCX-to-OPSIN requests for the
opt-in [acceptance regression](../../../scripts/check_dp5q_acceptance.py).
It retains input provenance rather than fixed score targets. The source names
for 3.9 revised and 3.10 original differ from the historical SDF structures.

`manifest.json`, `development-audit.json`, `historical-sdf-results.json` and
`bmrb/manifest.json` belong to the
[historical development study](../../../docs/dp5q-evaluation.md#historical-development-campaign).
They preserve source identities, labels, dataset overlap and earlier results,
including the superseded assignment-tie refusals. They are not current product
coverage or independent scientific truth. Keep their recorded bytes and hashes;
write new inference evidence separately rather than updating historical results.

`reference-network/`, `engineering-smoke.json`, `scoped_numerical_reference.json`
and the worker fixtures support software parity, numerical or process tests.
They remain active engineering controls and do not establish chemical accuracy.
