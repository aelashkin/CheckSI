"""Select a sole carbon-bearing component without changing its chemistry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from rdkit import Chem

from .assignment import build_groups

ORGANIC_COMPONENT_WARNING = (
    'DP5q calculated for the organic component only; the counter-ion/solvate was ignored, '
    'and protonation or salt formation may shift some 13C signals.'
)
MULTIPLE_CARBON_COMPONENTS = 'DP5q not performed: more than one component contains carbon.'


class ComponentSelectionError(ValueError):
    """The complete source has no unique carbon-bearing scoring component."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class ScoringComponent:
    """A fragment and its unchanged positions in the complete source molecule."""

    mol: Chem.Mol = field(repr=False, compare=False)
    original_atom_indices: tuple[int, ...]
    ignored_components: tuple[str, ...]

    def component_scope(self, original_smiles: str) -> dict:
        """Describe exactly which charged/protonated input fragment was scored."""
        return {'original_smiles': original_smiles,
                'scored_fragment_smiles': Chem.MolToSmiles(self.mol, isomericSmiles=True),
                'original_atom_indices': list(self.original_atom_indices),
                'ignored_components': list(self.ignored_components)}

    def project_groups(self, original_groups: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
        """Map validated original carbon labels into the fragment's atom frame."""
        original_carbons = [self.original_atom_indices[atom.GetIdx()]
                            for atom in self.mol.GetAtoms() if atom.GetAtomicNum() == 6]
        build_groups(original_carbons, original_groups)
        labels = {f'C{original + 1}': f'C{index + 1}'
                  for index, original in enumerate(self.original_atom_indices)}
        return tuple(tuple(labels[label] for label in group) for group in original_groups)

    def original_labels(self, atom_indices: Sequence[int]) -> tuple[str, ...]:
        """Transport fragment carbon identities back to original source labels."""
        return tuple(f'C{self.original_atom_indices[index] + 1}' for index in atom_indices)


def select_scoring_component(mol: Chem.Mol) -> ScoringComponent:
    """Keep the sole carbon component, preserving input atom order and charges.

    Boron applicability is intentionally checked against the complete source by
    callers before this selection. No peak is allocated between components.
    """
    mappings = []
    fragments = Chem.GetMolFrags(Chem.Mol(mol), asMols=True, fragsMolAtomMapping=mappings)
    carbon_components = [index for index, fragment in enumerate(fragments)
                         if any(atom.GetAtomicNum() == 6 for atom in fragment.GetAtoms())]
    if len(carbon_components) > 1:
        raise ComponentSelectionError('multiple_carbon_components', MULTIPLE_CARBON_COMPONENTS)
    if not carbon_components:
        raise ComponentSelectionError('no_carbon', 'The source structure contains no carbon-bearing component.')
    selected = carbon_components[0]
    return ScoringComponent(fragments[selected], tuple(mappings[selected]),
        tuple(Chem.MolToSmiles(fragment, isomericSmiles=True) for index, fragment in enumerate(fragments)
              if index != selected))
