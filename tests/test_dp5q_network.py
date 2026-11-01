"""Fixed inference contracts, without legacy runtime or external services."""
from hashlib import sha256
import json
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Geometry import Point3D

from checksi.dp5q.assets import tensor_shapes
from checksi.dp5q.graph import build_graph
from checksi.dp5q.network import ModelBundle, load_models


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.weights = {name: np.zeros(shape, np.float32) for name, shape in tensor_shapes().items()}
        self.weights['shared.atomwise_shift'][2] = 10
        self.weights['mean.loc_reduce.bias'][:] = 2
        self.weights['quantile.loc_reduce.bias'][:] = np.arange(99)
        self.model = ModelBundle(self.weights, {'tokens': {'6':2}})
        self.mol = Chem.MolFromSmiles('[C][C][C]')
        conf = Chem.Conformer(3)
        for index in range(3):
            conf.SetAtomPosition(index, Point3D(index,0.,0.))
        self.mol.AddConformer(conf)

    def test_both_heads_keep_atomic_shift_and_float32(self):
        prediction = self.model.predict(self.mol)
        np.testing.assert_array_equal(prediction.atom_indices, [0,1,2])
        np.testing.assert_array_equal(prediction.mean, [12,12,12])
        np.testing.assert_array_equal(prediction.quantiles, np.tile(np.arange(99)+10, (3,1)))
        self.assertEqual(prediction.mean.dtype, np.float32)
        self.assertEqual(prediction.quantiles.dtype, np.float32)

    def test_shared_encoder_runs_once_per_geometry(self):
        with patch.object(self.model, '_encode', wraps=self.model._encode) as encode:
            self.model.predict(self.mol)
        self.assertEqual(encode.call_count, 1)

    def test_optional_intermediates_and_repeated_inputs(self):
        first = self.model.predict(self.mol, intermediates=True)
        self.assertIn('block_3.atom_state', first.intermediates)
        self.assertIn('block_3.messages', first.intermediates)
        second = self.model.predict(self.mol)
        np.testing.assert_array_equal(first.mean, second.mean)
        self.assertIsNone(second.intermediates)


class RealNetworkReferenceTests(unittest.TestCase):
    """Frozen actual TensorFlow outputs remain usable with TensorFlow absent."""

    @classmethod
    def setUpClass(cls):
        cls.models = load_models()
        cls.fixtures = Path(__file__).parent/'fixtures/dp5q/reference-network'
        cls.provenance = json.loads((cls.fixtures/'provenance.json').read_text())

    def test_frozen_captures_cover_every_trained_element_token(self):
        covered = set()
        for fixture in self.provenance['fixtures']:
            name = fixture['name']
            for extension in ('sdf', 'npz'):
                data = (self.fixtures/(name+'.'+extension)).read_bytes()
                self.assertEqual(sha256(data).hexdigest(), fixture[extension+'_sha256'])
            mol = Chem.MolFromMolFile(str(self.fixtures/(name+'.sdf')), removeHs=False)
            covered.update(atom.GetAtomicNum() for atom in mol.GetAtoms())
        self.assertEqual(covered, set(self.models.tokens))

    def test_every_block_and_both_heads_match_actual_tensorflow(self):
        for fixture in self.provenance['fixtures']:
            name = fixture['name']
            with self.subTest(name=name):
                mol = Chem.MolFromMolFile(str(self.fixtures/(name+'.sdf')),removeHs=False)
                actual = self.models.predict(mol,intermediates=True)
                with np.load(self.fixtures/(name+'.npz'),allow_pickle=False) as expected:
                    np.testing.assert_array_equal(actual.atom_indices,expected['atom_indices'])
                    for stage, values in actual.intermediates.items():
                        np.testing.assert_allclose(values,expected['mean.'+stage],atol=1e-4,rtol=1e-5,err_msg=name+':'+stage)
                    np.testing.assert_allclose(actual.mean,expected['mean.output'][:,0],atol=1e-3,rtol=0)
                    np.testing.assert_allclose(actual.quantiles,expected['quantile.output'],atol=1e-3,rtol=0)
                    if 'graph_capture_keys' in fixture:
                        graph = build_graph(mol, self.models.tokens)
                        for key, array in (('atom', graph.atom_tokens), ('atom_index', graph.atom_index),
                                           ('connectivity', graph.connectivity), ('distance', graph.distances)):
                            np.testing.assert_array_equal(array, expected['graph.'+key], err_msg=name+':'+key)
                        np.testing.assert_allclose(graph.distance_rbf, expected['rbf.float32'], atol=1e-7, rtol=1e-6)

    def test_real_models_are_deterministic_across_A_B_A(self):
        ethanol = Chem.MolFromMolFile(str(self.fixtures/'ethanol.sdf'),removeHs=False)
        toluene = Chem.MolFromMolFile(str(self.fixtures/'toluene.sdf'),removeHs=False)
        first = self.models.predict(ethanol)
        self.models.predict(toluene)
        again = self.models.predict(ethanol)
        np.testing.assert_array_equal(first.mean,again.mean)
        np.testing.assert_array_equal(first.quantiles,again.quantiles)


if __name__ == '__main__':
    unittest.main()
