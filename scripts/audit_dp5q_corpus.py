"""Audit the historical public development corpus without executing dataset pickles.

Only hash-pinned gzip bytes and inert pickle opcodes are read. RDKit molecular
binary literals are parsed as data; pickle GLOBAL/REDUCE/BUILD are never run.
This measures identity overlap, not membership in an undisclosed training run.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
from hashlib import sha256
import io
import json
from pathlib import Path
import pickletools

from rdkit import Chem

SOURCE_REVISION = "8f8e22364654bbce71dcb6abb414eee793b00d92"
DATASETS = {
    "train": (5_493_741, "ce6ab7e5d09eeb5dcc981d6743d60447981dc1064241768c9725412fc32eaea6", 12_349),
    "valid": (2_284_331, "c89e87a680882825a04b82f1044abf9ca88cf18cd91b34708928608dead97382", 5_000),
    "test": (2_330_867, "8bf07182d16a9bcd28795a2c6895d89180f6ae633dc07702486347bc4f77f563", 5_000),
}
MAX_PICKLE_BYTES = 24 * 1024 * 1024
EXCLUDED_LABEL_FAMILIES = frozenset({"stereo/JB13A", "stereo/JB13B"})


def identities(mol: Chem.Mol) -> tuple[str, str]:
    """Return exact isomeric and stereo-erased constitutional graph identities."""
    copied = Chem.RemoveHs(Chem.Mol(mol))
    exact = Chem.MolToSmiles(copied, isomericSmiles=True)
    Chem.RemoveStereochemistry(copied)
    return exact, Chem.MolToSmiles(copied, isomericSmiles=True)


def molecular_literals(data: bytes) -> list[Chem.Mol]:
    """Inspect pickle syntax without invoking any pickle instruction."""
    if len(data) > MAX_PICKLE_BYTES:
        raise ValueError("Expanded dataset exceeds its byte bound.")
    result = []
    for _, value, _ in pickletools.genops(data):
        if not isinstance(value, bytes) or not value.startswith(b"\xef\xbe\xad\xde"):
            continue
        if len(value) > 65536 or len(result) >= 15000:
            raise ValueError("Dataset molecular literal bound exceeded.")
        mol = Chem.Mol(value)
        if mol is None or not 1 <= mol.GetNumAtoms() <= 512:
            raise ValueError("Invalid bounded RDKit molecular literal.")
        result.append(mol)
    if not result:
        raise ValueError("No RDKit molecular literals in dataset.")
    return result


def load_split(source: Path, split: str) -> tuple[dict, set[str], set[str]]:
    """Verify a pinned public file before inert decoding and identity extraction."""
    path = source / "nmrdb-dataset" / f"{split}_NMRdb-CASCADEset.pkl.gz"
    size, digest, count = DATASETS[split]
    with path.open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size or sha256(data).hexdigest() != digest:
        raise ValueError(f"{split} dataset does not match the pinned public source.")
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        expanded = stream.read(MAX_PICKLE_BYTES + 1)
    molecules = molecular_literals(expanded)
    if len(molecules) != count:
        raise ValueError(f"{split} dataset molecule count changed.")
    pairs = [identities(mol) for mol in molecules]
    exact = {pair[0] for pair in pairs}
    constitution = {pair[1] for pair in pairs}
    provenance = {"path": str(path.relative_to(source)), "bytes": size, "sha256": digest,
                  "molecules": count, "distinct_exact": len(exact), "distinct_constitutions": len(constitution),
                  "url": f"https://github.com/aelashkin/DP5/blob/{SOURCE_REVISION}/nmrdb-dataset/{path.name}"}
    return provenance, exact, constitution


def historical_metrics(ledger: dict, records: list[dict]) -> dict:
    """Recount archived SDF-order results, excluding unadjudicated JB13 labels."""
    index = {record["case_id"]: record for record in records}
    if set(index) != {case["case_id"] for case in ledger["cases"]}:
        raise ValueError("Historical ledger does not cover the exact corpus.")
    results = {}
    for threshold in (.1, .2):
        threshold_result = {}
        for cohort in ("all_eligible_labels", "train_constitution_overlap", "no_measured_train_constitution_overlap",
                       "provided_splits_constitution_overlap", "no_measured_provided_splits_constitution_overlap"):
            arms = {}
            for arm in ("F", "A", "B", "C"):
                counts = {label: {"submitted": 0, "assessed": 0, "compatible": 0, "flagged": 0,
                                  "unassessed_reasons": {}} for label in ("correct", "incorrect")}
                reasons = {label: Counter() for label in counts}
                for case in ledger["cases"]:
                    record = index[case["case_id"]]
                    if not record["label_eligible"]:
                        continue
                    split = "all_provided_splits" if "provided_splits" in cohort else "train"
                    overlap = record["overlap"][split]["constitution"]
                    if (cohort in ("train_constitution_overlap", "provided_splits_constitution_overlap") and not overlap
                            or cohort.startswith("no_measured_") and overlap):
                        continue
                    label = "correct" if record["source_label_correct"] else "incorrect"
                    count = counts[label]
                    count["submitted"] += 1
                    result = case["arms"][arm]
                    if result["status"] == "assessed":
                        count["assessed"] += 1
                        count["compatible" if result["score"] >= threshold else "flagged"] += 1
                    else:
                        reasons[label][result["reason"]] += 1
                for label, count in counts.items():
                    count["unassessed"] = count["submitted"] - count["assessed"]
                    count["unassessed_reasons"] = dict(sorted(reasons[label].items()))
                arms[arm] = counts
            threshold_result[cohort] = arms
        results[str(threshold)] = threshold_result
    return results


def audit(manifest: dict, datasets: dict, bmrb: dict, ledger: dict | None = None) -> dict:
    """Measure exact/constitutional overlap, duplicate identities and label caveats."""
    datasets = dict(datasets)
    datasets["all_provided_splits"] = (
        {"definition": "Union of the provided train, valid and test files; not the train split alone."},
        set().union(*(values[1] for values in datasets.values())),
        set().union(*(values[2] for values in datasets.values())))
    records = []
    duplicates = defaultdict(list)
    coverage_mismatches = []
    for case in manifest["cases"]:
        exact, constitution = identities(Chem.MolFromSmiles(case["identity"]["isomeric_smiles"]))
        duplicates[exact].append(case["case_id"])
        spectrum = manifest["spectra"][case["spectrum_id"]]
        groups = [group for group in spectrum["equivalence_groups"]
                  if all(label.startswith("C") and label[1:].isdigit() for label in group)]
        required = len(case["identity"]["carbon_indices"]) - sum(len(group) - 1 for group in groups)
        observed = len(spectrum["carbon_shifts"])
        if observed != required:
            coverage_mismatches.append({"case_id": case["case_id"], "required_groups": required,
                                        "observations": observed, "direction": "too_few" if observed < required else "too_many",
                                        "shared_family_spectrum": case["pairing_status"] == "shared_family_spectrum"})
        records.append({"case_id": case["case_id"], "family_id": case["family_id"],
                        "source_label_correct": case["source_label_correct"],
                        "label_eligible": case["family_id"] not in EXCLUDED_LABEL_FAMILIES,
                        "exact_identity": exact, "constitution": constitution,
                        "overlap": {name: {"exact": exact in values[1], "constitution": constitution in values[2]}
                                    for name, values in datasets.items()}})
    overlap = {}
    for split, values in datasets.items():
        overlap[split] = {**values[0], "candidate_exact": sum(row["overlap"][split]["exact"] for row in records),
                          "candidate_constitution": sum(row["overlap"][split]["constitution"] for row in records),
                          "correct_exact": sum(row["source_label_correct"] and row["overlap"][split]["exact"] for row in records)}
    external = []
    for record in bmrb["records"]:
        # This is the pinned depiction, not an adjudicated solution structure.
        depiction = record["topology_mapping"]["provided_depiction_smiles"]
        exact, constitution = identities(Chem.MolFromSmiles(depiction))
        external.append({"accession": record["accession"], "depiction": depiction,
                         "train_exact": exact in datasets["train"][1],
                         "train_constitution": constitution in datasets["train"][2],
                         "all_provided_splits_exact": exact in datasets["all_provided_splits"][1],
                         "all_provided_splits_constitution": constitution in datasets["all_provided_splits"][2]})
    result = {"schema_version": 1, "source_revision": SOURCE_REVISION,
              "interpretation": "Public developmental data; measured dataset overlap is not independent validation or proof of exact training-run membership.",
              "dataset_parsing": "Hash-pinned gzip; inert pickletools opcode scan; no pickle.load or executable pickle reconstruction.",
              "candidate_count": len(records), "distinct_exact_structures": len(duplicates),
              "duplicate_candidate_count": len(records) - len(duplicates),
              "duplicate_groups": [group for _, group in sorted(duplicates.items()) if len(group) > 1],
              "coverage_mismatches": coverage_mismatches,
              "label_exclusions": {"families": sorted(EXCLUDED_LABEL_FAMILIES),
                                   "candidates": sum(not row["label_eligible"] for row in records),
                                   "reason": "JB13A_1 and JB13B_1 share the exact SDF/structure but different spectra; domain adjudication required; entire pair excluded from aggregate labeled metrics."},
              "overlap_by_split": overlap, "bmrb_depictions": external, "cases": records}
    if ledger is not None:
        result["historical_sdf_order_metrics"] = historical_metrics(ledger, records)
    return result


def main() -> None:
    """Write a new reproducible audit; never alter inputs or run inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("tests/fixtures/dp5q/manifest.json"))
    parser.add_argument("--bmrb", type=Path, default=Path("tests/fixtures/dp5q/bmrb/manifest.json"))
    parser.add_argument("--historical-results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    datasets = {split: load_split(args.source, split) for split in DATASETS}
    ledger = json.loads(args.historical_results.read_text()) if args.historical_results else None
    result = audit(json.loads(args.manifest.read_text()), datasets, json.loads(args.bmrb.read_text()), ledger)
    result["manifest_sha256"] = sha256(args.manifest.read_bytes()).hexdigest()
    if args.historical_results:
        result["historical_results_sha256"] = sha256(args.historical_results.read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
