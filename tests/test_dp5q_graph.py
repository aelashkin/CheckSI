"""Supported graph construction preserves the frozen preprocessing contract."""
import unittest
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Geometry import Point3D

from checksi.dp5q.graph import GraphDomainError, build_graph
from checksi.dp5q.assets import asset_metadata


def molecule(smiles, positions):
    """Construct exact geometry without invoking conformer generation."""
    mol = Chem.MolFromSmiles(smiles)
    conf = Chem.Conformer(mol.GetNumAtoms())
    for index, point in enumerate(positions):
        conf.SetAtomPosition(index, Point3D(*point))
    mol.AddConformer(conf)
    return mol


class GraphTests(unittest.TestCase):
    def test_directed_order_targets_distance_cast_and_rbf(self):
        mol = molecule('[C][O][C]', [(0., 0., 0.), (1.23456789, 0., 0.), (3., 0., 0.)])
        graph = build_graph(mol, {6: 2, 8: 3})
        np.testing.assert_array_equal(graph.connectivity, [[0, 1], [0, 2], [1, 0], [1, 2], [2, 1], [2, 0]])
        np.testing.assert_array_equal(graph.atom_indices, [0, 2])
        np.testing.assert_array_equal(graph.atom_index, [0, -1, 1])
        self.assertEqual(graph.distances.dtype, np.float32)
        expected = np.exp(-((graph.distances.astype(np.float64)[:, None] - .1 * np.arange(256)) ** 2) / .1).astype(np.float32)
        np.testing.assert_array_equal(graph.distance_rbf, expected)

    def test_cutoff_is_strict(self):
        graph = build_graph(molecule('[C][C][C]', [(0.,0.,0.), (2.,0.,0.), (5.,0.,0.)]), {6:2})
        self.assertNotIn([0, 2], graph.connectivity.tolist())
        self.assertNotIn([2, 0], graph.connectivity.tolist())

    def test_single_atom_has_reference_self_link(self):
        graph = build_graph(molecule('[C]', [(0.,0.,0.)]), {6:2})
        np.testing.assert_array_equal(graph.connectivity, [[0,0]])
        np.testing.assert_array_equal(graph.distances, [0.])

    def test_unsupported_graphs_have_explicit_reasons(self):
        cases = [
            (molecule('[C].[C]', [(0.,0.,0.), (6.,0.,0.)]), {6:2}, 'isolated_atom'),
            (molecule('[C][C]', [(0.,0.,0.), (0.,0.,0.)]), {6:2}, 'coincident_atoms'),
            (molecule('[C][O]', [(0.,0.,0.), (1.,0.,0.)]), {6:2}, 'unsupported_element'),
            (molecule('C', [(0.,0.,0.)]), {6:2}, 'implicit_hydrogens'),
        ]
        for mol, tokens, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(GraphDomainError) as caught:
                build_graph(mol, tokens)
            self.assertEqual(caught.exception.code, reason)

    def test_atom_and_neighbor_caps_refuse_before_features(self):
        mol = molecule('.'.join(['[C]'] * 102), [(i*.01,0.,0.) for i in range(102)])
        with self.assertRaises(GraphDomainError) as caught:
            build_graph(mol, {6:2})
        self.assertEqual(caught.exception.code, 'neighbor_limit')
        with self.assertRaises(GraphDomainError) as caught:
            build_graph(mol, {6:2}, max_atoms=100)
        self.assertEqual(caught.exception.code, 'atom_limit')


class GraphIdentityTests(unittest.TestCase):
    def test_real_frozen_reference_graphs_match_exactly(self):
        fixtures = Path(__file__).parent/'fixtures/dp5q/reference-network'
        tokens = {int(key):value for key,value in asset_metadata()['tokens'].items()}
        for name in ('ethanol','toluene'):
            with self.subTest(name=name):
                mol = Chem.MolFromMolFile(str(fixtures/(name+'.sdf')),removeHs=False)
                actual = build_graph(mol,tokens)
                with np.load(fixtures/(name+'.npz'),allow_pickle=False) as expected:
                    for key,array in [('atom',actual.atom_tokens),('atom_index',actual.atom_index),('connectivity',actual.connectivity),('distance',actual.distances)]:
                        np.testing.assert_array_equal(array,expected['graph.'+key])
                    np.testing.assert_allclose(actual.distance_rbf,expected['rbf.float32'],atol=1e-7,rtol=1e-6)

    def test_atom_permutation_keeps_geometry_and_original_carbon_indices(self):
        mol = molecule('[C][O][C]', [(0.,0.,0.),(1.,0.,0.),(3.,0.,0.)])
        permuted = Chem.RenumberAtoms(mol, [1,2,0])
        graph = build_graph(permuted, {6:2,8:3})
        np.testing.assert_array_equal(graph.atom_indices, [1,2])
        np.testing.assert_array_equal(graph.atom_tokens, [3,2,2])
        # Mapping to old atom identity recovers the exact directed edge set.
        edges = np.asarray([1,2,0])[graph.connectivity]
        self.assertEqual(set(map(tuple, edges)), set(map(tuple, build_graph(mol,{6:2,8:3}).connectivity)))

    def test_edge_limit_and_nonfinite_geometry(self):
        mol = molecule('[C][C][C]', [(0.,0.,0.),(1.,0.,0.),(2.,0.,0.)])
        with self.assertRaises(GraphDomainError) as caught:
            build_graph(mol,{6:2},max_edges=5)
        self.assertEqual(caught.exception.code,'edge_limit')
        mol.GetConformer().SetAtomPosition(0,Point3D(float('nan'),0.,0.))
        with self.assertRaises(GraphDomainError) as caught:
            build_graph(mol,{6:2})
        self.assertEqual(caught.exception.code,'nonfinite_geometry')


if __name__ == '__main__':
    unittest.main()
