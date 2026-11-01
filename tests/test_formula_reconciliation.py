"""Reconcile native independent formula evidence without guessing components."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from checksi.analysis.cross_checks import reconcile_compositions, source_composition
from checksi.analysis.hrms import adjust_mass_by_hrms
from checksi.controller import CheckSIController
from checksi.report_service import export_reports
from checksi.report_rendering import _formula_html
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


NMR = ("1H NMR (400 MHz, CDCl3): δ 3.60 (t, J = 6.4 Hz, 2H), 2.00 (s, 1H), "
       "1.60 (m, 2H), 1.00 (t, J = 7.2 Hz, 3H). "
       "13C NMR (100 MHz, CDCl3): δ 63.0, 25.0, 10.0. ")
HRMS = "HRMS (ESI): calcd for C3H9O+ [M+H]+ 61.0648, found 61.0648. "
EA_PROPANOL = "Anal. Calcd for C3H8O: C, 59.96; H, 13.42; O, 26.62. Found: C, 59.96; H, 13.42; O, 26.62."
EA_ETHANOL = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13; O, 34.73. Found: C, 52.14; H, 13.13; O, 34.73."


class IndependentFormulaTests(unittest.TestCase):
    def evaluate(self, ea=EA_PROPANOL, hrms=HRMS, smiles='CCCO', overrides=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config(overrides=overrides))._analyze_single_description(NMR + hrms + ea, smiles, tracker)
        return result, tracker

    def test_native_matching_sources_retain_independent_compositions(self):
        result, tracker = self.evaluate()
        sources = result['formula_sources']
        self.assertEqual(sources['ea']['composition']['counts'], {'C': 3, 'H': 8, 'O': 1})
        self.assertEqual(sources['hrms']['composition']['counts'], {'C': 3, 'H': 9, 'O': 1})
        self.assertEqual(sources['hrms_neutral']['composition']['counts'], {'C': 3, 'H': 8, 'O': 1})
        self.assertEqual(sources['name']['composition']['counts'], {'C': 3, 'H': 8, 'O': 1})
        self.assertEqual([pair['status'] for pair in result['formula_comparisons']], ['agree'] * 3)
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_internally_valid_copied_ea_cannot_disappear_behind_hrms_preference(self):
        result, tracker = self.evaluate(ea=EA_ETHANOL)
        self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)
        self.assertEqual([p['status'] for p in result['formula_comparisons']], ['disagree', 'disagree', 'agree'])
        line, = [line for line in result['warnings'] if 'formula_source_mismatch.' in line]
        self.assertIn('C2H6O', line)
        self.assertIn('C3H8O', line)

    def test_disabled_or_overridden_mismatch_retains_the_same_science(self):
        for severity in ('disabled', 'info', 'warning', 'critical'):
            with self.subTest(severity=severity):
                result, tracker = self.evaluate(ea=EA_ETHANOL, overrides={'formula_source_mismatch': severity})
                self.assertEqual(tracker.get_count('formula_source_mismatch'), int(severity != 'disabled'))
                self.assertEqual(len([p for p in result['formula_comparisons'] if p['status'] == 'disagree']), 2)
                self.assertEqual(len([line for line in result['warnings'] if 'formula_source_mismatch.' in line]), int(severity != 'disabled'))

    def test_missing_identity_is_unavailable_without_new_review_warning(self):
        result, tracker = self.evaluate(ea='', hrms='', smiles=None)
        self.assertTrue(all(p['status'] == 'unavailable' for p in result['formula_comparisons']))
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)

    def test_missing_peers_do_not_create_a_review_for_an_unrecovered_ion_alone(self):
        result, tracker = self.evaluate(ea='', hrms=HRMS.replace('[M+H]+', ''), smiles=None)
        self.assertTrue(all(pair['status'] == 'unavailable' for pair in result['formula_comparisons']))
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        self.assertFalse(result['formula_sources']['hrms_neutral']['composition']['review_required'])

    def test_legacy_abbreviation_expansion_retains_literal_and_normalized_evidence(self):
        result, tracker = self.evaluate(ea=EA_ETHANOL.replace('C2H6O:', 'BArF:'))
        source = result['formula_sources']['ea']
        self.assertEqual(source['formula'], 'C32H12BF24')
        self.assertTrue(source['usable'])
        self.assertEqual(source['composition']['state'], 'unknown')
        self.assertEqual(source['normalization']['pre_expansion_candidate']['raw'], 'BArF')
        original = NMR + HRMS + EA_ETHANOL.replace('C2H6O:', 'BArF:')
        literal = source['normalization']['pre_normalization_candidate']
        self.assertEqual(original[literal['start']:literal['end']], 'BArF')
        normalized = result['formula_normalization']['normalized_description']
        candidate = source['candidate']
        self.assertEqual(normalized[candidate['start']:candidate['end']], 'C32H12BF24')
        self.assertNotIn('steps', result['formula_normalization'])
        self.assertNotIn('replacements', source['normalization'])
        self.assertEqual(source['normalization']['pre_expansion_candidate'], {'raw': 'BArF'})
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertIn('BArF → C32H12BF24', _formula_html(result))

    def test_hrms_abbreviation_does_not_create_a_new_neutral_assumption(self):
        result, tracker = self.evaluate(ea='', hrms=HRMS.replace('C3H9O', 'BArF'))
        source = result['formula_sources']['hrms']
        self.assertEqual(source['normalization']['pre_expansion_candidate'], {'raw': 'BArF'})
        self.assertNotIn('pre_normalization_candidate', source['normalization'])
        self.assertIn('BArF → C32H12BF24', _formula_html(result))
        self.assertEqual(result['formula_sources']['hrms']['formula'], 'C32H12BF24')
        self.assertEqual(result['formula_sources']['hrms_neutral']['formula'], 'C32H11BF24')
        self.assertEqual(result['formula_comparisons'][-1]['status'], 'not_comparable')
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_matching_ea_hrms_do_not_hide_name_composition_disagreement(self):
        result, tracker = self.evaluate(smiles='CCO')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)
        self.assertEqual([p['status'] for p in result['formula_comparisons']], ['agree', 'disagree', 'disagree'])

    def test_explicit_ea_charge_remains_visible_instead_of_becoming_neutral(self):
        result, tracker = self.evaluate(ea=EA_PROPANOL.replace('C3H8O:', 'C3H8O+:'))
        self.assertEqual(result['formula_sources']['ea']['composition']['state'], 'unknown')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_review_visibility_never_changes_comparability(self):
        for severity in ('disabled', 'info', 'warning', 'critical'):
            result, tracker = self.evaluate(smiles='CCCO.[Cl-]', overrides={'formula_source_review_required': severity})
            self.assertEqual(tracker.get_count('formula_source_review_required'), int(severity != 'disabled'))
            self.assertEqual(len([line for line in result['warnings'] if 'formula_source_review_required.' in line]), int(severity != 'disabled'))
            self.assertEqual(result['formula_sources']['name']['composition']['state'], 'disconnected')
            self.assertTrue(all(pair['status'] == 'not_comparable' for pair in result['formula_comparisons'] if pair['right'] == 'name'))

    def test_enrichment_mismatch_keeps_the_name_formula_identity(self):
        for severity in ('disabled', 'info', 'warning', 'critical'):
            result, tracker = self.evaluate(ea='', smiles='CCC[18OH]', overrides={'formula_source_review_required': severity})
            pair = result['formula_comparisons'][-1]
            self.assertEqual(pair['status'], 'disagree')
            self.assertEqual(result['formula_sources']['name']['formula'], 'C3H8[18O]')
            self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
            self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)
            self.assertEqual(tracker.get_count('name_formula_mass_delta'), 1)

    def test_failed_inverse_adduct_retains_original_ion_without_false_mismatch(self):
        result, tracker = self.evaluate(hrms=HRMS.replace('[M+H]+', '[M+Na]+'))
        self.assertEqual(result['formula_sources']['hrms']['formula'], 'C3H9O')
        self.assertIsNone(result['formula_sources']['hrms_neutral']['formula'])
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), 1)

    def test_unsupported_and_invalid_tokens_cannot_displace_valid_independent_sources(self):
        for token in ('C2 H6O', 'C2H6Xx'):
            result, tracker = self.evaluate(ea=EA_ETHANOL.replace('C2H6O:', token + ':'))
            self.assertFalse(result['formula_sources']['ea']['usable'])
            self.assertEqual(result['formula_comparisons'][-1]['status'], 'agree')
            self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)

    def test_enrichment_labels_agree_only_when_the_counts_match(self):
        def source(name, formula):
            return source_composition(name, {'formula': formula, 'usable': True}, state='neutral', basis='test', reason='Explicit test source')
        fixed = source('hrms_neutral', 'C2H6[18O]')
        same = source('name', 'C2H6[18O]')
        ordinary = source('ea', 'C2H6O')
        self.assertEqual(dict(fixed.counts), {'C': 2, 'H': 6, '18O': 1})
        pair, = reconcile_compositions([fixed, same])
        self.assertEqual(pair.status, 'agree')
        pair, = reconcile_compositions([ordinary, fixed])
        self.assertEqual(pair.status, 'disagree')
        self.assertFalse(pair.review_required)

    def test_neutralization_details_are_additive_json_safe_and_cleared(self):
        details = {'stale': True}
        output, formula, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), '[M+H]+', [], 'C2H7O', 0, details=details)
        self.assertEqual(formula, 'C2H6O')
        self.assertAlmostEqual(mass, 46.04186481295, places=8)
        self.assertEqual(output, [])
        self.assertTrue(details['comparable'])
        self.assertEqual(json.loads(json.dumps(details)), details)
        adjust_mass_by_hrms(make_config(), ErrorTracker(), 'No ion', [], 'C2H7O', 0, details=details)
        self.assertNotIn('stale', details)
        self.assertFalse(details['usable'])

    def test_unresolved_ion_charge_state_preserves_arithmetic_without_neutral_comparison(self):
        for ion in ('[M+H]2+', '[M+H]+2', '[M+H+]-', '[M+H]++'):
            details = {}
            _, formula, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), ion, [], 'C2H7O', 0, details=details)
            self.assertEqual(formula, 'C2H6O')
            self.assertFalse(details['comparable'])

    def test_inverse_expression_from_another_section_is_not_comparable(self):
        details = {}
        text = 'Anal. Calcd [M+H]+. 1H NMR (400 MHz, CDCl3): δ 1.00. HRMS C2H7O+ 47.0491, found 47.0491.'
        _, formula, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), text, [], 'C2H7O', 0, details=details)
        self.assertEqual(formula, 'C2H6O')
        self.assertFalse(details['comparable'])
        text = 'HRMS [M+H]+ (earlier description). HRMS C2H7O+ 47.0491, found 47.0491.'
        adjust_mass_by_hrms(make_config(), ErrorTracker(), text, [], 'C2H7O', 0, details=details)
        self.assertFalse(details['comparable'])

    def test_grouped_adduct_stays_unresolved_but_supported_isotope_fragment_is_preserved(self):
        details = {}
        _, formula, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), '[M+[Cl]]+', [], 'C2H7ClO', 0, details=details)
        self.assertEqual(formula, 'C2H7O')
        self.assertFalse(details['comparable'])
        _, formula, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), '[M+[13C]H3]+', [], 'C2H7[13C]O', 0, details=details)
        self.assertEqual(formula, 'C2H4O')
        # The isotope atoms are retained, but methyl-adduct parent charge is unadjudicated.
        self.assertFalse(details['comparable'])

    def test_explicit_ionic_disconnected_and_zwitterionic_names_are_not_neutral_peers(self):
        for smiles in ('CCC[O-]', 'CCCO.[Cl-]', '[NH3+]CC[O-]'):
            with self.subTest(smiles=smiles):
                result, tracker = self.evaluate(smiles=smiles)
                pairs = [p for p in result['formula_comparisons'] if p['right'] == 'name']
                self.assertTrue(all(p['status'] == 'not_comparable' for p in pairs))
                self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(tracker.get_count('name_formula_mass_delta'), 0)

    def test_salt_token_retains_full_source_and_no_component_is_stripped(self):
        result, tracker = self.evaluate(ea=EA_PROPANOL.replace('C3H8O:', 'C3H8O.H2O:'))
        source = result['formula_sources']['ea']
        self.assertEqual(source['candidate']['raw'], 'C3H8O.H2O')
        self.assertTrue(source['usable'])
        self.assertEqual(source['composition']['counts'], {'C': 3, 'H': 10, 'O': 2})
        self.assertEqual(source['formula'], 'C3H8O.H2O')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_readable_refusals_are_escaped_and_distinguish_unsupported_from_absent(self):
        result, _ = self.evaluate(ea=EA_ETHANOL.replace('C2H6O:', 'C2H6O<script>:'), overrides={'formula_invalid_format': 'disabled'})
        html = _formula_html(result)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('unsupported / unknown', html)

    def test_missing_charge_keeps_conventional_adduct_comparison(self):
        result, tracker = self.evaluate(hrms=HRMS.replace('[M+H]+', '[M+H]'))
        neutral = result['formula_sources']['hrms_neutral']
        self.assertEqual(neutral['formula'], 'C3H8O')
        self.assertEqual(neutral['composition']['state'], 'neutral')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_json_retains_disabled_mismatch_while_readable_evidence_table_is_hidden(self):
        cfg = make_config(overrides={'formula_source_mismatch': 'disabled'})
        resolution = {'status': 'resolved', 'original_name': 'propan-1-ol', 'accepted_name': 'propan-1-ol'}
        def resolve(config, tracker, text, stats, **kwargs):
            kwargs['details'].update(resolution)
            return 'CCCO', 'propan-1-ol', '1', stats, []
        with patch('checksi.controller._smiles_and_name_from_paragraph', side_effect=resolve):
            analysis = CheckSIController(cfg).run_analysis(['propan-1-ol (1)', NMR + HRMS + EA_ETHANOL])
        self.assertEqual(analysis['assessment']['status'], 'complete')
        self.assertEqual(analysis['compounds'][0]['name_resolution']['accepted_name'], 'propan-1-ol')
        before = deepcopy(analysis)
        with tempfile.TemporaryDirectory() as temporary:
            cfg.paths.output_dir = Path(temporary)
            output = export_reports(cfg, analysis, log_callback=lambda _: None)
            self.assertFalse(output['partial'])
            saved = json.loads(Path(output['analysis_path']).read_text())
            self.assertEqual(saved['analysis'], before)
            html = Path(output['html_path']).read_text()
            self.assertNotIn('Formula source evidence', html)
            self.assertEqual(saved['analysis']['compounds'][0]['formula_comparisons'][0]['status'], 'disagree')
            with fitz.open(output['pdf_path']) as pdf:
                self.assertNotIn('Formula source evidence', ''.join(page.get_text() for page in pdf))
        self.assertEqual(analysis, before)


if __name__ == '__main__':
    unittest.main()
