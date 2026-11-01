"""Recognized anions do not supply required parent NMR nuclei or C–F coupling."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.counterions import counterion_only_nmr_elements
from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import DP5PreparationSkip, prepare_dp5_requests
from checksi.dp5q.contracts import CheckRequest
from checksi.errors import ErrorCode
from checksi.errors.run_diagnostics import RunDiagnostics


DESCRIPTION = '1H NMR (400 MHz, CDCl3): δ 3.10 (s, 12H). 13C NMR (100 MHz, CDCl3): δ 55.0.'
BF4 = 'F[B-](F)(F)F'
PF6 = 'F[P-](F)(F)(F)(F)F'
OTF = 'O=S(=O)([O-])C(F)(F)F'


class CounterionNMRSpectrumScopeTests(unittest.TestCase):
    def setUp(self):
        self.missing_nuclei = {}

    def analyze(self, smiles, *, description=DESCRIPTION, disabled=False):
        cfg = ConfigManager.load_factory()
        cfg.modes.deep_name_analysis = 'on'
        cfg.modes.output_mode = 'essential'
        cfg.dp5.enabled = True
        if disabled:
            cfg.errors.overrides[ErrorCode.REQUIRED_NMR_MISSING] = ErrorSeverity.DISABLED
            cfg.errors.overrides[ErrorCode.C13_COUPLING_MISSING_WITH_F] = ErrorSeverity.DISABLED
        diagnostics = RunDiagnostics(cfg)
        with patch('checksi.controller._smiles_and_name_from_paragraph',
                   return_value=(smiles, 'Compound', '1', {}, [])):
            result = CheckSIController(cfg).run_analysis(['Compound (1)', description], _diagnostics=diagnostics)
        self.missing_nuclei[id(result)] = {finding.context['element']
            for finding in diagnostics.snapshot().occurrences if finding.code is ErrorCode.REQUIRED_NMR_MISSING}
        return result, cfg

    def missing(self, result, element):
        symbol = {'19F': 'F', '11B': 'B', '31P': 'P'}[element]
        return symbol in self.missing_nuclei[id(result)]

    def test_exact_bf4_pf6_and_triflate_anions_do_not_require_parent_spectra_or_cf_coupling(self):
        for anion in (BF4, PF6, OTF):
            with self.subTest(anion=anion):
                result, _ = self.analyze('C[N+](C)(C)C.' + anion)
                self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 0)
                self.assertFalse(self.missing(result, '19F'))
                self.assertFalse(self.missing(result, '11B'))
                self.assertFalse(self.missing(result, '31P'))

    def test_covalent_parent_fluorine_keeps_its_missing_spectrum_and_cf_coupling_checks(self):
        for anion in (BF4, PF6, OTF):
            with self.subTest(anion=anion):
                result, _ = self.analyze('FC[N+](C)(C)C.' + anion)
                self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 1)
                self.assertTrue(self.missing(result, '19F'))
                self.assertFalse(self.missing(result, '11B'))
                self.assertFalse(self.missing(result, '31P'))

    def test_parent_boron_and_phosphorus_keep_their_required_nuclei(self):
        for parent, anion, nucleus in (('C[N+](C)(C)CCB(O)O', BF4, '11B'),
                                       ('C[P+](C)(C)C', PF6, '31P')):
            with self.subTest(parent=parent):
                result, _ = self.analyze(parent + '.' + anion)
                self.assertTrue(self.missing(result, nucleus))
                self.assertFalse(self.missing(result, '19F'))
                self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 0)

    def test_reported_counterion_nuclei_remain_present_in_the_whole_formula(self):
        for anion, spectrum in ((BF4, '19F NMR (376 MHz, CDCl3): δ -151.0. 11B NMR (128 MHz, CDCl3): δ -1.0.'),
                                (PF6, '19F NMR (376 MHz, CDCl3): δ -73.0. 31P NMR (162 MHz, CDCl3): δ -144.0.')):
            with self.subTest(anion=anion):
                result, _ = self.analyze('C[N+](C)(C)C.' + anion, description=DESCRIPTION + ' ' + spectrum)
                self.assertEqual(result['error_stats']['nmr_mentioned_but_element_missing'], 0)
                self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 0)

    def test_charged_graph_atom_order_does_not_change_counterion_recognition_or_source(self):
        for anion, excluded in (('[B-](F)(F)(F)F', {'B', 'F'}),
                                 ('[P-](F)(F)(F)(F)(F)F', {'P', 'F'}),
                                 ('FC(F)(F)S(=O)(=O)[O-]', {'F'})):
            with self.subTest(anion=anion):
                molecule = Chem.MolFromSmiles(anion + '.C[N+](C)(C)C')
                before = molecule.ToBinary()
                self.assertEqual(counterion_only_nmr_elements(molecule), frozenset(excluded))
                self.assertEqual(molecule.ToBinary(), before)

    def test_sodium_triflate_is_an_analyte_without_a_separate_organic_cation(self):
        result, _ = self.analyze('[Na+].' + OTF)
        self.assertTrue(self.missing(result, '19F'))
        self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 1)

    def test_isolated_inorganic_and_covalently_bound_fragments_are_not_hidden(self):
        for source in (BF4, '[Na+].' + BF4, 'C[B-](F)(F)F.[Na+]',
                       'CCO.[Na+].' + BF4, 'C[N+](C)(C)C.FB(F)F'):
            with self.subTest(source=source):
                result, _ = self.analyze(source)
                self.assertTrue(self.missing(result, '19F'))
                self.assertTrue(self.missing(result, '11B'))

    def test_formula_only_fallback_does_not_guess_a_counterion(self):
        text = DESCRIPTION + ' Anal. Calcd. for C4H12BF4N: C, 29.86; H, 7.52; N, 8.71. Found: C, 29.86; H, 7.52; N, 8.71.'
        result, _ = self.analyze(None, description=text)
        self.assertTrue(self.missing(result, '19F'))
        self.assertTrue(self.missing(result, '11B'))
        self.assertEqual(result['error_stats']['c13_coupling_missing_with_f'], 1)

    def test_disabling_findings_keeps_formula_carbon_and_dp5_boron_policy(self):
        for anion, formula, count, dp5_reason in (
            (BF4, 'C4H12BF4N', 1, 'boron_not_supported'),
            (PF6, 'C4H12F6NP', 1, None), (OTF, 'C5H12F3NO3S', 2, 'multiple_carbon_components')):
            source = 'C[N+](C)(C)C.' + anion
            visible, cfg = self.analyze(source)
            hidden, _ = self.analyze(source, disabled=True)
            first, second = visible['compounds'][0], hidden['compounds'][0]
            for field in ('smiles', 'formula_sources', 'formula_comparisons', 'carbon_scope',
                          'dp5_carbon_groups', 'dp5_c_signals_list', 'carbon_observations', 'run_dp5'):
                with self.subTest(anion=anion, field=field):
                    self.assertEqual(first[field], second[field])
            self.assertEqual(first['smiles'], source)
            self.assertEqual(first['formula_sources']['name']['formula'], formula)
            self.assertEqual(first['carbon_scope']['native_candidate_environment_count'], count)
            prepared, = prepare_dp5_requests(cfg, visible)
            if dp5_reason is not None:
                self.assertIsInstance(prepared, DP5PreparationSkip)
                self.assertEqual(prepared.reason, dp5_reason)
            else:
                self.assertIsInstance(prepared, CheckRequest)
                self.assertEqual(prepared.smiles, source)


if __name__ == '__main__':
    unittest.main()
