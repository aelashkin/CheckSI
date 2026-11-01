"""Narrow structural exclusions for parent NMR presence and C–F coupling."""

from rdkit import Chem


_COUNTERION_GRAPHS = frozenset(Chem.MolToSmiles(Chem.MolFromSmiles(smiles)) for smiles in (
    'F[B-](F)(F)F',
    'F[P-](F)(F)(F)(F)F',
    'O=S(=O)([O-])C(F)(F)F',
))


def counterion_only_nmr_elements(mol: Chem.Mol | None) -> frozenset[str]:
    """Return F/B/P found only in exact BF4/PF6/OTf anions beside an organic cation.

    A positive carbon-bearing parent is required. An isolated inorganic salt or
    a neutral organic solvent beside it cannot justify this diagnostic scope.
    The complete molecule and every formula/count producer remain unchanged.
    """
    if mol is None:
        return frozenset()
    fragments = Chem.GetMolFrags(mol, asMols=True)
    counterions = {index for index, fragment in enumerate(fragments)
                  if Chem.MolToSmiles(fragment) in _COUNTERION_GRAPHS}
    if not counterions:
        return frozenset()
    parents = [fragment for index, fragment in enumerate(fragments) if index not in counterions]
    if not any(Chem.GetFormalCharge(fragment) > 0
               and any(atom.GetAtomicNum() == 6 for atom in fragment.GetAtoms()) for fragment in parents):
        return frozenset()
    parent_elements = {atom.GetSymbol() for fragment in parents for atom in fragment.GetAtoms()}
    counterion_elements = {atom.GetSymbol() for index in counterions for atom in fragments[index].GetAtoms()}
    return frozenset((counterion_elements - parent_elements) & {'F', 'B', 'P'})
