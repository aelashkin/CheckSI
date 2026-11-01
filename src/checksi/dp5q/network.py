"""Fixed float32 NumPy encoder and the independent mean/quantile heads."""
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Any
from time import perf_counter

import numpy as np
from rdkit import Chem

from .assets import load_assets
from .graph import MolecularGraph, build_graph


@dataclass(frozen=True)
class Prediction:
    """Unaveraged predictions in the molecule's original carbon index order."""

    atom_indices: np.ndarray
    mean: np.ndarray
    quantiles: np.ndarray
    intermediates: dict[str, np.ndarray] | None = None
    timings: dict[str, float] | None = None


class ModelBundle:
    """Immutable once-loaded parameters; one shared encoder per geometry."""

    def __init__(self, weights: Mapping[str, np.ndarray], metadata: Mapping[str, Any]) -> None:
        self.weights = weights
        self.metadata = metadata
        self.tokens = {int(key): value for key, value in metadata['tokens'].items()}

    def _dense(self, values: np.ndarray, name: str, softplus: bool = False) -> np.ndarray:
        output = values @ self.weights[name + '.kernel']
        bias = self.weights.get(name + '.bias')
        if bias is not None:
            output += bias
        if softplus:
            np.logaddexp(np.float32(0), output, out=output)
        return output

    def _encode(self, graph: MolecularGraph, capture: dict | None) -> tuple[np.ndarray, np.ndarray]:
        atom = self.weights['shared.atom_embedding'][graph.atom_tokens]
        shift = self.weights['shared.atomwise_shift'][graph.atom_tokens][graph.atom_indices]
        bond = graph.distance_rbf
        if capture is not None:
            capture['embedding'] = atom.copy()
        receiver, sender = graph.connectivity.T
        for block in range(3):
            names = ['shared.' + ('dense' if i == 0 else f'dense_{i}') for i in range(block * 7, block * 7 + 7)]
            atom = self._dense(atom, names[0])
            if capture is not None:
                capture[f'block_{block+1}.projected_atom'] = atom.copy()
            source = atom[sender]
            edge = np.concatenate((source, atom[receiver], bond), axis=1)
            edge = self._dense(edge, names[1], True)
            edge = self._dense(edge, names[2])
            edge = self._dense(edge, names[3], True)
            edge = self._dense(edge, names[4], True)
            bond = edge + bond
            messages = np.zeros_like(atom)
            # add.at retains repeated receiver contributions; indexed += loses them.
            np.add.at(messages, receiver, source * bond)
            if capture is not None:
                capture[f'block_{block+1}.bond_state'] = bond.copy()
                capture[f'block_{block+1}.messages'] = messages.copy()
            update = self._dense(messages, names[5], True)
            update = self._dense(update, names[6])
            atom = atom + update
            if capture is not None:
                capture[f'block_{block+1}.atom_state'] = atom.copy()
        # Each target is one carbon. Equivalence groups are averaged after heads.
        representations = atom[graph.atom_indices]
        if capture is not None:
            capture['representations'] = representations.copy()
            capture['atomic_shift'] = shift.copy()
        return representations, shift

    def _head(self, representation: np.ndarray, shift: np.ndarray, head: str) -> np.ndarray:
        values = representation
        for layer in ('loc_1', 'loc_2', 'loc_3'):
            values = self._dense(values, f'{head}.{layer}', True)
        return self._dense(values, f'{head}.loc_reduce') + shift

    def predict(self, mol: Chem.Mol, *, intermediates: bool = False) -> Prediction:
        """Evaluate one supported explicit-H geometry, preserving atom indices."""
        point = perf_counter()
        graph = build_graph(mol, self.tokens)
        timings = {"graph": perf_counter() - point}
        capture = {} if intermediates else None
        point = perf_counter()
        representation, shift = self._encode(graph, capture)
        timings["encoder"] = perf_counter() - point
        point = perf_counter()
        mean = self._head(representation, shift, 'mean').reshape(-1)
        timings["mean_head"] = perf_counter() - point
        point = perf_counter()
        quantiles = self._head(representation, shift, 'quantile')
        timings["quantile_head"] = perf_counter() - point
        if not np.isfinite(mean).all() or not np.isfinite(quantiles).all():
            raise ValueError('The model produced nonfinite predictions.')
        return Prediction(graph.atom_indices.copy(), mean, quantiles, capture, timings)


def load_models(asset_dir: str | Path | None = None) -> ModelBundle:
    """Load and verify the shared parameter set once in the owned worker."""
    weights, metadata = load_assets(asset_dir)
    return ModelBundle(weights, metadata)
