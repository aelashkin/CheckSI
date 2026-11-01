"""B applicability is an explicit warning/skip, independent of readiness."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.dp5_enrichment import DP5PreparationSkip, _compound_request, enrich_analysis_with_dp5
from checksi.dp5q.client import DP5qSession
from checksi.dp5q.client import DP5qWorkerUnavailable
from checksi.dp5q.contracts import CheckRequest, CheckResult, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.network import load_models
from checksi.dp5q.observation_policy import BORON_REASON, BORON_WARNING
from checksi.errors import ErrorCode


class BoronPreparationTests(unittest.TestCase):
    def test_mixed_run_keeps_boron_skipped_when_nonboron_readiness_fails(self):
        cfg = ConfigManager.load_factory()
        cfg.dp5.enabled = True
        analysis = {'compounds': [
            {'id': 1, 'smiles': 'CB', 'all_warnings': []},
            {'id': 2, 'smiles': 'CCO', 'all_warnings': [], 'run_dp5': True,
             'dp5_carbon_groups': [], 'dp5_c_signals_list': [18.5, 58.]},
            {'id': 3, 'smiles': 'BCCC', 'all_warnings': []}], 'error_stats': {}}
        with patch('checksi.dp5_enrichment.DP5qSession') as session:
            checker = session.return_value.__enter__.return_value
            checker.check.side_effect = DP5qWorkerUnavailable('worker_unavailable', 'controlled unavailable model')
            result = enrich_analysis_with_dp5(cfg, analysis)
        self.assertEqual([item['dp5']['status'] for item in result['compounds']], ['skipped', 'failed', 'skipped'])
        self.assertEqual([result['compounds'][index]['dp5']['reason'] for index in (0, 2)], [BORON_REASON] * 2)
        self.assertEqual(checker.check.call_count, 1)
        self.assertEqual(result['dp5_summary']['failed'], 1)

    def test_boron_only_run_never_checks_readiness_or_starts_calculation(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                cfg = ConfigManager.load_factory()
                cfg.dp5.enabled = True
                if disabled:
                    cfg.errors.overrides[ErrorCode.DP5_BORON_NOT_SUPPORTED] = ErrorSeverity.DISABLED
                analysis = {'compounds': [{'id': 1, 'smiles': 'CB', 'all_warnings': []}], 'error_stats': {}}
                with patch('checksi.dp5_enrichment.check_availability',
                           side_effect=AssertionError('readiness must stay lazy')) as ready, \
                     patch('checksi.dp5_enrichment.DP5qSession') as session:
                    result = enrich_analysis_with_dp5(cfg, analysis)
                ready.assert_not_called()
                session.return_value.__enter__.return_value.check.assert_not_called()
                compound = result['compounds'][0]
                self.assertEqual(compound['dp5']['status'], 'skipped')
                self.assertEqual(compound['dp5']['reason'], BORON_REASON)
                self.assertEqual(result['dp5_summary']['failed'], 0)
                self.assertEqual(any(BORON_WARNING in text for text in compound['all_warnings']), not disabled)

    def test_boron_skip_precedes_missing_shift_or_coverage_refusals(self):
        cfg = ConfigManager.load_factory()
        with self.assertRaises(DP5PreparationSkip) as raised:
            _compound_request(cfg, {'smiles': 'F[B-](F)(F)F.[K+]', 'run_dp5': False}, 1)
        self.assertEqual(raised.exception.reason, BORON_REASON)
        self.assertEqual(raised.exception.warning_code, ErrorCode.DP5_BORON_NOT_SUPPORTED)


class BoronWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()

    def test_engine_skips_before_geometry_and_inference(self):
        request = CheckRequest(compound_id='B', smiles='OB(O)c1ccccc1', carbon_shifts=[10.])
        stages = []
        with patch('checksi.dp5q.engine.prepare_geometry') as geometry, \
             patch.object(self.models, 'predict') as predict:
            result = check_compound(request, self.models, progress=stages.append)
        self.assertEqual((result.status, result.reason), ('not_applicable', BORON_REASON))
        self.assertEqual(result.diagnostics['message'], BORON_WARNING)
        self.assertEqual(result.assignments, [])
        self.assertIsNone(result.score_raw)
        geometry.assert_not_called()
        predict.assert_not_called()
        self.assertNotIn('geometry', stages)

    def test_real_child_and_host_agree_on_skip_and_reject_coherent_assessed_boron(self):
        with DP5qSession() as session:
            request = CheckRequest(compound_id='B', smiles='CB', carbon_shifts=[50.])
            skipped = session.check(request)
            self.assertEqual((skipped.status, skipped.reason), ('not_applicable', BORON_REASON))
            self.assertEqual(skipped.diagnostics['message'], BORON_WARNING)
            self.assertEqual(skipped.assignments, [])
            self.assertIsNone(skipped.score_raw)
            source = session.check(CheckRequest(compound_id='B', smiles='CO', carbon_shifts=[50.]))
            self.assertEqual(source.status, 'assessed', source)
            values = deepcopy(source.model_dump(mode='python'))
            values['provenance']['selected_isomer_smiles'] = 'CB'
            with self.assertRaisesRegex(ValueError, '[Bb]oron'):
                validate_result_for_request(request, CheckResult.model_validate(values), session.metadata)


if __name__ == '__main__':
    unittest.main()
