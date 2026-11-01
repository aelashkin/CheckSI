"""Bounded deterministic MMFF geometries for the owned DP5q calculation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable
from time import perf_counter

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDistGeom, rdForceFieldHelpers, rdMolAlign

from checksi.analysis.structure_analysis import coordinate_stereo_scope

from .contracts import CalculationCancelled, MAX_ATOMS

SINGLE_POLICY = "converged-single-v2"
ENSEMBLE_POLICY = "bounded-ensemble-v3"
GAS_CONSTANT = 8.31446261815324  # J mol^-1 K^-1; energies below are kJ/mol.
TEMPERATURE_K = 298.15
EMBEDDING_MAX_ATTEMPTS = 1000
EMBEDDING_TIMEOUT_SECONDS = 10


class GeometryError(ValueError):
    """A complete structure cannot supply the declared supported geometry."""

    def __init__(self, reason: str, message: str, *, diagnostics: dict | None = None) -> None:
        self.reason = reason
        self.diagnostics = diagnostics or {}
        super().__init__(message)


@dataclass(frozen=True)
class PreparedGeometry:
    """Selected original atom-order geometries and explicit population units."""

    mol: Chem.Mol
    conformer_ids: tuple[int, ...]
    energies_kj: np.ndarray
    populations: np.ndarray
    policy: str
    diagnostics: dict = field(default_factory=dict)


def normalized_populations(energies_kj) -> np.ndarray:
    """Normalize Boltzmann populations at 298.15 K from finite kJ/mol values."""
    values = np.asarray(energies_kj, dtype=np.float64)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all():
        raise GeometryError("invalid_energies", "Conformer energies must be a finite nonempty vector in kJ/mol.")
    with np.errstate(over="raise", invalid="raise", under="ignore"):
        try:
            log_weight = -(values - values.min()) * (1000 / (GAS_CONSTANT * TEMPERATURE_K))
            weights = np.exp(log_weight)
            weights /= weights.sum()
        except FloatingPointError as exc:
            raise GeometryError("invalid_energies", "Conformer populations cannot be normalized.") from exc
    return weights


def _stereochemistry_from_geometry(mol: Chem.Mol, conformer_id: int) -> Chem.Mol:
    """Perceive coordinate stereo on a copy, retaining original atom/bond order."""
    geometry = Chem.Mol(mol, confId=conformer_id)
    Chem.RemoveStereochemistry(geometry)
    Chem.AssignStereochemistryFrom3D(geometry, replaceExistingTags=True)
    return geometry


def _preserves_stereochemistry(mol: Chem.Mol, conformer_id: int,
                              expected: dict[int, Chem.ChiralType],
                              expected_bonds: dict[int, Chem.BondStereo]) -> bool:
    if not expected and not expected_bonds:
        return True
    geometry = _stereochemistry_from_geometry(mol, conformer_id)
    # Chiral tags are relative to neighbor order, which is unchanged on this
    # copy. Unlike legacy CIP labels, they include specified ring-relative
    # centers. Only input-specified tags constrain product geometry selection.
    scope = coordinate_stereo_scope(mol)
    reference = scope.reference
    atoms_match = all(geometry.GetAtomWithIdx(index).GetChiralTag() == tag
                      for index, tag in expected.items()
                      if reference.GetAtomWithIdx(index).GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED)
    supported_bonds = {info.centeredOn for info in scope.elements if info.type == Chem.StereoType.Bond_Double}
    return atoms_match and all(geometry.GetBondWithIdx(index).GetStereo() == stereo
                               for index, stereo in expected_bonds.items() if index in supported_bonds)


def _stereoconfiguration(mol: Chem.Mol, conformer_id: int) -> str:
    """Identify an isomer without changing source tags, indices or coordinates."""
    geometry = _stereochemistry_from_geometry(mol, conformer_id)
    return Chem.MolToSmiles(Chem.RemoveHs(geometry), isomericSmiles=True)


def prepare_geometry(structure: str | Chem.Mol, *, ensemble: bool = False,
                     cancel_check: Callable[[], None] | None = None) -> PreparedGeometry:
    """Generate the common B/C candidate set, preserving all atoms/fragments.

    A Mol input is a development-evaluation boundary to retain source SDF atom
    order. Product requests contain SMILES. Neither path canonicalizes or strips
    fragments, substitutes force fields, or keeps unconverged conformers.
    """
    check = cancel_check or (lambda: None)
    check()
    try:
        mol = Chem.MolFromSmiles(structure) if isinstance(structure, str) else Chem.Mol(structure)
        if mol is None:
            raise GeometryError("invalid_structure", "The proposed structure is invalid.")
        mol = Chem.AddHs(mol)
    except (RuntimeError, ValueError) as exc:
        if isinstance(exc, GeometryError):
            raise
        raise GeometryError("invalid_structure", "The complete structure could not be prepared.") from exc
    if mol.GetNumAtoms() > MAX_ATOMS:
        raise GeometryError("atom_limit", f"The complete explicit-H structure exceeds {MAX_ATOMS} atoms.")
    if not rdForceFieldHelpers.MMFFHasAllMoleculeParams(mol):
        raise GeometryError("mmff_parameters_missing", "MMFF94s parameters are unavailable for " + _fragments_without_mmff_parameters(mol) + ".")
    stereo_verification_complete = coordinate_stereo_scope(mol).complete
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    expected = {atom.GetIdx(): atom.GetChiralTag() for atom in mol.GetAtoms()
                if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED}
    expected_bonds = {bond.GetIdx(): bond.GetStereo() for bond in mol.GetBonds()
                      if bond.GetStereo() in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ,
                                              Chem.BondStereo.STEREOCIS, Chem.BondStereo.STEREOTRANS)}
    parameters = rdDistGeom.ETKDGv3()
    parameters.randomSeed = 0xD05
    parameters.numThreads = 1
    parameters.enforceChirality = True
    # Engineering work bounds, not calibrated chemistry applicability limits.
    # RDKit defines timeout per conformer/fragment; the parent enforces the
    # configured total request deadline across all 30 requested conformers.
    parameters.maxIterations = EMBEDDING_MAX_ATTEMPTS
    parameters.timeout = EMBEDDING_TIMEOUT_SECONDS
    check()
    try:
        point = perf_counter()
        # A native timeout in RDKit 2025.9.3 can return the sentinel [-1]
        # with no conformer rather than an empty vector.
        conformer_ids = tuple(index for index in rdDistGeom.EmbedMultipleConfs(mol, numConfs=30, params=parameters)
                              if index >= 0)
        embedding = "deterministic"
        check()
        if not conformer_ids:
            # Long, branched flexible molecules (for example triglycerides) can
            # fail every eigenvalue-based start. Random starting coordinates are
            # RDKit's remedy; molecules that already embed are unaffected.
            parameters.useRandomCoords = True
            conformer_ids = tuple(index for index in rdDistGeom.EmbedMultipleConfs(mol, numConfs=30, params=parameters)
                                  if index >= 0)
            embedding = "random_coordinates"
            check()
        embedding_seconds = perf_counter() - point
        if not conformer_ids:
            raise GeometryError("embedding_failed", "ETKDGv3 did not produce a geometry within the bounded embedding allowance.",
                                diagnostics={"geometry": {"requested_count": 30, "generated_count": 0,
                                                          "embedding": embedding,
                                                          "embedding_seconds": embedding_seconds}})
        point = perf_counter()
        optimized = rdForceFieldHelpers.MMFFOptimizeMoleculeConfs(mol, numThreads=1, maxIters=500, mmffVariant="MMFF94s")
        if len(optimized) != len(conformer_ids):
            raise GeometryError("geometry_failed", "Conformer optimization returned incomplete status data.")
        initial_statuses = [status for status, _ in optimized]
        # Continue only unfinished conformers, from their current coordinates.
        # The total allowance is 2,000 iterations; converged conformers retain
        # their original coordinates/energies and no unfinished pose is scored.
        for index, (conformer_id, (status, _)) in enumerate(zip(conformer_ids, optimized)):
            check()
            if status != 1:
                continue
            continuation = Chem.Mol(mol, confId=conformer_id)
            retried = rdForceFieldHelpers.MMFFOptimizeMoleculeConfs(
                continuation, numThreads=1, maxIters=1500, mmffVariant="MMFF94s")
            if len(retried) != 1:
                raise GeometryError("geometry_failed", "Conformer continuation returned incomplete status data.")
            optimized[index] = retried[0]
            coordinates = continuation.GetConformer().GetPositions()
            for atom_index, position in enumerate(coordinates):
                mol.GetConformer(conformer_id).SetAtomPosition(atom_index, position)
        optimization_seconds = perf_counter() - point
    except CalculationCancelled:
        raise
    except RuntimeError as exc:
        raise GeometryError("geometry_failed", "Bounded ETKDGv3/MMFF94s preparation failed.") from exc
    check()
    if len(optimized) != len(conformer_ids):
        raise GeometryError("geometry_failed", "Conformer optimization returned incomplete status data.")
    point = perf_counter()
    candidates = []
    audit = []
    for conformer_id, initial_status, (status, energy_kcal) in zip(conformer_ids, initial_statuses, optimized):
        check()
        positions = mol.GetConformer(conformer_id).GetPositions()
        finite = bool(np.isfinite(energy_kcal) and np.isfinite(positions).all())
        stereo = finite and _preserves_stereochemistry(mol, conformer_id, expected, expected_bonds)
        reason = ("optimizer_failed" if status == -1 else "nonfinite_geometry" if not finite else "not_converged" if status != 0
                  else "stereochemistry_changed" if not stereo else "candidate")
        audit.append({"conformer_id": int(conformer_id), "optimization_status": int(status),
                      "initial_optimization_status": int(initial_status),
                      "iteration_budget": 2000 if initial_status == 1 else 500,
                      "energy_kj": float(energy_kcal) * 4.184 if np.isfinite(energy_kcal) else None,
                      "selection": reason})
        if status == 0 and finite and stereo:
            candidates.append((float(energy_kcal) * 4.184, conformer_id))
    candidates.sort(key=lambda item: (item[0], item[1]))
    validation_seconds = perf_counter() - point
    if not candidates:
        counts = dict(Counter(item["selection"] for item in audit))
        reason, message = (
            ("geometry_failed", "No eligible conformer is available; MMFF94s setup failed for one or more candidates.") if "optimizer_failed" in counts else
            ("nonfinite_geometry", "Conformer optimization produced nonfinite coordinates or energies.") if "nonfinite_geometry" in counts else
            ("stereochemistry_changed", "No converged conformer preserves the specified atom/bond stereochemistry.") if "stereochemistry_changed" in counts else
            ("no_converged_conformer", "No conformer converged within the bounded 2,000-iteration MMFF94s allowance."))
        raise GeometryError(reason, message, diagnostics={"geometry": {
            "requested_count": 30, "generated_count": len(conformer_ids), "embedding": embedding,
            "converged_count": sum(item["optimization_status"] == 0 for item in audit),
            "eligible_count": 0, "retained_count": 0, "rejection_counts": counts,
            "candidates": audit,
            "timings": {"embedding": embedding_seconds, "optimization": optimization_seconds,
                        "validation": validation_seconds}}})
    point = perf_counter()
    retained = [candidates[0]]
    selections = {index: "higher_energy" for _, index in candidates}
    selections[candidates[0][1]] = "retained"
    if ensemble:
        reference_stereo = _stereoconfiguration(mol, candidates[0][1])
        heavy_map = [(atom.GetIdx(), atom.GetIdx()) for atom in mol.GetAtoms() if atom.GetAtomicNum() != 1]
        for energy, conformer_id in candidates[1:]:
            check()
            # Unspecified centers may resolve differently during embedding.
            # The coordinate key perceives tetrahedral and double-bond stereo;
            # it does not detect axial atropisomerism or establish axis barriers.
            if _stereoconfiguration(mol, conformer_id) != reference_stereo:
                selections[conformer_id] = "other_stereoisomer"
                continue
            if energy - candidates[0][0] > 10.0:
                selections[conformer_id] = "outside_energy_window"
                continue
            if len(retained) == 10:
                selections[conformer_id] = "conformer_limit"
                continue
            # Fixed original-atom correspondence, rigid rotation, no reflection.
            # This is not a symmetry-permutation or mirror-image alignment.
            rms = [rdMolAlign.GetAlignmentTransform(mol, mol, prbCid=conformer_id,
                   refCid=old_id, atomMap=heavy_map, reflect=False)[0] for _, old_id in retained]
            if all(value >= 0.5 for value in rms):
                retained.append((energy, conformer_id))
                selections[conformer_id] = "retained"
            else:
                selections[conformer_id] = "rmsd_duplicate"
    for item in audit:
        if item["selection"] == "candidate":
            item["selection"] = selections[item["conformer_id"]]
    energies = np.array([energy for energy, _ in retained], dtype=np.float64)
    selected_scope = coordinate_stereo_scope(_stereochemistry_from_geometry(mol, retained[0][1]))
    stereo_verification_complete = (stereo_verification_complete and selected_scope.complete
        and all(info.specified == Chem.StereoSpecified.Specified for info in selected_scope.elements))
    return PreparedGeometry(mol, tuple(index for _, index in retained), energies,
                            normalized_populations(energies), ENSEMBLE_POLICY if ensemble else SINGLE_POLICY,
                            {"stereo_verification_complete": stereo_verification_complete,
                             "requested_count": 30, "generated_count": len(conformer_ids),
                             "embedding": embedding,
                             "converged_count": sum(item["optimization_status"] == 0 for item in audit),
                             "eligible_count": len(candidates), "retained_count": len(retained),
                             "candidates": audit,
                             "timings": {"embedding": embedding_seconds,
                                         "optimization": optimization_seconds,
                                         "validation": validation_seconds,
                                         "selection": perf_counter() - point}})


def frozen_geometry(mol: Chem.Mol) -> PreparedGeometry:
    """Use an exact archived reference geometry, without a serialization change."""
    if mol is None or mol.GetNumConformers() != 1:
        raise GeometryError("invalid_reference_geometry", "A frozen reference must contain exactly one geometry.")
    copied = Chem.Mol(mol)
    return PreparedGeometry(copied, (copied.GetConformer().GetId(),), np.array([0.0]), np.array([1.0]), "frozen-reference-v1")


def _fragments_without_mmff_parameters(mol: Chem.Mol) -> str:
    """Describe failed connected-fragment typing using original atom indices.

    RDKit returns no MMFF properties for a fragment containing an untyped atom,
    so in that case the fragment is reported without guessing which atom failed.
    When properties are available, zero atom types identify the unsupported atoms.
    """
    mappings = []
    fragments = Chem.GetMolFrags(mol, asMols=True, fragsMolAtomMapping=mappings)
    descriptions = []
    for fragment, original_indices in zip(fragments, mappings):
        properties = rdForceFieldHelpers.MMFFGetMoleculeProperties(fragment)
        untyped_indices = [
            atom.GetIdx()
            for atom in fragment.GetAtoms()
            if properties is None or properties.GetMMFFAtomType(atom.GetIdx()) == 0
        ]
        if not untyped_indices:
            continue
        labels = [
            f"{fragment.GetAtomWithIdx(index).GetSymbol()}{original_indices[index] + 1}"
            for index in untyped_indices
            if fragment.GetAtomWithIdx(index).GetSymbol() != "H"
        ]
        fragment_smiles = Chem.MolToSmiles(Chem.RemoveHs(fragment))
        label_kind = "atoms" if properties is None else "untyped atoms"
        descriptions.append(f"fragment {fragment_smiles} ({label_kind}: {', '.join(labels)})")
    return "; ".join(descriptions) or "the complete structure (MMFF atom typing failed)"
