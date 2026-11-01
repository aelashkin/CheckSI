"""Formal 13C counts and separate manual coincidence count options."""

import unittest
from unittest.mock import patch

from rdkit import Chem
from rdkit.Chem import rdDistGeom

from checksi.analysis.structure_analysis import (
    analyze_molecular_structure,
    analyze_molecular_structure_detailed,
)


def counts(smiles):
    """Return (resolved count, merged count, stereogenic flag) for a SMILES string."""
    result = analyze_molecular_structure_detailed(Chem.AddHs(Chem.MolFromSmiles(smiles)))
    merged = [alt.count for alt in result.c13_alternatives if alt.name == "diastereotopic_merged"]
    return result.carbon_count, merged[0] if merged else result.carbon_count, result.stereogenic_signal_flag


# Validation set: (name, SMILES, resolved count, merged count).
# Supplied expectations were derived by hand assuming fast conformational exchange;
# they are regression assumptions, not experimentally validated reference data.
VALIDATION_SET = [
    # Catalogue section c13_count_structure_check
    ("N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)", '[C@@H]1(C[C@H](CC1)CNC1=CC=C(C=C1)C)CNC1=CC=C(C=C1)C', 9, 9),
    ('alpha-pinene', 'CC=1C2C(C(CC1)C2)(C)C', 10, 9),
    ('1-(1-chloro-2-ethylbut-1-en-1-yl)-4-methylbenzene', 'ClC(=C(CC)CC)C1=CC=C(C=C1)C', 11, 9),
    ('1-(chloro(cyclopentylidene)methyl)-4-methylbenzene', 'ClC(C1=CC=C(C=C1)C)=C1CCCC1', 11, 9),
    ('1-(cyclopentylidenemethyl)-4-methylbenzene', 'C1(CCCC1)=CC1=CC=C(C=C1)C', 11, 9),
    ('1-(2-ethylbut-1-en-1-yl)-4-methylbenzene', 'C(C)C(=CC1=CC=C(C=C1)C)CC', 11, 9),
    ("4,4'-(cyclopentylidenemethylene)bis(methylbenzene)", 'C1(CCCC1)=C(C1=CC=C(C=C1)C)C1=CC=C(C=C1)C', 9, 9),
    ('1-((3,4-diethyl-3,4-dimethylcyclopentylidene)methyl)-4-methylbenzene', 'C(C)C1(CC(CC1(C)CC)=CC1=CC=C(C=C1)C)C', 17, 13),
    ('1-(cyclohexylidenemethyl)-4-methylbenzene', 'C1(CCCCC1)=CC1=CC=C(C=C1)C', 12, 10),
    ("4,4'-(cyclohexylidenemethylene)bis(methylbenzene)", 'C1(CCCCC1)=C(C1=CC=C(C=C1)C)C1=CC=C(C=C1)C', 10, 10),
    ('1-methyl-4-((3,3,4,4-tetramethylcyclopentylidene)methyl)benzene', 'CC1=CC=C(C=C1)C=C1CC(C(C1)(C)C)(C)C', 13, 10),
    ('(2-methylbut-1-ene-1,1-diyl)dibenzene', 'CC(=C(C1=CC=CC=C1)C1=CC=CC=C1)CC', 13, 9),
    ('2-(cyclopentylidene(naphthalen-2-yl)methyl)-4,4,5,5-tetramethyl-1,3,2-dioxaborolane', 'C1(CCCC1)=C(B1OC(C(O1)(C)C)(C)C)C1=CC2=CC=CC=C2C=C1', 18, 16),
    ('(3,3-diphenylprop-2-ene-1,2-diyl)bis(phenylsulfane)', 'C1(=CC=CC=C1)C(=C(CSC1=CC=CC=C1)SC1=CC=CC=C1)C1=CC=CC=C1', 19, 15),
    ('1-isopropylcyclohexan-1-ol', 'C(C)(C)C1(CCCCC1)O', 6, 6),
    ('(E)-(1-chloroprop-1-en-2-yl)cyclohexane', 'Cl\\C=C(/C)\\C1CCCCC1', 7, 7),
    ('(E)-3-(1-chloroprop-1-en-2-yl)-1,1-dimethylcyclohexane', 'Cl\\C=C(/C)\\C1CC(CCC1)(C)C', 11, 10),
    # Hard cases: ring cis/trans, meso and C2, heteroatom stereocentres, axial chirality, natural products
    ('cis-1,4-dimethylcyclohexane', 'C[C@H]1CC[C@@H](C)CC1', 3, 3),
    ('trans-1,4-dimethylcyclohexane', 'C[C@H]1CC[C@H](C)CC1', 3, 3),
    ('1,1-dimethylcyclohexane', 'CC1(C)CCCCC1', 5, 5),
    ('4-tert-butylcyclohexanone', 'CC(C)(C)C1CCC(=O)CC1', 6, 6),
    ('cis-4-tert-butylcyclohexanol', 'CC(C)(C)[C@H]1CC[C@@H](O)CC1', 6, 6),
    ('(1r,3r,5s)-cyclohexane-1,3,5-triol (cis,trans)', 'O[C@H]1C[C@@H](O)C[C@H](O)C1', 4, 3),
    ('all-cis-cyclohexane-1,3,5-triol', 'O[C@H]1C[C@@H](O)C[C@@H](O)C1', 2, 2),
    ('trans-decalin', 'C1CC[C@@H]2CCCC[C@H]2C1', 3, 3),
    ('cis-decalin', 'C1CC[C@H]2CCCC[C@H]2C1', 3, 3),
    ('spiro[4.4]nonane', 'C1CCC2(C1)CCCC2', 3, 3),
    ('(+)-camphor', 'CC1(C)[C@@H]2CC[C@@]1(C)C(=O)C2', 10, 9),
    ('(+)-alpha-pinene', 'CC1=CC[C@@H]2C[C@H]1C2(C)C', 10, 9),
    ('(-)-menthone', 'CC(C)[C@@H]1CC[C@@H](C)CC1=O', 10, 9),
    ('meso-tartaric acid', 'O=C(O)[C@@H](O)[C@H](O)C(=O)O', 2, 2),
    ('L-tartaric acid', 'O=C(O)[C@@H](O)[C@@H](O)C(=O)O', 2, 2),
    ('diisopropyl L-tartrate', 'CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C', 5, 4),
    ('ribitol (meso)', 'OC[C@H](O)[C@H](O)[C@H](O)CO', 3, 3),
    ('xylitol (meso)', 'OC[C@H](O)[C@@H](O)[C@H](O)CO', 3, 3),
    ('D-arabinitol (chiral)', 'OC[C@@H](O)[C@@H](O)[C@H](O)CO', 5, 4),
    ('ribaric acid (meso, pseudoasymmetric C3)', 'OC(=O)[C@H](O)[C@H](O)[C@H](O)C(O)=O', 3, 3),
    ('xylaric acid (meso)', 'OC(=O)[C@H](O)[C@@H](O)[C@H](O)C(O)=O', 3, 3),
    ('D-arabinaric acid (chiral)', 'OC(=O)[C@@H](O)[C@@H](O)[C@H](O)C(O)=O', 5, 4),
    ('scyllo-inositol', 'O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@@H](O)[C@@H]1O', 1, 1),
    ('myo-inositol', 'O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@@H](O)[C@H]1O', 4, 4),
    ('citric acid (prochiral)', 'OC(=O)CC(O)(CC(O)=O)C(O)=O', 4, 4),
    ('L-phenylalanine', 'N[C@@H](Cc1ccccc1)C(=O)O', 7, 7),
    ('(R)-glycerol 1-acetate', 'CC(=O)OC[C@H](O)CO', 5, 5),
    ('(S)-ibuprofen', 'CC(C)Cc1ccc(cc1)[C@H](C)C(=O)O', 11, 10),
    ('(R)-BINOL (axial chirality)', 'Oc1ccc2ccccc2c1-c1c(O)ccc2ccccc12', 10, 10),
    ("2,2'-dimethylbiphenyl", 'Cc1ccccc1-c1ccccc1C', 7, 7),
    ('(R)-penta-2,3-diene (allene)', 'C/C=C=C/C', 3, 3),
    ('4-methylpenta-2,3-dien-1-ol (Me2C=C=CH-R)', 'CC(C)=C=CCO', 5, 5),
    ('(R)-methyl phenyl isopropyl phosphine oxide', 'C[P@](=O)(c1ccccc1)C(C)C', 8, 7),
    ('(S)-isopropyl methyl sulfoxide', 'C[S@](=O)C(C)C', 4, 3),
    ("Tröger's base", 'Cc1ccc2c(c1)CN1CN2Cc2cc(C)ccc21', 9, 9),
    ('hexamethylbenzene', 'Cc1c(C)c(C)c(C)c(C)c1C', 2, 2),
    ('triphenylmethanol', 'OC(c1ccccc1)(c1ccccc1)c1ccccc1', 5, 5),
    ('testosterone', 'C[C@]12CC[C@H]3[C@@H](CCC4=CC(=O)CC[C@@]34C)[C@@H]1CC[C@@H]2O', 19, 19),
    ('sucrose', 'OC[C@H]1O[C@@](CO)(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@H]2O)[C@@H](O)[C@@H]1O', 12, 12),
    ('strychnine', 'O=C7N2c1ccccc1[C@@]64[C@@H]2[C@@H]3[C@@H](OC/C=C5\\[C@@H]3C[C@@H]6N(CC4)C5)C7', 21, 21),
    ('erythromycin A', 'CC[C@@H]1[C@@]([C@@H]([C@H](C(=O)[C@@H](C[C@@]([C@@H]([C@H]([C@@H]([C@H](C(=O)O1)C)O[C@H]2C[C@@]([C@H]([C@@H](O2)C)O)(C)OC)C)O[C@H]3[C@@H]([C@H](C[C@H](O3)C)N(C)C)O)(C)O)C)C)O)(C)O', 36, 36),
    ('paclitaxel', 'CC1=C2[C@@]([C@]([C@H]([C@@H]3[C@]4([C@H](OC4)C[C@@H]([C@]3(C(=O)[C@@H]2OC(=O)C)C)O)OC(=O)C)OC(=O)c5ccccc5)(C[C@@H]1OC(=O)[C@@H]([C@H](c6ccccc6)NC(=O)c7ccccc7)O)O)(C)C', 41, 40),
    ('cholesterol', 'C[C@H](CCCC(C)C)[C@H]1CC[C@@H]2[C@@]1(CC[C@H]3[C@H]2CC=C4[C@@]3(CC[C@@H](C4)O)C)C', 27, 26),
    ('meso-(2R,4S)-3-methylpentane-2,4-diol', 'C[C@@H](O)C(C)[C@H](C)O', 4, 4),
    ('(2R,4R)-3-methylpentane-2,4-diol', 'C[C@@H](O)C(C)[C@@H](C)O', 6, 5),
    ('Boc-L-Val-OMe', 'CC(C)[C@H](NC(=O)OC(C)(C)C)C(=O)OC', 9, 8),
]


class C13CountTests(unittest.TestCase):
    """Formal counts follow substitution; lower counts are manual coincidence options."""

    def test_no_stereo_element_keeps_graph_classes(self):
        self.assertEqual(counts("Cc1cc(C)cc(C)c1"), (3, 3, False))
        self.assertEqual(counts("CC(C)C1(O)CCCCC1"), (6, 6, False))

    def test_isopropyl_next_to_stereocenter(self):
        self.assertEqual(counts("CC(C)[C@H](N)C(=O)O"), (5, 4, True))

    def test_meso_compounds_are_not_split(self):
        self.assertEqual(counts("C[C@@H](O)C(C)[C@H](C)O"), (4, 4, False))
        self.assertEqual(counts("OC[C@H](O)[C@H](O)[C@H](O)CO"), (3, 3, False))

    def test_ring_cis_trans_carbons_stay_split_when_merged(self):
        self.assertEqual(counts("O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@@H](O)[C@H]1O"), (4, 4, False))

    def test_geminal_groups_on_double_bond(self):
        self.assertEqual(counts("Cc1ccc(cc1)C(Cl)=C(CC)CC"), (11, 9, True))

    def test_sulfur_stereocenter(self):
        self.assertEqual(counts("C[S@](=O)C(C)C"), (4, 3, True))

    def test_diisopropyl_ketone_has_three_carbon_environments(self):
        # 2,4-Dimethylpentan-3-one: carbonyl, methines and four equivalent methyls.
        self.assertEqual(counts("CC(C)C(=O)C(C)C")[:2], (3, 3))

    def test_validation_set(self):
        for name, smiles, resolved, merged in VALIDATION_SET:
            with self.subTest(name=name):
                self.assertEqual(counts(smiles)[:2], (resolved, merged))

    def test_unassigned_diastereomers_keep_complete_configuration_counts_or_budget_refusal(self):
        for smiles, expected in (
            ("CC(O)C(C)C(C)O", (6, {4, 6})),
            ("OCC(O)C(O)C(O)CO", (5, {3, 5})),
            ("OC1C(O)C(O)C(O)C(O)C1O", (1, {1})),
        ):
            mol = Chem.MolFromSmiles(smiles)
            for variant in (mol, Chem.AddHs(mol), Chem.RenumberAtoms(
                mol, list(reversed(range(mol.GetNumAtoms()))),
            )):
                with self.subTest(smiles=smiles, atoms=variant.GetNumAtoms()):
                    result = analyze_molecular_structure_detailed(variant)
                    if smiles.startswith('OC1'):
                        self.assertEqual((result.carbon_count, result.c13_count_alternatives), expected)
                        self.assertFalse(result.partition_complete)
                        self.assertEqual(result.partition_reason, 'enumeration_budget')
                    else:
                        self.assertTrue(result.partition_complete)
                        self.assertEqual(set(result.expected_counts('C')), expected[1])
                        self.assertEqual(result.c13_alternatives[0].name, 'formal')
                        self.assertTrue(all(option.manual_check_only for option in result.c13_alternatives[1:]))

    def test_unspecified_enantiomers_with_identical_partitions_can_be_resolved(self):
        self.assertEqual(counts("CC(O)C(C)C"), (5, 4, True))

    def test_isotope_marker_does_not_collide_with_existing_labels(self):
        self.assertEqual(counts("[1000CH3]C([1000CH3])[C@H](N)C(=O)O"), (5, 4, True))

    def test_atom_order_hydrogens_and_enantiomer_do_not_change_the_counts(self):
        for smiles in (
            "CC(C)[C@H](N)C(=O)O", "C[C@@H](O)C(C)[C@H](C)O",
            "O[C@H]1[C@H](O)[C@@H](O)[C@H](O)[C@@H](O)[C@H]1O",
        ):
            mol = Chem.MolFromSmiles(smiles)
            expected = counts(smiles)
            mirror = Chem.Mol(mol)
            for atom in mirror.GetAtoms():
                atom.InvertChirality()
            for variant in (mol, Chem.AddHs(mol), mirror, Chem.RenumberAtoms(
                mol, list(reversed(range(mol.GetNumAtoms()))),
            )):
                with self.subTest(smiles=smiles):
                    result = analyze_molecular_structure_detailed(variant)
                    self.assertEqual(result.carbon_count, expected[0])
                    self.assertEqual(result.c13_count_alternatives, set(expected[:2]))

    def test_embedding_failure_and_exception_preserve_legacy_counts_and_wrappers(self):
        mol = Chem.MolFromSmiles("CC(C)[C@H](N)C(=O)O")
        for outcome in (-1, ValueError("embedding failed")):
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=outcome), patch(
                "checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule", **kwargs,
            ):
                result = analyze_molecular_structure_detailed(mol)
                self.assertEqual(result.carbon_count, 5)
                self.assertEqual([alt.name for alt in result.c13_alternatives],
                                 ["base", "diastereotopic", "alkene_plus_stereocenter"])
                self.assertEqual(analyze_molecular_structure(mol), result.legacy_tuple())

    def test_exhausted_embedding_work_has_fixed_attempts_and_no_elapsed_deadline(self):
        attempts = []

        def exhaust_budget(mol, parameters):
            attempts.append((parameters.maxIterations, parameters.timeout,
                             parameters.randomSeed, parameters.numThreads, parameters.useRandomCoords))
            return -1

        with patch("checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule",
                   side_effect=exhaust_budget):
            result = analyze_molecular_structure_detailed(Chem.MolFromSmiles("CC(C)[C@H](N)C(=O)O"))
        self.assertEqual(attempts, [(100, 0, 42, 1, False), (100, 0, 42, 1, True)])
        self.assertEqual(result.carbon_count, 5)
        self.assertEqual(result.c13_count_alternatives, {4, 5})
        self.assertEqual([alt.name for alt in result.c13_alternatives],
                         ["base", "diastereotopic", "alkene_plus_stereocenter"])

    def test_retry_with_remaining_work_preserves_substitution_results(self):
        embed = rdDistGeom.EmbedMolecule
        attempts = []

        def first_start_fails(mol, parameters):
            attempts.append(parameters.useRandomCoords)
            return -1 if len(attempts) == 1 else embed(mol, parameters)

        with patch("checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule",
                   side_effect=first_start_fails):
            result = analyze_molecular_structure_detailed(Chem.MolFromSmiles("CC(C)[C@H](N)C(=O)O"))
        self.assertEqual(attempts, [False, True])
        self.assertEqual(result.carbon_count, 5)
        self.assertEqual([(alt.name, alt.count) for alt in result.c13_alternatives],
                         [("formal", 5), ("diastereotopic_merged", 4)])

    def test_input_stereo_isotopes_conformer_and_atom_indices_are_preserved(self):
        mol = Chem.MolFromSmiles("[13CH3]C(C)[C@H](N)C(=O)O")
        mol.AddConformer(Chem.Conformer(mol.GetNumAtoms()))
        before = mol.ToBinary()
        result = analyze_molecular_structure_detailed(mol)
        self.assertEqual(mol.ToBinary(), before)
        self.assertEqual(analyze_molecular_structure(mol), result.legacy_tuple())
        self.assertEqual(len(result.legacy_tuple()), 7)

    def test_noncarbon_elements_use_the_formal_partition(self):
        for smiles, field, expected in (
            ("FC(F)[C@H](Cl)Br", "fluorine_count", 2),
            ("F[Si](F)(Cl)[C@H](Cl)Br", "fluorine_count", 2),
            ("B(O)(O)C", "boron_count", 1),
            ("PCCP", "phosphorus_count", 1),
        ):
            with self.subTest(smiles=smiles):
                result = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
                self.assertEqual(getattr(result, field), expected)

    def test_disconnected_or_enhanced_stereo_retains_existing_analysis(self):
        for smiles in ("CC(C)[C@H](N)C(=O)O.O", "CC(C)[C@H](N)C(=O)O |&1:3|"):
            with self.subTest(smiles=smiles):
                result = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
                self.assertEqual(result.c13_alternatives[0].name, "base")
                self.assertEqual(result.carbon_count, 5)


if __name__ == "__main__":
    unittest.main()
