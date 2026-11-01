"""Finite data contracts and bounded framing for the DP5q worker."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = 2
CAPABILITIES = ("coincident-observations-v1",)
MAX_FRAME_BYTES = 1024 * 1024
MAX_GROUPS = 128
MAX_ATOMS = 512
MAX_SMILES_LENGTH = 16_384
MAX_EDGES = 32_768
MAX_TEXT_LENGTH = 4096
MAX_SDF_LENGTH = 256 * 1024
SCORE_VERSION = "dp5q-logtail-v1"


class CalculationCancelled(RuntimeError):
    """A run cancellation observed at a calculation stage boundary."""


class _Contract(BaseModel):
    """Reject unknown fields and nonfinite numerical values at process edges."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CheckRequest(_Contract):
    """One spectrum; legacy observation_atom_counts stores overlapping signals."""

    compound_id: str = Field(min_length=1, max_length=128)
    smiles: str = Field(min_length=1, max_length=MAX_SMILES_LENGTH)
    carbon_shifts: tuple[float, ...] = Field(min_length=1, max_length=MAX_GROUPS)
    carbon_groups: tuple[tuple[str, ...], ...] = ()
    carbon_labels: tuple[str, ...] | None = Field(default=None, max_length=MAX_ATOMS)
    observation_atom_counts: tuple[int | None, ...] | None = Field(
        default=None, max_length=MAX_GROUPS, exclude_if=lambda value: value is None,
    )
    fixed_assignments: dict[int, int] = Field(default_factory=dict)
    coincident_environments: tuple[tuple[int, ...], ...] = ()
    include_geometry: bool = Field(default=False, strict=True)

    @field_validator("carbon_labels", mode="before")
    @classmethod
    def validate_inventory_types(cls, value):
        if value is not None:
            _carbon_labels(value)
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> CheckRequest:
        if self.observation_atom_counts is not None and len(self.observation_atom_counts) != len(self.carbon_shifts):
            raise ValueError("observation atom counts must align with carbon shifts")
        if self.carbon_labels is not None:
            labels = _carbon_labels(self.carbon_labels)
            if tuple(sorted(labels, key=lambda label: int(label[1:]))) != self.carbon_labels:
                raise ValueError("carbon inventory must retain original atom order")
            if any(label not in labels for group in self.carbon_groups for label in group):
                raise ValueError("equivalence groups must belong to the carbon inventory")
        count = (len(self.carbon_labels) - sum(len(group) - 1 for group in self.carbon_groups)
                 if self.carbon_labels is not None else MAX_GROUPS)
        if any(index >= count for group in self.coincident_environments for index in group):
            raise ValueError("coincidence coalitions must refer to full environment indices")
        if any(group >= count or peak >= len(self.carbon_shifts) for group, peak in self.fixed_assignments.items()):
            raise ValueError("fixed assignments must address original groups and observations")
        for peak in set(self.fixed_assignments.values()):
            contributors = {group for group, value in self.fixed_assignments.items() if value == peak}
            capacity = self.observation_atom_counts[peak] if self.observation_atom_counts is not None else None
            if capacity is not None and len(contributors) > capacity:
                raise ValueError("fixed assignments exceed an exact observation capacity")
            if capacity is None and len(contributors) > 1 and not any(
                    contributors <= set(coalition) for coalition in self.coincident_environments):
                raise ValueError("shared fixed assignments require a justified coincidence relation")
        return self

    @field_validator("coincident_environments", mode="before")
    @classmethod
    def validate_coincidence_types(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) > MAX_GROUPS:
            raise ValueError("coincidence coalitions must be a bounded sequence")
        seen = set()
        for group in value:
            if (not isinstance(group, (list, tuple)) or not 2 <= len(group) <= MAX_GROUPS
                    or any(type(index) is not int or not 0 <= index < MAX_GROUPS for index in group)
                    or len(set(group)) != len(group) or seen.intersection(group)):
                raise ValueError("coincidence coalitions require disjoint bounded nonboolean integer indices")
            seen.update(group)
        return value

    @field_validator("carbon_groups", mode="before")
    @classmethod
    def validate_group_types(cls, value):
        if (not isinstance(value, (list, tuple)) or len(value) > MAX_GROUPS
                or any(not isinstance(group, (list, tuple)) or len(group) > MAX_ATOMS
                       or any(not isinstance(label, str) for label in group) for group in value)):
            raise ValueError("equivalence groups require bounded original carbon-label sequences")
        return value

    @field_validator("observation_atom_counts", mode="before")
    @classmethod
    def validate_observation_counts(cls, value: Any) -> Any:
        if value is not None:
            if not isinstance(value, (list, tuple)):
                raise ValueError("observation atom counts must be an aligned sequence")
            if any(item is not None and (type(item) is not int or not 1 <= item <= MAX_ATOMS) for item in value):
                raise ValueError("observation atom counts must be positive bounded integers or None")
        return value

    @field_validator("carbon_shifts", mode="before")
    @classmethod
    def validate_shifts(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise ValueError("carbon shifts must be finite numbers in ppm")
        for item in value:
            _number(item)
        return value

    @field_validator("carbon_groups")
    @classmethod
    def validate_groups(cls, groups: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
        seen: set[str] = set()
        if len(groups) > MAX_GROUPS:
            raise ValueError("too many equivalence groups")
        for group in groups:
            if len(group) < 2:
                raise ValueError("explicit equivalence groups need at least two carbon labels")
            for label in group:
                if not re.fullmatch(r"C[1-9][0-9]*", label) or label in seen:
                    raise ValueError("invalid or repeated carbon equivalence label")
                seen.add(label)
        return groups

    @field_validator("fixed_assignments", mode="before")
    @classmethod
    def validate_fixed_types(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("fixed assignments must be an index mapping")
        normalized = {}
        for key, peak in value.items():
            if isinstance(key, str) and re.fullmatch(r"0|[1-9][0-9]*", key):
                key = int(key)
            if type(key) is not int or type(peak) is not int or key in normalized:
                raise ValueError("fixed assignment indices must be integers, not booleans or strings")
            normalized[key] = peak
        return normalized

    @field_validator("fixed_assignments")
    @classmethod
    def validate_fixed(cls, assignments: dict[int, int]) -> dict[int, int]:
        if any(key < 0 or value < 0 for key, value in assignments.items()):
            raise ValueError("assignment indices must be nonnegative")
        return assignments


class CheckResult(_Contract):
    """A score exists only after a valid, complete assessment."""

    compound_id: str
    status: Literal["assessed", "not_applicable", "failed"]
    reason: str | None = None
    score_raw: float | None = Field(default=None, ge=0.0, le=1.0)
    log_score: float | None = Field(default=None, le=0.0)
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)

    @field_validator("score_raw", "log_score", mode="before")
    @classmethod
    def validate_score_types(cls, value: Any) -> Any:
        if value is not None:
            _number(value)
        return value

    @field_validator("timings", mode="before")
    @classmethod
    def validate_timings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("timings must be finite nonnegative numbers")
        for item in value.values():
            _number(item, nonnegative=True)
        return value

    @model_validator(mode="after")
    def validate_assessment(self) -> CheckResult:
        if self.status == "assessed" and self.score_raw is None:
            raise ValueError("an assessed result requires a finite compatibility score")
        if self.status != "assessed" and (self.score_raw is not None or self.log_score is not None):
            raise ValueError("an unassessed result cannot carry a score")
        if self.status != "assessed" and not self.reason:
            raise ValueError("an unassessed result requires a reason")
        _validate_text(self.model_dump(mode="python"))
        try:
            json.dumps(self.model_dump(mode="python"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("results must contain only finite JSON data, including diagnostics") from exc
        return self


class Frame(_Contract):
    """One process message with an explicit run and worker generation."""

    version: Literal[2] = PROTOCOL_VERSION
    kind: Literal["check", "cancel", "shutdown", "self_check", "ready", "progress", "result", "error", "closed"]
    run_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1, strict=True)
    sequence: int = Field(ge=0, strict=True)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("protocol version must be an integer")
        return value


def _number(value: Any, *, nonnegative: bool = False) -> float:
    """Require a genuine finite number instead of coercing protocol data."""
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (nonnegative and value < 0):
        raise ValueError("result fields must be genuine finite numbers in range")
    return float(value)


def _validate_text(value: Any, path: tuple = (), depth: int = 0) -> None:
    """Reject bounded protocol text that cannot safely enter an XML report."""
    if depth > 20:
        raise ValueError("result text nesting exceeds the protocol limit")
    if isinstance(value, str):
        limit = MAX_TEXT_LENGTH
        if path == ("diagnostics", "geometry_sdf"):
            limit = MAX_SDF_LENGTH
        elif path in {
            ('provenance', 'source_smiles'), ('provenance', 'selected_isomer_smiles'),
            ('provenance', 'component_scope', 'original_smiles'),
            ('provenance', 'component_scope', 'scored_fragment_smiles'),
            ('provenance', 'component_scope', 'ignored_components'),
        }:
            limit = MAX_SMILES_LENGTH
        if len(value) > limit or any(not (char in "\t\r\n" or 0x20 <= ord(char) <= 0xD7FF
                or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF) for char in value):
            raise ValueError("result text must be bounded and XML-safe")
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_text(key, (), depth + 1)
            _validate_text(item, (*path, key), depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_text(item, path, depth + 1)


def _carbon_labels(values) -> tuple[str, ...]:
    """Validate bounded original-atom labels without reconstructing chemistry."""
    if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_ATOMS:
        raise ValueError("carbon labels must be a nonempty bounded inventory")
    if any(not isinstance(label, str) or not re.fullmatch(r"C[1-9][0-9]{0,2}", label)
           or int(label[1:]) > MAX_ATOMS for label in values):
        raise ValueError("invalid original carbon label")
    if len(set(values)) != len(values):
        raise ValueError("original carbon labels cannot be repeated")
    return tuple(values)


def _finite_sum(values) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as exc:
        raise ValueError("result diagnostic aggregation exceeds the finite range") from exc
    if not math.isfinite(total):
        raise ValueError("result diagnostic aggregation exceeds the finite range")
    return total


def validate_result_for_request(request: CheckRequest, result: CheckResult, ready: dict, *, cancel_check=None) -> None:
    """Validate terminal schema and request binding; the worker owns chemical analysis."""
    from rdkit import Chem
    from .assignment import AssignmentError, build_groups
    from .observation_policy import BORON_REASON, BORON_WARNING, has_boron
    from checksi.analysis.structure_analysis import coordinate_stereo_scope

    check = cancel_check or (lambda: None)
    check()
    request = CheckRequest.model_validate(request.model_dump(mode='python'))
    result = CheckResult.model_validate(result.model_dump(mode='python'))
    if result.compound_id != request.compound_id:
        raise ValueError("result compound ID does not match the request")
    if ready.get('capabilities') != list(CAPABILITIES):
        raise ValueError('READY does not advertise the required observation capability')
    for name in ("model_id", "score_version", "preprocessing_id", "geometry_policy", "weights_sha256"):
        value = result.provenance.get(name)
        if not isinstance(value, str) or not value or value != ready.get(name):
            raise ValueError(f"result {name} does not match READY")
    if "total" not in result.timings or any(value > result.timings["total"] + 1e-8 for value in result.timings.values()):
        raise ValueError("result timings require a consistent finite total")
    mol = Chem.MolFromSmiles(request.smiles)
    if mol is not None and has_boron(mol):
        if (result.status != 'not_applicable' or result.reason != BORON_REASON
                or result.assignments or result.diagnostics.get('message') != BORON_WARNING):
            raise ValueError('Boron structures require the explicit unsupported warning and no assessment')
    if "geometry_sdf" in result.diagnostics:
        sdf = result.diagnostics["geometry_sdf"]
        if (not request.include_geometry or result.status != "assessed" or not isinstance(sdf, str)
                or not sdf or len(sdf) > MAX_SDF_LENGTH or "\nM  END\n" not in sdf or not sdf.endswith("\n$$$$\n")):
            raise ValueError("geometry SDF must be requested, assessed and a bounded complete text record")
    if result.status != "assessed":
        if result.assignments or not isinstance(result.diagnostics.get("message"), str) or not result.diagnostics["message"]:
            raise ValueError("unassessed results require a diagnostic message and no assignments")
        return
    if mol is None:
        raise ValueError('an assessed request requires a valid original structure')
    if result.reason is not None or any(name not in result.timings for name in (
            "coverage", "geometry", "inference", "assignment", "scoring")):
        raise ValueError("assessed results require complete stage timings and no refusal reason")
    source_atoms = tuple(atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6)
    actual_labels = tuple(f'C{index+1}' for index in source_atoms)
    if request.carbon_labels is not None and request.carbon_labels != actual_labels:
        raise ValueError('the request carbon inventory differs from original structure atoms')
    try:
        expected = [group.labels for group in build_groups(source_atoms, request.carbon_groups)]
    except AssignmentError as exc:
        raise ValueError(str(exc)) from exc
    count, observation_count = len(expected), len(request.carbon_shifts)
    if len(result.assignments) != count:
        raise ValueError('assessed assignments must retain every full environment')
    coordinate_verified = result.provenance.get('coordinate_partition_verified')
    partition_constraint = ('exact-original-atom-partition-v1' if coordinate_verified
                            else 'source-groups-single-isomer-v1')
    if (type(coordinate_verified) is not bool
            or (not coordinate_verified and not result.provenance.get('coordinate_partition_reason'))
            or result.provenance.get('partition_constraint') != partition_constraint):
        raise ValueError('coordinate verification metadata is missing or inconsistent')
    scored_mol = mol
    if len(Chem.GetMolFrags(mol)) > 1:
        from .components import select_scoring_component
        selection = select_scoring_component(mol)
        scored_mol = selection.mol
        scope = result.provenance.get('component_scope')
        if (not isinstance(scope, dict) or not isinstance(scope.get('original_atom_indices'), list)
                or any(type(index) is not int for index in scope['original_atom_indices'])
                or scope != selection.component_scope(request.smiles)):
            raise ValueError('component scope does not match the original submitted structure')
    elif 'component_scope' in result.provenance:
        raise ValueError('a connected structure cannot claim ignored components')
    selected = result.provenance.get('selected_isomer_smiles')
    chosen = Chem.MolFromSmiles(selected) if isinstance(selected, str) and selected else None
    if chosen is None:
        raise ValueError('the selected coordinate isomer is invalid')
    source_graph, chosen_graph = Chem.Mol(scored_mol), Chem.Mol(chosen)
    Chem.RemoveStereochemistry(source_graph)
    Chem.RemoveStereochemistry(chosen_graph)
    source_scope, chosen_scope = coordinate_stereo_scope(scored_mol), coordinate_stereo_scope(chosen)
    if (Chem.MolToSmiles(source_graph) != Chem.MolToSmiles(chosen_graph)
            or not chosen_scope.reference.HasSubstructMatch(source_scope.reference, useChirality=True)):
        raise ValueError('the selected isomer changes the source graph or specified supported stereo')
    stereo_verified = result.provenance.get('stereo_verification_complete')
    if type(stereo_verified) is not bool or (stereo_verified and (
            not source_scope.complete or not chosen_scope.complete
            or any(info.specified != Chem.StereoSpecified.Specified for info in chosen_scope.elements))):
        raise ValueError('coordinate stereo verification scope metadata is inconsistent')
    conditional_keys = {'source_smiles', 'expected_carbon_counts', 'isomer_selection_policy'}
    if result.provenance.get('configuration_dependent') is True:
        counts = result.provenance.get('expected_carbon_counts')
        if (result.provenance.get('source_smiles') != request.smiles
                or not isinstance(counts, list)
                or any(type(value) is not int or not 1 <= value <= MAX_GROUPS for value in counts)
                or result.provenance.get('isomer_selection_policy') not in {
                    'lowest-energy-embedded-isomer-v1', 'first-compatible-original-partition-v1'}):
            raise ValueError('configuration-dependent scope is not bound to the request')
    elif conditional_keys.intersection(result.provenance) or 'configuration_dependent' in result.provenance:
        raise ValueError('conditional scope fields require a configuration-dependent assessment')
    requested_coalitions = {tuple(sorted(group)) for group in request.coincident_environments}
    labels_seen, contributors = set(), {peak: [] for peak in range(observation_count)}
    errors, scaled_errors, percentages, evidence = [], [], [], []
    keys = {"atom_label", "atom_labels", "calculated_shift", "experimental_shift", "observation_index",
            "scaled_error", "score_percent", "atom_probability_percent", "evidence_weight"}
    for index, row in enumerate(result.assignments):
        check()
        if set(row) != keys or not isinstance(row['atom_labels'], list):
            raise ValueError('assignment rows require all full-environment fields')
        labels = _carbon_labels(row['atom_labels'])
        if (labels != tuple(sorted(labels, key=lambda label: int(label[1:])))
                or row['atom_label'] != ','.join(labels) or labels != expected[index]
                or labels_seen.intersection(labels)):
            raise ValueError('assignment rows change original group identity or ordering')
        labels_seen.update(labels)
        observation = row['observation_index']
        if type(observation) is not int or not 0 <= observation < observation_count:
            raise ValueError('every full environment needs an original integer observation index')
        contributors[observation].append(index)
        if index in request.fixed_assignments and request.fixed_assignments[index] != observation:
            raise ValueError('assignment contradicts a fixed original observation')
        experimental = _number(row['experimental_shift'])
        if experimental != request.carbon_shifts[observation]:
            raise ValueError('assignment changes the original indexed experimental value')
        calculated = _number(row['calculated_shift'])
        errors.append(abs(calculated - experimental))
        scaled_errors.append(None if row['scaled_error'] is None else _number(row['scaled_error']))
        weight = _number(row['evidence_weight'])
        if not 0 < weight <= 1:
            raise ValueError('assessed rows cannot omit evidence or inflate its weight')
        evidence.append(weight)
        percent = _number(row['score_percent'], nonnegative=True)
        if percent > 100 or _number(row['atom_probability_percent']) != percent:
            raise ValueError('assignment compatibility aliases disagree')
        percentages.append(percent)
    shared = {}
    for peak, indices in contributors.items():
        if not indices:
            raise ValueError('every original measured observation must be used')
        capacity = request.observation_atom_counts[peak] if request.observation_atom_counts is not None else None
        if capacity is not None and len(indices) != capacity:
            raise ValueError('assignment violates an exact formal-environment capacity')
        if capacity is None and len(indices) > 1 and tuple(indices) not in requested_coalitions:
            raise ValueError('assignment shares an unannotated observation without its justified coalition')
        if any(not math.isclose(evidence[index], 1/len(indices), rel_tol=1e-12, abs_tol=1e-14) for index in indices):
            raise ValueError('assignment weights do not preserve one unit of original measured evidence')
        if len(indices) > 1:
            shared[str(peak)] = indices
    diagnostics = result.diagnostics
    for name, expected_count in (('full_environment_count', count), ('observation_count', observation_count),
                                 ('scored_environment_count', count)):
        if type(diagnostics.get(name)) is not int or diagnostics[name] != expected_count:
            raise ValueError('full/observed/scored diagnostic inventories are inconsistent')
    supplied_shared = diagnostics.get('coincident_observations')
    if (not isinstance(supplied_shared, dict) or any(not isinstance(key, str)
            or re.fullmatch(r'0|[1-9][0-9]*', key) is None or not isinstance(value, list)
            or any(type(index) is not int for index in value) for key, value in supplied_shared.items())
            or supplied_shared != shared):
        raise ValueError('coincident observation identities contradict assignments')
    if (diagnostics.get('score_scope') != ('coincident_observations' if shared else 'complete_spectrum')
            or not isinstance(diagnostics.get('scope_note'), str) or not diagnostics['scope_note']):
        raise ValueError('the score requires truthful measured-evidence scope')
    # Bind redundant report representations; chemical analysis and fitted
    # compatibilities are computed only by the worker.
    objective = _finite_sum(weight * error for weight, error in zip(evidence, errors))
    for name, expected_value in (('raw_mae', objective / observation_count), ('assignment_objective', objective)):
        if not math.isclose(_number(diagnostics.get(name), nonnegative=True), expected_value,
                            rel_tol=1e-12, abs_tol=1e-10):
            raise ValueError('reported absolute errors contradict assignment rows')
    if type(diagnostics.get('assignment_exchangeable')) is not bool:
        raise ValueError('exchangeability diagnostics must be boolean')
    conformers = diagnostics.get("conformer_count")
    if type(conformers) is not int or not 1 <= conformers <= 10:
        raise ValueError("assessed results require a bounded conformer count")
    if ready["geometry_policy"] in ("converged-single-v1", "converged-single-v2") and conformers != 1:
        raise ValueError("the selected single-geometry policy requires exactly one conformer")
    reason = diagnostics.get("regression_reason")
    corrected = diagnostics.get("corrected_mae")
    if corrected is None:
        allowed = {"insufficient_signals", "constant_experimental_shifts", "unidentifiable_slope", "nonfinite_correction"}
        if not isinstance(reason, str) or reason not in allowed or diagnostics.get("scaled_errors") is not None or any(value is not None for value in scaled_errors):
            raise ValueError("unavailable corrected diagnostics require an explicit reason")
    else:
        supplied = diagnostics.get("scaled_errors")
        if not isinstance(supplied, list) or len(supplied) != count:
            raise ValueError("corrected error diagnostics must cover every group")
        supplied = [_number(value) for value in supplied]
        _number(corrected, nonnegative=True)
        if (reason is not None or any(value is None for value in scaled_errors) or supplied != scaled_errors
                or (conformers == 1 and not math.isclose(corrected,
                    _finite_sum(weight * abs(value) for weight, value in zip(evidence, supplied)) / observation_count,
                    rel_tol=1e-12, abs_tol=1e-10))):
            raise ValueError("corrected error diagnostics are inconsistent")
    if observation_count == 1 or len(set(request.carbon_shifts)) == 1:
        expected_reason = "insufficient_signals" if observation_count == 1 else "constant_experimental_shifts"
        if corrected is not None or reason != expected_reason:
            raise ValueError("degenerate regression diagnostics must be explicitly unavailable")
    for name in ("energies_kj", "populations", "fit_residual_rms", "quantile_crossings"):
        values = diagnostics.get(name)
        if not isinstance(values, list) or len(values) != conformers:
            raise ValueError("conformer diagnostics must cover every selected geometry")
        for value in values:
            if name in ("fit_residual_rms", "quantile_crossings"):
                if not isinstance(value, list) or len(value) != count:
                    raise ValueError("fit diagnostics must cover every conformer and group")
                if name == "fit_residual_rms":
                    for residual in value:
                        _number(residual, nonnegative=True)
                elif any(type(crossings) is not int or not 0 <= crossings <= 98 for crossings in value):
                    raise ValueError("invalid quantile crossing counts")
            else:
                _number(value, nonnegative=name == "populations")
    for name in ('fit_locations', 'fit_sigmas'):
        values = diagnostics.get(name)
        if (not isinstance(values, list) or len(values) != conformers
                or any(not isinstance(row, list) or len(row) != count for row in values)):
            raise ValueError('reported distribution fits must retain every conformer and full environment')
        for row in values:
            for value in row:
                number = _number(value)
                if name == 'fit_sigmas' and number <= 0:
                    raise ValueError('reported Gaussian widths must be positive')
    if not math.isclose(_finite_sum(diagnostics["populations"]), 1, abs_tol=1e-12, rel_tol=0):
        raise ValueError("conformer populations must be normalized")
    geometry = diagnostics.get("geometry")
    names = ("retained_count", "eligible_count", "converged_count", "generated_count", "requested_count")
    counts = [geometry.get(name) for name in names] if isinstance(geometry, dict) else []
    if len(counts) != len(names) or any(type(value) is not int or not 1 <= value <= 30 for value in counts) or counts != sorted(counts) or counts[0] != conformers:
        raise ValueError("geometry diagnostics require consistent selected/generated counts")
    if geometry.get("embedding", "deterministic") not in ("deterministic", "random_coordinates"):
        raise ValueError("geometry diagnostics name an unknown embedding procedure")
    score, log_score = result.score_raw, result.log_score
    zero_reason = diagnostics.get("zero_score_reason")
    if score == 0:
        if log_score is None:
            consistent = zero_reason == "tail_beyond_float64"
        else:
            consistent = math.exp(log_score) == 0 and zero_reason == "display_underflow"
    else:
        predicted = math.exp(log_score) if log_score is not None else 0.
        consistent = predicted > 0 and zero_reason is None and math.isclose(
            score, predicted, rel_tol=1e-12, abs_tol=4 * max(math.ulp(score), math.ulp(predicted)))
    if not consistent:
        raise ValueError("raw/log compatibility scores or their zero reasons contradict one another")
    represented = [percent / 100 for percent in percentages]
    if all(value > 0 for value in represented):
        expected_log = math.fsum(weight * math.log(value) for weight, value in zip(evidence, represented)) / observation_count
        rounding = math.fsum(weight * 4 * math.ulp(value) / value for weight, value in zip(evidence, represented)) / observation_count
        if log_score is None or not math.isclose(log_score, expected_log, rel_tol=1e-12, abs_tol=1e-10 + rounding):
            raise ValueError('reported group percentages contradict the aggregate score')


def encode_frame(frame: Frame) -> bytes:
    """Encode exactly one finite JSON object followed by a newline."""
    try:
        raw = (json.dumps(frame.model_dump(mode="python"), ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("worker messages must contain finite JSON data") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("worker frame exceeds the size limit")
    return raw


def decode_frame(raw: bytes) -> Frame:
    """Decode a bounded frame; reject NaN, malformed UTF-8 and extra objects."""
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise ValueError("worker frame is empty or oversized")
    try:
        text = raw.decode("utf-8")
        if not text.endswith("\n") or "\n" in text[:-1]:
            raise ValueError("worker frame must be one complete JSON line")
        def invalid_constant(value: str) -> None:
            raise ValueError(f"nonfinite JSON constant: {value}")
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON object key")
                value[key] = item
            return value
        value = json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_object)
        # Overflowing JSON exponents (1e999) bypass parse_constant.
        json.dumps(value, allow_nan=False)
        return Frame.model_validate(value)
    except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid worker frame") from exc
