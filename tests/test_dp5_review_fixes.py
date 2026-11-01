"""DP5 review regressions through the real controller and adapter boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdkit import Chem
from rdkit.Geometry import Point3D

from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import (
    enrich_analysis_with_dp5,
)
from checksi.dp5q.contracts import CheckRequest, CheckResult
from checksi.dp5q.engine import check_compound
from checksi.dp5q.geometry import prepare_geometry
from checksi.dp5q.network import load_models


def review_config(report_path="report.docx"):
    """Build an isolated deep-analysis config without invoking external resolution."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "on", "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "warning", "overrides": {"dp5_not_applicable": "info"}},
        "dp5": {"enabled": True},
    })


class DP5ReviewTests(unittest.TestCase):
    """Exercise real grouping/coverage; replace only resolution and process transport."""

    def setUp(self):
        self.cfg = review_config()
        self.runtime = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}

    @classmethod
    def setUpClass(cls):
        cls.models = load_models()

    def analyze(self, smiles, shifts):
        """Use real canonicalization, structure analysis and NMR preparation."""
        description = f"1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). 13C NMR (100 MHz, CDCl3): δ {shifts}."
        with patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(smiles, "Compound", "1", {}, [])):
            return CheckSIController(self.cfg).run_analysis(["Compound (1)", description])

    def enrich(self, data, *, real_engine=False):
        """Capture exact typed requests; optionally run the actual numerical core."""
        requests = []
        def check(request):
            requests.append(request)
            if real_engine:
                return check_compound(request, self.models)
            return CheckResult(compound_id=request.compound_id, status="assessed", score_raw=0.25)
        with patch("checksi.dp5_enrichment.check_availability", return_value=self.runtime), patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.side_effect = check
            result = enrich_analysis_with_dp5(self.cfg, data)
        return result, requests

    def test_real_group_producer_reaches_engine_with_exact_constraints_and_shifts(self):
        for smiles, shifts, expected_groups, expected_labels in [
            ("Cc1ccccc1", "137.8, 129.2, 128.3, 125.4, 21.4", [["C3", "C7"], ["C4", "C6"]], list(range(1, 8))),
            ("CCc1ccccc1", "144.2, 128.4, 127.9, 125.7, 28.9, 15.6", [["C4", "C8"], ["C5", "C7"]], list(range(1, 9))),
            ("CCOCC", "65.9, 15.4", [["C1", "C5"], ["C2", "C4"]], [1, 2, 4, 5]),
        ]:
            with self.subTest(smiles=smiles):
                result, requests = self.enrich(self.analyze(smiles, shifts))
                self.assertEqual(result["compounds"][0]["dp5"]["status"], "success")
                self.assertEqual(result["compounds"][0]["dp5_carbon_groups"], expected_groups)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].carbon_groups, tuple(tuple(group) for group in expected_groups))
                self.assertEqual(requests[0].carbon_shifts, tuple(float(shift.strip()) for shift in shifts.split(",")))
                mol = Chem.MolFromSmiles(requests[0].smiles)
                self.assertEqual([atom.GetIdx() + 1 for atom in mol.GetAtoms() if atom.GetSymbol() == "C"], expected_labels)

    def test_nmr_refusal_precedes_geometry_and_creates_no_artifacts(self):
        for shifts, cause, expected_text, observed_count in [
            ("60.0", "count_mismatch", "count", 1),
            ("60.0 20.0", "malformed_separators", "separator", 2),
            ("60.0, 20.0, 5", "incomplete_numeric_tokens", "numeric", 2),
        ]:
            with self.subTest(shifts=shifts), tempfile.TemporaryDirectory() as tmp:
                self.cfg.paths.output_dir = Path(tmp)
                self.cfg.dp5.keep_artifacts = True
                data = self.analyze("CCO", shifts)
                with patch("checksi.dp5q.geometry.prepare_geometry", side_effect=AssertionError("refused spectra must not prepare geometry")):
                    result, requests = self.enrich(data)
                compound = result["compounds"][0]
                self.assertEqual(requests, [])
                self.assertEqual(compound["dp5"]["reason"],
                                 'strict_nmr_coverage_mismatch' if cause == 'count_mismatch' else cause)
                self.assertEqual(compound["dp5_refusal_details"]["cause"], cause)
                if cause == "count_mismatch":
                    self.assertEqual(compound['dp5']['message'],
                        'DP5q not performed: the number of 13C signals (1) does not match the expected number (2).')
                    self.assertEqual(compound['dp5']['message_severity'], 'info')
                    self.assertFalse(any('dp5_analysis_failed.' in line for line in compound['all_warnings']))
                    self.assertFalse((Path(tmp) / "dp5_artifacts").exists())
                    continue
                warning = next(line for line in compound["all_warnings"] if "dp5_not_applicable." in line)
                self.assertIn(expected_text, warning.lower())
                self.assertIn(cause, warning)
                self.assertNotIn('Observed shifts:', warning)
                self.assertNotIn("c13_count_structure_check", warning)
                self.assertFalse((Path(tmp) / "dp5_artifacts").exists())

    def test_adapter_coverage_refusal_precedes_geometry_and_explains_counts(self):
        for shifts, groups in [([60.0], []), ([60.0, 20.0], [["C1", "C3"]])]:
            with self.subTest(shifts=shifts, groups=groups), tempfile.TemporaryDirectory() as tmp:
                self.cfg.paths.output_dir = Path(tmp)
                self.cfg.dp5.keep_artifacts = True
                compound = {"id": 1, "smiles": "CCO", "run_dp5": True, "dp5_c_signals_list": shifts, "dp5_carbon_groups": groups}
                with patch("checksi.dp5q.geometry.prepare_geometry", side_effect=AssertionError("invalid coverage must not prepare geometry")):
                    result, requests = self.enrich({"compounds": [compound], "error_stats": {}})
                self.assertEqual(requests, [])
                self.assertEqual(result["compounds"][0]["dp5"]["status"], "skipped")
                if not groups:
                    self.assertEqual(compound['dp5']['message'],
                        'DP5q not performed: the number of 13C signals (1) does not match the expected number (2).')
                    self.assertEqual(compound['dp5']['message_severity'], 'info')
                    self.assertFalse((Path(tmp) / "dp5_artifacts").exists())
                    continue
                warning = " ".join(compound["all_warnings"])
                self.assertIn('DP5q not applicable:', warning)
                self.assertIn('invalid_equivalence_groups', warning)
                self.assertNotIn("c13_count_structure_check", warning)
                self.assertFalse((Path(tmp) / "dp5_artifacts").exists())

    def test_adapter_preserves_actual_annotation_failure_with_matching_counts(self):
        compound = {'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                    'dp5_c_signals_list': [60., 20.], 'dp5_carbon_groups': [],
                    'carbon_observations': [{'shift': 60., 'represented_atoms': 0},
                                            {'shift': 20., 'represented_atoms': 2}]}
        result, requests = self.enrich({'compounds': [compound]})
        self.assertEqual(requests, [])
        self.assertEqual(result['compounds'][0]['dp5']['reason'], 'annotation_partition_mismatch')
        self.assertIn('annotation_partition_mismatch:', ' '.join(compound['all_warnings']))

    def test_non_count_preparation_refusal_reports_its_real_reason(self):
        compound = {'id': 1, 'smiles': 'CCO', 'run_dp5': False,
                    'dp5_c_signals_list': [60., 20.], 'dp5_carbon_groups': [],
                    'dp5_refusal_details': {'cause': 'structure_partition_incomplete',
                                           'structure_reason': 'substitution_incomplete'}}
        result, requests = self.enrich({'compounds': [compound]})
        self.assertEqual(requests, [])
        self.assertEqual(result['compounds'][0]['dp5']['reason'], 'structure_partition_incomplete')
        self.assertIn('structure_partition_incomplete: substitution_incomplete', ' '.join(compound['all_warnings']))
        self.assertNotIn('coverage mismatch', ' '.join(compound['all_warnings']))

    def test_enumeration_budget_preparation_reaches_real_engine_with_warning(self):
        smiles = 'CC(O)C(F)C(Cl)C(Br)C'
        source_count = sum(atom.GetAtomicNum() == 6 for atom in Chem.MolFromSmiles(smiles).GetAtoms())
        shifts = ', '.join(f'{20. + 10 * index:.1f}' for index in range(source_count))
        data = self.analyze(smiles, shifts)
        self.assertTrue(data['compounds'][0]['run_dp5'])
        result, requests = self.enrich(data, real_engine=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['compounds'][0]['dp5']['status'], 'success')
        self.assertEqual(result['error_stats']['dp5_configuration_dependent'], 1)

    def test_unsupported_structures_skip_before_geometry_and_preserve_structure(self):
        from checksi.dp5q.observation_policy import BORON_REASON, BORON_WARNING
        for smiles, shifts, fragment, reason in [
            ("CB(O)O", [20.0], "B", BORON_REASON),
            ("C[Se]C", [20.0], "Se", "unsupported_element"),
            ("C[Sn](C)(C)C", [20.0], "Sn", "unsupported_element"),
        ]:
            with self.subTest(smiles=smiles), tempfile.TemporaryDirectory() as tmp:
                self.cfg.paths.output_dir = Path(tmp)
                self.cfg.dp5.keep_artifacts = True
                carbon_labels = [f"C{atom.GetIdx() + 1}" for atom in Chem.MolFromSmiles(smiles).GetAtoms() if atom.GetSymbol() == "C"]
                groups = [carbon_labels] if len(carbon_labels) > 1 else []
                compound = {"id": 1, "smiles": smiles, "run_dp5": True, "dp5_c_signals_list": shifts, "dp5_carbon_groups": groups}
                with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=AssertionError("unsupported MMFF must not embed")):
                    result, requests = self.enrich({"compounds": [compound], "error_stats": {}}, real_engine=True)
                self.assertEqual(len(requests), 0 if reason == BORON_REASON else 1)
                self.assertEqual(compound["dp5"]["reason"], reason)
                self.assertEqual(compound["smiles"], smiles)
                if reason == BORON_REASON:
                    self.assertTrue(any(BORON_WARNING in warning for warning in compound['all_warnings']))
                    self.assertEqual(result['error_stats']['dp5_boron_not_supported'], 1)
                    self.assertEqual(result['error_stats']['dp5_not_applicable'], 0)
                    self.assertNotIn('score_raw', compound['dp5'])
                    self.assertFalse((Path(tmp) / 'dp5_artifacts').exists())
                    continue
                warning = next(line for line in compound["all_warnings"] if "dp5_not_applicable." in line)
                self.assertIn(fragment, warning)
                self.assertIn("trained vocabulary" if reason == "unsupported_element" else "fragment", warning.lower())
                self.assertNotIn("some atom types", warning)
                self.assertEqual(compound["highlight_info"]["color"], "blue")
                self.assertEqual(result["error_stats"]["dp5_not_applicable"], 1)
                self.assertFalse((Path(tmp) / "dp5_artifacts").exists())

    def test_multiple_carbon_components_are_info_skips_before_geometry(self):
        for smiles in ('CC[NH3+].[O-]C(=O)C(F)(F)F', 'C[NH3+].CC(=O)[O-]', 'CCO.CCC'):
            with self.subTest(smiles=smiles), tempfile.TemporaryDirectory() as tmp:
                self.cfg.paths.output_dir = Path(tmp)
                self.cfg.dp5.keep_artifacts = True
                compound = {'id': 1, 'smiles': smiles, 'run_dp5': True,
                            'dp5_c_signals_list': [35., 13.], 'dp5_carbon_groups': []}
                with patch('checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs',
                           side_effect=AssertionError('mixtures must not embed')):
                    result, requests = self.enrich({'compounds': [compound]}, real_engine=True)
                self.assertEqual(requests, [])
                self.assertEqual(compound['dp5'], {'status': 'skipped', 'reason': 'multiple_carbon_components',
                    'message': 'DP5q not performed: more than one component contains carbon.',
                    'message_severity': 'info'})
                self.assertEqual(compound['smiles'], smiles)
                self.assertEqual(result['dp5_summary']['skipped'], 1)
                self.assertFalse((Path(tmp) / 'dp5_artifacts').exists())

    def test_carbon_free_counterions_and_water_are_ignored_without_neutralizing(self):
        for smiles, shifts, groups in (
            ('C[N+](C)(C)C.[Br-]', [55.], [['C1', 'C3', 'C4', 'C5']]),
            ('[Na+].CC(=O)[O-]', [181.5, 25.9], []),
            ('CC(=O)O.O', [178., 20.8], []),
        ):
            compound = {'id': 1, 'smiles': smiles, 'run_dp5': True,
                        'dp5_c_signals_list': shifts, 'dp5_carbon_groups': groups}
            with self.subTest(smiles=smiles):
                result, requests = self.enrich({'compounds': [compound]}, real_engine=True)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].smiles, smiles)
                self.assertEqual(compound['smiles'], smiles)
                self.assertEqual(compound['dp5']['status'], 'success', compound['dp5'])
                self.assertEqual(result['dp5_summary']['successful'], 1)
                self.assertEqual(compound['dp5']['provenance']['component_scope']['original_smiles'], smiles)
                self.assertIn('DP5q calculated for the organic component only; the counter-ion/solvate was ignored, '
                              'and protonation or salt formation may shift some 13C signals.',
                              ' '.join(compound['all_warnings']))

    def test_boron_counterion_retains_the_whole_source_boron_skip(self):
        from checksi.dp5q.observation_policy import BORON_REASON, BORON_WARNING
        smiles = 'C[N+](C)(C)C.F[B-](F)(F)F'
        compound = {'id': 1, 'smiles': smiles, 'run_dp5': True,
                    'dp5_c_signals_list': [55.], 'dp5_carbon_groups': [['C1', 'C3', 'C4', 'C5']]}
        result, requests = self.enrich({'compounds': [compound]}, real_engine=True)
        self.assertEqual(requests, [])
        self.assertEqual(compound['dp5']['reason'], BORON_REASON)
        self.assertEqual(compound['smiles'], smiles)
        self.assertTrue(any(BORON_WARNING in line for line in compound['all_warnings']))
        self.assertEqual(result['dp5_summary']['skipped'], 1)

    def test_organic_component_warning_preserves_the_full_stereoisomer_warning(self):
        smiles = '[Cl-].CN1CC1C(O)C(F)C(Cl)C(Br)C'
        count = sum(atom.GetAtomicNum() == 6 for atom in Chem.MolFromSmiles(smiles).GetAtoms())
        shifts = ', '.join(f'{20. + 10*i:.1f}' for i in range(count))
        result, requests = self.enrich(self.analyze(smiles, shifts), real_engine=True)
        self.assertEqual(len(requests), 1)
        compound = result['compounds'][0]
        self.assertEqual(compound['dp5']['status'], 'success', compound['dp5'])
        warnings = ' '.join(compound['all_warnings'])
        self.assertIn('DP5q calculated for the organic component only; the counter-ion/solvate was ignored, '
                      'and protonation or salt formation may shift some 13C signals.', warnings)
        self.assertIn('DP5q calculated for one stereoisomer only; another isomer may give a very different result.',
                      warnings)

    def test_child_keeps_independent_coverage_defense(self):
        request = CheckRequest(compound_id="1", smiles="CCO", carbon_shifts=[60.0])
        with patch("checksi.dp5q.engine.prepare_geometry", side_effect=AssertionError("coverage must precede embedding")):
            result = check_compound(request, self.models)
        self.assertEqual(result.status, "not_applicable")
        self.assertEqual(result.reason, "strict_nmr_coverage_mismatch")
        self.assertIsNone(result.score_raw)

    def test_lowest_finite_usable_energy_conformer_is_selected(self):
        def embed(mol, numConfs, params):
            mol.RemoveAllConformers()
            for conf_id in [3, 7, 11, 15, 19]:
                conformer = Chem.Conformer(mol.GetNumAtoms())
                conformer.SetId(conf_id)
                for atom_idx in range(mol.GetNumAtoms()):
                    conformer.SetAtomPosition(atom_idx, Point3D(float(conf_id), float(atom_idx), 0.0))
                mol.AddConformer(conformer, assignId=False)
            self.assertEqual(numConfs, 30)
            return [3, 7, 11, 15, 19]

        energies = [(0, 8.0), (0, -4.0), (1, -90.0), (0, float("nan")), (0, 12.0)]
        def optimize(mol, **kwargs):
            return list(energies) if mol.GetNumConformers() == 5 else [(1, -90.0)]
        with patch("checksi.dp5q.geometry.rdDistGeom.EmbedMultipleConfs", side_effect=embed), patch("checksi.dp5q.geometry.rdForceFieldHelpers.MMFFOptimizeMoleculeConfs", side_effect=optimize) as optimize_mock:
            prepared = prepare_geometry("CCO")
        self.assertEqual(prepared.conformer_ids, (7,))
        self.assertEqual(prepared.mol.GetConformer(7).GetAtomPosition(0).x, 7.0)
        self.assertEqual([call.kwargs['maxIters'] for call in optimize_mock.call_args_list], [500, 1500])
        self.assertEqual(next(item for item in prepared.diagnostics['candidates'] if item['conformer_id'] == 11)['selection'], 'not_converged')
