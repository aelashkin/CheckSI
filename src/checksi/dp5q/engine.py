"""Pure single-compound orchestration for the owned DP5q calculation."""

from __future__ import annotations

import math
import json
from dataclasses import replace
from time import perf_counter
from typing import Callable

import numpy as np
from rdkit import Chem

from .assignment import AssignmentError, group_predictions
from .observation_assignment import assign_observations, validate_observation_coverage
from .observation_policy import BORON_REASON, BORON_WARNING, derive_observation_policy, has_boron
from checksi.analysis.structure_analysis import carbon_partition_from_coordinates, coordinate_stereo_scope
from .contracts import CalculationCancelled, CheckRequest, CheckResult
from .components import ComponentSelectionError, ORGANIC_COMPONENT_WARNING, select_scoring_component
from .geometry import (GeometryError, PreparedGeometry, frozen_geometry,
                       prepare_geometry, _preserves_stereochemistry, _stereoconfiguration)
from .graph import GraphDomainError, validate_elements
from .network import ModelBundle
from .scoring import ScoringError, aggregate_compatibility, diagnostic_errors, fit_gaussian

APPLICABILITY_REASONS = frozenset({
    "invalid_structure", "atom_limit", "unsupported_element", "unsupported_stereo",
    "mmff_parameters_missing", "embedding_failed", "no_converged_conformer", "stereochemistry_changed",
    "strict_nmr_coverage_mismatch", "assignment_ambiguous", "no_carbon",
    "neighbor_limit", "edge_limit", "isolated_atom", "multiple_carbon_components",
    "annotation_partition_mismatch", "boron_not_supported", "source_partition_mismatch",
    "structure_partition_incomplete", "unsupported_coincidence_policy", "assignment_search_limit",
    "invalid_observation_constraints", "invalid_equivalence_groups", "group_limit",
})


def check_compound(request: CheckRequest, models: ModelBundle, cancel_event=None,
                   progress: Callable[[str], None] | None = None, *,
                   frozen_mol: Chem.Mol | None = None,
                   prepared_geometry: PreparedGeometry | None = None,
                   ensemble: bool = False) -> CheckResult:
    """Assess one complete spectrum without I/O or retained per-request state.

    The process worker uses only the SMILES request path. Frozen/prepared Mol
    arguments are development evaluation boundaries preserving source atom
    order and geometry; they never cross the product protocol.
    """
    started = perf_counter()
    timings: dict[str, float] = {}
    metadata = models.metadata
    provenance = {key: metadata[key] for key in
                  ("model_id", "preprocessing_id", "score_version") if key in metadata}
    # The metadata uses score_id; the result uses the public score_version key.
    provenance["score_version"] = metadata["score_id"]
    provenance["weights_sha256"] = metadata["files"]["weights.npz"]["sha256"]
    provenance["geometry_policy"] = metadata["geometry_policy"]

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CalculationCancelled("DP5q calculation cancelled.")

    def stage(name: str) -> float:
        check_cancelled()
        if progress is not None:
            progress(name)
        check_cancelled()
        return perf_counter()

    def finish(status: str, reason: str | None = None, **values) -> CheckResult:
        check_cancelled()
        timings["total"] = perf_counter() - started
        try:
            json.dumps(values, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            status, reason = "failed", "nonfinite_diagnostics"
            values = {"diagnostics": {"message": "The calculation produced diagnostics outside the finite JSON numerical range."}}
        return CheckResult(compound_id=request.compound_id, status=status, reason=reason,
                           provenance=provenance, timings=timings, **values)

    check_cancelled()
    try:
        point = stage("coverage")
        if frozen_mol is not None and prepared_geometry is not None:
            raise GeometryError("invalid_reference_geometry", "Only one development geometry source may be supplied.")
        source = (prepared_geometry.mol if prepared_geometry is not None else frozen_mol)
        mol = Chem.Mol(source) if source is not None else Chem.MolFromSmiles(request.smiles)
        if mol is None:
            raise GeometryError("invalid_structure", "The proposed structure is invalid.")
        if source is not None:
            requested = Chem.MolFromSmiles(request.smiles)
            if (requested is None or Chem.MolToSmiles(Chem.RemoveHs(mol))
                    != Chem.MolToSmiles(Chem.RemoveHs(requested))):
                raise GeometryError("reference_identity_mismatch", "The development geometry does not represent the requested complete structure.")
        if has_boron(mol):
            return finish('not_applicable', BORON_REASON, diagnostics={'message': BORON_WARNING})
        original_carbons = tuple(atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6)
        if request.carbon_labels is not None and request.carbon_labels != tuple(f"C{index+1}" for index in original_carbons):
            raise AssignmentError("carbon_identity_mismatch", "The request inventory differs from the original structure atoms.")
        component = select_scoring_component(mol)
        if component.ignored_components:
            provenance['component_scope'] = component.component_scope(request.smiles)
            if prepared_geometry is not None:
                prepared_geometry = replace(prepared_geometry, mol=component.mol)
        mol = component.mol
        requested_groups = component.project_groups(request.carbon_groups)
        tokens = {int(key): value for key, value in metadata["tokens"].items()}
        validate_elements(mol, tokens)
        atom_indices = tuple(atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6)
        policy = derive_observation_policy(mol, requested_groups, cancel_check=check_cancelled,
                                           allowed_elements=set(tokens))
        groups = policy.groups
        if any(tuple(sorted(group)) not in policy.coincident_environments for group in request.coincident_environments):
            raise AssignmentError('unsupported_coincidence_policy', 'A requested coincidence coalition lacks source structural support.')
        validate_observation_coverage(len(groups), len(request.carbon_shifts),
            observation_atom_counts=request.observation_atom_counts,
            coincident_environments=request.coincident_environments, fixed=request.fixed_assignments,
            cancel_check=check_cancelled)
        if policy.selected_isomer is not None:
            # Enumeration retains original atom/bond indices. The SMILES is an
            # identity/provenance value only; never reparse it into a new frame.
            mol = Chem.Mol(policy.selected_isomer)
        expected_partition = {frozenset(group.atom_indices) for group in groups}
        timings["coverage"] = perf_counter() - point

        point = stage("geometry")
        geometry = (prepared_geometry if prepared_geometry is not None else
                    frozen_geometry(mol) if frozen_mol is not None else
                    prepare_geometry(mol, ensemble=ensemble,
                                     cancel_check=check_cancelled))
        provenance["geometry_policy"] = geometry.policy
        if (not geometry.conformer_ids or len(geometry.conformer_ids) > 10
                or geometry.populations.shape != (len(geometry.conformer_ids),)
                or not np.isfinite(geometry.populations).all()
                or np.any(geometry.populations < 0)
                or not math.isclose(float(geometry.populations.sum()), 1, abs_tol=1e-12)):
            raise GeometryError("invalid_populations", "The selected geometries need finite normalized populations.")
        expected_atoms = {atom.GetIdx(): atom.GetChiralTag() for atom in mol.GetAtoms()
                          if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED}
        expected_bonds = {bond.GetIdx(): bond.GetStereo() for bond in mol.GetBonds()
                          if bond.GetStereo() in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ,
                                                  Chem.BondStereo.STEREOCIS, Chem.BondStereo.STEREOTRANS)}
        coordinate_verified = policy.partition_complete
        coordinate_reason = policy.partition_reason
        stereo_verified = coordinate_stereo_scope(mol).complete
        for conformer_id in geometry.conformer_ids:
            check_cancelled()
            if not np.isfinite(geometry.mol.GetConformer(conformer_id).GetPositions()).all():
                raise GeometryError('nonfinite_geometry', 'A retained geometry has nonfinite coordinates.')
            if not _preserves_stereochemistry(geometry.mol, conformer_id, expected_atoms, expected_bonds):
                raise GeometryError('stereochemistry_changed', 'A retained geometry changes source atom/bond stereo.')
            selected_smiles = _stereoconfiguration(geometry.mol, conformer_id)
            selected_scope = coordinate_stereo_scope(Chem.MolFromSmiles(selected_smiles))
            stereo_verified = (stereo_verified and selected_scope.complete
                               and all(info.specified == Chem.StereoSpecified.Specified
                                       for info in selected_scope.elements))
            if (policy.selected_isomer is not None and selected_smiles != Chem.MolToSmiles(
                    Chem.RemoveHs(coordinate_stereo_scope(policy.selected_isomer).reference))):
                raise GeometryError('stereochemistry_changed', 'A retained geometry differs from the selected supported isomer.')
            measured = carbon_partition_from_coordinates(geometry.mol, conformer_id)
            check_cancelled()
            if not measured.complete or {frozenset(group) for group in measured.groups} != expected_partition:
                coordinate_verified = False
                coordinate_reason = coordinate_reason or measured.reason or 'coordinate_partition_mismatch'
        provenance.update(coordinate_partition_verified=coordinate_verified,
                          stereo_verification_complete=stereo_verified,
                          selected_isomer_smiles=_stereoconfiguration(geometry.mol, geometry.conformer_ids[0]),
                          configuration_status=policy.configuration_status,
                          partition_constraint=('source-groups-single-isomer-v1' if not coordinate_verified
                                                else 'exact-original-atom-partition-v1'))
        if not coordinate_verified:
            provenance['coordinate_partition_reason'] = coordinate_reason
        if policy.embedding_selected:
            provenance['partition_reason'] = policy.partition_reason
        if policy.configuration_dependent:
            provenance.update(configuration_dependent=True,
                              expected_carbon_counts=list(policy.expected_counts),
                              source_smiles=request.smiles,
                              isomer_selection_policy=('lowest-energy-embedded-isomer-v1' if policy.embedding_selected
                                                       else 'first-compatible-original-partition-v1'))
        timings["geometry"] = perf_counter() - point

        point = stage("inference")
        predictions = []
        for conformer_id in geometry.conformer_ids:
            check_cancelled()
            prediction = models.predict(Chem.Mol(geometry.mol, confId=conformer_id))
            if tuple(prediction.atom_indices) != atom_indices:
                raise AssignmentError("carbon_identity_mismatch", "Prediction indices changed the original carbon identity.")
            predictions.append(prediction)
            for name, seconds in (prediction.timings or {}).items():
                timings[name] = timings.get(name, 0.0) + seconds
        means, quantiles = group_predictions(np.stack([value.mean for value in predictions]),
                                             np.stack([value.quantiles for value in predictions]), groups)
        timings["inference"] = perf_counter() - point

        point = stage("assignment")
        weighted_means = geometry.populations @ means
        distributions = quantiles.transpose(1, 0, 2).reshape(len(groups), -1)
        assignment = assign_observations(weighted_means, request.carbon_shifts,
            distributions=distributions, fixed=request.fixed_assignments,
            observation_atom_counts=request.observation_atom_counts,
            coincident_environments=request.coincident_environments, cancel_check=check_cancelled)
        observed = np.asarray(request.carbon_shifts, dtype=np.float64)[assignment.indices]
        timings["assignment"] = perf_counter() - point

        point = stage("scoring")
        fits = [[fit_gaussian(values, metadata["quantile_levels"], cancel_check=check_cancelled)
                 for values in conformer] for conformer in quantiles]
        locations = np.array([[fit.mu for fit in conformer] for conformer in fits])
        widths = np.array([[fit.sigma for fit in conformer] for conformer in fits])
        score = aggregate_compatibility(observed, locations, widths, geometry.populations,
                                        evidence_weights=assignment.evidence_weights)
        errors = diagnostic_errors(means, observed, geometry.populations,
                                   evidence_weights=assignment.evidence_weights)
        shared = {str(peak): list(indices) for peak, indices in assignment.coincident_observations.items()}
        scope_note = (f"Full {len(groups)} carbon environments are compared with {len(request.carbon_shifts)} original observations. "
                      + ("Coincident environments share measured evidence without averaging predictions. " if shared else "")
                      + "Selected isomer only; other configurations, if present, may differ.")
        if policy.embedding_selected:
            scope_note += (' DP5q calculated for one stereoisomer only; another isomer may give a very different result.'
                           f' The source carbon partition is unverified ({policy.partition_reason}).')
        if not stereo_verified:
            scope_note += ' Some source stereochemistry cannot be verified from 3D coordinates.'
        if not coordinate_verified:
            scope_note += f' The prediction carbon partition is unverified ({coordinate_reason}).'
        if component.ignored_components:
            scope_note += ' ' + ORGANIC_COMPONENT_WARNING
        diagnostics = {**errors, "geometry": geometry.diagnostics,
                       "full_environment_count": len(groups), "observation_count": len(request.carbon_shifts),
                       "scored_environment_count": len(groups), "coincident_observations": shared,
                       "score_scope": "coincident_observations" if shared else "complete_spectrum",
                       "scope_note": scope_note,
                       "fit_locations": locations.tolist(), "fit_sigmas": widths.tolist(),
                       "assignment_objective": assignment.objective,
                       "assignment_exchangeable": assignment.exchangeable,
                       "conformer_count": len(geometry.conformer_ids),
                       "energies_kj": geometry.energies_kj.tolist(),
                       "populations": geometry.populations.tolist(),
                       "fit_residual_rms": [[fit.residual_rms for fit in row] for row in fits],
                       "quantile_crossings": [[fit.crossing_count for fit in row] for row in fits]}
        if score.score == 0:
            diagnostics["zero_score_reason"] = ("tail_beyond_float64" if not math.isfinite(score.log_score)
                                                  else "display_underflow")
        rows = []
        for index, group in enumerate(groups):
            labels = component.original_labels(group.atom_indices)
            percent = float(math.exp(float(score.group_log_scores[index])) * 100)
            rows.append({"atom_label": ",".join(labels), "atom_labels": list(labels),
                         "calculated_shift": float(weighted_means[index]),
                         "experimental_shift": float(observed[index]),
                         "observation_index": int(assignment.indices[index]),
                         "evidence_weight": float(assignment.evidence_weights[index]),
                         "scaled_error": (errors["scaled_errors"][index]
                                          if errors["scaled_errors"] is not None else None),
                         "score_percent": percent, "atom_probability_percent": percent})
        if request.include_geometry:
            diagnostics["geometry_sdf"] = Chem.MolToMolBlock(geometry.mol, confId=geometry.conformer_ids[0]) + "\n$$$$\n"
        timings["scoring"] = perf_counter() - point
        return finish("assessed", score_raw=score.score,
                      log_score=score.log_score if math.isfinite(score.log_score) else None,
                      assignments=rows, diagnostics=diagnostics)
    except CalculationCancelled:
        raise
    except (GeometryError, AssignmentError, GraphDomainError, ComponentSelectionError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc.code
        return finish("not_applicable" if reason in APPLICABILITY_REASONS else "failed", reason,
                      diagnostics={"message": str(exc), **getattr(exc, "diagnostics", {})})
    except ScoringError as exc:
        return finish("failed", exc.reason, diagnostics={"message": str(exc)})
    except (ValueError, RuntimeError, FloatingPointError) as exc:
        return finish("failed", "calculation_failed", diagnostics={"message": str(exc)})
