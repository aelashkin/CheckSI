"""Environment cardinalities constrain numerical and process assignments."""

import math
import unittest

from checksi.dp5q.assignment import AssignmentError, assign_groups
from checksi.dp5q.contracts import CheckRequest, CheckResult, MAX_ATOMS, validate_result_for_request


class CarbonCountContractTests(unittest.TestCase):

    def test_optional_counts_are_strict_aligned_and_omitted_when_absent(self):
        legacy = CheckRequest(compound_id="plain", smiles="CCO", carbon_shifts=[60.0, 20.0])
        self.assertNotIn("observation_atom_counts", legacy.model_dump(mode="json"))
        supplied = CheckRequest(compound_id="pair", smiles="CC(=O)C", carbon_shifts=[207.1, 30.9],
                                observation_atom_counts=[None, 2])
        self.assertEqual(CheckRequest.model_validate_json(supplied.model_dump_json()).observation_atom_counts, (None, 2))
        for invalid in ([1], [None, True], [None, "2"], [None, 2.5], [None, 0], [None, -1], [None, MAX_ATOMS + 1]):
            with self.subTest(counts=invalid), self.assertRaises(ValueError):
                CheckRequest(compound_id="bad", smiles="CCO", carbon_shifts=[60.0, 20.0], observation_atom_counts=invalid)





    def test_numeric_assignment_failure_is_not_mislabelled_as_atom_annotation_failure(self):
        with self.assertWarns(RuntimeWarning), self.assertRaises(ValueError) as caught:
            assign_groups([1e308], [-1e308])
        self.assertNotIsInstance(caught.exception, AssignmentError)

    def test_host_validation_rejects_symmetry_atoms_as_overlap_evidence(self):
        ready = {"capabilities": ["coincident-observations-v1"], "model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
                 "geometry_policy": "single", "weights_sha256": "a" * 64}
        request = CheckRequest(compound_id="acetone", smiles="CC(=O)C", carbon_shifts=[207.1, 30.9],
            carbon_groups=[["C1", "C4"]], carbon_labels=["C1", "C2", "C4"], observation_atom_counts=[1, 1])

        def result(order):
            groups = [["C1", "C4"], ["C2"]]
            return CheckResult(compound_id="acetone", status="assessed", score_raw=.75, log_score=math.log(.75),
                provenance={**ready, "coordinate_partition_verified": True, "stereo_verification_complete": True,
                    "partition_constraint": "exact-original-atom-partition-v1",
                    "selected_isomer_smiles": "CC(C)=O", "configuration_status": "admissible"}, assignments=[{
                    "atom_label": ",".join(group), "atom_labels": group, "calculated_shift": request.carbon_shifts[peak],
                    "experimental_shift": request.carbon_shifts[peak], "observation_index": peak, "evidence_weight": 1.,
                    "scaled_error": 0., "score_percent": 75., "atom_probability_percent": 75.,
                } for group, peak in zip(groups, order)],
                timings={name: .01 if name == "total" else 0. for name in
                    ("coverage", "geometry", "inference", "assignment", "scoring", "total")},
                diagnostics={"full_environment_count": 2, "observation_count": 2,
                    "scored_environment_count": 2, "coincident_observations": {}, "score_scope": "complete_spectrum",
                    "scope_note": "Controlled schema fixture.", "raw_mae": 0., "corrected_mae": 0., "scaled_errors": [0., 0.], "regression_reason": None,
                    "assignment_objective": 0., "assignment_exchangeable": False, "conformer_count": 1,
                    "energies_kj": [0.], "populations": [1.], "fit_residual_rms": [[0., 0.]], "quantile_crossings": [[0, 0]],
                    "fit_locations": [[request.carbon_shifts[peak] + .31863936396437514 for peak in order]],
                    "fit_sigmas": [[1., 1.]],
                    "geometry": {name: 1 for name in ("requested_count", "generated_count", "converged_count", "eligible_count", "retained_count")}})

        validate_result_for_request(request, result([0, 1]), ready)
        incompatible = request.model_copy(update={"observation_atom_counts": (2, 1)})
        with self.assertRaisesRegex(ValueError, "capacity"):
            validate_result_for_request(incompatible, result([0, 1]), ready)


if __name__ == "__main__":
    unittest.main()
