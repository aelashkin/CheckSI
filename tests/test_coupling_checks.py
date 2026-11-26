"""Coupling-constant rules for 1H and 13C descriptions."""

import unittest

from checksi.config import ConfigManager
from checksi.coupling_enrichment import enrich_analysis_with_couplings
from checksi.couplings.checks import carbon_issues, proton_issues, structure_facts
from checksi.couplings.parsing import parse_signals
from checksi.errors import ErrorCode


def codes(issues):
    return [(issue.code, issue.variant) for issue in issues]


class ParsingTests(unittest.TestCase):
    def test_every_constant_of_a_signal_is_read(self):
        signals = parse_signals('δ 7.90 (dt, J = 22.6, 6.8 Hz, 2H), 5.10 (dd, J1 = 7.6 Hz, J2 = 1.2 Hz, 1H), '
                                '4.00 (d, 2JH-F = 47.5 Hz, 2H), 2.00 (d, 1H, J = 16.0 Hz)')
        self.assertEqual([[c.value for c in s.couplings] for s in signals], [[22.6, 6.8], [7.6, 1.2], [47.5], [16.0]])
        self.assertEqual([s.protons for s in signals], [2, 1, 2, 1])
        self.assertTrue(signals[2].couplings[0].heteronuclear)

    def test_label_with_a_comma(self):
        signals = parse_signals('δ 162.3 (d, JC, F = 246.5 Hz), 129.8 (q, JC,F = 32.8 Hz), 7.2 (dd, J = 8.0, 2.0 Hz)')
        self.assertEqual([[c.value for c in s.couplings] for s in signals], [[246.5], [32.8], [8.0, 2.0]])
        self.assertTrue(signals[0].couplings[0].heteronuclear)

    def test_order_digit_of_the_next_constant_is_not_a_value(self):
        signals = parse_signals('δ 2.77 (dd, 2J = 13.9 Hz, 3J = 8.7 Hz, 1H), 5.93 (dd, 3Jtrans = 17.2 Hz, 3Jcis = 10.9 Hz, 1H)')
        self.assertEqual([[c.value for c in s.couplings] for s in signals], [[13.9, 8.7], [17.2, 10.9]])

    def test_atom_counts_are_not_constants(self):
        signals = parse_signals('δ 159.4 (d, J = 245.4 Hz, 1C), 124.1(q, J = 272.7 Hz, 1C), 7.3 (d, J = 8.0Hz, 2H)')
        self.assertEqual([[c.value for c in s.couplings] for s in signals], [[245.4], [272.7], [8.0]])

    def test_labelled_carbon_constants(self):
        signals = parse_signals('δ 162.5 (d, 1JC-F = 247.5 Hz), 45.0 (dd, J = 250, 12 Hz), 21.0')
        self.assertEqual([[c.value for c in s.couplings] for s in signals], [[247.5], [250.0, 12.0]])


class FluorineCountTests(unittest.TestCase):
    def test_entries_count_as_signals_only_when_every_one_has_an_integral(self):
        from checksi.analysis.nmr import _fluorine_entries_with_integrals as entries
        self.assertEqual(entries('δ -62.4 (s, 3F), -110.2 (m, 1F)'), 2)
        self.assertIsNone(entries('δ −62.4 (s, 3F), −110.2 (m)'))       # "3F" here may mean coinciding groups
        self.assertIsNone(entries('δ -113.3'))


class ProtonTests(unittest.TestCase):
    CINNAMIC = 'O=C(O)/C=C/c1ccc(C)cc1'
    AROMATIC = '7.45 (d, J = 8.0 Hz, 2H), 7.20 (d, J = 8.0 Hz, 2H), 2.38 (s, 3H)'

    def issues(self, text, smiles, **kwargs):
        return codes(proton_issues(parse_signals('δ ' + text), structure_facts(smiles),
                                   has_fluorine_or_phosphorus=False, **kwargs))

    def test_matching_trans_alkene_is_clean(self):
        self.assertEqual(self.issues(f'7.75 (d, J = 16.0 Hz, 1H), {self.AROMATIC}, 6.40 (d, J = 16.0 Hz, 1H)',
                                     self.CINNAMIC), [])

    def test_unpaired_constant(self):
        found = self.issues(f'7.75 (d, J = 16.0 Hz, 1H), {self.AROMATIC}, 6.40 (d, J = 12.0 Hz, 1H)', self.CINNAMIC)
        self.assertEqual(found, [('hnmr_j_partner_missing', 'default')] * 2)

    def test_two_unpaired_constants_within_one_hertz_are_one_milder_mismatch(self):
        found = proton_issues(parse_signals(f'δ 7.75 (d, J = 16.0 Hz, 1H), {self.AROMATIC}, 6.40 (d, J = 15.2 Hz, 1H)'),
                              structure_facts(self.CINNAMIC), has_fluorine_or_phosphorus=False)
        self.assertEqual([(i.code, i.context['value'], i.context['other_value'], i.context['difference']) for i in found],
                         [('hnmr_j_partner_mismatch', '16', '15.2', '0.8')])

    def test_ambiguous_neighbours_stay_a_missing_partner(self):
        # 4.3 Hz is within 1 Hz of both 5.1 and 3.5: no single pair can be named.
        found = self.issues('8.08 (d, J = 4.3 Hz, 1H), 7.23 (d, J = 5.1 Hz, 1H), 6.87 (d, J = 3.5 Hz, 1H)', 'Clc1cccs1')
        self.assertEqual(found, [('hnmr_j_partner_missing', 'default')] * 3)

    def test_a_constant_too_large_for_the_structure_is_not_excused_as_a_mismatch(self):
        found = self.issues('7.50 (d, J = 10.9 Hz, 1H), 7.30 (d, J = 10.2 Hz, 1H)', 'Clc1c(Cl)c(Br)c(Br)cc1')
        self.assertEqual(sorted(found), [('hnmr_j_partner_missing', 'default')] * 2 + [('hnmr_j_value_implausible', 'structure')] * 2)

    def test_triplet_constant_is_the_mean_of_two_partners(self):
        # 4.05 (dt, 9.1) couples to 2.77 (8.7) and 2.46 (9.5): consistent, not three unpaired constants.
        text = ('4.05 (dt, J = 9.1, 1.9 Hz, 1H), 2.77 (dd, J = 13.9, 8.7 Hz, 1H), 2.46 (dd, J = 14.0, 9.5 Hz, 1H), '
                '1.10 (s, 3H)')
        self.assertNotIn(('hnmr_j_value_implausible', 'absolute'), self.issues(text, ''))
        unequal = text.replace('dt, J = 9.1', 'dd, J = 9.1').replace('8.7', '8.5').replace('9.5', '9.7')
        self.assertEqual(len(self.issues(unequal, '')), 3)

    def test_partners_within_half_a_hertz_are_one_constant(self):
        self.assertEqual(self.issues('7.35 (d, J = 8.2 Hz, 2H), 7.29 (d, J = 7.8 Hz, 2H), 2.38 (s, 3H)', 'Cc1ccc(Cl)cc1'), [])
        found = self.issues('7.35 (d, J = 8.4 Hz, 2H), 7.29 (d, J = 7.8 Hz, 2H), 2.38 (s, 3H)', 'Cc1ccc(Cl)cc1')
        self.assertEqual(found, [('hnmr_j_partner_mismatch', 'default')])

    def test_mismatch_is_not_offered_for_signals_that_cannot_be_coupled(self):
        # An aromatic doublet of doublets and a chain CH2 with constants 0.7 Hz apart are not partners.
        text = ('8.54 (d, J = 2.7 Hz, 1H), 8.18 (dd, J = 9.5, 2.8 Hz, 1H), 7.40 (d, J = 2.6 Hz, 1H), '
                '3.59 (dd, J = 10.2, 5.1 Hz, 2H), 3.31 (t, J = 5.5 Hz, 2H), 3.05 (s, 3H)')
        found = self.issues(text + ', 2.10 (s, 1H)', 'O=[N+]([O-])c1ccc(N(C)CCO)c([N+](=O)[O-])c1')
        self.assertNotIn(('hnmr_j_partner_mismatch', 'default'), found)
        self.assertIn(('hnmr_j_partner_missing', 'default'), found)

    def test_size_rule_without_a_structure_does_not_claim_the_absence_of_f_or_p(self):
        text = '4.39 (dd, J = 38.3, 14.9 Hz, 2H), 2.25 (s, 3H)'
        self.assertIn(('hnmr_j_value_implausible', 'no_structure'), self.issues(text, ''))
        self.assertNotIn(('hnmr_j_value_implausible', 'absolute'), self.issues(text, ''))
        self.assertIn(('hnmr_j_value_implausible', 'absolute'), self.issues(text, 'CC(=O)N(C)Cc1ccccc1'))

    def test_a_multiplet_makes_the_partner_unverifiable(self):
        self.assertEqual(self.issues('7.75 (d, J = 16.0 Hz, 1H), 7.50–7.15 (m, 4H), 6.40 (d, J = 15.0 Hz, 1H), '
                                     '2.38 (s, 3H)', self.CINNAMIC), [])

    def test_tolerance_follows_the_reporting_step(self):
        self.assertEqual(self.issues('7.75 (d, J = 16 Hz, 1H), 7.45 (d, J = 8 Hz, 2H), 7.20 (d, J = 8 Hz, 2H), '
                                     '6.40 (d, J = 15 Hz, 1H), 2.38 (s, 3H)', self.CINNAMIC), [])

    def test_coupling_larger_than_any_proton_coupling(self):
        found = self.issues('7.58 (dt, J = 22.6, 6.8 Hz, 2H), 7.30–7.10 (m, 3H), 2.30 (s, 3H)', 'Cc1ccccc1')
        self.assertEqual(found, [('hnmr_j_value_implausible', 'absolute')])

    def test_coupling_larger_than_the_structure_allows(self):
        found = self.issues('7.30 (d, J = 13.5 Hz, 2H), 7.20–7.10 (m, 3H), 2.30 (s, 3H)', 'Cc1ccccc1')
        self.assertEqual(found, [('hnmr_j_value_implausible', 'structure')])

    def test_carbon_line_inside_the_proton_text_is_ignored(self):
        self.assertEqual(self.issues('7.30–7.10 (m, 5H), 2.30 (s, 3H), 162.2 (d, J = 246.6 Hz)', 'Cc1ccccc1'), [])

    def test_fluorine_in_the_structure_silences_proton_proton_rules(self):
        self.assertEqual(self.issues('7.05 (dd, J = 8.6, 5.5 Hz, 2H), 6.95 (t, J = 8.7 Hz, 2H), 2.30 (s, 3H)',
                                     'Cc1ccc(F)cc1'), [])

    def test_geminal_fluorine_needs_a_large_coupling(self):
        self.assertEqual(self.issues('7.40–7.30 (m, 5H), 5.35 (s, 2H)', 'FCc1ccccc1'),
                         [('hnmr_heteronuclear_coupling_missing', 'geminal_hf')])
        self.assertEqual(self.issues('7.40–7.30 (m, 5H), 5.35 (d, J = 47.9 Hz, 2H)', 'FCc1ccccc1'), [])

    def test_fluorine_singlet_without_decoupling(self):
        text = '7.40–7.30 (m, 5H), 5.35 (d, J = 47.9 Hz, 2H)'
        self.assertEqual(self.issues(text, 'FCc1ccccc1', fluorine_section='19F NMR (376 MHz, CDCl3) δ -206.5 (s)'),
                         [('fnmr_decoupling_not_stated', 'default')])
        self.assertEqual(self.issues(text, 'FCc1ccccc1',
                                     fluorine_section='19F{1H} NMR (376 MHz, CDCl3) δ -206.5 (s)'), [])


class CarbonTests(unittest.TestCase):
    FLUOROTOLUENE = 'Cc1ccc(F)cc1'
    FULL = 'δ 161.2 (d, J = 243.5 Hz), 133.4 (d, J = 3.1 Hz), 130.4 (d, J = 7.8 Hz), 115.0 (d, J = 21.1 Hz), 20.6'

    def issues(self, text, smiles):
        return codes(carbon_issues(parse_signals(text), structure_facts(smiles), text))

    def test_complete_aryl_fluoride_is_clean(self):
        self.assertEqual(self.issues(self.FULL, self.FLUOROTOLUENE), [])

    def test_only_three_bond_coupling_missing_is_a_milder_finding(self):
        text = 'δ 161.2 (d, J = 243.5 Hz), 133.4, 130.4, 115.0 (d, J = 21.1 Hz), 20.6'
        self.assertEqual(self.issues(text, self.FLUOROTOLUENE), [('c13_cf_long_range_coupling_missing', 'default')])

    def test_missing_two_bond_coupling_is_not_a_long_range_remark(self):
        # Aryl-CF3 with the 1J quartet and one small quartet: the 2J quartet (about 30 Hz) is absent.
        text = 'δ 145.8, 131.0, 129.7, 125.6 (q, J = 3.6 Hz), 123.0 (q, J = 277.6 Hz), 21.3'
        self.assertEqual(self.issues(text, 'Cc1ccc(C(F)(F)F)cc1'), [('c13_cf_coupling_incomplete', 'default')])

    def test_two_lines_of_a_cf_doublet_listed_as_two_signals(self):
        text = '(101 MHz, CDCl3) δ 163.3, 160.9, 133.4 (d, J = 3.1 Hz), 130.4 (d, J = 7.8 Hz), 115.0 (d, J = 21.1 Hz), 20.6'
        found = carbon_issues(parse_signals(text), structure_facts(self.FLUOROTOLUENE), text)
        self.assertEqual([(i.code, i.variant, i.context) for i in found],
                         [('c13_cf_coupling_incomplete', 'separate_lines',
                           {'low': '161', 'high': '163'})])
        # Without the spectrometer frequency the spacing cannot be judged: the ordinary finding.
        self.assertEqual(self.issues(text.replace('(101 MHz, CDCl3) ', ''), self.FLUOROTOLUENE),
                         [('c13_cf_coupling_incomplete', 'default')])

    def test_three_bond_coupling_is_not_required_from_fluorine_on_an_sp3_carbon(self):
        # Benzylic C-F: 1J and 2J reported, the ortho carbons of the ring are plain signals.
        text = 'δ 136.2 (d, J = 17.2 Hz), 128.8, 128.6, 127.5, 84.6 (d, J = 166.3 Hz)'
        self.assertEqual(self.issues(text, 'FCc1ccccc1'), [])
        # Aryl-CF3: the ortho carbons are expected as quartets, so their absence is noted.
        text = 'δ 138.0, 130.2 (q, J = 32.4 Hz), 128.9, 125.4, 124.3 (q, J = 272.0 Hz), 21.3'
        self.assertEqual(self.issues(text, 'Cc1ccc(C(F)(F)F)cc1'), [('c13_cf_long_range_coupling_missing', 'default')])

    def test_cf3_carbon_must_be_a_quartet(self):
        text = 'δ 138.0, 130.2 (q, J = 32.4 Hz), 128.9, 125.4 (q, J = 3.8 Hz), 124.3 (dt, J = 272.0, 270.1 Hz), 21.3'
        self.assertEqual(self.issues(text, 'Cc1ccc(C(F)(F)F)cc1'), [('c13_cf_multiplicity_mismatch', 'default')])

    def test_missing_couplings_are_counted_by_class(self):
        self.assertEqual(self.issues('δ 161.2 (d, J = 243.5 Hz), 133.4, 130.4, 115.0, 20.6', self.FLUOROTOLUENE),
                         [('c13_cf_coupling_incomplete', 'default')])

    def test_small_two_bond_coupling_counts_as_split_next_to_a_vinyl_fluoride(self):
        # CH2=CF-CH2-Ph: the 2J to the CH2= carbon is large, the one to the benzylic carbon can be small.
        text = ('δ 165.0 (d, J = 258.0 Hz), 136.0, 129.0, 128.6, 127.0, 91.5 (d, J = 20.0 Hz), '
                '39.5 (d, J = 8.0 Hz)')
        self.assertEqual(self.issues(text, 'C=C(F)Cc1ccccc1'), [])

    def test_small_two_bond_coupling_of_a_substituted_ortho_carbon_counts_as_split(self):
        text = ('δ 162.0, 160.5 (d, J = 256.4 Hz), 151.0, 148.0, 138.5, 134.2 (d, J = 9.4 Hz), 132.0, '
                '125.0 (d, J = 3.4 Hz), 121.4 (d, J = 11.2 Hz), 120.3, 116.5 (d, J = 24.7 Hz), 114.8')
        self.assertEqual(self.issues(text, 'O=C(Nc1ccccn1)c1ccccc1F'), [])

    def test_small_two_bond_coupling_of_an_aryl_fluoride_is_a_missing_coupling(self):
        text = self.FULL.replace('21.1', '7.9')
        self.assertEqual(self.issues(text, self.FLUOROTOLUENE), [('c13_cf_coupling_incomplete', 'default')])

    def test_value_between_the_ranges(self):
        text = self.FULL.replace('21.1', '92.1')
        self.assertIn(('c13_cf_j_value_implausible', 'range'), self.issues(text, self.FLUOROTOLUENE))

    def test_second_constant_is_checked_too(self):
        text = 'δ 161.2 (dd, J = 243.5, 92.0 Hz), 133.4 (d, J = 3.1 Hz), 130.4 (d, J = 7.8 Hz), 115.0 (d, J = 21.1 Hz)'
        self.assertEqual(self.issues(text, self.FLUOROTOLUENE), [('c13_cf_j_value_implausible', 'range')])
        text = 'δ 161.2 (d, J = 243.5 Hz), 133.4 (d, J = 3.1 Hz), 130.4 (dd, J = 7.8, 831.4 Hz), 115.0 (d, J = 21.1 Hz)'
        self.assertIn(('c13_cf_j_value_implausible', 'range'), self.issues(text, self.FLUOROTOLUENE))

    def test_multiplicity_follows_the_number_of_fluorines(self):
        cf3 = 'FC(F)(F)c1ccccc1'
        good = 'δ 131.8, 130.9 (q, J = 32.3 Hz), 128.8, 125.3 (q, J = 3.8 Hz), 124.3 (q, J = 272.1 Hz)'
        self.assertEqual(self.issues(good, cf3), [])
        self.assertEqual(self.issues(good.replace('(q, J = 272.1', '(d, J = 272.1'), cf3),
                         [('c13_cf_multiplicity_mismatch', 'default')])

    def test_coupling_without_fluorine_or_phosphorus(self):
        self.assertEqual(self.issues('δ 137.8, 129.0 (d, J = 8.0 Hz), 21.4', 'Cc1ccccc1'),
                         [('c13_cf_j_value_implausible', 'no_partner')])

    def test_fluorine_decoupled_spectrum_is_skipped(self):
        self.assertEqual(self.issues('13C{19F} δ 161.2, 133.4, 130.4, 115.0, 20.6', self.FLUOROTOLUENE), [])


class EnrichmentTests(unittest.TestCase):
    def test_formula_with_fluorine_silences_proton_rules_without_a_structure(self):
        from checksi.coupling_enrichment import _mentions_fluorine_or_phosphorus
        self.assertTrue(_mentions_fluorine_or_phosphorus('HRMS (ESI) calcd for C26H19ClF6NO [M+H]+ 510.1054'))
        self.assertTrue(_mentions_fluorine_or_phosphorus('31P NMR (162 MHz) δ 33.5'))
        self.assertFalse(_mentions_fluorine_or_phosphorus('HRMS calcd for C15H12N2OPd, C24H19ClNOBr2 found'))

    def test_gem_difluoroalkene_carbons_may_be_doublets_of_doublets(self):
        # The two fluorines of =CF2 are not equivalent: 1J and 2J appear as dd, not t.
        facts = structure_facts('CCC(=C(F)F)c1ccccc1')
        text = ('δ 154.4 (dd, J = 291.5, 288.3 Hz), 133.5 (dd, J = 4.5, 3.0 Hz), 128.6, 128.3, 127.4, 92.3 (dd, J = 21.3, 14.7 Hz), '
                '21.0, 12.9')
        self.assertEqual(codes(carbon_issues(parse_signals(text), facts)), [])
        wrong = text.replace('(dd, J = 291.5, 288.3 Hz)', '(q, J = 291.5 Hz)')
        self.assertEqual(codes(carbon_issues(parse_signals(wrong), facts)), [('c13_cf_multiplicity_mismatch', 'default')])

    def test_aromatic_multiplet_does_not_hide_an_aliphatic_partner(self):
        facts = structure_facts('CC(OC(C)=O)c1ccccc1')
        wrong = parse_signals('δ 7.38–7.27 (m, 5H), 5.88 (q, J = 6.6 Hz, 1H), 2.07 (s, 3H), 1.54 (d, J = 5.6 Hz, 3H)')
        found = proton_issues(wrong, facts, has_fluorine_or_phosphorus=False)
        # 5.88 could be an aromatic proton with a partner in the aromatic multiplet, so only the doublet,
        # which can only be aliphatic, is reported
        self.assertEqual([(i.code, i.context['signal'][:4]) for i in found], [('hnmr_j_partner_missing', '1.54')])
        right = parse_signals('δ 7.38–7.27 (m, 5H), 5.88 (q, J = 6.6 Hz, 1H), 2.07 (s, 3H), 1.54 (d, J = 6.6 Hz, 3H)')
        self.assertEqual(proton_issues(right, facts, has_fluorine_or_phosphorus=False), [])

    def test_multiplet_where_the_partner_can_be_still_silences(self):
        # the CH2 next to the CH3 is a multiplet: the partner of the triplet may be inside it
        facts = structure_facts('CCCCc1ccccc1')
        signals = parse_signals('δ 7.30–7.15 (m, 5H), 2.61 (t, J = 7.7 Hz, 2H), 1.65–1.55 (m, 2H), '
                                '1.40–1.30 (m, 2H), 0.93 (t, J = 6.1 Hz, 3H)')
        self.assertEqual(proton_issues(signals, facts, has_fluorine_or_phosphorus=False), [])
        # an aromatic doublet is hidden by an aromatic multiplet
        facts = structure_facts('Cc1ccccc1C')
        signals = parse_signals('δ 7.15–7.05 (m, 2H), 7.02 (d, J = 6.1 Hz, 2H), 2.26 (s, 6H)')
        self.assertEqual(proton_issues(signals, facts, has_fluorine_or_phosphorus=False), [])

    def test_without_a_structure_any_multiplet_silences(self):
        signals = parse_signals('δ 7.38–7.27 (m, 5H), 5.88 (q, J = 6.6 Hz, 1H), 1.54 (d, J = 5.6 Hz, 3H)')
        self.assertEqual(proton_issues(signals, None, has_fluorine_or_phosphorus=False), [])

    def test_enantiotopic_arms_of_a_meso_compound_count_once(self):
        facts = structure_facts('Fc1ccc(NC[C@H]2CC[C@@H](CNc3ccc(F)cc3)C2)cc1')
        self.assertEqual((len(facts.cf_one), len(facts.cf_two), facts.cf_three_aromatic), (1, 1, 1))
        signals = parse_signals('δ 156.4 (d, J = 234.7 Hz), 144.7 (d, J = 2.1 Hz), 115.6 (d, J = 22.1 Hz), '
                                '113.5 (d, J = 7.3 Hz), 50.3, 39.5, 36.2, 29.6')
        self.assertEqual(carbon_issues(signals, facts), [])

    def test_fluorine_shift_keeps_its_sign(self):
        for text in ('δ –128.09 (s)', 'δ −128.09 (s)', 'δ -128.09 (s)'):
            signal = parse_signals(text)[0]
            self.assertEqual(signal.low, -128.09)
            self.assertTrue(signal.text.startswith(text[2]))
        ranged = parse_signals('δ 7.36–7.25 (m, 2H), 3.12 – 3.04 (m, 4H)')
        self.assertEqual([(s.low, s.high) for s in ranged], [(7.25, 7.36), (3.04, 3.12)])

    def test_findings_reach_the_compound(self):
        compound = {'id': 1, 'source_paragraph_1based': 1, 'smiles': 'Cc1ccc(F)cc1', 'all_warnings': [],
                    'original_text': '', 'highlight_info': {
                        'hnmr_text': ' (400 MHz, CDCl3) δ 7.10–6.90 (m, 4H), 2.31 (s, 3H)',
                        'cnmr_text': ' (101 MHz, CDCl3) δ 161.2 (d, J = 243.5 Hz), 133.4, 130.4, 115.0, 20.6'}}
        data = {'compounds': [compound]}
        enrich_analysis_with_couplings(ConfigManager.load_factory(), data)
        self.assertEqual(data['error_stats'][ErrorCode.C13_CF_COUPLING_INCOMPLETE.value], 1)
        self.assertIn('c13_cf_coupling_incomplete', ' '.join(compound['all_warnings']))


if __name__ == '__main__':
    unittest.main()
