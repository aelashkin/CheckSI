"""NumPy evaluation of the bundled CASCADE DFTNN proton-shift model."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import numpy as np
from rdkit import Chem

from checksi.dp5q.geometry import GeometryError, prepare_geometry

WEIGHTS_SHA256 = 'd7b71e8f7526af7c4161a7fc9574ce342e5682bce74ab897858c4fdc2f881688'
# Atom tokens of the published CASCADE preprocessor; other elements are refused.
TOKENS = {6: 2, 8: 3, 1: 4, 7: 5, 16: 6, 17: 7, 9: 8, 15: 9}
# Br was not in the training set. It is replaced by Cl before geometry generation:
# alpha-H shifts of chloro and bromo analogues differ by about 0.1 ppm, whereas the
# Cl token on a longer C-Br bond under-predicts CH2Br by up to 0.6 ppm.
PROXY_ELEMENTS = {35: 17}
SUPPORTED_ELEMENTS = frozenset(Chem.GetPeriodicTable().GetElementSymbol(z) for z in (*TOKENS, *PROXY_ELEMENTS))
MAX_ATOMS = 150
CUTOFF = 5.0


class DomainError(ValueError):
    """The structure lies outside the model's supported domain."""


@dataclass(frozen=True)
class ProtonEnvironment:
    """Topologically equivalent C-H protons and their averaged predicted shift."""

    shift: float
    count: int
    atom_indices: tuple[int, ...]
    near_partners: int = 99
    far_partners: int = 99
    must_split: bool = False
    geminal_methyls: bool = False
    # Name of a proton class for which the model is known to be systematically wrong ('' = none).
    unreliable: str = ''
    # Couplings the signal must show at least: ortho protons of a benzene-type CH (each 7-9 Hz).
    min_partners: int = 0


@lru_cache(maxsize=1)
def load_weights() -> dict[str, np.ndarray]:
    """Load and verify the bundled float32 parameters once per process."""
    raw = resources.files('checksi.h1shift').joinpath('data/weights.npz').read_bytes()
    if hashlib.sha256(raw).hexdigest() != WEIGHTS_SHA256:
        raise RuntimeError('The bundled 1H model weights failed their checksum.')
    import io
    with np.load(io.BytesIO(raw)) as data:
        return {key: data[key].astype(np.float32) for key in data.files}


def _dense(weights, values, name, softplus=False):
    output = values @ weights[name + '.kernel']
    bias = weights.get(name + '.bias')
    if bias is not None:
        output = output + bias
    return np.logaddexp(np.float32(0), output) if softplus else output


def _graph(positions: np.ndarray):
    distance = np.linalg.norm(positions[:, None] - positions[None], axis=-1)
    pairs = []
    for receiver, row in enumerate(distance):
        senders = row.argsort()[1:int((row < CUTOFF).sum())]
        pairs.extend((receiver, int(sender)) for sender in (senders if len(senders) else [receiver]))
    connectivity = np.array(pairs, dtype=np.int64)
    lengths = distance[connectivity[:, 0], connectivity[:, 1]].astype(np.float32)
    rbf = np.exp(-((lengths[:, None] - .1 * np.arange(256)) ** 2) / .1).astype(np.float32)
    return connectivity, rbf


def forward(weights, tokens: np.ndarray, positions: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Predict shifts (ppm) of ``targets`` in one explicit-H geometry."""
    connectivity, bond = _graph(positions)
    receiver, sender = connectivity.T
    atom = weights['atom_embedding.embeddings'][tokens]
    for first in (1, 8, 15):
        atom = _dense(weights, atom, f'dense_{first}')
        source = atom[sender]
        edge = np.concatenate((source, atom[receiver], bond), axis=1)
        edge = _dense(weights, edge, f'dense_{first + 1}', True)
        edge = _dense(weights, edge, f'dense_{first + 2}')
        edge = _dense(weights, edge, f'dense_{first + 3}', True)
        bond = _dense(weights, edge, f'dense_{first + 4}', True) + bond
        messages = np.zeros_like(atom)
        np.add.at(messages, receiver, source * bond)
        update = _dense(weights, messages, f'dense_{first + 5}', True)
        atom = atom + _dense(weights, update, f'dense_{first + 6}')
    values = atom[targets]
    for name in ('dense_22', 'dense_23', 'dense_24'):
        values = _dense(weights, values, name, True)
    shift = weights['atomwise_shift.embeddings'][tokens[targets]]
    return (_dense(weights, values, 'dense_25') + shift).reshape(-1)


def unsupported_elements(mol: Chem.Mol) -> list[str]:
    """Element symbols the proton model was not trained on."""
    return sorted({atom.GetSymbol() for atom in mol.GetAtoms() if atom.GetSymbol() not in SUPPORTED_ELEMENTS})


def predict_environments(smiles: str, cancel_check=None) -> list[ProtonEnvironment]:
    """Boltzmann-average C-H shifts over the DP5q conformer ensemble, then over equivalent protons.

    Geometries come from the same ETKDGv3/MMFF94s generator as DP5q
    (``prepare_geometry`` with the bounded ensemble: 30 seeded candidates,
    converged and stereo-preserving, 10 kJ/mol window, 0.5 A RMSD, at most ten).
    """
    parsed = Chem.MolFromSmiles(smiles)
    if parsed is None:
        raise DomainError('the SMILES could not be parsed')
    if len(Chem.GetMolFrags(parsed)) != 1:
        raise DomainError('multi-component structures are not supported')
    unknown = unsupported_elements(parsed)
    if unknown:
        raise DomainError('unsupported element(s): ' + ', '.join(unknown))
    parsed = Chem.RWMol(parsed)
    for atom in parsed.GetAtoms():
        atom.SetAtomicNum(PROXY_ELEMENTS.get(atom.GetAtomicNum(), atom.GetAtomicNum()))
    proxy = parsed.GetMol()
    if Chem.AddHs(proxy).GetNumAtoms() > MAX_ATOMS:
        raise DomainError(f'more than {MAX_ATOMS} atoms')
    try:
        geometry = prepare_geometry(proxy, ensemble=True, cancel_check=cancel_check, num_threads=0)
    except GeometryError as exc:
        raise DomainError(str(exc)) from exc
    mol = geometry.mol
    targets = np.array([atom.GetIdx() for atom in mol.GetAtoms()
                        if atom.GetAtomicNum() == 1 and atom.GetNeighbors()[0].GetAtomicNum() == 6], dtype=np.int64)
    if not len(targets):
        return []
    tokens = np.array([TOKENS[atom.GetAtomicNum()] for atom in mol.GetAtoms()], dtype=np.int64)
    weights = load_weights()
    predictions = np.array([forward(weights, tokens, mol.GetConformer(cid).GetPositions(), targets)
                            for cid in geometry.conformer_ids])
    averaged = (predictions * geometry.populations[:, None]).sum(0)
    if not np.isfinite(averaged).all():
        raise DomainError('the model produced nonfinite predictions')
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    groups: dict[int, list[int]] = {}
    for position, atom_index in enumerate(targets):
        groups.setdefault(ranks[atom_index], []).append(position)
    topology = Chem.GetDistanceMatrix(mol)
    # Structural classes are read from a freshly perceived copy: geometry preparation can leave
    # aromaticity flags of fused heteroaromatic rings unset on its molecule. Atom order is the same.
    reference = Chem.AddHs(Chem.Mol(proxy))
    if reference.GetNumAtoms() != mol.GetNumAtoms():
        reference = mol
    environments = []
    for members in sorted(groups.values(), key=lambda m: targets[m[0]]):
        near, far, split = _coupling_capacity(mol, topology, int(targets[members[0]]))
        environments.append(ProtonEnvironment(float(averaged[members].mean()), len(members),
                                              tuple(int(targets[m]) for m in members), near, far, split,
                                              _single_carbon_methyls(mol, [int(targets[m]) for m in members]),
                                              unreliable_class(reference, int(targets[members[0]])),
                                              ortho_protons(reference, int(targets[members[0]]))))
    return environments


# Free distance, in ppm, for the proton classes named by ``unreliable_class``: the largest
# systematic error seen for the class on reviewed correct data, plus 0.2 ppm.
UNRELIABLE_ALLOWANCES = {
    'alpha_sulfonyl': 1.5,      # CH-SO2 4.5 against 3.2 predicted
    'vinyl_sulfonyl': 1.5,      # HC=C-SO2 7.2 against 6.2
    'n_fluorovinyl': 1.9,       # N-CH=CF 5.3 against 7.0
    'aminal': 3.0,              # N-CH-N 4.5 against 7.3
    'strained_cage': 2.7,       # cage CH2 1.5 against 4.0
}


def unreliable_class(mol: Chem.Mol, proton: int) -> str:
    """Proton classes with a systematic model error of 1 ppm or more on correct literature data.

    Established on reviewed SI sets (Org. Lett. and JOC, 2026-11): the same proton type was
    mispredicted by nearly the same amount across a whole series. The class is a property of
    one proton, so the other protons of the compound are still compared.
    """
    carbon = mol.GetAtomWithIdx(proton).GetNeighbors()[0]
    heavy = [n for n in carbon.GetNeighbors() if n.GetAtomicNum() != 1]

    def sulfonyl(atom) -> bool:
        return atom.GetAtomicNum() == 16 and sum(
            1 for bond in atom.GetBonds() if bond.GetBondTypeAsDouble() == 2.0
            and bond.GetOtherAtom(atom).GetAtomicNum() in (7, 8)) >= 2

    if any(sulfonyl(atom) for atom in heavy):
        return 'alpha_sulfonyl'                   # CH-SO2 and CH-S(O)=N: predicted about 1 ppm too low
    for bond in carbon.GetBonds():
        other = bond.GetOtherAtom(carbon)
        if (bond.GetBondTypeAsDouble() == 2.0 and not bond.GetIsAromatic() and other.GetAtomicNum() == 6
                and any(sulfonyl(n) for n in other.GetNeighbors())):
            return 'vinyl_sulfonyl'               # HC=C-SO2: predicted about 1 ppm too low
    if not carbon.GetIsAromatic() and sum(1 for atom in heavy if atom.GetAtomicNum() == 7) >= 2:
        return 'aminal'                           # N-CH-N: predicted up to 3 ppm too high
    if any(atom.GetAtomicNum() == 7 for atom in heavy):
        for bond in carbon.GetBonds():
            other = bond.GetOtherAtom(carbon)
            if (bond.GetBondTypeAsDouble() == 2.0 and other.GetAtomicNum() == 6 and not bond.GetIsAromatic()
                    and any(n.GetAtomicNum() == 9 for n in other.GetNeighbors())):
                return 'n_fluorovinyl'            # N-CH=CF: predicted about 1.6 ppm too high
    ring = mol.GetRingInfo()
    if ring.IsAtomInRingOfSize(carbon.GetIdx(), 3) or (
            ring.NumAtomRings(carbon.GetIdx()) >= 2 and any(ring.IsAtomInRingOfSize(n.GetIdx(), 3) for n in heavy)):
        return 'strained_cage'                    # cyclopropane CH and CH/CH2 of a cage fused to it
    return ''


def exchangeable_protons(smiles: str) -> int:
    """Protons on heteroatoms (NH, OH, SH), which the model does not predict."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return 0
    return sum(atom.GetTotalNumHs() for atom in mol.GetAtoms() if atom.GetAtomicNum() not in (1, 6))


def _single_carbon_methyls(mol: Chem.Mol, protons: list[int]) -> bool:
    """Whether a 3n-proton group is methyls on one carbon, which may be diastereotopic singlets."""
    carbons = {mol.GetAtomWithIdx(p).GetNeighbors()[0].GetIdx() for p in protons}
    if len(protons) % 3 or len(carbons) < 2:
        return False
    if any(mol.GetAtomWithIdx(c).GetTotalNumHs(includeNeighbors=True) != 3 for c in carbons):
        return False
    anchors = {frozenset(n.GetIdx() for n in mol.GetAtomWithIdx(c).GetNeighbors() if n.GetAtomicNum() != 1)
               for c in carbons}
    return len(anchors) == 1


def ortho_protons(mol: Chem.Mol, proton: int) -> int:
    """Protons ortho to a CH of a six-membered aromatic ring; each gives a resolved 7-9 Hz coupling."""
    carbon = mol.GetAtomWithIdx(proton).GetNeighbors()[0]
    ring = mol.GetRingInfo()
    if not carbon.GetIsAromatic() or not ring.IsAtomInRingOfSize(carbon.GetIdx(), 6):
        return 0
    return sum(1 for neighbor in carbon.GetNeighbors()
               if neighbor.GetIsAromatic() and neighbor.GetAtomicNum() == 6
               and mol.GetBondBetweenAtoms(carbon.GetIdx(), neighbor.GetIdx()).GetIsAromatic()
               and neighbor.GetTotalNumHs(includeNeighbors=True) > 0)


def _coupling_capacity(mol: Chem.Mol, topology: np.ndarray, proton: int) -> tuple[int, int, bool]:
    """Generous upper bounds on coupling partners, and whether a methyl must be split.

    Near partners can give J >= 5 Hz: protons two or three bonds away (geminal
    partners except inside a methyl) and F/P up to four bonds. Far partners also
    include protons up to five bonds and F/P up to six bonds away. A methyl bonded
    to a carbon that carries a proton always shows a vicinal 3J of about 7 Hz.
    """
    carbon = mol.GetAtomWithIdx(proton).GetNeighbors()[0]
    methyl = carbon.GetTotalNumHs(includeNeighbors=True) == 3
    near = far = 0
    for atom in mol.GetAtoms():
        index, number = atom.GetIdx(), atom.GetAtomicNum()
        bonds = topology[proton, index]
        if number == 1 and index != proton:
            if methyl and atom.GetNeighbors()[0].GetIdx() == carbon.GetIdx():
                continue
            near += bonds in (2, 3)
            far += 2 <= bonds <= 5
        elif number in (9, 15):
            near += bonds <= 4
            far += bonds <= 6
    split = methyl and any(neighbor.GetAtomicNum() == 6 and neighbor.GetTotalNumHs(includeNeighbors=True) > 0
                           for neighbor in carbon.GetNeighbors())
    return int(near), int(far), bool(split)
