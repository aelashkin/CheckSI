"""Deterministic geometry, convergence, identity and population boundaries."""

import unittest
from unittest.mock import patch

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDistGeom

from checksi.dp5q.contracts import CalculationCancelled
from checksi.dp5q.geometry import (EMBEDDING_MAX_ATTEMPTS, EMBEDDING_TIMEOUT_SECONDS, SINGLE_POLICY,
                                       GeometryError, normalized_populations, prepare_geometry)


class DP5qGeometryTests(unittest.TestCase):
    def test_single_geometry_uses_the_lowest_converged_candidate_without_a_partition_filter(self):
        result = prepare_geometry('CCO')
        eligible = [item for item in result.diagnostics['candidates']
                    if item['selection'] in ('retained', 'higher_energy')]
        expected = min(eligible, key=lambda item: (item['energy_kj'], item['conformer_id']))
        self.assertEqual(result.conformer_ids, (expected['conformer_id'],))
        self.assertEqual(float(result.energies_kj[0]), expected['energy_kj'])
        self.assertNotIn('partition_constraint', result.diagnostics)

    def test_source_stereo_and_mmff_guards_precede_population_selection(self):
        for target, value, reason in (
            ('_preserves_stereochemistry', False, 'stereochemistry_changed'),
            ('rdForceFieldHelpers.MMFFHasAllMoleculeParams', False, 'mmff_parameters_missing'),
        ):
            with self.subTest(target=target):
                with patch('checksi.dp5q.geometry.' + target, return_value=value), \
                     patch('checksi.dp5q.geometry.normalized_populations') as populations:
                    with self.assertRaises(GeometryError) as raised:
                        prepare_geometry('CCO')
                    self.assertEqual(raised.exception.reason, reason)
                    populations.assert_not_called()

    def test_embedding_has_explicit_native_work_bounds(self):
        def no_embedding(mol, *, numConfs, params):
            self.assertEqual(params.maxIterations, EMBEDDING_MAX_ATTEMPTS)
            self.assertEqual(params.timeout, EMBEDDING_TIMEOUT_SECONDS)
            self.assertEqual((params.maxIterations, params.timeout, numConfs), (1000, 10, 30))
            return []
        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=no_embedding):
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry("CCO")
        self.assertEqual(caught.exception.reason, "embedding_failed")

    def test_native_embedding_timeout_sentinel_never_reaches_mmff(self):
        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", return_value=(-1,)), \
             patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs") as optimize:
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry("C" * 50)
        self.assertEqual(caught.exception.reason, "embedding_failed")
        self.assertEqual(caught.exception.diagnostics["geometry"]["generated_count"], 0)
        optimize.assert_not_called()

    def test_native_large_chain_has_audited_finite_converged_geometry(self):
        # A real 50-heavy-atom geometry/optimizer regression with one candidate
        # bounds suite cost; production still requests 30 candidates.
        native = rdDistGeom.EmbedMultipleConfs
        def one_candidate(mol, *, numConfs, params):
            self.assertEqual(numConfs, 30)
            return native(mol, numConfs=1, params=params)
        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=one_candidate):
            try:
                geometry = prepare_geometry("C" * 50)
            except GeometryError as exc:
                self.assertIn(exc.reason, ("embedding_failed", "no_converged_conformer"))
                self.assertIn("geometry", exc.diagnostics)
                self.assertNotIn("stereochemistry", str(exc))
            else:
                self.assertEqual(geometry.mol.GetNumHeavyAtoms(), 50)
                candidate = geometry.diagnostics["candidates"][0]
                self.assertEqual(candidate["optimization_status"], 0)
                self.assertIn(candidate["iteration_budget"], (500, 2000))

    @staticmethod
    def _coordinates(smiles):
        """Build a real atom-ordered 3D stereo fixture without force-field work."""
        mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
        parameters = rdDistGeom.ETKDGv3()
        parameters.randomSeed = 0xD05
        parameters.numThreads = 1
        if rdDistGeom.EmbedMolecule(mol, parameters) != 0:
            raise AssertionError("Native stereo fixture embedding failed.")
        return mol.GetConformer().GetPositions()

    @staticmethod
    def _replace_optimized_coordinates(positions):
        """Inject native counterexample coordinates at the MMFF boundary."""
        def optimized(mol, **kwargs):
            for conformer in mol.GetConformers():
                for index, point in enumerate(positions):
                    conformer.SetAtomPosition(index, point)
            return [(0, 0.0)] * mol.GetNumConformers()
        return optimized

    def test_single_geometry_is_converged_and_retains_identity(self):
        geometry = prepare_geometry("CCO")
        self.assertEqual(len(geometry.conformer_ids), 1)
        self.assertEqual(geometry.policy, "converged-single-v2")
        self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(geometry.mol)), "CCO")
        np.testing.assert_array_equal(geometry.populations, [1.0])
        self.assertTrue(np.isfinite(geometry.energies_kj).all())

    def test_deterministic_embedding_and_selection(self):
        first, second = prepare_geometry("CCCO"), prepare_geometry("CCCO")
        np.testing.assert_array_equal(first.energies_kj, second.energies_kj)
        a = first.mol.GetConformer(first.conformer_ids[0]).GetPositions()
        b = second.mol.GetConformer(second.conformer_ids[0]).GetPositions()
        np.testing.assert_array_equal(a, b)

    def test_iteration_limit_is_not_accepted_as_convergence(self):
        def unfinished(mol, **kwargs):
            return [(1, 0.0)] * mol.GetNumConformers()
        with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs", side_effect=unfinished):
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry("CCO")
        self.assertEqual(caught.exception.reason, "no_converged_conformer")
        self.assertEqual(caught.exception.diagnostics["geometry"]["rejection_counts"], {"not_converged": 30})
        self.assertNotIn("stereochemistry", str(caught.exception))

    def test_unfinished_conformers_receive_only_a_bounded_continuation(self):
        calls = []
        def optimize(mol, **kwargs):
            calls.append((mol.GetNumConformers(), kwargs["maxIters"]))
            return [(1 if len(calls) == 1 else 0, 0.)] * mol.GetNumConformers()
        with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs", side_effect=optimize):
            result = prepare_geometry("CCO")
        self.assertEqual(calls, [(30, 500)] + [(1, 1500)] * 30)
        self.assertEqual(result.diagnostics["converged_count"], 30)
        self.assertTrue(all(item["initial_optimization_status"] == 1 and item["iteration_budget"] == 2000
                            for item in result.diagnostics["candidates"]))

    def test_three_coordinate_tagged_p_and_n_embed_with_unverified_scope(self):
        for smiles in ("CCC[P@](C)c1ccccc1", "C[N@]1C(C)C1"):
            with self.subTest(smiles=smiles):
                geometry = prepare_geometry(smiles)
                self.assertEqual(len(geometry.conformer_ids), 1)
                self.assertFalse(geometry.diagnostics['stereo_verification_complete'])

    def test_four_coordinate_n_and_carbon_tags_are_not_blanket_refused(self):
        for smiles in ("C[N@@+]1(CC)CCC(C)C1", "C[C@H](O)C(=O)O"):
            with self.subTest(smiles=smiles):
                self.assertEqual(len(prepare_geometry(smiles).conformer_ids), 1)

    def test_tagged_bridgehead_n_cannot_be_silently_cleaned_before_scope_recording(self):
        mol = Chem.MolFromSmiles("C1CN2CCC1CC2")
        nitrogen = next(atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 7)
        self.assertEqual(nitrogen.GetDegree(), 3)
        self.assertTrue(nitrogen.IsInRing())
        nitrogen.SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CW)
        geometry = prepare_geometry(mol)
        self.assertEqual(len(geometry.conformer_ids), 1)
        self.assertFalse(geometry.diagnostics['stereo_verification_complete'])

    def test_unsupported_force_field_preserves_the_complete_structure(self):
        with self.assertRaises(GeometryError) as caught:
            prepare_geometry("C[N+](C)(C)C.[I-]")
        # No fragment stripping or alternative force field makes it eligible.
        self.assertIn(caught.exception.reason, ("mmff_parameters_missing", "no_converged_conformer"))

    def test_failed_deterministic_embedding_retries_once_from_random_coordinates(self):
        real_embed = rdDistGeom.EmbedMultipleConfs
        calls = []

        def embed(mol, numConfs, params):
            calls.append(params.useRandomCoords)
            if not params.useRandomCoords:
                return []
            return real_embed(mol, numConfs=numConfs, params=params)

        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=embed):
            geometry = prepare_geometry("CCCO")
        self.assertEqual(calls, [False, True])
        self.assertEqual(geometry.diagnostics["embedding"], "random_coordinates")
        self.assertEqual(geometry.policy, SINGLE_POLICY)
        self.assertEqual(len(geometry.conformer_ids), 1)

    def test_random_coordinate_retry_keeps_the_seed_and_embedding_bounds(self):
        seen = []

        def embed(mol, numConfs, params):
            seen.append((params.useRandomCoords, params.randomSeed, params.maxIterations, params.timeout))
            return []

        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=embed):
            with self.assertRaises(GeometryError):
                prepare_geometry("CCO")
        self.assertEqual(seen, [(False, 0xD05, EMBEDDING_MAX_ATTEMPTS, EMBEDDING_TIMEOUT_SECONDS),
                                (True, 0xD05, EMBEDDING_MAX_ATTEMPTS, EMBEDDING_TIMEOUT_SECONDS)])

    def test_cancellation_after_a_failed_embedding_skips_the_retry(self):
        calls = 0

        def cancel_after_first_embedding():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise CalculationCancelled()

        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", return_value=[]) as embed:
            with self.assertRaises(CalculationCancelled):
                prepare_geometry("CCO", cancel_check=cancel_after_first_embedding)
        self.assertEqual(embed.call_count, 1)

    def test_embedding_that_succeeds_never_uses_random_coordinates(self):
        real_embed = rdDistGeom.EmbedMultipleConfs
        calls = []

        def embed(mol, numConfs, params):
            calls.append(params.useRandomCoords)
            return real_embed(mol, numConfs=numConfs, params=params)

        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=embed):
            geometry = prepare_geometry("CCCO")
        self.assertEqual(calls, [False])
        self.assertEqual(geometry.diagnostics["embedding"], "deterministic")

    def test_embedding_failure_survives_the_random_coordinate_retry(self):
        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", return_value=[]) as embed:
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry("CCO")
        self.assertEqual(caught.exception.reason, "embedding_failed")
        self.assertEqual(embed.call_count, 2)

    def test_cancellation_precedes_native_work(self):
        def cancel():
            raise CalculationCancelled()
        with self.assertRaises(CalculationCancelled):
            prepare_geometry("CCO", cancel_check=cancel)

    def test_cancellation_after_embedding_keeps_its_cancellation_type(self):
        calls = 0
        def cancel_after_embedding():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise CalculationCancelled()
        with self.assertRaises(CalculationCancelled):
            prepare_geometry("CCO", cancel_check=cancel_after_embedding)

    def test_kj_population_units_and_energy_offset_invariance(self):
        values = normalized_populations([0, 10])
        self.assertAlmostEqual(values[1] / values[0], 0.0177, places=4)
        np.testing.assert_allclose(values, normalized_populations([1000, 1010]), rtol=0, atol=0)
        for energies in ([float("nan")], [], [float("inf")]):
            with self.subTest(energies=energies), self.assertRaises(GeometryError):
                normalized_populations(energies)

    def test_coordinates_must_preserve_explicit_double_bond_stereo(self):
        cis = prepare_geometry("C/C=C\\C")
        positions = cis.mol.GetConformer(cis.conformer_ids[0]).GetPositions()
        def inverted_coordinates(mol, **kwargs):
            for conformer in mol.GetConformers():
                for index, point in enumerate(positions):
                    conformer.SetAtomPosition(index, point)
            return [(0, 0.0)] * mol.GetNumConformers()
        with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs", side_effect=inverted_coordinates):
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry("C/C=C/C")
        self.assertEqual(caught.exception.reason, "stereochemistry_changed")

    def test_ring_relative_stereo_without_cip_must_be_preserved(self):
        requested = Chem.MolFromSmiles("C[C@H]1CC[C@@H](C)CC1")
        tagged = [atom for atom in requested.GetAtoms()
                  if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED]
        self.assertEqual(len(tagged), 2)
        self.assertTrue(all(not atom.HasProp("_CIPCode") for atom in tagged))
        opposite = self._coordinates("C[C@H]1CC[C@H](C)CC1")
        with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs",
                   side_effect=self._replace_optimized_coordinates(opposite)):
            with self.assertRaises(GeometryError) as caught:
                prepare_geometry(requested)
        self.assertEqual(caught.exception.reason, "stereochemistry_changed")

    def test_specified_ring_stereo_and_original_atom_mapping_are_retained(self):
        mol = Chem.MolFromSmiles("O[C@H]1C[C@@H](O)C[C@H](O)C1")
        for index, atom in enumerate(mol.GetAtoms()):
            atom.SetAtomMapNum(index + 1)
        mol = Chem.RenumberAtoms(mol, list(reversed(range(mol.GetNumAtoms()))))
        original = [(atom.GetAtomicNum(), atom.GetAtomMapNum(), atom.GetChiralTag())
                    for atom in mol.GetAtoms()]
        geometry = prepare_geometry(mol)
        self.assertEqual(original, [(atom.GetAtomicNum(), atom.GetAtomMapNum(), atom.GetChiralTag())
                                    for atom in list(geometry.mol.GetAtoms())[:len(original)]])

    def test_only_input_specified_stereo_is_required(self):
        for resolved in ("C[C@H](O)[C@H](O)C", "C[C@H](O)[C@@H](O)C"):
            with self.subTest(resolved=resolved):
                positions = self._coordinates(resolved)
                with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs",
                           side_effect=self._replace_optimized_coordinates(positions)):
                    geometry = prepare_geometry("C[C@H](O)C(O)C")
                self.assertEqual(geometry.mol.GetAtomWithIdx(3).GetChiralTag(),
                                 Chem.ChiralType.CHI_UNSPECIFIED)

    def test_native_ensemble_contains_only_lowest_energy_stereoconfiguration(self):
        geometry = prepare_geometry("CC(O)C(C)O", ensemble=True)
        single = prepare_geometry("CC(O)C(C)O")
        self.assertEqual(geometry.conformer_ids[0], single.conformer_ids[0])
        self.assertEqual(geometry.energies_kj[0], single.energies_kj[0])
        np.testing.assert_array_equal(
            geometry.mol.GetConformer(geometry.conformer_ids[0]).GetPositions(),
            single.mol.GetConformer(single.conformer_ids[0]).GetPositions())
        configurations = set()
        for conformer_id in geometry.conformer_ids:
            mol = Chem.Mol(geometry.mol, confId=conformer_id)
            Chem.RemoveStereochemistry(mol)
            Chem.AssignStereochemistryFrom3D(mol, replaceExistingTags=True)
            configurations.add(Chem.MolToSmiles(Chem.RemoveHs(mol)))
        self.assertEqual(len(configurations), 1)
        self.assertTrue(any(item["selection"] == "other_stereoisomer"
                            for item in geometry.diagnostics["candidates"]))
        self.assertEqual(geometry.policy, "bounded-ensemble-v3")
        self.assertAlmostEqual(float(geometry.populations.sum()), 1.0)
        self.assertTrue(all(atom.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED
                            for atom in geometry.mol.GetAtoms()))


if __name__ == "__main__":
    unittest.main()
