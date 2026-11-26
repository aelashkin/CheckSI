"""Advisory 1H shift comparison with the bundled CASCADE proton model."""

import unittest

from checksi.config import ConfigManager
from checksi.errors import ErrorCode
from checksi.h1_enrichment import enrich_analysis_with_h1_shifts
from checksi.h1shift.comparison import compare, multiplicity_issues, parse_signals
from checksi.h1shift.model import DomainError, ProtonEnvironment, load_weights, predict_environments

TOLUENE = ' (400 MHz, CDCl3) δ 7.29–7.24 (m, 2H), 7.19–7.14 (m, 3H), 2.36 (s, 3H)'


def compound(section, smiles='Cc1ccccc1'):
    return {'id': 1, 'source_paragraph_1based': 1, 'smiles': smiles, 'all_warnings': [],
            'highlight_info': {'hnmr_text': section}}


def config(enabled=True):
    cfg = ConfigManager.load_factory()
    cfg.h1_shift.enabled = enabled
    return cfg


class SignalParsingTests(unittest.TestCase):
    def test_ranges_integrals_and_labelled_couplings(self):
        signals = parse_signals(' (400 MHz, CDCl3) δ 8.52-8.49 (m, 1H), 7.66 – 7.58 (m, 2H), '
                                '2.68 (d, J(H,F) = 5.3 Hz, 3H)')
        self.assertEqual([(s.low, s.high, s.protons) for s in signals],
                         [(8.49, 8.52, 1), (7.58, 7.66, 2), (2.68, 2.68, 3)])

    def test_multiplicity_codes_and_couplings(self):
        signals = parse_signals('δ 7.2 (dd, J = 8.0, 2.0 Hz, 1H), 3.1 (quint, J = 6 Hz, 2H), '
                                '2.0 (br s, 1H), 4.0 (app t, J = 7 Hz, 2H), 2.3 (s, 3H)')
        self.assertEqual([(s.multiplicity, s.couplings, s.partner_counts()) for s in signals],
                         [('dd', (8.0, 2.0), [1, 1]), ('quint', (6.0,), [4]), (None, (), None),
                          (None, (7.0,), None), ('s', (), None)])

    def test_signal_without_integral_disables_comparison(self):
        self.assertIsNone(parse_signals('δ 7.26 (m, 2H), 2.30 (s)'))


class ComparisonTests(unittest.TestCase):
    def test_protons_inside_a_reported_range_have_no_error(self):
        result = compare([ProtonEnvironment(7.20, 2, (0, 1))], parse_signals('δ 7.30–7.10 (m, 2H)'), 1.5)
        self.assertEqual((result.mean_error, result.matched, result.outliers), (0.0, 2, ()))

    def test_far_signal_is_reported_once_with_its_prediction(self):
        environments = [ProtonEnvironment(7.1, 2, (0, 1)), ProtonEnvironment(2.3, 3, (2, 3, 4))]
        result = compare(environments, parse_signals('δ 9.10 (d, J = 8.0 Hz, 2H), 2.30 (s, 3H)'), 1.5)
        self.assertAlmostEqual(result.mean_error, 0.8)
        self.assertEqual([(s.text, round(p, 2)) for s, p in result.outliers], [('9.10 (d, J = 8.0 Hz, 2H)', 7.1)])

    def test_proton_of_a_mispredicted_class_gets_a_wide_tolerance_and_leaves_the_mean(self):
        # N-CH-N reported at 4.50 and predicted at 7.2: without the class the aromatic proton
        # and the aminal proton are both 'far'; with it nothing is reported.
        signals = parse_signals('δ 7.20 (m, 2H), 4.50 (s, 1H)')
        plain = [ProtonEnvironment(7.2, 2, (0, 1)), ProtonEnvironment(7.3, 1, (2,))]
        marked = [plain[0], ProtonEnvironment(7.3, 1, (2,), unreliable='aminal')]
        self.assertEqual(len(compare(plain, signals, 1.5).outliers), 1)
        result = compare(marked, signals, 1.5)
        self.assertEqual((result.outliers, result.unaccounted, result.unreliable, result.mean_error), ((), (), 1, 0.0))

    def test_mispredicted_class_does_not_hide_an_error_beyond_the_allowance(self):
        marked = [ProtonEnvironment(7.3, 1, (0,), unreliable='aminal')]
        result = compare(marked, parse_signals('δ 1.20 (s, 1H)'), 1.5)
        self.assertEqual(len(result.outliers), 1)

    def test_other_protons_of_a_compound_with_a_mispredicted_class_are_still_compared(self):
        marked = [ProtonEnvironment(3.2, 2, (0, 1), unreliable='alpha_sulfonyl'), ProtonEnvironment(7.4, 2, (2, 3))]
        result = compare(marked, parse_signals('δ 4.50 (s, 2H), 1.40 (t, J = 7.1 Hz, 2H)'), 1.5)
        self.assertEqual(len(result.outliers), 1)
        self.assertGreater(result.mean_error, 1.5)

    def test_mispredicted_proton_is_a_multiplicity_candidate_within_its_allowance(self):
        # The vinyl proton of a vinyl sulfone is predicted near 6.2 and observed near 7.2 as a triplet.
        environments = [ProtonEnvironment(7.2, 1, (0,), near_partners=1, far_partners=1),
                        ProtonEnvironment(6.16, 1, (1,), near_partners=2, far_partners=2, unreliable='vinyl_sulfonyl')]
        signals = parse_signals('δ 7.30 (d, J = 8.0 Hz, 1H), 7.23 (t, J = 7.6 Hz, 1H)')
        self.assertEqual(multiplicity_issues(environments, signals, compare(environments, signals, 1.5)), [])
        # ...but it does not excuse a larger signal that other protons make up.
        environments[0] = ProtonEnvironment(7.2, 4, (0, 2, 3, 4), near_partners=1, far_partners=1)
        signals = parse_signals('δ 7.30 (t, J = 7.6 Hz, 4H), 7.23 (t, J = 7.6 Hz, 1H)')
        found = multiplicity_issues(environments, signals, compare(environments, signals, 1.5))
        self.assertEqual([issue.signal.text for issue in found], ['7.30 (t, J = 7.6 Hz, 4H)'])

    def test_signal_no_ch_proton_fits_goes_to_an_exchangeable_proton(self):
        # 19 C-H protons, 18 C-H signals and one NH signal: the spare prediction must not land on the NH.
        environments = [ProtonEnvironment(7.4, 2, (0, 1)), ProtonEnvironment(2.3, 3, (2, 3, 4))]
        signals = parse_signals('δ 10.02 (s, 1H), 7.40 (d, J = 8.0 Hz, 1H), 2.30 (s, 3H)')
        plain = compare(environments, signals, 1.5)
        self.assertEqual([s.text for s, _ in plain.outliers], ['10.02 (s, 1H)'])
        result = compare(environments, signals, 1.5, exchangeable=1)
        self.assertEqual((result.outliers, result.exchangeable_slots), ((), 1))
        self.assertAlmostEqual(result.mean_error, plain.mean_error)
        self.assertEqual(result.unaccounted, (7.4,))
        self.assertEqual(result.assigned[0], ())

    def test_exchangeable_protons_take_leftover_signals_first(self):
        # The NH signal is listed and left over, so no credit remains to hide the far C-H signal.
        environments = [ProtonEnvironment(7.4, 1, (0,)), ProtonEnvironment(2.3, 3, (1, 2, 3))]
        signals = parse_signals('δ 10.02 (s, 1H), 4.90 (d, J = 8.0 Hz, 1H), 2.30 (s, 3H)')
        result = compare(environments, signals, 1.5, exchangeable=1)
        self.assertEqual((len(result.outliers), result.exchangeable_slots), (1, 0))

    def test_class_allowances_differ(self):
        sulfonyl = [ProtonEnvironment(3.2, 2, (0, 1), unreliable='alpha_sulfonyl')]
        self.assertEqual(compare(sulfonyl, parse_signals('δ 4.50 (s, 2H)'), 1.5).outliers, ())
        self.assertEqual(len(compare(sulfonyl, parse_signals('δ 6.40 (s, 2H)'), 1.5).outliers), 1)   # 3.2 ppm off
        aminal = [ProtonEnvironment(7.3, 1, (0,), unreliable='aminal')]
        self.assertEqual(compare(aminal, parse_signals('δ 4.50 (s, 1H)'), 1.5).outliers, ())

    def test_aromatic_solvent_is_read_from_the_header(self):
        from checksi.h1shift.comparison import aromatic_solvent
        self.assertTrue(aromatic_solvent(' (400 MHz, C6D6) δ 7.10 (m, 5H)'))
        self.assertTrue(aromatic_solvent(' (500 MHz, toluene-d8) δ 7.10 (m, 5H)'))
        self.assertFalse(aromatic_solvent(' (400 MHz, CDCl3) δ 7.26 (s, C6D6 residual, 1H)'))

    def test_isopropyl_methyls_reported_as_singlets_are_flagged_but_gem_dimethyl_is_not(self):
        signals = parse_signals('δ 1.20 (s, 3H), 1.18 (s, 3H)')
        isopropyl = [ProtonEnvironment(1.2, 6, tuple(range(6)), near_partners=1, far_partners=1,
                                       must_split=True, geminal_methyls=True)]
        found = multiplicity_issues(isopropyl, signals, compare(isopropyl, signals, 1.5))
        self.assertEqual([issue.reason for issue in found], ['must_split', 'must_split'])
        quaternary = [ProtonEnvironment(1.2, 6, tuple(range(6)), near_partners=0, far_partners=0,
                                        must_split=False, geminal_methyls=True)]
        self.assertEqual(multiplicity_issues(quaternary, signals, compare(quaternary, signals, 1.5)), [])
        together = parse_signals('δ 1.20 (s, 6H)')
        self.assertEqual(multiplicity_issues(quaternary, together, compare(quaternary, together, 1.5)), [])
        self.assertEqual(len(multiplicity_issues(isopropyl, together, compare(isopropyl, together, 1.5))), 1)

    def test_multiplicity_must_be_possible_for_every_proton_of_the_signal(self):
        # "7.31 (t, 6H)": only two protons near it have two neighbours; the other four are
        # doublets of a para-substituted ring written under the same label.
        triplet = ProtonEnvironment(7.30, 2, (0, 1), near_partners=2, far_partners=2, min_partners=1)
        ring = ProtonEnvironment(7.25, 4, (2, 3, 4, 5), near_partners=1, far_partners=1, min_partners=1)
        other = ProtonEnvironment(7.70, 4, (6, 7, 8, 9), near_partners=1, far_partners=1, min_partners=1)
        environments = [triplet, ring, other]
        wrong = parse_signals('δ 7.67 (d, J = 7.9 Hz, 4H), 7.31 (t, J = 6.9 Hz, 6H)')
        found = multiplicity_issues(environments, wrong, compare(environments, wrong, 1.5))
        self.assertEqual([(issue.signal.text, issue.reason, issue.available) for issue in found],
                         [('7.31 (t, J = 6.9 Hz, 6H)', 'partly_possible', 2)])
        right = parse_signals('δ 7.67 (d, J = 7.9 Hz, 4H), 7.31 (t, J = 6.9 Hz, 2H), 7.26 (d, J = 8.0 Hz, 4H)')
        self.assertEqual(multiplicity_issues(environments, right, compare(environments, right, 1.5)), [])
        lumped = parse_signals('δ 7.67 (d, J = 7.9 Hz, 4H), 7.35–7.22 (m, 6H)')
        self.assertEqual(multiplicity_issues(environments, lumped, compare(environments, lumped, 1.5)), [])

    def test_first_order_signal_needs_one_group_of_equivalent_protons(self):
        # "d, 6H" is an isopropyl group; two different methyl doublets under one label are not.
        isopropyl = [ProtonEnvironment(1.20, 6, tuple(range(6)), near_partners=1, far_partners=1, must_split=True)]
        signals = parse_signals('δ 1.21 (d, J = 6.9 Hz, 6H)')
        self.assertEqual(multiplicity_issues(isopropyl, signals, compare(isopropyl, signals, 1.5)), [])
        two = [ProtonEnvironment(1.22, 3, (0, 1, 2), near_partners=1, far_partners=1, must_split=True),
               ProtonEnvironment(1.18, 3, (3, 4, 5), near_partners=1, far_partners=1, must_split=True)]
        found = multiplicity_issues(two, signals, compare(two, signals, 1.5))
        self.assertEqual([(issue.reason, issue.available) for issue in found], [('partly_possible', 3)])
        apart = parse_signals('δ 1.22 (d, J = 6.9 Hz, 3H), 1.18 (d, J = 6.9 Hz, 3H)')
        self.assertEqual(multiplicity_issues(two, apart, compare(two, apart, 1.5)), [])

    def test_a_group_is_not_used_for_two_signals(self):
        # One group of four doublet protons cannot be both "7.98 (d, 4H)" and "7.43 (d, 4H)".
        ortho = ProtonEnvironment(7.46, 4, (0, 1, 2, 3), near_partners=1, far_partners=3, min_partners=1)
        h8 = ProtonEnvironment(7.44, 2, (4, 5), near_partners=1, far_partners=2, min_partners=1)
        para = ProtonEnvironment(7.47, 2, (6, 7), near_partners=2, far_partners=4, min_partners=2)
        signals = parse_signals('δ 7.98 (d, J = 8.3 Hz, 4H), 7.43 (d, J = 9.3 Hz, 4H)')
        environments = [ortho, h8, para]
        found = multiplicity_issues(environments, signals, compare(environments, signals, 1.5))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reason, 'partly_possible')

    def test_benzene_proton_with_two_ortho_neighbours_cannot_fill_a_doublet(self):
        # A doublet of two protons where one candidate has two ortho neighbours (a triplet) and one has one.
        environments = [ProtonEnvironment(7.40, 1, (0,), near_partners=2, far_partners=4, min_partners=2),
                        ProtonEnvironment(7.45, 1, (1,), near_partners=1, far_partners=3, min_partners=1)]
        signals = parse_signals('δ 7.43 (d, J = 9.3 Hz, 2H)')
        found = multiplicity_issues(environments, signals, compare(environments, signals, 1.5))
        self.assertEqual([(issue.reason, issue.available) for issue in found], [('partly_possible', 1)])

    def test_extra_reported_protons_such_as_nh_stay_unmatched(self):
        result = compare([ProtonEnvironment(3.0, 3, (0, 1, 2))], parse_signals('δ 4.75 (br s, 1H), 3.00 (s, 3H)'), 1.5)
        self.assertEqual((result.mean_error, result.matched), (0.0, 3))

    def test_fractional_integrals_round_up_so_mixture_signals_keep_a_slot(self):
        signals = parse_signals('δ 4.66 (m, 0.5H), 4.44 (m, 0.5H), 5.00 (s, 0.4H), 4.38 (s, 1.6H), 3.73 (s, 3.0H)')
        self.assertEqual([s.protons for s in signals], [1, 1, 1, 2, 3])
        # The alpha-CH takes one of the half signals instead of a distant slot.
        result = compare([ProtonEnvironment(4.6, 1, (0,)), ProtonEnvironment(3.7, 3, (1, 2, 3))],
                         parse_signals('δ 6.55 (m, 1H), 4.66 (m, 0.5H), 4.44 (m, 0.5H), 3.73 (s, 3H)'), 1.5)
        self.assertEqual(result.outliers, ())


class MultiplicityTests(unittest.TestCase):
    ETHYL_BENZOATE = 'CCOC(=O)c1ccccc1'
    AROMATIC = '8.05 (d, J = 7.6 Hz, 2H), 7.55 (t, J = 7.4 Hz, 1H), 7.43 (t, J = 7.7 Hz, 2H)'

    def issues(self, aliphatic):
        environments = predict_environments(self.ETHYL_BENZOATE)
        signals = parse_signals(f'δ {self.AROMATIC}, {aliphatic}')
        return [(i.signal.text, i.reason) for i in
                multiplicity_issues(environments, signals, compare(environments, signals, 1.5))]

    def test_correct_description_has_no_issue(self):
        self.assertEqual(self.issues('4.38 (q, J = 7.1 Hz, 2H), 1.40 (t, J = 7.1 Hz, 3H)'), [])

    def test_split_methyl_reported_as_singlet(self):
        self.assertEqual(self.issues('4.38 (q, J = 7.1 Hz, 2H), 1.40 (s, 3H)'), [('1.40 (s, 3H)', 'must_split')])

    def test_more_partners_than_the_structure_has(self):
        self.assertEqual(self.issues('4.38 (qt, J = 7.1, 6.5 Hz, 2H), 1.40 (t, J = 7.1 Hz, 3H)'),
                         [('4.38 (qt, J = 7.1, 6.5 Hz, 2H)', 'too_many_partners')])

    def test_window_lets_a_nearby_environment_explain_the_signal(self):
        # In ethyl acetate the acetyl singlet is predicted within 1 ppm of the ethyl methyl.
        environments = predict_environments('CCOC(C)=O')
        signals = parse_signals('δ 4.12 (q, J = 7.1 Hz, 2H), 2.05 (s, 3H), 1.26 (s, 3H)')
        comparison = compare(environments, signals, 1.5)
        self.assertEqual(len(multiplicity_issues(environments, signals, comparison, window=0.0)), 1)
        self.assertEqual(multiplicity_issues(environments, signals, comparison, window=1.0), [])


    def test_diastereotopic_gem_dimethyl_singlets_are_accepted(self):
        # 2,2-dimethyloxazolidine next to a stereocentre: two separate 3H singlets are correct.
        smiles = 'CC[C@H](C)[C@@H]1COC(C)(C)N1C(=O)OC'
        environments = predict_environments(smiles)
        gem = next(env for env in environments if env.geminal_methyls)
        self.assertEqual(gem.count, 6)
        signals = parse_signals('δ 3.95 (m, 2H), 3.70 (s, 3H), 3.60 (m, 1H), 1.65 (s, 3H), 1.50 (s, 3H), '
                                '1.40 (m, 2H), 1.30 (m, 1H), 0.90 (t, J = 7.4 Hz, 3H), 0.85 (d, J = 6.8 Hz, 3H)')
        comparison = compare(environments, signals, 1.5)
        self.assertEqual([i.signal.text for i in multiplicity_issues(environments, signals, comparison)], [])


class ModelTests(unittest.TestCase):
    def test_bundled_weights_load_with_checksum(self):
        self.assertEqual(load_weights()['atom_embedding.embeddings'].shape, (10, 256))

    def test_benzene_and_symmetric_ring_protons(self):
        benzene = predict_environments('c1ccccc1')
        self.assertEqual([env.count for env in benzene], [6])
        self.assertAlmostEqual(benzene[0].shift, 7.3, delta=0.15)
        ring = predict_environments('C1CCCCCC1')
        self.assertEqual([env.count for env in ring], [14])
        self.assertAlmostEqual(ring[0].shift, 1.54, delta=0.2)

    def test_unsupported_elements_are_refused(self):
        with self.assertRaisesRegex(DomainError, 'I'):
            predict_environments('ICc1ccccc1')

    def test_bromine_is_evaluated_as_chlorine(self):
        methylene = [env for env in predict_environments('BrCc1ccccc1') if env.shift < 6]
        self.assertEqual(len(methylene), 1)
        self.assertAlmostEqual(methylene[0].shift, 4.5, delta=0.4)  # experimental 4.50 ppm

    def test_coupling_capacity_of_ethyl_acetate(self):
        environments = predict_environments('CCOC(C)=O')
        ethyl = next(env for env in environments if env.count == 3 and env.shift < 1.7)
        acetyl = next(env for env in environments if env.count == 3 and env.shift >= 1.7)
        self.assertEqual((ethyl.must_split, ethyl.near_partners), (True, 2))
        self.assertEqual((acetyl.must_split, acetyl.near_partners), (False, 0))


class EnrichmentTests(unittest.TestCase):
    def test_enabled_by_default_and_can_be_switched_off(self):
        self.assertTrue(ConfigManager.load_factory().h1_shift.enabled)
        data = {'compounds': [compound(TOLUENE)]}
        enrich_analysis_with_h1_shifts(config(enabled=False), data)
        self.assertNotIn('h1_shift', data['compounds'][0])

    def test_matching_description_has_no_findings(self):
        data = {'compounds': [compound(TOLUENE)]}
        enrich_analysis_with_h1_shifts(config(), data)
        result = data['compounds'][0]
        self.assertEqual(result['h1_shift']['status'], 'completed')
        self.assertLess(result['h1_shift']['mean_error_ppm'], 0.3)
        self.assertEqual(result['all_warnings'], [])

    def test_wrong_structure_and_typo_are_reported(self):
        typo = ' (400 MHz, CDCl3) δ 7.29–7.24 (m, 2H), 7.19–7.14 (m, 3H), 5.36 (s, 3H)'
        data = {'compounds': [compound(typo)]}
        enrich_analysis_with_h1_shifts(config(), data)
        stats = data['error_stats']
        self.assertEqual(stats[ErrorCode.H1_SHIFT_STRUCTURE_MISMATCH.value], 1)
        self.assertEqual(stats[ErrorCode.H1_SHIFT_SIGNAL_OUTLIER.value], 1)
        self.assertIn('5.36 (s, 3H)', ' '.join(data['compounds'][0]['all_warnings']))

    def test_missing_ch_proton_is_not_excused_as_exchangeable(self):
        # 4-aminotoluene: 4 ArH + CH3 + NH2. One ArH is missing; NH2 is listed, far from any C-H.
        short = ' (400 MHz, CDCl3) δ 6.97 (d, J = 8.0 Hz, 2H), 6.61 (d, J = 8.0 Hz, 1H), 2.24 (s, 3H), 12.50 (br s, 2H)'
        data = {'compounds': [compound(short, smiles='Cc1ccc(N)cc1')]}
        enrich_analysis_with_h1_shifts(config(), data)
        result = data['compounds'][0]
        self.assertEqual(data['error_stats'][ErrorCode.H1_PROTONS_UNACCOUNTED.value], 1)
        self.assertEqual(result['h1_shift']['missing_protons']['unaccounted_ch'], 1)
        self.assertIn('8 H against 9', ' '.join(result['all_warnings']))

    def test_missing_exchangeable_proton_is_confirmed_by_the_matching(self):
        # The NH2 is not listed; every C-H has a signal, so the shortfall is exchangeable.
        fine = ' (400 MHz, CDCl3) δ 6.97 (d, J = 8.0 Hz, 2H), 6.61 (d, J = 8.0 Hz, 2H), 2.24 (s, 3H)'
        data = {'compounds': [compound(fine, smiles='Cc1ccc(N)cc1')]}
        enrich_analysis_with_h1_shifts(config(), data)
        result = data['compounds'][0]
        self.assertTrue(result['h1_shift']['missing_protons']['explained_by_exchangeable'])
        self.assertEqual(data['error_stats'][ErrorCode.H1_PROTONS_UNACCOUNTED.value], 0)

    def test_unsupported_structure_is_an_informational_skip(self):
        data = {'compounds': [compound(TOLUENE, smiles='Cc1ccc(I)cc1')]}
        enrich_analysis_with_h1_shifts(config(), data)
        self.assertEqual(data['compounds'][0]['h1_shift']['reason'], 'not_applicable')
        self.assertEqual(data['error_stats'][ErrorCode.H1_SHIFT_NOT_APPLICABLE.value], 1)


if __name__ == '__main__':
    unittest.main()


class UnreliableClassTests(unittest.TestCase):
    def classes(self, smiles):
        from rdkit import Chem
        from checksi.h1shift.model import unreliable_class
        mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
        return {unreliable_class(mol, atom.GetIdx()) for atom in mol.GetAtoms()
                if atom.GetAtomicNum() == 1 and atom.GetNeighbors()[0].GetAtomicNum() == 6}

    def test_known_classes_are_recognised_per_proton(self):
        self.assertEqual(self.classes('CS(=O)(=O)C/C(=C/c1ccccc1)S(C)(=O)=O'), {'alpha_sulfonyl', 'vinyl_sulfonyl', ''})
        self.assertEqual(self.classes('CN(/C=C(/F)C)C(C)=O'), {'n_fluorovinyl', ''})
        self.assertEqual(self.classes('CNC(C)NC'), {'aminal', ''})
        self.assertEqual(self.classes('CC1CC1'), {'strained_cage', ''})

    def test_ordinary_groups_have_no_class(self):
        for smiles in ('CCOC(=O)c1ccccc1', 'CSc1ccccc1', 'CS(C)=O', 'CN(C)C=O', 'c1ncccn1', 'C1CCCCC1'):
            self.assertEqual(self.classes(smiles), {''}, smiles)
