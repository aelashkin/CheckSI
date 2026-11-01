"""Workbook scope preserves measured evidence and legacy diagnostic counts."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.errors.tracker import ERROR_NAMES, ErrorTracker
from checksi.report_service import export_reports
from checksi.statistics_export import export_error_stats
from checksi.utils.error_handler import export_error_stats as legacy_export


def shared_compound():
    return {
        'id': 1, 'identifier': 'valine', 'name': 'Valine', 'original_text': '13C NMR data.',
        'all_warnings': [], 'carbon_scope': {
            'full_environment_count': 5, 'native_candidate_environment_count': 5,
            'observation_count': 4, 'represented_signal_count': 4,
            'accepted_missing_direct_cb_count': 0, 'accepted_missing_direct_cb_environments': [],
            'partition_complete': True, 'configuration_status': 'admissible',
        },
        'stereo_assessment': {'status': 'confirmed_non_meso', 'reason': None,
                              'enumeration_complete': True},
        'dp5': {'status': 'success', 'score_percent': 75.0, 'diagnostics': {
            'full_environment_count': 5, 'observation_count': 4, 'scored_environment_count': 5,
            'coincident_observations': {'3': [0, 4]}, 'score_scope': 'coincident_observations',
            'scope_note': 'Separate predictions share one original observation.',
        }, 'provenance': {'selected_isomer_smiles': 'CC(C)[C@H](N)C(=O)O'}, 'atoms': [
            {'label': f'C{index + 1}', 'atom_labels': [f'C{index + 1}'],
             'assigned_shift': [20., 60., 170., 30., 20.][index],
             'calculated_shift': [19., 61., 172., 31., 22.][index],
             'observation_index': [3, 0, 1, 2, 3][index],
             'evidence_weight': .5 if index in (0, 4) else 1.,
             'error': None, 'score_percent': 80. + index}
            for index in range(5)
        ]},
    }


def rows_by_header(sheet):
    values = iter(sheet.values)
    headers = next(values)
    return [dict(zip(headers, row)) for row in values]


class CarbonScopeWorkbookTests(unittest.TestCase):
    def test_configuration_columns_keep_counts_warnings_and_separate_isomer_scope(self):
        compound = shared_compound()
        compound['carbon_scope'].update(expected_counts=[4, 6], configuration_dependent=True,
            isomer_scope_dependent=True, selected_isomer_smiles='preparation-identity')
        compound['all_warnings'] = ['nmr_configuration_dependent. expected 4 or 6 depending on configuration',
                                    'dp5_configuration_dependent. another isomer may give a very different result']
        with tempfile.TemporaryDirectory() as directory:
            path = export_error_stats({}, 'source.pdf', str(Path(directory) / 'statistics.xlsx'),
                                      open_file=False, compounds=[compound])
            workbook = openpyxl.load_workbook(path)
            try:
                scope, = rows_by_header(workbook['Carbon scope'])
                self.assertEqual(json.loads(scope['Expected carbon counts by configuration']), [4, 6])
                self.assertTrue(scope['Carbon grouping depends on configuration'])
                self.assertTrue(scope['Any nucleus grouping depends on configuration'])
                self.assertIn('4 or 6', scope['Configuration warning'])
                self.assertIn('very different result', scope['Conditional DP5 warning'])
                self.assertEqual(scope['Selected preparation isomer SMILES'], 'preparation-identity')
                self.assertEqual(scope['Selected isomer SMILES'], 'CC(C)[C@H](N)C(=O)O')
            finally:
                workbook.close()

    def test_legacy_call_keeps_only_canonical_error_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            path = legacy_export({'c13_boron_signal_not_observed': 1}, '=source.pdf',
                                 str(Path(directory) / 'statistics.xlsx'), open_file=False)
            with_workbook = openpyxl.load_workbook(path)
            try:
                self.assertEqual(with_workbook.sheetnames, ['error_stat'])
                self.assertEqual(list(with_workbook.active.values)[0], ('ERROR', '=source.pdf'))
                self.assertEqual(with_workbook.active['B1'].data_type, 's')
                self.assertEqual([row[0] for row in list(with_workbook.active.values)[1:]], ERROR_NAMES)
                self.assertEqual(dict(list(with_workbook.active.values)[1:])['c13_boron_signal_not_observed'], 1)
            finally:
                with_workbook.close()

    def test_shared_rows_keep_original_indices_weights_and_separate_predictions(self):
        compounds = [shared_compound()]
        before = deepcopy(compounds)
        with tempfile.TemporaryDirectory() as directory:
            path = export_error_stats({}, 'source.pdf', str(Path(directory) / 'statistics.xlsx'),
                                      open_file=False, compounds=compounds)
            workbook = openpyxl.load_workbook(path)
            try:
                self.assertEqual(workbook.sheetnames, ['error_stat', 'Carbon scope', 'DP5 assignments'])
                scope, = rows_by_header(workbook['Carbon scope'])
                self.assertEqual([scope[key] for key in ('Full expected environments', 'Listed observations',
                    'Represented signals', 'Accepted absent direct C–B environments', 'Scored environments')],
                    [5, 4, 4, 0, 5])
                self.assertEqual(scope['Structural meso status'], 'confirmed_non_meso')
                self.assertEqual(scope['DP5 status'], 'success')
                self.assertEqual(scope['DP5 score (%)'], 75.)
                self.assertEqual(json.loads(scope['Shared observations']), {'3': [0, 4]})
                assignments = rows_by_header(workbook['DP5 assignments'])
                self.assertEqual([row['Carbon environment'] for row in assignments], ['C1', 'C2', 'C3', 'C4', 'C5'])
                self.assertEqual([row['Original observation index (0-based)'] for row in assignments], [3, 0, 1, 2, 3])
                self.assertEqual([row['Evidence weight'] for row in assignments], [.5, 1., 1., 1., .5])
                self.assertEqual(sum(row['Evidence weight'] for row in assignments), 4.)
                self.assertEqual([assignments[index]['Calculated shift (ppm)'] for index in (0, 4)], [19., 22.])
                self.assertEqual([assignments[index]['Experimental shift (ppm)'] for index in (0, 4)], [20., 20.])
                self.assertFalse(any('omitt' in str(cell.value).lower() for sheet in workbook for row in sheet for cell in row))
            finally:
                workbook.close()
        self.assertEqual(compounds, before)

    def test_boron_scope_retains_full_count_and_no_score_or_assignment_rows(self):
        compound = shared_compound()
        compound['carbon_scope'].update(full_environment_count=18, observation_count=17,
            represented_signal_count=17, accepted_missing_direct_cb_count=1,
            accepted_missing_direct_cb_environments=[['C6']])
        compound['stereo_assessment'] = {'status': 'unassessed', 'reason': 'unsupported_representation'}
        # A non-success status must withhold even stale data in a legacy dictionary.
        compound['dp5'].update(status='skipped', reason='boron_not_supported')
        with tempfile.TemporaryDirectory() as directory:
            path = legacy_export({'dp5_boron_not_supported': 1, 'c13_boron_signal_not_observed': 1},
                'source.pdf', str(Path(directory) / 'statistics.xlsx'), open_file=False, compounds=[compound])
            workbook = openpyxl.load_workbook(path)
            try:
                scope, = rows_by_header(workbook['Carbon scope'])
                self.assertEqual([scope[key] for key in ('Full expected environments', 'Listed observations',
                    'Represented signals', 'Accepted absent direct C–B environments')], [18, 17, 17, 1])
                self.assertEqual(json.loads(scope['Accepted absent direct C–B labels']), [['C6']])
                self.assertEqual((scope['Structural meso status'], scope['Structural meso reason']),
                                 ('unassessed', 'unsupported_representation'))
                self.assertEqual((scope['DP5 status'], scope['DP5 reason']), ('skipped', 'boron_not_supported'))
                self.assertIsNone(scope['DP5 score (%)'])
                self.assertIsNone(scope['Scored environments'])
                self.assertEqual(rows_by_header(workbook['DP5 assignments']), [])
                counts = dict(list(workbook['error_stat'].values)[1:])
                self.assertEqual((counts['dp5_boron_not_supported'], counts['c13_boron_signal_not_observed']), (1, 1))
                self.assertEqual(counts['c13_count_structure_check'], 0)
            finally:
                workbook.close()

    def test_controller_boron_explanations_follow_existing_warning_visibility(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled), tempfile.TemporaryDirectory() as directory:
                cfg = ConfigManager.load_factory()
                cfg.dp5.enabled = True
                if disabled:
                    for code in ('dp5_boron_not_supported', 'c13_boron_signal_not_observed'):
                        cfg.errors.overrides[code] = ErrorSeverity.DISABLED
                tracker = ErrorTracker()
                description = CheckSIController(cfg)._analyze_single_description(
                    '13C NMR (100 MHz, CDCl3): δ 180.0.', 'CCB(O)O', tracker)
                compound = {**description, 'id': 1, 'smiles': 'CCB(O)O',
                            'all_warnings': description['warnings']}
                data = enrich_analysis_with_dp5(cfg, {'compounds': [compound], 'error_stats': tracker.to_dict()})
                self.assertEqual(data['compounds'][0]['dp5'], {'status': 'skipped', 'reason': 'boron_not_supported'})
                before = deepcopy(data)
                path = export_error_stats(data['error_stats'], 'source.pdf',
                    str(Path(directory) / 'statistics.xlsx'), open_file=False, compounds=data['compounds'])
                workbook = openpyxl.load_workbook(path)
                try:
                    scope, = rows_by_header(workbook['Carbon scope'])
                    self.assertEqual((scope['Full expected environments'], scope['Listed observations'],
                                      scope['Accepted absent direct C–B environments']), (2, 1, 1))
                    for column, code in (('DP5 message', 'dp5_boron_not_supported'),
                                         ('Accepted absence note', 'c13_boron_signal_not_observed')):
                        existing = [line for line in compound['all_warnings'] if code + '.' in line]
                        self.assertEqual(scope[column], '\n'.join(existing) or None)
                        self.assertEqual(bool(existing), not disabled)
                    self.assertEqual(rows_by_header(workbook['DP5 assignments']), [])
                finally:
                    workbook.close()
                self.assertEqual(data, before)

    def test_source_strings_are_literal_text_in_all_sheets(self):
        for prefix in ('=', '+', '-', '@'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                compound = shared_compound()
                value = prefix + 'untrusted(1)'
                compound.update(id=value, identifier=value, name=value)
                compound['stereo_assessment']['reason'] = value
                compound['dp5']['diagnostics']['scope_note'] = value
                compound['dp5']['diagnostics']['message'] = value
                compound['dp5']['atoms'][0]['label'] = value
                path = export_error_stats({}, value, str(Path(directory) / 'statistics.xlsx'),
                                          open_file=False, compounds=[compound])
                workbook = openpyxl.load_workbook(path)
                try:
                    matches = [cell for sheet in workbook for row in sheet for cell in row if cell.value == value]
                    self.assertGreaterEqual(len(matches), 10)
                    self.assertTrue(all(cell.data_type == 's' for cell in matches))
                    self.assertFalse(any(cell.data_type == 'f' for sheet in workbook for row in sheet for cell in row))
                finally:
                    workbook.close()

    def test_report_service_passes_compounds_through_existing_patch_point(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = ConfigManager.load_factory()
            cfg.paths.input_path = Path(directory) / 'input.pdf'
            cfg.paths.output_dir = Path(directory)
            data = {'total_found': 1, 'error_stats': {}, 'compounds': [shared_compound()]}
            before = deepcopy(data)
            with patch('checksi.report_service.export_error_stats', wraps=legacy_export) as exporter:
                paths = export_reports(cfg, data, log_callback=lambda _: None)
            self.assertIs(exporter.call_args.kwargs['compounds'], data['compounds'])
            workbook = openpyxl.load_workbook(paths['error_stats_path'])
            try:
                self.assertEqual(len(rows_by_header(workbook['DP5 assignments'])), 5)
            finally:
                workbook.close()
            self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
