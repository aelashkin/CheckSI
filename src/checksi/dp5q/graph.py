"""Build the supported explicit-hydrogen spatial graph used by DP5q."""
from dataclasses import dataclass
from typing import Mapping

import numpy as np
from rdkit import Chem


class GraphDomainError(ValueError):
    """A graph lies outside the frozen model's supported preprocessing domain."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MolecularGraph:
    """One geometry, with original carbon indices and receiver/sender edges."""

    atom_tokens: np.ndarray
    atom_indices: np.ndarray
    atom_index: np.ndarray
    connectivity: np.ndarray
    distances: np.ndarray
    distance_rbf: np.ndarray


def validate_elements(mol: Chem.Mol, tokens: Mapping[int, int]) -> None:
    """Refuse absent trained tokens before expensive coordinate preparation."""
    unsupported = sorted({atom.GetSymbol() for atom in mol.GetAtoms()
                          if atom.GetAtomicNum() not in tokens})
    if unsupported:
        raise GraphDomainError('unsupported_element', 'Elements absent from the trained vocabulary: '
                               + ', '.join(unsupported) + '.')


def build_graph(
    mol: Chem.Mol,
    tokens: Mapping[int, int],
    *,
    max_atoms: int = 512,
    max_edges: int = 32768,
) -> MolecularGraph:
    """Match the legacy normal domain; refuse its ambiguous allocation cases.

    The caller supplies a geometry with explicit H. This function never modifies
    the molecule, constructs a conformer, or adds atoms. Limits bound the dense
    distance matrix and edge features before their allocation.
    """
    if mol is None or mol.GetNumAtoms() == 0:
        raise GraphDomainError('empty_molecule', 'The molecule has no atoms.')
    count = mol.GetNumAtoms()
    if count > max_atoms:
        raise GraphDomainError('atom_limit', f'The geometry exceeds {max_atoms} atoms.')
    if mol.GetNumConformers() == 0:
        raise GraphDomainError('missing_geometry', 'The molecule has no geometry.')
    positions = np.asarray(mol.GetConformer().GetPositions(), dtype=np.float64)
    if not np.isfinite(positions).all():
        raise GraphDomainError('nonfinite_geometry', 'Coordinates must be finite.')
    atoms = list(mol.GetAtoms())
    if any(atom.GetNumImplicitHs() or atom.GetNumExplicitHs() for atom in atoms):
        raise GraphDomainError('implicit_hydrogens', 'All hydrogens must be explicit atoms.')
    validate_elements(mol, tokens)
    atom_tokens = np.array([tokens[atom.GetAtomicNum()] for atom in atoms], dtype=np.int64)
    if np.any((atom_tokens < 2) | (atom_tokens >= 13)):
        raise GraphDomainError('invalid_token', 'The trained token index is invalid.')
    targets = np.array([atom.GetIdx() for atom in atoms if atom.GetAtomicNum() == 6], dtype=np.int64)
    if not len(targets):
        raise GraphDomainError('no_carbon', 'The geometry has no carbon atoms.')
    # force=True avoids reusing RDKit's cached matrix after coordinate mutation.
    distance = Chem.Get3DDistanceMatrix(mol, force=True)
    if np.any((distance == 0) & ~np.eye(count, dtype=bool)):
        raise GraphDomainError('coincident_atoms', 'Distinct atoms have coincident coordinates.')
    within = (distance < 5.) & (distance != 0.)
    neighbors = within.sum(axis=1)
    if np.any(neighbors > 100):
        raise GraphDomainError('neighbor_limit', 'An atom has more than 100 neighbors within 5 angstrom.')
    if count > 1 and np.any(neighbors == 0):
        raise GraphDomainError('isolated_atom', 'An isolated atom is unsupported in a multi-atom geometry.')
    edge_count = max(1, int(neighbors.sum()))
    if edge_count > max_edges:
        raise GraphDomainError('edge_limit', f'The geometry exceeds {max_edges} directed edges.')
    connectivity = np.empty((edge_count, 2), dtype=np.int64)
    cursor = 0
    for receiver in range(count):
        # Preserve the frozen NumPy default argsort and receiver-major order.
        sender_indices = distance[receiver].argsort()[1:1 + int(neighbors[receiver])]
        if count == 1:
            sender_indices = np.array([receiver])
        stop = cursor + len(sender_indices)
        connectivity[cursor:stop, 0] = receiver
        connectivity[cursor:stop, 1] = sender_indices
        cursor = stop
    distances = distance[connectivity[:, 0], connectivity[:, 1]].astype(np.float32)
    # Legacy stores distances float32, expands with float64 centers, then Keras
    # casts its Input to float32. Keep that sequence rather than rounding early.
    centers = .1 * np.arange(256)
    rbf = np.exp(-((distances[:, None] - centers) ** 2) / .1).astype(np.float32)
    target_map = np.full(count, -1, dtype=np.int64)
    target_map[targets] = np.arange(len(targets), dtype=np.int64)
    return MolecularGraph(atom_tokens, targets, target_map, connectivity, distances, rbf)
