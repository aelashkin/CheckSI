"""Expected groups predate this implementation and have independent graph proofs."""

import json
from pathlib import Path
import unittest

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed


ORACLE = json.loads((Path(__file__).parent / 'fixtures/chemistry/nucleus_configuration_oracle.json').read_text())


class NucleusPartitionOracleTests(unittest.TestCase):
    def test_all_preimplementation_original_atom_partitions_match(self):
        for case in ORACLE['cases']:
            with self.subTest(case=case['id']):
                source = Chem.MolFromSmiles(case['source_smiles'])
                result = analyze_molecular_structure_detailed(source)
                self.assertTrue(result.partition_complete, result.partition_reason)
                actual = {variant.smiles: variant for variant in result.isomer_partitions}
                self.assertEqual(set(actual), {form['smiles'] for form in case['forms']})
                for form in case['forms']:
                    for element, groups in form['groups'].items():
                        self.assertEqual(actual[form['smiles']].for_element(element).groups,
                                         tuple(map(tuple, groups)), (case['id'], form['smiles'], element))
                for element in {element for form in case['forms'] for element in form['groups']}:
                    self.assertEqual(result.expected_counts(element), tuple(sorted({
                        len(form['groups'].get(element, [])) for form in case['forms']})))

    def test_fluorine_only_same_count_groups_are_not_mixed_between_isomers(self):
        result = analyze_molecular_structure_detailed(Chem.MolFromSmiles('FC(F)C(O)C(O)C(F)F'))
        self.assertEqual(result.expected_counts('C'), (2,))
        self.assertEqual(result.expected_counts('F'), (2,))
        self.assertFalse(result.configuration_dependent('C'))
        self.assertTrue(result.configuration_dependent('F'))
        self.assertEqual({variant.for_element('F').groups for variant in result.isomer_partitions},
                         {((0, 9), (2, 8)), ((0, 8), (2, 9))})


if __name__ == '__main__':
    unittest.main()
