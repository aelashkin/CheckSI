"""Scoped analytical evidence survives human-readable exports without mutation."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

import fitz
from lxml import html

from checksi.config_schema import AppConfig
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_error
from checksi.errors.tracker import ErrorTracker
from checksi.report_rendering import _carbon_scope_html, _dp5_html
from checksi.report_service import export_reports


def scope_config(directory):
    return AppConfig.model_validate({
        'paths': {'input_path': 'synthetic-scope.docx', 'output_dir': str(directory)},
        'modes': {'deep_name_analysis': 'off', 'output_mode': 'essential'},
        'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
        'dp5': {'enabled': True, 'score_threshold_percent': 10},
    })


def shared_compound():
    """Renderer fixture only; no geometry, experimental or worker claim."""
    return {
        'id': 1, 'name': '<script>literal name</script>',
        'original_text': 'Synthetic overlap renderer control.', 'all_warnings': [],
        'carbon_scope': {'full_environment_count': 5, 'observation_count': 4,
                         'represented_signal_count': 5, 'accepted_missing_direct_cb_count': 0,
                         'partition_complete': True, 'scoring_partition_complete': True},
        'stereo_assessment': {'status': 'confirmed_non_meso', 'configuration_status': 'admissible',
                              'enumeration_complete': True, 'rs_center_count': 1},
        'dp5': {'status': 'success', 'score_percent': 55., 'atoms': [
            {'label': 'C1', 'assigned_shift': 20., 'calculated_shift': 19.,
             'error': None, 'score_percent': 50., 'observation_index': 3, 'evidence_weight': .5},
            {'label': 'C3', 'assigned_shift': 20., 'calculated_shift': 21.,
             'error': 1., 'score_percent': 60., 'observation_index': 3, 'evidence_weight': .5},
        ], 'diagnostics': {'full_environment_count': 5, 'observation_count': 4,
            'scored_environment_count': 5, 'coincident_observations': {'3': [0, 2]},
            'score_scope': 'coincident_observations',
            'scope_note': 'Full 5 carbon environments are compared with 4 original observations. '
                          'Coincident environments share measured evidence without averaging predictions. '
                          'Selected isomer only; other configurations, if present, may differ.'}},
    }


class CarbonScopeReportTests(unittest.TestCase):
    def test_configuration_warnings_and_neutral_scope_survive_html_pdf_json(self):
        compound = shared_compound()
        compound['carbon_scope'].update(full_environment_count=4, expected_counts=[4, 6],
            configuration_dependent=True, isomer_scope_dependent=True,
            selected_isomer_smiles='C[C@@H]([C@H](C)O)[C@@H](C)O')
        compound['stereo_assessment']['status'] = 'meso_possible'
        self.assertNotIn('class="warning"', _carbon_scope_html(compound))
        with tempfile.TemporaryDirectory() as directory:
            config = scope_config(directory)
            tracker = ErrorTracker()
            compound['all_warnings'] = [
                emit_error(config, tracker, ErrorCode.NMR_CONFIGURATION_DEPENDENT,
                           context={'nuclei': '13C', 'counts': '4 or 6'}),
                emit_error(config, tracker, ErrorCode.DP5_CONFIGURATION_DEPENDENT,
                           context={'selected_isomer_smiles': compound['carbon_scope']['selected_isomer_smiles']}),
            ]
            analysis = {'total_found': 1, 'error_stats': tracker.to_dict(), 'compounds': [compound]}
            before = deepcopy(analysis)
            report = export_reports(config, analysis, log_callback=lambda _: None)
            self.assertFalse(report['partial'])
            html_text = html.fromstring(Path(report['html_path']).read_text()).text_content()
            with fitz.open(report['pdf_path']) as document:
                pdf_text = unicodedata.normalize('NFKC', ' '.join(
                    ' '.join(page.get_text() for page in document).split()))
            for text in (html_text, pdf_text):
                self.assertIn('4 or 6', text)
                self.assertIn('very different result', text)
                self.assertIn('meso_possible', text)
                self.assertNotIn('Structural meso assessment: confirmed_meso', text)
            self.assertEqual(json.loads(Path(report['analysis_path']).read_text())['analysis'], before)
            self.assertEqual(analysis, before)

    def test_shared_rows_stay_visible_above_threshold_with_original_peak_and_weight(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = html.fromstring(_dp5_html(shared_compound(), scope_config(directory)))
        tables = tree.xpath('.//table[@class="dp5"]')
        self.assertEqual(len(tables), 1)
        headers = tables[0].xpath('.//th/text()')
        self.assertIn('Original peak (1-based)', headers)
        self.assertIn('Evidence weight', headers)
        rows = [[cell.text_content() for cell in row] for row in tables[0].xpath('.//tbody/tr')]
        self.assertEqual([row[0] for row in rows], ['C1', 'C3'])
        self.assertEqual([row[2] for row in rows], ['19.00', '21.00'])
        self.assertEqual([row[5:] for row in rows], [['4 (shared)', '0.50'], ['4 (shared)', '0.50']])
        self.assertEqual(rows[0][3], '')
        self.assertIn('without averaging predictions', tree.text_content())

    def test_boron_skip_explains_no_score_even_if_stale_rows_are_present(self):
        compound = shared_compound()
        warning = ('DP5q not calculated: boron-containing compounds are not supported '
                   '(no validated geometry/prediction for boron)')
        compound['dp5'].update(status='skipped', reason='boron_not_supported',
                               diagnostics={'message': warning})
        with tempfile.TemporaryDirectory() as directory:
            tree = html.fromstring(_dp5_html(compound, scope_config(directory)))
        self.assertIn(warning, tree.text_content())
        self.assertNotIn('compatibility score:', tree.text_content())
        self.assertEqual(tree.xpath('.//table'), [])

    def test_html_pdf_json_keep_full_scope_meso_status_and_original_evidence(self):
        shared = shared_compound()
        boron = deepcopy(shared)
        boron.update(id=2, name='Synthetic boron control', original_text='Separate source description.')
        boron['carbon_scope'].update(full_environment_count=18, observation_count=17,
            represented_signal_count=17, accepted_missing_direct_cb_count=1,
            accepted_missing_direct_cb_environments=[['C6']],
            scoring_partition_complete=False, scoring_partition_reason='unsupported_representation')
        boron['stereo_assessment'].update(status='unassessed', enumeration_complete=False,
                                         reason='unsupported_stereo_inventory')
        boron['dp5'] = {'status': 'skipped', 'reason': 'boron_not_supported', 'diagnostics': {
            'message': 'DP5q not calculated: boron-containing compounds are not supported '
                       '(no validated geometry/prediction for boron)'}}
        analysis = {'total_found': 2, 'error_stats': {}, 'compounds': [shared, boron]}
        before = deepcopy(analysis)
        with tempfile.TemporaryDirectory() as directory:
            report = export_reports(scope_config(directory), analysis, log_callback=lambda _: None)
            self.assertFalse(report['partial'])
            tree = html.fromstring(Path(report['html_path']).read_text())
            self.assertFalse(tree.xpath('//script'))
            text = tree.xpath('//body')[0].text_content()
            for expected in ('Full carbon environments: 18', 'Original observations: 17',
                             'Represented signals: 17', 'Accepted missing direct C–B environments: 1',
                             'C6', 'Structural meso assessment: confirmed_non_meso',
                             'Structural meso assessment: unassessed', 'unsupported_stereo_inventory'):
                self.assertIn(expected, text)
            with fitz.open(report['pdf_path']) as document:
                pdf_text = unicodedata.normalize('NFKC', ' '.join(
                    ' '.join(page.get_text() for page in document).split()))
                self.assertIn('without averaging predictions', pdf_text)
                self.assertIn('no validated geometry/prediction for boron', pdf_text)
                self.assertIn('Structural meso assessment: confirmed_non_meso', pdf_text)
            self.assertEqual(json.loads(Path(report['analysis_path']).read_text())['analysis'], before)
        self.assertEqual(analysis, before)
