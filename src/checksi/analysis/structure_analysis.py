# Standard library imports
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from math import isfinite

# Third-party imports
from rdkit import Chem
from rdkit.Chem import rdDistGeom
from rdkit.Chem.EnumerateStereoisomers import (
    EnumerateStereoisomers,
    GetStereoisomerCount,
    StereoEnumerationOptions,
)


ALLOWED_ELEMENTS = {"C", "F", "Si", "B", "P"}
MAX_SUBSTITUTION_ATOMS = 512
MAX_STEREO_ISOMERS = 8


@dataclass(frozen=True, slots=True)
class SupportedStereoisomers:
    """Bounded stereo completions, with topology-proved exclusions explicit."""

    isomers: tuple[Chem.Mol, ...] = ()
    complete: bool = False
    reason: str | None = None
    rejected_isomer_count: int = 0


@dataclass(frozen=True, slots=True)
class StereoElement:
    """A potential atom/bond element with its appropriate descriptor role."""

    type: str
    centered_on: int
    element: str | None
    specified: str
    descriptor_role: str


@dataclass(frozen=True, slots=True)
class StereoAssessment:
    """Structure-derived meso status, separate from spectral coverage."""

    status: str
    enumeration_complete: bool
    elements: tuple[StereoElement, ...]
    relevant_center_count: int
    rs_center_count: int
    distinct_isomer_count: int | None = None
    reason: str | None = None
    configuration_status: str = "unassessed"

    def to_dict(self) -> dict:
        """Return the JSON-native assessment used by reports and public adapters."""
        return {**asdict(self), "elements": [asdict(element) for element in self.elements]}


@dataclass(frozen=True, slots=True)
class CoincidenceCandidate:
    """Two distinct full environments that may share an observation after review."""

    environments: tuple[tuple[int, ...], ...]
    methine_atoms: tuple[int, ...]
    reason: str = "isopropyl_methyl_coincidence"


@dataclass(frozen=True, slots=True)
class CoordinateCarbonPartition:
    """Complete carbon groups in the supplied geometry's original atom indices."""

    groups: tuple[tuple[int, ...], ...] = ()
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class CoordinateStereoScope:
    """Source stereo that RDKit's 3D perception can independently verify."""

    reference: Chem.Mol = field(repr=False, compare=False)
    elements: tuple[Chem.StereoInfo, ...]
    complete: bool


def coordinate_stereo_scope(mol: Chem.Mol) -> CoordinateStereoScope:
    """Separate perceivable tetrahedral/bond stereo from unverified source tags.

    Three-coordinate N/P, cumulenes, non-tetrahedral and enhanced stereo cannot
    be certified by the coordinate-perception path. The comparison copy omits
    unverified atom tags without changing the original molecule.
    """
    reference = Chem.Mol(mol)
    complete = not bool(mol.GetStereoGroups())

    def supported_atom(atom):
        return (atom.GetChiralTag() in (Chem.ChiralType.CHI_UNSPECIFIED,
                                       Chem.ChiralType.CHI_TETRAHEDRAL_CW,
                                       Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
                and not (atom.GetAtomicNum() in (7, 15) and atom.GetTotalDegree() == 3))

    for atom in reference.GetAtoms():
        if not supported_atom(atom) and atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED:
            atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
            complete = False
    elements = []
    for info in Chem.FindPotentialStereo(Chem.Mol(mol)):
        supported = (info.type == Chem.StereoType.Atom_Tetrahedral
                     and supported_atom(mol.GetAtomWithIdx(info.centeredOn)))
        if info.type == Chem.StereoType.Bond_Double:
            bond = mol.GetBondWithIdx(info.centeredOn)
            supported = all(sum(other.GetBondType() == Chem.BondType.DOUBLE
                                for other in atom.GetBonds()) == 1
                            for atom in (bond.GetBeginAtom(), bond.GetEndAtom()))
        if supported:
            elements.append(info)
        if not supported or info.specified == Chem.StereoSpecified.Unknown:
            complete = False
    supported_bonds = {info.centeredOn for info in elements if info.type == Chem.StereoType.Bond_Double}
    for bond in reference.GetBonds():
        if bond.GetStereo() != Chem.BondStereo.STEREONONE and bond.GetIdx() not in supported_bonds:
            bond.SetStereo(Chem.BondStereo.STEREONONE)
            complete = False
    return CoordinateStereoScope(reference, tuple(elements), complete)


@dataclass
class C13Alternative:
    """A count option; manual coincidences never establish merged atom groups."""

    name: str
    count: int
    groups_dp5: list[list[str]]
    manual_check_only: bool = False


@dataclass(frozen=True, slots=True)
class NucleusPartition:
    """One isomer's proved environments in the original source atom frame."""

    element: str
    groups: tuple[tuple[int, ...], ...]
    diastereotopic_split: bool = False


@dataclass(frozen=True, slots=True)
class IsomerPartition:
    """A complete supported isomer, without serializing and reordering its atoms."""

    smiles: str
    nuclei: tuple[NucleusPartition, ...]
    isomer: Chem.Mol = field(repr=False, compare=False)
    merged_carbon_count: int = 0
    coincidence_candidates: tuple[CoincidenceCandidate, ...] = ()

    def for_element(self, element: str) -> NucleusPartition:
        return next(partition for partition in self.nuclei if partition.element == element)

    @property
    def carbon_groups(self) -> list[list[str]]:
        return [[f'C{index + 1}' for index in group]
                for group in self.for_element('C').groups if len(group) > 1]

@dataclass(frozen=True, slots=True)
class StructureAnalysisResult:
    """Detailed molecule symmetry analysis used by NMR and stereochemistry checks."""

    carbon_count: int
    carbon_groups: list[list[str]]
    stereocenter_count: int
    one_pair_center_count: int
    fluorine_count: int
    silicon_count: int
    boron_count: int
    phosphorus_count: int
    c13_count_alternatives: set[int]
    c13_alternatives: list[C13Alternative]
    stereogenic_signal_flag: bool
    partition_complete: bool = True
    partition_reason: str | None = None
    configuration_status: str = "admissible"
    stereo_assessment: StereoAssessment | None = None
    coincidence_candidates: tuple[CoincidenceCandidate, ...] = ()
    isomer_partitions: tuple[IsomerPartition, ...] = ()

    def expected_counts(self, element: str) -> tuple[int, ...]:
        """Return formal configuration counts, excluding manual coincidences."""
        return tuple(sorted({len(variant.for_element(element).groups)
                             for variant in self.isomer_partitions}))

    def configuration_dependent(self, element: str) -> bool:
        return len({variant.for_element(element).groups for variant in self.isomer_partitions}) > 1

    def legacy_tuple(self):
        """Return the historical analyze_molecular_structure tuple."""
        return (
            self.carbon_count,
            self.stereocenter_count,
            bool(self.one_pair_center_count),
            self.fluorine_count,
            self.silicon_count,
            self.boron_count,
            self.phosphorus_count,
        )

def count_exchangeable_protons(molecule):
    """
    Counts the number of protons attached to nitrogen or oxygen atoms (O-H, N-H).
    These are often exchangeable in NMR and may not appear in the spectrum.
    """
    exchangeable_proton_count = 0
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() in [7, 8]:
            exchangeable_proton_count += atom.GetTotalNumHs()
    return exchangeable_proton_count


def canonical_rank_atoms_no3d(mol):
    """Return graph-based atom symmetry classes without chirality."""
    return Chem.CanonicalRankAtoms(mol, breakTies=False, includeChirality=False)


def substituents_excluding(atom, exclude_idx):
    """Return neighbor atom indices except exclude_idx."""
    return [
        bond.GetOtherAtom(atom).GetIdx()
        for bond in atom.GetBonds()
        if bond.GetOtherAtom(atom).GetIdx() != exclude_idx
    ]


def neighbor_signature(mol, atom, ranks):
    """Return a local atom environment signature using bond order and neighbor rank."""
    signature = []
    for bond in atom.GetBonds():
        neighbor = bond.GetOtherAtom(atom)
        signature.append((bond.GetBondTypeAsDouble(), ranks[neighbor.GetIdx()]))
    return tuple(sorted(signature))


def inequivalent_atoms_by_rank(mol, ranks, extra_tags=None):
    """Group NMR-relevant atoms by graph rank, local signature, and optional geometry tags."""
    if extra_tags is None:
        extra_tags = {}

    groups = {}
    for atom in mol.GetAtoms():
        if atom.GetSymbol() not in ALLOWED_ELEMENTS:
            continue

        idx = atom.GetIdx()
        key = (
            ranks[idx],
            tuple(sorted(extra_tags.get(idx, []))),
            neighbor_signature(mol, atom, ranks),
        )
        groups.setdefault(key, []).append(idx)

    return groups


def summarize_by_element(mol, groups):
    """Return inequivalent atom counts by element."""
    groups_by_element = {element: {} for element in ALLOWED_ELEMENTS}

    for key, atom_indices in groups.items():
        element = mol.GetAtomWithIdx(atom_indices[0]).GetSymbol()
        if element in ALLOWED_ELEMENTS:
            groups_by_element[element][key] = atom_indices

    return {element: len(groups_by_element[element]) for element in ALLOWED_ELEMENTS}


def carbons_with_4_distinct_substituents(mol, include_h=True, one_based=True):
    """Return carbon atoms with exactly four distinct substituent symmetry classes."""
    mol_with_h = Chem.AddHs(mol) if include_h else mol
    ranks = Chem.CanonicalRankAtoms(mol_with_h, breakTies=False, includeChirality=False)
    original_atom_count = mol.GetNumAtoms()

    centers = []
    for atom in mol_with_h.GetAtoms():
        if atom.GetSymbol() != "C":
            continue
        idx = atom.GetIdx()
        if include_h and idx >= original_atom_count:
            continue

        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 4:
            continue

        frequencies = sorted(
            Counter(ranks[neighbor.GetIdx()] for neighbor in neighbors).values(),
            reverse=True,
        )
        if frequencies == [1, 1, 1, 1]:
            centers.append(idx + 1 if one_based else idx)

    return sorted(centers)


def atoms_with_one_pair_among_four(mol, include_h=True, one_based=True, allowed_elements=None):
    """Return atoms with four substituents where exactly one substituent pair is equivalent."""
    mol_with_h = Chem.AddHs(mol) if include_h else mol
    ranks = Chem.CanonicalRankAtoms(mol_with_h, breakTies=False, includeChirality=False)
    original_atom_count = mol.GetNumAtoms()
    allowed = set(allowed_elements) if allowed_elements is not None else None

    centers = []
    for atom in mol_with_h.GetAtoms():
        idx = atom.GetIdx()
        if include_h and idx >= original_atom_count:
            continue
        if allowed is not None and atom.GetSymbol() not in allowed:
            continue

        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 4:
            continue

        frequencies = sorted(
            Counter(ranks[neighbor.GetIdx()] for neighbor in neighbors).values(),
            reverse=True,
        )
        if frequencies == [2, 1, 1]:
            centers.append(idx + 1 if one_based else idx)

    return sorted(centers)


def collect_substituent_branch(mol, start, forbidden):
    """Collect a heavy-atom branch without crossing forbidden atoms."""
    seen = set()
    stack = [start]
    while stack:
        idx = stack.pop()
        if idx in seen or idx in forbidden:
            continue
        seen.add(idx)
        for neighbor in mol.GetAtomWithIdx(idx).GetNeighbors():
            neighbor_idx = neighbor.GetIdx()
            if neighbor.GetAtomicNum() == 1:
                continue
            if neighbor_idx not in seen and neighbor_idx not in forbidden:
                stack.append(neighbor_idx)
    return seen


def diastereotopic_tags_from_preclassified_centers(mol, stereogenic_centers, one_pair_centers):
    """Tag equivalent substituent branches that become diastereotopic near stereogenic centers."""
    if not stereogenic_centers or not one_pair_centers:
        return {}

    mol_with_h = Chem.AddHs(mol)
    ranks = Chem.CanonicalRankAtoms(mol_with_h, breakTies=False, includeChirality=False)
    original_atom_count = mol.GetNumAtoms()
    tags = {}

    for center_1based in one_pair_centers:
        center = center_1based - 1
        atom_with_h = mol_with_h.GetAtomWithIdx(center)
        neighbors = list(atom_with_h.GetNeighbors())
        if len(neighbors) != 4:
            continue

        rank_counts = Counter(ranks[neighbor.GetIdx()] for neighbor in neighbors)
        pair_ranks = [rank for rank, count in rank_counts.items() if count == 2]
        if not pair_ranks:
            continue

        pair_atoms = [
            neighbor.GetIdx()
            for neighbor in neighbors
            if ranks[neighbor.GetIdx()] == pair_ranks[0]
        ]
        pair_heavy_atoms = [idx for idx in pair_atoms if idx < original_atom_count]
        if len(pair_heavy_atoms) != 2:
            continue

        def collect_branch_from_center(start):
            seen = set()
            stack = [start]
            while stack:
                idx = stack.pop()
                if idx in seen:
                    continue
                seen.add(idx)
                for neighbor in mol.GetAtomWithIdx(idx).GetNeighbors():
                    neighbor_idx = neighbor.GetIdx()
                    if neighbor_idx != center and neighbor_idx not in seen:
                        stack.append(neighbor_idx)
            return seen

        branch1 = collect_branch_from_center(pair_heavy_atoms[0])
        branch2 = collect_branch_from_center(pair_heavy_atoms[1])

        if min(branch2) < min(branch1):
            branch1, branch2 = branch2, branch1

        for idx in branch1:
            tags.setdefault(idx, []).append(("diasplit", center, 1))
        for idx in branch2:
            tags.setdefault(idx, []).append(("diasplit", center, 2))

    return tags


def alkene_diastereotopic_tags(mol, ranks):
    """Tag alkene substituent branches that can split equivalent atoms."""
    tags = {}
    unsymmetrical_double_bond = False

    def collect_branch(start, forbidden):
        seen = set()
        stack = [start]
        while stack:
            idx = stack.pop()
            if idx in seen:
                continue
            seen.add(idx)
            for neighbor in mol.GetAtomWithIdx(idx).GetNeighbors():
                neighbor_idx = neighbor.GetIdx()
                if neighbor_idx not in forbidden and neighbor_idx not in seen:
                    stack.append(neighbor_idx)
        return seen

    def connected_without_forbidden(first, second, forbidden):
        seen = set()
        stack = [first]
        while stack:
            idx = stack.pop()
            if idx == second:
                return True
            if idx in seen:
                continue
            seen.add(idx)
            for neighbor in mol.GetAtomWithIdx(idx).GetNeighbors():
                neighbor_idx = neighbor.GetIdx()
                if neighbor_idx in forbidden or neighbor_idx in seen:
                    continue
                stack.append(neighbor_idx)
        return False

    def ring_step_split(start1, start2, center, forbidden):
        branch1 = []
        branch2 = []
        visited = {center}
        current1 = start1
        current2 = start2
        previous1 = center
        previous2 = center

        while True:
            if current1 in visited or current2 in visited:
                break

            branch1.append(current1)
            branch2.append(current2)
            visited.add(current1)
            visited.add(current2)

            def next_ring_atom(current, previous):
                next_atoms = []
                for neighbor in mol.GetAtomWithIdx(current).GetNeighbors():
                    if neighbor.GetAtomicNum() == 1:
                        continue
                    neighbor_idx = neighbor.GetIdx()
                    if neighbor_idx == previous or neighbor_idx in forbidden:
                        continue
                    next_atoms.append(neighbor_idx)
                return next_atoms

            next1 = next_ring_atom(current1, previous1)
            next2 = next_ring_atom(current2, previous2)
            if len(next1) != 1 or len(next2) != 1:
                break

            candidate1 = next1[0]
            candidate2 = next2[0]
            if candidate1 in visited or candidate2 in visited:
                break

            previous1, current1 = current1, candidate1
            previous2, current2 = current2, candidate2

        return set(branch1), set(branch2)

    for bond in mol.GetBonds():
        if bond.GetBondType() != Chem.BondType.DOUBLE:
            continue

        first = bond.GetBeginAtomIdx()
        second = bond.GetEndAtomIdx()
        if mol.GetAtomWithIdx(first).GetSymbol() != "C":
            continue
        if mol.GetAtomWithIdx(second).GetSymbol() != "C":
            continue

        first_subs = substituents_excluding(mol.GetAtomWithIdx(first), second)
        second_subs = substituents_excluding(mol.GetAtomWithIdx(second), first)
        if len(first_subs) != 2 or len(second_subs) != 2:
            continue

        first_same = ranks[first_subs[0]] == ranks[first_subs[1]]
        second_same = ranks[second_subs[0]] == ranks[second_subs[1]]
        if first_same and not second_same:
            unsymmetrical_double_bond = True
            same_side = first_subs
            same_center = first
            other_center = second
        elif second_same and not first_same:
            unsymmetrical_double_bond = True
            same_side = second_subs
            same_center = second
            other_center = first
        else:
            continue

        side1, side2 = same_side
        forbidden = {same_center, other_center}
        cyclic_case = connected_without_forbidden(side1, side2, forbidden)

        if cyclic_case:
            branch1, branch2 = ring_step_split(side1, side2, same_center, forbidden)
            if not branch1 or not branch2:
                continue
        else:
            branch1 = collect_branch(side1, forbidden)
            branch2 = collect_branch(side2, forbidden)
            if branch1 & branch2:
                continue

        if min(branch2) < min(branch1):
            branch1, branch2 = branch2, branch1

        core = set(branch1) | set(branch2) | {same_center, other_center}
        for idx in branch1:
            tags.setdefault(idx, []).append(("alkene_diasplit", bond.GetIdx(), 1))
        for idx in branch2:
            tags.setdefault(idx, []).append(("alkene_diasplit", bond.GetIdx(), 2))

        for branch_atoms, side_label in ((branch1, 1), (branch2, 2)):
            for atom_idx in branch_atoms:
                atom = mol.GetAtomWithIdx(atom_idx)
                for neighbor in atom.GetNeighbors():
                    if neighbor.GetAtomicNum() == 1:
                        continue
                    neighbor_idx = neighbor.GetIdx()
                    if neighbor_idx in core:
                        continue

                    side_branch = collect_substituent_branch(mol, neighbor_idx, forbidden=core)
                    for tagged_idx in side_branch:
                        tags.setdefault(tagged_idx, []).append(
                            ("alkene_diasplit", bond.GetIdx(), side_label)
                        )

    return tags, unsymmetrical_double_bond

def converter_base_groups_to_dp5(
    mol,
    base_groups,
    element_symbol: str = "C",
    include_singletons: bool = False,
):
    """
    Convert atom groups from inequivalent_atoms_by_rank(...) to DP5-style labels.

    Example
    -------
    Input:
        {
            some_key_1: [2, 6],
            some_key_2: [3, 5],
        }

    Output:
        [
            ["C3", "C7"],
            ["C4", "C6"],
        ]

    Parameters
    ----------
    mol
        RDKit molecule.

    base_groups
        Output of inequivalent_atoms_by_rank(...).
        Usually a dictionary in the form:
            {signature_key: [atom_idx, ...]}

    element_symbol
        Element symbol to keep in the output.
        Default is "C".

    include_singletons
        If False, groups with only one atom are skipped.
        If True, single-atom groups are included.

    Returns
    -------
    list[list[str]]
        DP5-style atom groups.
        Example:
            [["C3", "C7"], ["C4", "C6"]]
    """

    dp5_groups_with_sort_keys = []

    for atom_indices in base_groups.values():
        # Sort atom indices inside each equivalence group.
        atom_indices = sorted(atom_indices)

        # Keep only atoms with the requested element symbol.
        filtered_indices = [
            atom_idx
            for atom_idx in atom_indices
            if mol.GetAtomWithIdx(atom_idx).GetSymbol() == element_symbol
        ]

        # Skip groups that do not contain the requested element.
        if not filtered_indices:
            continue

        # Skip one-atom groups unless singletons are explicitly requested.
        if not include_singletons and len(filtered_indices) < 2:
            continue

        # Convert zero-based RDKit atom indices to one-based DP5-style labels.
        dp5_group = [
            f"{element_symbol}{atom_idx + 1}"
            for atom_idx in filtered_indices
        ]

        # Store the first atom index as a sorting key.
        dp5_groups_with_sort_keys.append(
            (filtered_indices[0], dp5_group)
        )

    # Sort groups by the first atom index in each group.
    dp5_groups_with_sort_keys.sort(key=lambda item: item[0])

    # Remove sorting keys and return only DP5-style groups.
    base_group_dp5 = [
        dp5_group
        for _, dp5_group in dp5_groups_with_sort_keys
    ]

    return base_group_dp5



def _legacy_structure_analysis(mol) -> StructureAnalysisResult:
    """Analyze atom equivalence with stereogenic and alkene split alternatives."""
    Chem.SanitizeMol(mol)

    stereocenters = carbons_with_4_distinct_substituents(mol, include_h=True)
    one_pair_centers = atoms_with_one_pair_among_four(
        mol,
        include_h=True,
        allowed_elements=ALLOWED_ELEMENTS,
    )
    ranks = canonical_rank_atoms_no3d(mol)
    base_groups = inequivalent_atoms_by_rank(mol, ranks)
    base_groups_dp5 = converter_base_groups_to_dp5(mol, base_groups)
    base_counts = summarize_by_element(
        mol,
        base_groups,
    )

    diastereotopic_tags = diastereotopic_tags_from_preclassified_centers(
        mol,
        stereocenters,
        one_pair_centers,
    )
    diastereotopic_groups = inequivalent_atoms_by_rank(mol, ranks, diastereotopic_tags)
    diastereotopic_groups_dp5 = converter_base_groups_to_dp5(mol, diastereotopic_groups)
    diastereotopic_counts = summarize_by_element(
        mol,
        diastereotopic_groups,
    )

    alkene_tags, unsymmetrical_double_bond = alkene_diastereotopic_tags(mol, ranks)
    stereogenic_signal_flag = bool(stereocenters or unsymmetrical_double_bond)
    combined_diastereotopic_tags = diastereotopic_tags_from_preclassified_centers(
        mol,
        [1] if stereogenic_signal_flag else [],
        one_pair_centers,
    )
    merged_tags = {}
    for tag_map in (combined_diastereotopic_tags, alkene_tags):
        for atom_idx, atom_tags in tag_map.items():
            merged_tags.setdefault(atom_idx, []).extend(atom_tags)
    merged_groups = inequivalent_atoms_by_rank(mol, ranks, merged_tags)
    merged_groups_dp5 = converter_base_groups_to_dp5(mol, merged_groups)
    merged_counts = summarize_by_element(
        mol,
        merged_groups,
    )

    alkene_plus_stereocenter_tags = {}
    for tag_map in (diastereotopic_tags, alkene_tags):
        for atom_idx, atom_tags in tag_map.items():
            alkene_plus_stereocenter_tags.setdefault(atom_idx, []).extend(atom_tags)
    alkene_plus_stereocenter_groups = inequivalent_atoms_by_rank(mol, ranks, alkene_plus_stereocenter_tags)
    alkene_plus_stereocenter_groups_dp5 = converter_base_groups_to_dp5(mol, alkene_plus_stereocenter_groups)
    alkene_plus_stereocenter_counts = summarize_by_element(
        mol,
        alkene_plus_stereocenter_groups,
    )

    c13_alternatives = [
        C13Alternative(
            name="base",
            count=base_counts["C"],
            groups_dp5=base_groups_dp5,
        ),
        C13Alternative(
            name="diastereotopic",
            count=diastereotopic_counts["C"],
            groups_dp5=diastereotopic_groups_dp5,
        ),
        C13Alternative(
            name="alkene_plus_stereocenter",
            count=alkene_plus_stereocenter_counts["C"],
            groups_dp5=alkene_plus_stereocenter_groups_dp5,
        ),
        
    ]

    c13_count_alternatives = {
        alternative.count
        for alternative in c13_alternatives
    }
    
    
    
    
    return StructureAnalysisResult(
        carbon_count=merged_counts["C"],
        carbon_groups = merged_groups_dp5,
        stereocenter_count=len(stereocenters),
        one_pair_center_count=len(one_pair_centers),
        fluorine_count=merged_counts["F"],
        silicon_count=merged_counts["Si"],
        boron_count=merged_counts["B"],
        phosphorus_count=merged_counts["P"],
        c13_count_alternatives=c13_count_alternatives,
        c13_alternatives = c13_alternatives,
        stereogenic_signal_flag=stereogenic_signal_flag,
    )


def _stereo_atoms(mol: Chem.Mol) -> set[int]:
    """Return atoms belonging to potential tetrahedral and double-bond stereo."""
    atoms = set()
    for info in Chem.FindPotentialStereo(Chem.Mol(mol)):
        if info.type == Chem.StereoType.Atom_Tetrahedral:
            atoms.add(info.centeredOn)
        elif info.type == Chem.StereoType.Bond_Double:
            bond = mol.GetBondWithIdx(info.centeredOn)
            atoms.update((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
    return atoms


def _isomer_substitution_keys(mol: Chem.Mol) -> dict[int, str] | None:
    """Label atoms on a bounded conformer, comparing each product to its mirror."""
    mol3d = Chem.AddHs(mol)
    parameters = rdDistGeom.ETKDGv3()
    parameters.randomSeed = 42
    parameters.numThreads = 1
    # Limit native attempts, not elapsed time: CPU load must not choose a
    # different scientific model by expiring a wall-clock deadline.
    parameters.maxIterations = 100
    parameters.timeout = 0
    try:
        if rdDistGeom.EmbedMolecule(mol3d, parameters) < 0:
            parameters.useRandomCoords = True
            if rdDistGeom.EmbedMolecule(mol3d, parameters) < 0:
                return None
    except (RuntimeError, ValueError):
        return None

    return _coordinate_substitution_keys(mol3d)


def _coordinate_substitution_keys(
    mol3d: Chem.Mol, *, require_assigned: bool = False,
) -> dict[int, str]:
    """Measure substitution identity on existing coordinates, without embedding."""

    isotopes = {atom.GetIsotope() for atom in mol3d.GetAtoms()}
    marker = next(value for value in range(1000, 65536) if value not in isotopes)
    keys = {}
    for atom in mol3d.GetAtoms():
        if atom.GetSymbol() not in ALLOWED_ELEMENTS:
            continue
        labelled = Chem.Mol(mol3d)
        labelled.GetAtomWithIdx(atom.GetIdx()).SetIsotope(marker)
        # Input stereo is retained; only newly stereogenic elements need tags.
        Chem.AssignStereochemistryFrom3D(labelled, replaceExistingTags=False)
        if require_assigned and any(
            info.specified != Chem.StereoSpecified.Specified
            for info in Chem.FindPotentialStereo(labelled)
        ) and not _has_unique_exact_stereo_completion(labelled):
            raise ValueError("The coordinates did not determine every substitution stereo element.")
        labelled = Chem.RemoveHs(labelled)
        mirror = Chem.Mol(labelled)
        for mirror_atom in mirror.GetAtoms():
            if mirror_atom.GetChiralTag() in (
                Chem.ChiralType.CHI_TETRAHEDRAL_CW,
                Chem.ChiralType.CHI_TETRAHEDRAL_CCW,
            ):
                mirror_atom.InvertChirality()
        keys[atom.GetIdx()] = min(Chem.MolToSmiles(labelled), Chem.MolToSmiles(mirror))
    return keys


def _has_unique_exact_stereo_completion(mol: Chem.Mol) -> bool:
    """Discharge only residual candidates with one complete exact isomeric identity.

    Coordinate assignment can expose conditional ring candidates that do not
    distinguish any completed structure. Distinct enantiomers and E/Z forms
    remain distinct here: mirror equivalence is applied only after this proof.
    """
    if any(info.type not in (Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double)
           or info.specified == Chem.StereoSpecified.Unknown
           for info in Chem.FindPotentialStereo(Chem.Mol(mol))):
        return False
    options = StereoEnumerationOptions(onlyUnassigned=True, unique=True, maxIsomers=0)
    if GetStereoisomerCount(mol, options) > MAX_STEREO_ISOMERS:
        return False
    identities = {Chem.MolToSmiles(Chem.RemoveHs(isomer), isomericSmiles=True)
                  for isomer in EnumerateStereoisomers(mol, options)}
    return len(identities) == 1


def carbon_partition_from_coordinates(
    mol: Chem.Mol, conformer_id: int = -1,
) -> CoordinateCarbonPartition:
    """Measure a full partition on actual coordinates; never repair atom mapping.

    The caller must separately establish physical admissibility and retain only
    finite, converged, input-stereo-preserving prediction geometries. Comparing
    this partition with a source partition requires the exact original atom sets,
    not just their count. No new conformer is generated and the input is unchanged.
    """
    if mol.GetNumConformers() == 0:
        return CoordinateCarbonPartition(reason="missing_geometry")
    if len(Chem.GetMolFrags(mol)) != 1 or mol.GetStereoGroups() or any(
        info.type not in (Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double)
        or info.specified == Chem.StereoSpecified.Unknown
        for info in Chem.FindPotentialStereo(Chem.Mol(mol))
    ):
        return CoordinateCarbonPartition(reason="unsupported_representation")
    if not coordinate_stereo_scope(mol).complete:
        return CoordinateCarbonPartition(reason="unsupported_coordinate_stereo")
    try:
        actual = Chem.Mol(mol, confId=conformer_id)
        if not actual.GetConformer().Is3D():
            return CoordinateCarbonPartition(reason="not_3d_geometry")
        if not all(isfinite(value) for point in actual.GetConformer().GetPositions()
                   for value in point):
            return CoordinateCarbonPartition(reason="nonfinite_geometry")
        actual = Chem.AddHs(actual, addCoords=True)
        if actual.GetNumAtoms() > MAX_SUBSTITUTION_ATOMS:
            return CoordinateCarbonPartition(reason="atom_limit")
        keys = _coordinate_substitution_keys(actual, require_assigned=True)
    except (RuntimeError, ValueError, OverflowError):
        return CoordinateCarbonPartition(reason="coordinate_assessment_failed")
    groups: dict[str, list[int]] = {}
    for index, key in keys.items():
        if actual.GetAtomWithIdx(index).GetAtomicNum() == 6:
            groups.setdefault(key, []).append(index)
    return CoordinateCarbonPartition(tuple(sorted(
        (tuple(sorted(group)) for group in groups.values()),
    )))


def _bridge_311_constraints(mol: Chem.Mol, centers: list[int]) -> list[tuple[int, ...]]:
    """Locate two one-atom bridges and one three-atom bridge between centres."""
    adjacent = {atom.GetIdx(): {n.GetIdx() for n in atom.GetNeighbors()
                               if n.GetAtomicNum() != 1} for atom in mol.GetAtoms()}
    constraints = []
    for position, first in enumerate(centers):
        for second in centers[position + 1:]:
            if any(mol.GetAtomWithIdx(index).GetAtomicNum() != 6 for index in (first, second)):
                continue
            short = sorted(adjacent[first] & adjacent[second])
            if len(short) != 2 or second in adjacent[first]:
                continue
            if any(mol.GetAtomWithIdx(index).GetAtomicNum() != 6 for index in short):
                continue
            if any(mol.GetBondBetweenAtoms(end, middle).GetBondType() != Chem.BondType.SINGLE
                   for end in (first, second) for middle in short):
                continue
            paths, pending = [], [(first,)]
            while pending:
                path = pending.pop()
                if path[-1] == second:
                    if len(path) == 5:
                        paths.append(path)
                    continue
                if len(path) < 5:
                    pending.extend(path + (n,) for n in adjacent[path[-1]]
                                   if n not in path and n not in short)
            if len(paths) == 1:
                constraints.append((first, second, *short, paths[0][1], paths[0][-2]))
    return constraints


def _bridge_handedness(mol: Chem.Mol, center: int, long: int, short: tuple[int, int]) -> int:
    atom = mol.GetAtomWithIdx(center)
    order = [n.GetIdx() for n in atom.GetNeighbors()]
    if len(order) == 3:
        order.append(-1)  # RDKit's implicit tetrahedral H follows the neighbours.
    canonical = [long, *short]
    fourth = [index for index in order if index not in canonical]
    if len(order) != 4 or len(fourth) != 1:
        return 0
    canonical += fourth
    indices = [canonical.index(index) for index in order]
    parity = -1 if sum(a > b for i, a in enumerate(indices) for b in indices[i + 1:]) % 2 else 1
    tags = {Chem.ChiralType.CHI_TETRAHEDRAL_CW: 1, Chem.ChiralType.CHI_TETRAHEDRAL_CCW: -1}
    return parity * tags.get(atom.GetChiralTag(), 0)


def _enumerate_supported_stereoisomers(mol: Chem.Mol) -> SupportedStereoisomers:
    """Enumerate completely within budget; embedding never proves impossibility."""
    mol = Chem.Mol(mol)
    # Enhanced relative/mixture stereo and non-tetrahedral stereo need a separate
    # equivalence policy; do not replace them with one arbitrary 3D arrangement.
    infos = tuple(Chem.FindPotentialStereo(Chem.Mol(mol)))
    if len(Chem.GetMolFrags(mol)) != 1 or mol.GetStereoGroups() or any(
        atom.GetChiralTag() not in (Chem.ChiralType.CHI_UNSPECIFIED,
                                   Chem.ChiralType.CHI_TETRAHEDRAL_CW,
                                   Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
        for atom in mol.GetAtoms()
    ) or any(
        info.type not in (Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double)
        or info.specified == Chem.StereoSpecified.Unknown
        for info in infos
    ):
        return SupportedStereoisomers(reason="unsupported_representation")
    options = StereoEnumerationOptions(onlyUnassigned=True, unique=True, maxIsomers=0)
    if GetStereoisomerCount(mol, options) > MAX_STEREO_ISOMERS:
        return SupportedStereoisomers(reason="enumeration_budget")
    constraints = _bridge_311_constraints(mol, [info.centeredOn for info in infos
                                               if info.type == Chem.StereoType.Atom_Tetrahedral])
    accepted, rejected = [], 0
    for isomer in EnumerateStereoisomers(mol, options):
        # In bicyclo[3.1.1] the two long-bridge bonds must leave the same face
        # of the four-membered bridge ring. Canonical neighbour orders are
        # [long, short-a, short-b, fourth] at both ends, hence opposite signs.
        if any(_bridge_handedness(isomer, a, long_a, (x, y))
               == _bridge_handedness(isomer, b, long_b, (x, y)) != 0
               for a, b, x, y, long_a, long_b in constraints):
            rejected += 1
        else:
            accepted.append(isomer)
    return SupportedStereoisomers(tuple(accepted), True,
                                 "physically_inadmissible" if not accepted and rejected else None,
                                 rejected)


def enumerate_supported_stereoisomers(mol: Chem.Mol) -> SupportedStereoisomers:
    """Return an explicit unassessed result when bounded stereo enumeration fails."""
    try:
        return _enumerate_supported_stereoisomers(mol)
    except (RuntimeError, ValueError, OverflowError):
        return SupportedStereoisomers(reason="stereo_assessment_failed")


def _stereo_inventory(mol: Chem.Mol) -> tuple[StereoElement, ...]:
    ranks = Chem.CanonicalRankAtoms(mol, breakTies=False, includeChirality=False)
    elements = []
    for info in Chem.FindPotentialStereo(Chem.Mol(mol)):
        symbol, role = None, "unsupported"
        if info.type == Chem.StereoType.Atom_Tetrahedral:
            atom = mol.GetAtomWithIdx(info.centeredOn)
            symbol, role = atom.GetSymbol(), "R/S"
            neighbors = [("H", n.GetIsotope()) if n.GetAtomicNum() == 1 else ("rank", ranks[n.GetIdx()])
                         for n in atom.GetNeighbors()]
            neighbors += [("H", 0)] * atom.GetTotalNumHs()
            if symbol == "C" and len(set(neighbors)) < 4:
                role = "relative_ring" if atom.IsInRing() else "coupled_tetrahedral"
        elif info.type == Chem.StereoType.Bond_Double:
            role = "E/Z"
        elements.append(StereoElement(str(info.type), info.centeredOn, symbol,
                                     str(info.specified), role))
    return tuple(elements)


def assess_molecular_stereochemistry(
    mol: Chem.Mol, enumeration: SupportedStereoisomers | None = None,
) -> StereoAssessment:
    """Compare complete supported stereo forms with their mirrors, without a spectrum."""
    elements, centers, rs_centers = (), 0, 0
    try:
        elements = _stereo_inventory(mol)
        centers = sum(e.type == "Atom_Tetrahedral" for e in elements)
        rs_centers = sum(e.descriptor_role == "R/S" for e in elements)
        enumeration = enumeration or enumerate_supported_stereoisomers(mol)
        if not enumeration.complete or enumeration.reason:
            return StereoAssessment("unassessed", enumeration.complete, elements, centers, rs_centers,
                                    reason=enumeration.reason,
                                    configuration_status=("inadmissible" if enumeration.reason == "physically_inadmissible"
                                                          else "unassessed"))
        count = len(enumeration.isomers)
        if not centers:
            return StereoAssessment("achiral_no_centres", True, elements, 0, 0, count,
                                    configuration_status="admissible")
        meso = False
        for isomer in enumeration.isomers:
            mirror = Chem.Mol(isomer)
            for atom in mirror.GetAtoms():
                if atom.GetChiralTag() in (Chem.ChiralType.CHI_TETRAHEDRAL_CW,
                                          Chem.ChiralType.CHI_TETRAHEDRAL_CCW):
                    atom.InvertChirality()
            meso |= (Chem.MolToSmiles(isomer, isomericSmiles=True)
                     == Chem.MolToSmiles(mirror, isomericSmiles=True))
        specified = all(e.specified == "Specified" for e in elements)
        status = ("confirmed_meso" if meso else "confirmed_non_meso") if specified else (
            "meso_possible" if meso else "no_meso_form")
        return StereoAssessment(status, True, elements, centers, rs_centers, count,
                                configuration_status="admissible")
    except (RuntimeError, ValueError, OverflowError):
        return StereoAssessment("unassessed", False, elements, centers, rs_centers,
                                reason="stereo_assessment_failed")


def _incomplete_structure_analysis(
    mol: Chem.Mol, reason: str, stereo: StereoAssessment,
) -> StructureAnalysisResult:
    """Retain the count estimate without certifying a fallback scoring partition."""
    legacy = _legacy_structure_analysis(mol)
    alternatives = [replace(option, groups_dp5=[], manual_check_only=True)
                    if option.count < legacy.carbon_count else option
                    for option in legacy.c13_alternatives]
    return replace(legacy, c13_alternatives=alternatives, partition_complete=False,
                   partition_reason=reason, configuration_status=stereo.configuration_status,
                   stereo_assessment=stereo, stereocenter_count=stereo.relevant_center_count)


def isopropyl_methyl_coincidences(
    mol: Chem.Mol, carbon_groups: Sequence[Sequence[int]],
) -> tuple[CoincidenceCandidate, ...]:
    """Identify justified iPr coalitions without merging their original atom sets."""
    carbons = {atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6}
    flat = [index for group in carbon_groups for index in group]
    if (mol.GetNumAtoms() > MAX_SUBSTITUTION_ATOMS or any(not group for group in carbon_groups)
            or any(type(index) is not int for index in flat)
            or len(flat) != len(set(flat)) or set(flat) != carbons):
        raise ValueError("Coincidence candidates require complete disjoint original carbon groups within budget.")
    groups = {index: tuple(sorted(group)) for group in carbon_groups for index in group}

    def hydrogen_count(atom):
        return atom.GetTotalNumHs() + sum(n.GetAtomicNum() == 1 for n in atom.GetNeighbors())

    candidates: dict[tuple[tuple[int, ...], ...], list[int]] = {}
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 6 or hydrogen_count(atom) != 1:
            continue
        methyls = [n.GetIdx() for n in atom.GetNeighbors()
                   if n.GetAtomicNum() == 6 and hydrogen_count(n) == 3
                   and sum(other.GetAtomicNum() != 1 for other in n.GetNeighbors()) == 1]
        if len(methyls) != 2:
            continue
        environments = tuple(sorted({groups[index] for index in methyls}))
        if len(environments) == 2:
            candidates.setdefault(environments, []).append(atom.GetIdx())
    return tuple(CoincidenceCandidate(environments, tuple(sorted(methines)))
                 for environments, methines in sorted(candidates.items()))


def analyze_molecular_structure_detailed(mol: Chem.Mol) -> StructureAnalysisResult:
    """Retain complete nucleus partitions for every bounded admissible isomer."""
    mol = Chem.Mol(mol)
    Chem.SanitizeMol(mol)
    enumeration = enumerate_supported_stereoisomers(mol)
    stereo = assess_molecular_stereochemistry(mol, enumeration)
    if not enumeration.complete or enumeration.reason:
        return _incomplete_structure_analysis(mol, enumeration.reason or "stereo_assessment_incomplete", stereo)
    ranks = canonical_rank_atoms_no3d(mol)
    base_groups = inequivalent_atoms_by_rank(mol, ranks)
    stereo_atoms = _stereo_atoms(mol)
    # Isotope substitution can reveal coupled prochirality even when the input
    # has no stereo element or remote carbonyl/alkene (the (iPr)2CH–OR family).
    needs_substitution = any(len(group) > 1 for group in base_groups.values())
    variants = []
    for isomer in enumeration.isomers:
        keys = _isomer_substitution_keys(isomer) if needs_substitution else None
        if needs_substitution and keys is None:
            return _incomplete_structure_analysis(mol, "substitution_incomplete", stereo)
        formal_groups, merged_groups, split_elements = {}, {}, set()
        for base_key, indices in base_groups.items():
            split = {}
            for idx in indices:
                split.setdefault(keys[idx] if keys is not None else base_key, []).append(idx)
            for key, atoms in split.items():
                formal_groups[(base_key, key)] = atoms
            if len(split) > 1:
                split_elements.add(mol.GetAtomWithIdx(indices[0]).GetSymbol())
            if len(split) > 1 and not stereo_atoms.intersection(indices):
                merged_groups[base_key] = list(indices)
            else:
                for key, atoms in split.items():
                    merged_groups[(base_key, key)] = atoms
        nuclei = tuple(NucleusPartition(element, tuple(sorted(
            tuple(sorted(atoms)) for atoms in formal_groups.values()
            if mol.GetAtomWithIdx(atoms[0]).GetSymbol() == element)), element in split_elements)
            for element in sorted(ALLOWED_ELEMENTS))
        carbon = next(partition for partition in nuclei if partition.element == 'C')
        variants.append(IsomerPartition(
            Chem.MolToSmiles(Chem.RemoveHs(isomer), isomericSmiles=True), nuclei, Chem.Mol(isomer),
            summarize_by_element(mol, merged_groups)['C'],
            isopropyl_methyl_coincidences(mol, carbon.groups)
            if mol.GetNumAtoms() <= MAX_SUBSTITUTION_ATOMS else (),
        ))
    # Keep the old consensus representative (the final enumerated form). Only
    # genuinely different partitions introduce deterministic isomer selection.
    varying = any(len({variant.for_element(element).groups for variant in variants}) > 1
                  for element in ALLOWED_ELEMENTS)
    if varying:
        variants.sort(key=lambda variant: variant.smiles)
    selected = variants[0] if varying else variants[-1]
    counts = {partition.element: len(partition.groups) for partition in selected.nuclei}
    merged_count = selected.merged_carbon_count
    carbon_groups = selected.carbon_groups
    alternatives = [C13Alternative("formal", counts["C"], carbon_groups)]
    if merged_count != counts["C"]:
        alternatives.append(C13Alternative(
            "diastereotopic_merged", merged_count, [], manual_check_only=True,
        ))
    return StructureAnalysisResult(
        carbon_count=counts["C"], carbon_groups=carbon_groups,
        stereocenter_count=stereo.relevant_center_count,
        one_pair_center_count=len(atoms_with_one_pair_among_four(mol, allowed_elements=ALLOWED_ELEMENTS)),
        fluorine_count=counts["F"], silicon_count=counts["Si"],
        boron_count=counts["B"], phosphorus_count=counts["P"],
        c13_count_alternatives=({alternative.count for alternative in alternatives}
                                | {len(variant.for_element('C').groups) for variant in variants}),
        c13_alternatives=alternatives, stereogenic_signal_flag=merged_count != counts["C"],
        stereo_assessment=stereo,
        coincidence_candidates=selected.coincidence_candidates,
        isomer_partitions=tuple(variants),
    )


def analyze_molecular_structure(molecule):
    """
    Analyze a molecule and return the historical seven-value tuple.

    For richer C13/stereochemistry diagnostics, use
    analyze_molecular_structure_detailed().
    """
    return analyze_molecular_structure_detailed(molecule).legacy_tuple()
