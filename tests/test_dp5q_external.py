"""Pinned external data retain assignment ambiguity and missing adjudication."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('dp5q_external',ROOT/'scripts/dp5q-reference/external.py')
external=importlib.util.module_from_spec(spec)
spec.loader.exec_module(external)


class ExternalDataTests(unittest.TestCase):
    def test_selected_loop_preserves_quoted_values_and_alternative_rows(self):
        source='''data_test
loop_
_Atom_chem_shift.Atom_ID
_Atom_chem_shift.Atom_type
_Atom_chem_shift.Atom_isotope_number
_Atom_chem_shift.Val
_Atom_chem_shift.Ambiguity_code
C1 C 13 24.751 2
C1 C 13 23.589 2 # alternative atom assignment
'C #2' C 13 24.751 2
stop_
'''
        rows=external.selected_loops(source,'_Atom_chem_shift.')
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[2]['Atom_ID'],'C #2')
        self.assertEqual([row['Val'] for row in rows[:2]],['24.751','23.589'])

    def test_partial_rows_and_multiline_selected_fields_are_refused(self):
        for data in ('C1 C 13', ';multiline\n;'):
            source='loop_\n_Atom_chem_shift.Atom_ID\n_Atom_chem_shift.Atom_type\n'+data+'\nstop_\n'
            with self.subTest(data=data),self.assertRaises(ValueError):
                external.selected_loops(source,'_Atom_chem_shift.')

    def test_pinned_external_cases_have_no_adjudicated_scientific_labels(self):
        manifest=json.loads((ROOT/'tests/fixtures/dp5q/bmrb/manifest.json').read_text())
        self.assertEqual(len(manifest['requests']),8)
        self.assertEqual(len(manifest['records']),4)
        self.assertEqual({item['scientific_status'] for item in manifest['requests']},{'unscorable_adjudication_pending'})
        self.assertTrue(all(item['source_label_correct'] is None for item in manifest['requests']))
        leucine=next(item for item in manifest['records'] if item['accession']=='bmse000042')
        self.assertEqual(len(leucine['carbon_assignment_rows']),8)
        self.assertEqual(leucine['chemical_equivalence_groups'],[])
        self.assertEqual(leucine['carbon_assignment_status'],'alternative_methyl_assignments_unadjudicated')
        self.assertEqual(len({row['Val'] for row in leucine['carbon_assignment_rows']}),6)
        mapping=leucine['topology_mapping']
        self.assertEqual(mapping['match_count'],2)
        self.assertEqual(mapping['carbon_bmrb_id_to_possible_mol_indices_1based']['C1'],[1,2])
        alanine=next(item for item in manifest['records'] if item['accession']=='bmse000028')
        self.assertEqual(alanine['topology_mapping']['carbon_bmrb_id_to_possible_mol_indices_1based']['C2'],[2])
        self.assertTrue(all(not item['assessment_executed'] for item in manifest['requests']))


if __name__=='__main__':
    unittest.main()
