"""Real-weight single-compound behavior and explicit unassessed outcomes."""

import threading
import unittest
import json
import math
from pathlib import Path
from unittest.mock import patch

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDistGeom
from scipy.optimize import curve_fit
from scipy.special import ndtr

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.dp5q.contracts import CalculationCancelled, CheckRequest, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.geometry import GeometryError, PreparedGeometry, prepare_geometry
from checksi.dp5q.network import load_models


class DP5qEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()

    def request(self, identifier="ethanol", **extra):
        return CheckRequest(compound_id=identifier, smiles="CCO", carbon_shifts=[18.2, 58.1], **extra)

    def test_real_models_produce_complete_finite_assessment(self):
        stages = []
        result = check_compound(self.request(include_geometry=True), self.models, progress=stages.append)
        self.assertEqual(result.status, "assessed", result)
        self.assertGreaterEqual(result.score_raw, 0)
        self.assertLessEqual(result.score_raw, 1)
        self.assertEqual(len(result.assignments), 2)
        self.assertIn("geometry", stages)
        self.assertIn("geometry_sdf", result.diagnostics)
        self.assertEqual(result.provenance["geometry_policy"], "converged-single-v2")

    def test_repeated_requests_do_not_contaminate_loaded_models(self):
        first = check_compound(self.request(), self.models)
        other = CheckRequest(compound_id="propane", smiles="CCC", carbon_shifts=[15.0, 25.0], carbon_groups=[["C1", "C3"]])
        check_compound(other, self.models)
        repeated = check_compound(self.request(), self.models)
        self.assertEqual(first.score_raw, repeated.score_raw)
        self.assertEqual(first.assignments, repeated.assignments)

    def test_eight_engineering_requests_use_real_weights_without_state_contamination(self):
        cases = json.loads((Path(__file__).parent / "fixtures/dp5q/engineering-smoke.json").read_text())["cases"]
        first = check_compound(CheckRequest.model_validate(cases[0]), self.models)
        for case in cases:
            with self.subTest(compound=case["compound_id"]):
                result = check_compound(CheckRequest.model_validate(case), self.models)
                self.assertEqual(result.status, "assessed", result)
                self.assertTrue(0 <= result.score_raw <= 1)
                validate_result_for_request(CheckRequest.model_validate(case), result, self.ready())
        repeated = check_compound(CheckRequest.model_validate(cases[0]), self.models)
        self.assertEqual(first.score_raw, repeated.score_raw)
        self.assertEqual(first.assignments, repeated.assignments)

    def test_incomplete_coverage_is_refused_before_geometry_or_inference(self):
        stages = []
        request = CheckRequest(compound_id="incomplete", smiles="CCO", carbon_shifts=[18.2])
        result = check_compound(request, self.models, progress=stages.append)
        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.reason, "strict_nmr_coverage_mismatch")
        self.assertNotIn("geometry", stages)
        self.assertIsNone(result.score_raw)

    def test_cross_center_prochiral_pairs_score_without_filtering_geometry(self):
        smiles = "CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C"
        structure = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
        self.assertEqual(structure.carbon_count, 5)
        # All four methyls share a stereo-aware graph orbit. Its two formal
        # pairs depend on prochiral labels chosen in a separate conformer.
        methyls = {"C1", "C3", "C15", "C16"}
        pairs = [set(group) for group in structure.carbon_groups if set(group) <= methyls]
        self.assertEqual(sorted(map(len, pairs)), [2, 2])
        stages = []
        request = CheckRequest(compound_id="tartrate", smiles=smiles,
            carbon_shifts=[170., 100., 80., 30., 20.], carbon_groups=structure.carbon_groups,
            fixed_assignments={0: 3, 1: 2, 2: 4, 3: 0, 4: 1})
        result = check_compound(request, self.models, progress=stages.append)
        self.assertEqual(result.status, "assessed", result)
        self.assertIn("geometry", stages)
        self.assertFalse(result.provenance['coordinate_partition_verified'])
        self.assertEqual(result.provenance['coordinate_partition_reason'], 'coordinate_partition_mismatch')
        self.assertEqual(result.diagnostics['energies_kj'], prepare_geometry(smiles).energies_kj.tolist())
        validate_result_for_request(request, result, self.ready())

    def test_formal_partitions_reach_geometry_and_manual_counts_require_sharing(self):
        for smiles in (
            "CC(C)[C@H](N)C(=O)O",
            "O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@@H](O)[C@H]1O",
            "CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C",
        ):
            structure = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
            alternatives = structure.c13_alternatives if "OC(=O)" not in smiles else structure.c13_alternatives[1:]
            for alternative in alternatives:
                with self.subTest(smiles=smiles, alternative=alternative.name):
                    request = CheckRequest(compound_id="complete-orbits", smiles=smiles,
                        carbon_shifts=[float(10 * i) for i in range(1, alternative.count + 1)],
                        carbon_groups=alternative.groups_dp5)
                    with patch("checksi.dp5q.engine.prepare_geometry",
                               side_effect=GeometryError("embedding_failed", "fixture")) as prepare:
                        result = check_compound(request, self.models)
                    if alternative.manual_check_only:
                        # A lower count is not a merged chemical environment.
                        # Until the shared-observation worker path is wired, the
                        # unchanged worker must refuse this incomplete coverage.
                        prepare.assert_not_called()
                        self.assertEqual(result.status, "not_applicable")
                        expected = ("source_partition_mismatch" if alternative.groups_dp5 != structure.carbon_groups
                                    else "strict_nmr_coverage_mismatch")
                        self.assertEqual(result.reason, expected)
                    else:
                        prepare.assert_called_once()
                        self.assertEqual(result.reason, "embedding_failed")

    def test_signal_count_partition_mismatch_is_refused_before_geometry(self):
        requests = [
            CheckRequest(compound_id="ether", smiles="CCOCC", carbon_shifts=[65.9, 15.4],
                carbon_groups=[["C1", "C5"], ["C2", "C4"]], observation_atom_counts=[2, None]),
            CheckRequest(compound_id="fixed-conflict", smiles="CC(=O)C", carbon_shifts=[207.1, 30.9],
                carbon_groups=[["C1", "C4"]], observation_atom_counts=[2, 1], fixed_assignments={0: 1}),
            CheckRequest(compound_id="fixed-unknown", smiles="CC(=O)C", carbon_shifts=[207.1, 30.9],
                carbon_groups=[["C1", "C4"]], observation_atom_counts=[2, None], fixed_assignments={0: 1}),
        ]
        for request in requests:
            with self.subTest(compound=request.compound_id):
                stages = []
                result = check_compound(request, self.models, progress=stages.append)
                self.assertEqual(result.status, "not_applicable")
                self.assertEqual(result.reason, "annotation_partition_mismatch")
                self.assertNotIn("geometry", stages)
                self.assertIsNone(result.score_raw)
                validate_result_for_request(request, result, self.ready())

    def test_real_engine_refuses_symmetry_atom_counts_in_either_peak_order(self):
        for counts in ([1, 2], [2, 1]):
            with self.subTest(counts=counts):
                request = CheckRequest(compound_id="acetone", smiles="CC(=O)C", carbon_shifts=[207.1, 30.9],
                    carbon_groups=[["C1", "C4"]], carbon_labels=["C1", "C2", "C4"], observation_atom_counts=counts)
                result = check_compound(request, self.models)
                self.assertEqual((result.status, result.reason), ('not_applicable', 'annotation_partition_mismatch'))
                self.assertEqual(result.assignments, [])
                validate_result_for_request(request, result, self.ready())

    def test_hydrate_scores_only_the_carbon_bearing_component(self):
        stages = []
        request = CheckRequest(compound_id="hydrate", smiles="CC(=O)O.O", carbon_shifts=[178.0, 20.8])
        result = check_compound(request, self.models, progress=stages.append)
        self.assertEqual(result.status, "assessed", result)
        self.assertIn("geometry", stages)
        self.assertEqual(result.provenance['component_scope']['ignored_components'], ['O'])
        self.assertEqual(result.provenance['component_scope']['original_smiles'], request.smiles)

    def test_development_reference_identity_is_checked_before_fragments(self):
        reference = Chem.AddHs(Chem.MolFromSmiles("CCO.O"))
        self.assertEqual(rdDistGeom.EmbedMolecule(reference, randomSeed=0xD05), 0)
        request = CheckRequest(compound_id="hydrate", smiles="CC(=O)O.O", carbon_shifts=[178.0, 20.8])
        result = check_compound(request, self.models, frozen_mol=reference)
        self.assertEqual(result.reason, "reference_identity_mismatch")

    def test_validator_rejects_an_unknown_embedding_procedure(self):
        request = self.request()
        result = check_compound(request, self.models)
        validate_result_for_request(request, result, self.ready())
        diagnostics = {**result.diagnostics, "geometry": {**result.diagnostics["geometry"], "embedding": "guessed"}}
        with self.assertRaises(ValueError):
            validate_result_for_request(request, result.model_copy(update={"diagnostics": diagnostics}), self.ready())

    def test_multi_carbon_component_refusal_is_bounded(self):
        smiles = ".".join(["C" * 120, 'CO'] + ["O"] * 7)
        # Refusal precedes the coverage check, so one shift suffices here.
        request = CheckRequest(compound_id="mixture", smiles=smiles, carbon_shifts=[30.0])
        result = check_compound(request, self.models)
        self.assertEqual(result.reason, "multiple_carbon_components")
        message = result.diagnostics["message"]
        self.assertEqual(message, 'DP5q not performed: more than one component contains carbon.')
        self.assertLess(len(message), 650)

    def test_request_inventory_is_checked_against_actual_original_atoms(self):
        result = check_compound(self.request(carbon_labels=("C1","C3")), self.models)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "carbon_identity_mismatch")
        valid = CheckRequest(compound_id="ether", smiles="COC", carbon_shifts=(60.,),
            carbon_labels=("C1","C3"), carbon_groups=(("C1","C3"),))
        self.assertEqual(check_compound(valid, self.models).status, "assessed")

    def ready(self, policy="converged-single-v2"):
        """Construct expected parent identity independently of a result."""
        metadata = self.models.metadata
        return {"capabilities": ["coincident-observations-v1"], "model_id": metadata["model_id"], "preprocessing_id": metadata["preprocessing_id"],
                "score_version": metadata["score_id"], "geometry_policy": policy,
                "weights_sha256": metadata["files"]["weights.npz"]["sha256"]}

    def test_internal_geometry_failure_remains_a_failure_with_audit(self):
        error = GeometryError("geometry_failed", "MMFF failed.", diagnostics={"geometry": {"generated_count": 30}})
        with patch("checksi.dp5q.engine.prepare_geometry", side_effect=error):
            result = check_compound(self.request(), self.models)
        self.assertEqual((result.status, result.reason), ("failed", "geometry_failed"))
        self.assertEqual(result.diagnostics["geometry"]["generated_count"], 30)
        with patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs", side_effect=RuntimeError("Native MMFF failed.")):
            result = check_compound(self.request(), self.models)
        self.assertEqual((result.status, result.reason), ("failed", "geometry_failed"))

    def test_unsupported_scored_component_elements_refuse_before_geometry(self):
        request = CheckRequest(compound_id="unsupported", smiles="CI.[Na+]", carbon_shifts=[1.])
        with patch("checksi.dp5q.engine.prepare_geometry") as prepare:
            result = check_compound(request, self.models)
        self.assertEqual((result.status, result.reason), ("not_applicable", "unsupported_element"))
        self.assertIn("I", result.diagnostics["message"])
        self.assertNotIn("Na", result.diagnostics["message"])
        self.assertEqual(result.provenance['component_scope']['ignored_components'], ['[Na+]'])
        prepare.assert_not_called()

    def test_frozen_real_engine_golden_rows_match_reference_heads_and_independent_fit(self):
        root = Path(__file__).parent / "fixtures/dp5q/reference-network"
        mol = Chem.MolFromMolFile(str(root / "toluene.sdf"), removeHs=False)
        request = CheckRequest(compound_id="toluene-golden", smiles="Cc1ccccc1",
            carbon_shifts=[138., 21., 129., 125., 128.],
            carbon_labels=tuple(f"C{i+1}" for i in range(7)), carbon_groups=[["C3", "C7"], ["C4", "C6"]])
        result = check_compound(request, self.models, frozen_mol=mol)
        self.assertEqual(result.status, "assessed", result)
        # Frozen Mol input is a developer oracle boundary, not worker protocol.
        # F006 separately validates every real product-SMILES engineering case.
        expected_indices = [2, 4, 1, 0, 3]
        self.assertEqual([row["observation_index"] for row in result.assignments], expected_indices)
        self.assertEqual([row["atom_label"] for row in result.assignments], ["C3,C7", "C4,C6", "C1", "C2", "C5"])
        self.assertAlmostEqual(result.score_raw, .6365820967727656, delta=2e-5)
        self.assertAlmostEqual(result.log_score, -.45164188772664726, delta=3e-5)
        expected_percent = [63.245012262465835, 72.17346474475058, 60.32661991296169, 97.29571237596872, 39.018050946865124]
        expected_error = [-.14021669909513435, .09328179185061458, .005573164296556854, .06620234674383596, -.024840603795851735]
        np.testing.assert_allclose([row["score_percent"] for row in result.assignments], expected_percent, atol=.003, rtol=0)
        np.testing.assert_allclose([row["scaled_error"] for row in result.assignments], expected_error, atol=.0002, rtol=0)
        # Frozen TensorFlow heads are an independent prediction oracle. Fit the
        # original CDF objective in ordinary (mu,sigma) coordinates, not through
        # the owned fitter, then calculate two-sided tails directly.
        with np.load(root / "toluene.npz", allow_pickle=False) as archive:
            positions = ((2, 6), (3, 5), (0,), (1,), (4,))
            mean = np.array([archive["mean.output"][list(group)].mean() for group in positions])
            quantiles = np.array([archive["quantile.output"][list(group)].mean(axis=0) for group in positions])
        observed = np.array(request.carbon_shifts)[expected_indices]
        percents = []
        for values, value in zip(quantiles, observed):
            fit, _ = curve_fit(lambda x, mu, sigma: ndtr((x-mu)/sigma), values,
                               self.models.metadata["quantile_levels"], p0=[np.mean(values), np.std(values)],
                               ftol=1e-12, xtol=1e-12, gtol=1e-12, maxfev=3000)
            percents.append(100 * math.erfc(abs(value-fit[0]) / (fit[1] * math.sqrt(2))))
        np.testing.assert_allclose([row["calculated_shift"] for row in result.assignments], mean, atol=.0002, rtol=0)
        np.testing.assert_allclose([row["score_percent"] for row in result.assignments], percents, atol=.003, rtol=0)
        self.assertAlmostEqual(result.score_raw, math.exp(np.log(np.array(percents) / 100).mean()), delta=2e-5)
        for order in ([4, 3, 2, 1, 0], [1, 2, 3, 4, 0], [2, 0, 4, 1, 3]):
            shuffled = request.model_copy(update={"carbon_shifts": tuple(request.carbon_shifts[i] for i in order)})
            actual = check_compound(shuffled, self.models, frozen_mol=mol)
            self.assertAlmostEqual(actual.score_raw, result.score_raw, places=12)
            for first, second in zip(result.assignments, actual.assignments):
                self.assertEqual(first["experimental_shift"], second["experimental_shift"])
                self.assertEqual(first["score_percent"], second["score_percent"])

    def test_real_engine_underflow_and_unrepresentable_tail_pass_parent_validation(self):
        for value, reason in ((1e5, "display_underflow"), (1e200, "tail_beyond_float64")):
            with self.subTest(reason=reason):
                request = self.request(carbon_labels=("C1", "C2"), fixed_assignments={0: 0, 1: 1})
                request = request.model_copy(update={"carbon_shifts": (value, value)})
                result = check_compound(request, self.models)
                self.assertEqual(result.status, "assessed", result)
                self.assertEqual(result.score_raw, 0.)
                self.assertEqual(result.diagnostics["zero_score_reason"], reason)
                validate_result_for_request(request, result, self.ready())

    def test_exported_sdf_is_the_scored_nondefault_conformer(self):
        root = Path(__file__).parent / "fixtures/dp5q/reference-network"
        mol = Chem.MolFromMolFile(str(root / "ethanol.sdf"), removeHs=False)
        selected = Chem.Conformer(mol.GetConformer())
        selected.SetId(7)
        expected = selected.GetPositions() + np.array([10., 20., 30.])
        for index, value in enumerate(expected):
            selected.SetAtomPosition(index, value)
        mol.AddConformer(selected, assignId=False)
        geometry = PreparedGeometry(mol, (7,), np.array([0.]), np.array([1.]), "frozen-reference-v1")
        result = check_compound(self.request(include_geometry=True), self.models, prepared_geometry=geometry)
        self.assertEqual(result.status, "assessed", result)
        exported = Chem.MolFromMolBlock(result.diagnostics["geometry_sdf"], removeHs=False)
        np.testing.assert_allclose(exported.GetConformer().GetPositions(), expected, atol=5.1e-5, rtol=0)

    def test_pre_cancelled_check_is_not_a_failed_or_scored_compound(self):
        token = threading.Event()
        token.set()
        with self.assertRaises(CalculationCancelled):
            check_compound(self.request(), self.models, cancel_event=token)

    def test_extreme_finite_input_cannot_publish_nonfinite_diagnostics(self):
        request = CheckRequest(compound_id="extreme", smiles="CCO", carbon_shifts=[1e308, 1e308],
                               fixed_assignments={0: 0, 1: 1})
        result = check_compound(request, self.models)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "nonfinite_diagnostics")
        self.assertIsNone(result.score_raw)


if __name__ == "__main__":
    unittest.main()
