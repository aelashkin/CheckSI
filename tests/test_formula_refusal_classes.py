"""Separate formulas CheckSI cannot read from definite author formula errors."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checksi import main
from checksi.analysis.hrms_fields import classify_refusal
from checksi.config import ConfigManager
from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from tests.test_formula_reconciliation import NMR
from tests.test_hrms import make_config


def hrms_block(token):
    return f'HRMS (ESI): calcd for {token}+ [M+H]+ 61.0648, found 61.0648. '


def ea_block(token):
    return f'Anal. Calcd for {token}: C, 59.96; H, 13.42. Found: C, 59.96; H, 13.42.'


AUTHOR_ERRORS = (
    ('hrms', 'C2.1H26N2', 'non_integer_count', "atom count 'C2.1' is not a whole number"),
    ('ea', 'C2.1H26N2', 'non_integer_count', "atom count 'C2.1' is not a whole number"),
    ('hrms', 'Ng2C21H29', 'unknown_element', "unknown element symbol 'Ng'"),
    ('ea', 'Ng2C21H29', 'unknown_element', "unknown element symbol 'Ng'"),
    ('ea', 'CH4O0', 'zero_count', "zero atom count 'O0'"),
    ('hrms', 'C3H9[18O', 'unbalanced_brackets', "unbalanced bracket '['"),
    ('ea', 'C3H8[18O', 'unbalanced_brackets', "unbalanced bracket '['"),
)
NOT_CHECKED = (
    ('hrms', 'C3H9O·H2O'), ('hrms', 'C3H9O.H2O'),
    ('ea', 'C3H8O2.5H2O'), ('ea', 'C3H8O hydrate'), ('hrms', 'C3H9O hydrate'),
    ('hrms', '[13C] C2H9O'), ('ea', 'C3H7[18O](H)'), ('hrms', '2C3H9O'), ('ea', '0.5 C3H8O'),
    ('hrms', 'C3 H9O'), ('ea', 'C3H8O(H2O)'), ('ea', 'CH3CH2CH2OH'), ('ea', 'MeOH'),
    ('hrms', 'C3H9O+H'), ('ea', 'C3H8O_opaque'),
    # Letter case and group or placeholder abbreviations are notation, not author errors.
    ('hrms', 'kC3H8O'), ('ea', 'cH4O'), ('ea', 'iPrOH'), ('ea', 'nPrOH'), ('hrms', 'AdC10H15'),
    ('ea', 'FcC10H9O'), ('ea', 'TrC10H9'), ('ea', 'TMSCl'), ('ea', 'CuL2'),
)


class FormulaRefusalClassTests(unittest.TestCase):
    def analyze(self, block, overrides=None):
        text = NMR + block
        result = CheckSIController(make_config(overrides=overrides), event_dispatcher=lambda _: None).run_analysis([text])
        return text, result

    def test_definite_rule_violations_keep_the_author_error_identity(self):
        for source, token, variant, message in AUTHOR_ERRORS:
            with self.subTest(source=source, token=token):
                block = hrms_block(token) if source == 'hrms' else ea_block(token)
                _, result = self.analyze(block)
                compound = result['compounds'][0]
                candidate = compound['formula_sources'][source]['candidate']
                self.assertEqual(candidate['raw'], token)
                self.assertEqual(candidate['refusal'], variant)
                self.assertEqual(compound['formula_sources'][source]['status'], 'invalid')
                self.assertEqual(result['error_stats']['formula_invalid_format'], 1)
                line, = [line for line in compound['all_warnings'] if 'formula_invalid_format.' in line]
                self.assertIn(message, line)
                self.assertFalse(any('not checked by CheckSI' in line for line in compound['all_warnings']))
                self.assertEqual(result['assessment']['status'], 'complete')
                _, disabled = self.analyze(block, {'formula_invalid_format': 'disabled'})
                self.assertEqual(disabled['assessment'], result['assessment'])
                self.assertEqual(disabled['error_stats']['formula_invalid_format'], 0)

    def test_unsupported_notation_is_reported_as_not_checked(self):
        for source, token in NOT_CHECKED:
            with self.subTest(source=source, token=token):
                block = hrms_block(token) if source == 'hrms' else ea_block(token)
                text, result = self.analyze(block)
                compound = result['compounds'][0]
                evidence = compound['formula_sources'][source]
                self.assertEqual(evidence['candidate']['raw'], token)
                self.assertEqual(evidence['candidate']['refusal'], 'notation_not_supported')
                self.assertIn('Not checked by CheckSI', evidence['composition']['reason'])
                self.assertFalse(evidence['usable'])
                self.assertEqual(result['error_stats']['formula_invalid_format'], 0)
                self.assertFalse(any('formula_invalid_format.' in line for line in compound['all_warnings']))
                note, = [line for line in compound['all_warnings'] if 'not checked by CheckSI' in line]
                self.assertIn(token, note)
                self.assertEqual(result['assessment']['status'], 'partial')
                record, = result['assessment']['unassessed']
                self.assertEqual(record['reason'], 'formula_notation_not_supported')
                self.assertEqual(record['excerpt'], token)
                self.assertEqual(text[record['source']['start_offset']:record['source']['end_offset']], token)
                _, disabled = self.analyze(block, {'formula_invalid_format': 'disabled'})
                self.assertEqual(disabled['assessment'], result['assessment'])

    def test_doubtful_readings_are_not_called_author_errors(self):
        for raw in ('C3H8O2.5H2O', 'CH3CH2OH', 'MeOH', 'BocC3H8O', '(C3H8O', 'C3H8O)', 'C2H5)2O', 'as', 'this'):
            with self.subTest(raw=raw):
                self.assertEqual(classify_refusal(raw), ('notation_not_supported', None))
        self.assertEqual(classify_refusal('MeNg2C3'), ('unknown_element', 'Ng'))

    def test_parenthesized_declarations_are_read_or_left_unchecked(self):
        # A parenthesis enclosing the whole declaration is not part of it (#24 counts).
        for block, findings in (
            ('HRMS (ESI): m/z 165.0910 (C10H13O2).',
             {'hrms_formula_charge_missing', 'hrms_found_mass_missing', 'hrms_calcd_mass_missing'}),
            ('HRMS (ESI): m/z 165.0910 (C10H13O2+).',
             {'hrms_found_mass_missing', 'hrms_calcd_mass_missing'}),
        ):
            with self.subTest(block=block):
                _, result = self.analyze(block)
                self.assertEqual(result['compounds'][0]['formula_sources']['hrms']['formula'], 'C10H13O2')
                self.assertEqual({code for code, count in result['error_stats'].items()
                                  if count and code != 'name_not_found'}, findings)
                self.assertEqual(result['assessment']['status'], 'complete')
        # Correct group notation outside the grammar is never an author error.
        for block, raw in (('HRMS (EI): m/z 74.0732 (C2H5)2O.', '(C2H5)2O'),
                           ('HRMS (EI) m/z (M+) 262.0908 (C6H5)3P.', '(C6H5)3P'),
                           ('HRMS (ESI) m/z 187.0730 (C10H12O2)Na+.', '(C10H12O2)Na')):
            with self.subTest(block=block):
                _, result = self.analyze(block)
                candidate = result['compounds'][0]['formula_sources']['hrms']['candidate']
                self.assertEqual((candidate['raw'], candidate['refusal']), (raw, 'notation_not_supported'))
                self.assertEqual(result['error_stats']['formula_invalid_format'], 0)
                self.assertEqual([record['reason'] for record in result['assessment']['unassessed']],
                                 ['formula_notation_not_supported'])

    def test_normalized_token_keeps_original_coverage_offsets(self):
        # Legacy normalization shortens '•+' before the token; coverage keeps source offsets.
        text, result = self.analyze('HRMS (EI): [M]•+ calcd for C3H8O–H 60.0570, found 60.0570.')
        self.assertEqual(result['compounds'][0]['formula_sources']['hrms']['candidate']['raw'], 'C3H8O-H')
        record, = result['assessment']['unassessed']
        self.assertEqual(record['reason'], 'formula_notation_not_supported')
        self.assertEqual(text[record['source']['start_offset']:record['source']['end_offset']], 'C3H8O–H')
        self.assertEqual(record['excerpt'], 'C3H8O–H')


class FormulaRefusalCLITests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.manager = ConfigManager(self.root / 'settings.toml')
        self.manager.save(AppConfig.model_validate({
            'paths': {'input_path': 'sample.pdf', 'output_dir': self.root / 'reports'},
            'modes': {'deep_name_analysis': 'off', 'output_mode': 'essential', 'open_reports_on_success': False},
            'solvents': {'entries': [{'name': 'CDCl3', 'aliases': [], 'proton_shifts': [7.26], 'carbon_shifts': [77.0]}]},
            'errors': {'default_severity': 'critical'},
        }))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.export = self.enterContext(patch('checksi.application_runner.export_reports', return_value={
            'html_path': 'actual.html', 'pdf_path': 'actual.pdf', 'error_stats_path': 'actual.xlsx',
            'highlighted_compounds': 0,
        }))

    def run_cli(self, block):
        paragraphs = [NMR + block]
        stdout = io.StringIO()
        with patch('checksi.document_parser.paragraphs_from_input', return_value=paragraphs):
            code = main._handle_run(main._build_parser().parse_args(['run', 'sample.pdf']), self.manager,
                                    log_callback=lambda message: print(message, file=stdout))
        return code, self.export.call_args.args[1], stdout.getvalue()

    def test_not_checked_formula_returns_incomplete_assessment(self):
        code, analysis, summary = self.run_cli(hrms_block('C3H9O·H2O'))
        self.assertEqual(code, 3)
        self.assertIn('assessment_status: partial', summary)
        self.assertEqual(analysis['error_stats']['formula_invalid_format'], 0)
        self.assertEqual([record['reason'] for record in analysis['assessment']['unassessed']],
                         ['formula_notation_not_supported'])

    def test_author_formula_errors_remain_findings_with_complete_assessment(self):
        code, analysis, summary = self.run_cli(hrms_block('C2.1H26N2') + ea_block('C21H29Ng2'))
        self.assertEqual(code, 2)
        self.assertIn('assessment_status: complete', summary)
        self.assertEqual(analysis['error_stats']['formula_atom_symbol_invalid'], 1)
        sources = analysis['compounds'][0]['formula_sources']
        self.assertEqual(sources['hrms']['candidate']['refusal'], 'non_integer_count')
        self.assertEqual(sources['ea']['status'], 'invalid')
        warnings = analysis['compounds'][0]['all_warnings']
        self.assertTrue(any("atom count 'C2.1' is not a whole number" in line for line in warnings))
        self.assertFalse(any('not checked by CheckSI' in line for line in warnings))


if __name__ == '__main__':
    unittest.main()
