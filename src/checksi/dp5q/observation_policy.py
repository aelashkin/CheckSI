"""Original-atom structural facts shared by preparation, worker and parent."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Collection, Sequence

from rdkit import Chem

from checksi.analysis.structure_analysis import StructureAnalysisResult, analyze_molecular_structure_detailed
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.registry import ERROR_TEMPLATES

from .assignment import AssignmentError, CarbonGroup, build_groups
from .contracts import MAX_ATOMS, MAX_EDGES, MAX_GROUPS

BORON_REASON = 'boron_not_supported'
BORON_WARNING = ERROR_TEMPLATES[AnalysisErrorCode.DP5_BORON_NOT_SUPPORTED]['default']


@dataclass(frozen=True)
class ObservationPolicy:
    """Full source-frame groups and justified relations; no scoring decision."""

    groups: tuple[CarbonGroup, ...]
    coincident_environments: tuple[tuple[int, ...], ...]
    direct_boron_environments: tuple[int, ...]
    contains_boron: bool
    partition_complete: bool
    partition_reason: str | None
    configuration_status: str
    configuration_dependent: bool = False
    expected_counts: tuple[int, ...] = ()
    selected_isomer_smiles: str | None = None
    selected_isomer: Chem.Mol | None = field(default=None, repr=False, compare=False)
    carbon_configuration_dependent: bool = False

    @property
    def embedding_selected(self) -> bool:
        """Retain one computable form when the source stereo partition is unproved."""
        return not self.partition_complete


@dataclass(frozen=True)
class BoronDiagnosticPolicy:
    """Whole-source diagnostic evidence, independent of scoring applicability."""

    groups: tuple[CarbonGroup, ...]
    direct_environments: tuple[int, ...]
    direct_atom_pairs: tuple[tuple[int, int], ...]
    source_fragments: tuple[tuple[int, ...], ...]
    carbon_component_original_indices: tuple[int, ...]
    partition_complete: bool
    partition_reason: str | None
    configuration_status: str


def derive_boron_diagnostic_policy(
    mol: Chem.Mol, carbon_groups: Sequence[Sequence[str]] | None = None, *,
    structure: StructureAnalysisResult | None = None, cancel_check: Callable[[], None] | None = None,
) -> BoronDiagnosticPolicy:
    """Transport a single carbon component's proved groups without removing ions.

    This diagnostic view cannot authorize prediction or scoring. Every retained
    group is compared as a full original atom set, never by count alone.
    """
    check = cancel_check or (lambda: None)
    native = derive_observation_policy(mol, carbon_groups, structure=structure, cancel_check=check)
    check()
    infos = tuple(Chem.FindPotentialStereo(Chem.Mol(mol)))
    unsupported_stereo = (bool(mol.GetStereoGroups()) or any(
        info.specified == Chem.StereoSpecified.Unknown
        or info.type not in (Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double)
        for info in infos))
    mappings = []
    fragments = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, fragsMolAtomMapping=mappings)
    mappings = tuple(tuple(mapping) for mapping in mappings)
    carbon_components = [index for index, fragment in enumerate(fragments)
                         if any(atom.GetAtomicNum() == 6 for atom in fragment.GetAtoms())]
    pairs = tuple((atom.GetIdx(), neighbor.GetIdx()) for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6
                  for neighbor in atom.GetNeighbors() if neighbor.GetAtomicNum() == 5)
    component_map = mappings[carbon_components[0]] if len(carbon_components) == 1 else ()

    def result(complete, reason, status=native.configuration_status):
        sites = {carbon for carbon, _ in pairs}
        direct = tuple(index for index, group in enumerate(native.groups)
                       if set(group.atom_indices) <= sites) if complete else ()
        return BoronDiagnosticPolicy(native.groups, direct, pairs, mappings, component_map,
                                     complete, reason, status)

    if not native.contains_boron:
        return result(False, 'boron_absent')
    if native.carbon_configuration_dependent:
        return result(False, 'configuration_dependent_partition')
    if unsupported_stereo:
        return result(False, 'unsupported_diagnostic_stereo')
    if len(carbon_components) != 1:
        return result(False, 'multiple_carbon_components')
    direct_sites = {carbon for carbon, _ in pairs}
    if any(set(group.atom_indices) & direct_sites and not set(group.atom_indices) <= direct_sites
           for group in native.groups):
        return result(False, 'mixed_direct_environment')
    if native.partition_complete:
        return result(True, None)
    # A single-fragment refusal is a genuine source-partition boundary, rather
    # than the disconnected counterion seam this view can prove independently.
    if len(fragments) == 1:
        return result(False, native.partition_reason)
    check()
    component = derive_observation_policy(fragments[carbon_components[0]], cancel_check=check)
    if not component.partition_complete:
        return result(False, component.partition_reason, component.configuration_status)
    if component.carbon_configuration_dependent:
        return result(False, 'configuration_dependent_partition', component.configuration_status)
    transported = {frozenset(component_map[index] for index in group.atom_indices)
                   for group in component.groups}
    if transported != {frozenset(group.atom_indices) for group in native.groups}:
        return result(False, 'diagnostic_partition_mismatch')
    check()
    return result(True, None, component.configuration_status)


def has_boron(mol: Chem.Mol) -> bool:
    """Recognize applicability before any geometry or prediction work."""
    return any(atom.GetAtomicNum() == 5 for atom in mol.GetAtoms())


def derive_observation_policy(
    mol: Chem.Mol,
    carbon_groups: Sequence[Sequence[str]] | None = None,
    *,
    structure: StructureAnalysisResult | None = None,
    cancel_check: Callable[[], None] | None = None,
    allowed_elements: Collection[int] | None = None,
) -> ObservationPolicy:
    """Bind a request to the frozen native producer partition without relabeling.

    Supplied explicit outer ordering is retained. A hand-defined alternative
    frame is not silently transported to this native source frame. Direct-C–B
    group identities are diagnostic facts only; all boron scoring is skipped.
    """
    check = cancel_check or (lambda: None)
    check()
    if not isinstance(mol, Chem.Mol):
        raise AssignmentError('invalid_structure', 'A complete parsed molecule is required.')
    if mol.GetNumAtoms() > MAX_ATOMS or mol.GetNumBonds() > MAX_EDGES:
        raise AssignmentError('atom_limit', 'The source structure exceeds the bounded scoring domain.')
    if any(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()):
        raise AssignmentError('unsupported_element', 'Unspecified atom types cannot justify observation policy.')
    if allowed_elements is not None and any(atom.GetAtomicNum() not in allowed_elements for atom in mol.GetAtoms()):
        raise AssignmentError('unsupported_element', 'The structure lies outside the supplied prediction element domain.')
    atoms = tuple(atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6)
    if not atoms:
        raise AssignmentError('no_carbon', 'No original carbon inventory is available.')
    supplied = None
    if carbon_groups is not None:
        if (not isinstance(carbon_groups, (list, tuple)) or len(carbon_groups) > MAX_GROUPS
                or any(not isinstance(group, (list, tuple)) or len(group) < 2
                       or any(not isinstance(label, str) or re.fullmatch(r'C[1-9][0-9]{0,2}', label) is None
                              for label in group) for group in carbon_groups)):
            raise AssignmentError('invalid_equivalence_groups', 'Equivalence groups require bounded original carbon-label sequences.')
        supplied = tuple(tuple(group) for group in carbon_groups)
        build_groups(atoms, supplied)  #Inventory/duplicate/group-budget proof before analysis.
    check()
    try:
        explicit = Chem.AddHs(Chem.Mol(mol))
    except (RuntimeError, ValueError) as exc:
        raise AssignmentError('invalid_structure', 'The explicit-H source structure could not be prepared.') from exc
    if explicit.GetNumAtoms() > MAX_ATOMS or explicit.GetNumBonds() > MAX_EDGES:
        raise AssignmentError('atom_limit', 'The explicit-H source structure exceeds the bounded scoring domain.')
    if structure is not None and not isinstance(structure, StructureAnalysisResult):
        raise AssignmentError('invalid_request', 'Source partition metadata has an invalid type.')
    check()
    source = structure if structure is not None else analyze_molecular_structure_detailed(Chem.Mol(mol))
    check()
    groups = build_groups(atoms, source.carbon_groups if supplied is None else supplied)
    status = (source.stereo_assessment.configuration_status if source.stereo_assessment is not None
              else source.configuration_status)
    if not source.partition_complete:
        # Disconnected structures remain unscored; their separate boron
        # diagnostic view must retain its existing fragment-analysis semantics.
        if supplied is not None and len(Chem.GetMolFrags(mol)) == 1:
            native = build_groups(atoms, source.carbon_groups)
            if {frozenset(group.atom_indices) for group in groups} != {
                    frozenset(group.atom_indices) for group in native}:
                raise AssignmentError('source_partition_mismatch',
                                      'Requested groups differ from the original source fallback partition.')
        return ObservationPolicy(groups, (), (), has_boron(mol), False, source.partition_reason, status,
                                 configuration_dependent=True)
    identities = {frozenset(group.atom_indices) for group in groups}
    variants = [variant for variant in source.isomer_partitions
                if {frozenset(group) for group in variant.for_element('C').groups} == identities]
    conditional = any(source.configuration_dependent(element) for element in ('C', 'F', 'Si', 'B', 'P'))
    selected = variants[0] if variants else None
    # A source representative describes another possible form. Its metadata
    # must be exact, but its group budget cannot reject a smaller selected form.
    native_groups = {frozenset(group) for group in source.carbon_groups}
    native = next((variant for variant in source.isomer_partitions
                   if {frozenset(group) for group in variant.carbon_groups} == native_groups), None)
    if (native is None or len(native.for_element('C').groups) != source.carbon_count
            or selected is None or len(groups) != len(selected.for_element('C').groups)):
        raise AssignmentError('source_partition_mismatch',
                              'Requested groups differ from every complete native source-frame isomer partition.')
    lookup = {frozenset(group.atom_indices): index for index, group in enumerate(groups)}
    coalitions = []
    for candidate in selected.coincidence_candidates:
        check()
        try:
            indices = tuple(sorted(lookup[frozenset(environment)] for environment in candidate.environments))
        except KeyError as exc:
            raise AssignmentError('source_partition_mismatch', 'A coincidence candidate lost its original full environment.') from exc
        if len(indices) >= 2 and len(set(indices)) == len(indices) and indices not in coalitions:
            coalitions.append(indices)
    members = [index for coalition in coalitions for index in coalition]
    if len(members) != len(set(members)):
        raise AssignmentError('unsupported_coincidence_policy', 'Structural coincidence coalitions overlap.')
    direct_sites = {atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6
                    and any(neighbor.GetAtomicNum() == 5 for neighbor in atom.GetNeighbors())}
    direct = tuple(index for index, group in enumerate(groups) if set(group.atom_indices) <= direct_sites)
    return ObservationPolicy(groups, tuple(coalitions), direct, has_boron(mol), True, None, status,
                             conditional, source.expected_counts('C'),
                             selected.smiles if conditional else None,
                             Chem.Mol(selected.isomer) if conditional else None,
                             source.configuration_dependent('C'))
