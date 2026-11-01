"""Original catalogue source boundaries; expected names come from the headings."""

import unittest

from checksi.analysis.name_resolver import smiles_and_name_from_paragraph
from checksi.assessment import SUPPORTED_PROTON_HEADER
from checksi.utils.error_handler import ErrorTracker
from tests.test_name_resolver import make_config
from tests import test_wave3_regressions

NAME = "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)"
PROCEDURE_25 = 'Product 5.3 was synthesized from alkene 1.1 triazene 2.3 (0.2 mmol) by the general procedure A. Eluent for chromatography EtOAc:Hexane = 1:10. Pale yellow oil (48, 77% yield).'
SOURCE_25 = '1H NMR (400 MHz, CDCl3) δ 7.19 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.44 (br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (m, 8H), 2.17–2.11 (m, 1H), 1.91–1.84 (m, 2H), 1.47–1.38 (m, 2H), 1.03–0.18 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 141.9, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 20.3. APCI [M+H]+ calcd for C21H29N2+ 309.2325; found 309.2302.'
SOURCE_57 = '1H NMR (400 MHz, (CD3)2SO) δ 7.75 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.4 (br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (m, 8H), 2.17–2.11 (m, 1H), 1.91–1.84 (m, 2H), 1.47–1.38 (m, 2H), 1.03–0.95 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 148, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 20.3. APCI [M+H]+ calcd for C21H29N2+'
SOURCE_58 = '1H NMR (400 MHz, CDCl3) δ 7.76 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.44 (br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (d, 8H), 2.17–2.11 (q, 1H), 1.91–1.84 (m, 2H), 1.47–1.38 (m, 2H), 1.03–0.95 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 147.6, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 20.3. APCI [M+H]+ calcd for C21H29N2+ 309.2325; found 309.2302.'
SOURCE_127 = '1H NMR (400 MHz, CDCl3) δ 8.49 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.44 (br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (m, 8H), 2.17–2.11 (m, 1H), 1.91–1.84 (m, 2H), 1.47–1.38 (m, 2H), 1.03–0.95 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 154.9, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 20.3. APCI [M+H]+ calcd for C21H29N2+ 309.2325; found.'
SOURCE_128 = '1H NMR (400 MHz, (CD3)2SO) δ 8.50 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.44 (br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (m, 8H), 2.17–2.11 (m, 1H), 1.91–1.84 (m, 2H), 1.47–1.38 (m, 2H), 1.03–0.95 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 155.0, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 20.3. Anal. Calcd: C, 81.77; H, 9.15; N, 9.08. Found: C, 81.77; H, 9.15; N, 9.08.'


class CatalogueSourceOwnershipTests(unittest.TestCase):
    def test_later_heading_takes_precedence_over_earlier_property_followup(self):
        cases = (
            ('ethanol (1). Colorless oil. methanol (2). Colorless solid.', ('methanol', '2')),
            ('ethanol (1). Colorless oil; methanol (2)', ('', '2')),
            ('ethanol (1). Colorless oil. methanol (2, yield 77%)', ('methanol', '2, yield 77%')),
            ('ethanol (GP1). Colorless oil; methanol (GP2)', ('', 'GP2')),
            ('ethanol (GP1). Colorless oil methanol (GP2)', ('', 'GP2')),
            ('ethanol (GP1). Colorless oil, route B (GP2)', ('ethanol', 'GP1')),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                result = smiles_and_name_from_paragraph(make_config(mode='off'), ErrorTracker(), text, {})
                self.assertEqual(result[1:3], expected)

    def extract(self, parts, extension):
        paragraphs = test_wave3_regressions.ReviewNameBoundaryTests.parse_sources(self, parts, extension)
        results = []
        for i, text in enumerate(paragraphs):
            if SUPPORTED_PROTON_HEADER.search(text):
                result = smiles_and_name_from_paragraph(make_config(mode='off'), ErrorTracker(),
                    paragraphs[i - 1] if i else '', {})
                results.append((result[1], result[2], text))
        return results

    def test_source_25_uses_the_heading_instead_of_property_and_yield(self):
        for extension in ('docx', 'pdf'):
            with self.subTest(extension=extension):
                result, = self.extract([NAME + ' (1.25)', PROCEDURE_25, SOURCE_25], extension)
                self.assertEqual(result[:2], (NAME, '1.25'))

    def test_first_pdf_heading_is_isolated_after_table_of_contents(self):
        result, = self.extract(['Table of Contents',
            'Name section validation-------------------------------------------------------------------------12',
            'Formula validation (HRMS/EA)--------------------------------------------------------------100',
            NAME + ' (1.1,', 'correct)', SOURCE_25], 'pdf')
        self.assertEqual(result[:2], (NAME, '1.1, correct'))

    def test_truncated_57_formula_tail_and_charge_stay_out_of_58_name(self):
        for extension in ('docx', 'pdf'):
            for separate_charge in (False, True):
                with self.subTest(extension=extension, separate_charge=separate_charge):
                    parts = [NAME + ' (1.57)', SOURCE_57[:-1] if separate_charge else SOURCE_57]
                    if separate_charge:
                        parts.append('+')
                    first, second = self.extract(parts + [NAME + ' (1.58)', SOURCE_58], extension)
                    self.assertEqual(first[:2], (NAME, '1.57'))
                    self.assertEqual(second[:2], (NAME, '1.58'))
                    self.assertRegex(first[2], r'APCI \[M\+H\]\+ calcd for C21H29N2\s*\+')
                    self.assertNotIn('C21H29N2+ ' + NAME, second[0])
                    self.assertNotIn('(1.58)', first[2])

    def test_127_missing_found_mass_never_owns_next_128_heading(self):
        for extension in ('docx', 'pdf'):
            with self.subTest(extension=extension):
                # The actual PDF wraps before its charge, then ends 'found.'
                source, charge = SOURCE_127.split('C21H29N2', 1)
                first, second = self.extract([NAME + ' (1.127)', source + 'C21H29N2',
                    charge.strip().replace('found.', ''), 'found.', NAME + ' (1.128)', SOURCE_128], extension)
                self.assertEqual(first[:2], (NAME, '1.127'))
                self.assertEqual(second[:2], (NAME, '1.128'))
                self.assertNotIn('(1.128)', first[2])
                self.assertIn('found.', first[2])

    def test_bare_identifier_26_and_noname_27_remain_negative_controls(self):
        for heading in (NAME + ' 1a', 'Noname'):
            for extension in ('docx', 'pdf'):
                with self.subTest(heading=heading, extension=extension):
                    result, = self.extract([heading, SOURCE_25], extension)
                    self.assertEqual(result[:2], ('', ''))

    def test_no_space_heading_before_property_does_not_join_truncated_neighbor(self):
        for extension in ('docx', 'pdf'):
            with self.subTest(extension=extension):
                first, second = self.extract([NAME + ' (1.57)', SOURCE_57,
                    'methanol(1.58).', 'Colorless oil, route B (GP1)', SOURCE_58], extension)
                self.assertEqual(first[:2], (NAME, '1.57'))
                self.assertEqual(second[:2], ('methanol', '1.58'))
                self.assertNotIn('methanol', first[2])
                self.assertRegex(first[2], r'C21H29N2\s*\+')


if __name__ == '__main__':
    unittest.main()
