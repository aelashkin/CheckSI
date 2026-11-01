"""Analytical continuations stay qualified and inside compound ownership."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document

from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from tests.test_formula_reconciliation import EA_ETHANOL, EA_PROPANOL, HRMS, NMR
from tests.test_name_resolver import StubWorker, make_config


class FormulaContinuationBoundaries(unittest.TestCase):
    def evaluate(self, parts):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input.docx'
            document = Document()
            for part in parts:
                document.add_paragraph(part)
            document.save(path)
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
        def resolver(backend, name):
            return {'status': 'resolved', 'smiles': 'CCCO' if name == 'propan-1-ol' else 'CCO'}
        worker = StubWorker(resolver)
        with patch('checksi.analysis.name_resolver._ResolverWorker', return_value=worker):
            result = CheckSIController(make_config()).run_analysis(parsed.paragraphs)
        return parsed, result

    def test_truncated_175_keeps_its_own_name_formula_and_missing_mass_findings(self):
        _, result = self.evaluate(['propan-1-ol (1.75)', NMR + 'HRMS [M+H]+ calcd for C3H9O+',
                                  'ethanol (1.76)', NMR + EA_ETHANOL])
        first, second = result['compounds']
        self.assertEqual(first['name'], 'propan-1-ol')
        self.assertEqual(second['name'], 'ethanol')
        self.assertEqual(first['formula_sources']['hrms']['formula'], 'C3H9O')
        for code in ('hrms_calcd_mass_missing', 'hrms_found_mass_missing'):
            self.assertTrue(any(code + '.' in line for line in first['all_warnings']))
        self.assertFalse(any('name_formula_mismatch.' in line for line in first['all_warnings']))
        self.assertNotIn('ethanol', first['original_text'])

    def test_short_instrument_prefixes_keep_ea_first_pairing(self):
        for prefix in ('HRMS (ESI)', 'EI-HRMS', 'HR-ESI-MS', 'MS (ESI)'):
            with self.subTest(prefix=prefix):
                _, result = self.evaluate(['propan-1-ol (1)', NMR + EA_ETHANOL,
                                          HRMS.replace('HRMS (ESI)', prefix)])
                first = result['compounds'][0]
                self.assertTrue(first['formula_sources']['hrms_neutral']['usable'])
                self.assertTrue(any('formula_source_mismatch.' in line for line in first['all_warnings']))

    def test_data_free_mass_words_are_not_absorbed_as_an_analytical_continuation(self):
        for tail in ('HRMS spectra: Figure S12', 'ESI-2', 'TOF MS measurements were not possible'):
            with self.subTest(tail=tail):
                _, result = self.evaluate(['propan-1-ol (1)', NMR + EA_PROPANOL,
                                          tail, 'ethanol (2)', NMR + EA_ETHANOL])
                self.assertNotIn(tail, result['compounds'][0]['original_text'])
                self.assertEqual(result['compounds'][1]['name'], 'ethanol')

    def test_a_property_prefix_can_precede_a_real_analytical_continuation(self):
        _, result = self.evaluate(['propan-1-ol (1)', NMR + EA_ETHANOL,
                                  'IR (ATR): 1650 cm-1. ' + HRMS])
        self.assertTrue(result['compounds'][0]['formula_sources']['hrms_neutral']['usable'])
