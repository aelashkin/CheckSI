"""Stable report rows and canonical saved error-key compatibility."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSettings, ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.registry import ERROR_TEMPLATES
from checksi.errors.manager import emit_finding, format_finding, get_error_severity
from checksi.utils.error_handler import ERROR_NAMES, ErrorTracker, export_error_stats
from tests.test_errors_config import make_config


# Existing XLSX column-A contract, in its original 72-row order.
LEGACY_REPORT_ROWS = (
    'stereo_check_required',
    'stereo_descriptor_invalid',
    'stereo_descriptor_inconsistent',
    'formula_invalid_format',
    'ea_number_format_invalid',
    'ea_calcd_found_element_mismatch',
    'ea_formula_calcd_mismatch',
    'required_nmr_missing',
    'name_to_smiles_failed',
    'nmr_frequency_invalid',
    'nmr_frequency_mismatch',
    'solvent_proton_peak_found',
    'solvent_carbon_peak_found',
    'solvent_not_found',
    'name_modified_for_smiles',
    'hrms_adduct_mismatch',
    'hrms_ion_balance_invalid',
    'halogen_isotope_unspecified',
    'h_count_mismatch',
    'h_count_exchangeable_ok',
    'h_count_ratio_check',
    'hnmr_j_missing',
    'hnmr_h_count_missing',
    'h_count_not_integer',
    'hnmr_multiplicity_missing',
    'hnmr_signal_desc_invalid',
    'hnmr_wrong_multiplicity',
    'isotope_format_invalid',
    'formula_atom_symbol_invalid',
    'formula_not_found',
    'hrms_formula_charge_missing',
    'hrms_ion_charge_missing',
    'hrms_formula_missing',
    'hrms_charge_inconsistent',
    'calcd_formula_mass_mismatch',
    'calcd_formula_mass_delta',
    'hrms_ppm_too_large',
    'hrms_found_mass_missing',
    'hrms_calcd_mass_missing',
    'nmr_signal_order_invalid',
    'nmr_signal_precision_invalid',
    'nmr_signal_rounding_needed',
    'nmr_signal_format',
    'bad_ending_nmr',
    'hnmr_duplicate_signals',
    'c13_coupling_missing_with_f',
    'nmr_j_missing',
    'nmr_multiplicity_missing',
    'nmr_signal_desc_invalid',
    'nmr_wrong_multiplicity',
    'nmr_mentioned_but_element_missing',
    'nmr_solvent_or_frequency_missing',
    'hnmr_missing_or_uncounted',
    'nmr_signal_count_structure_mismatch',
    'nmr_diastereotopic_note',
    'nmr_section_missing',
    'c13_count_exceeds_formula',
    'c13_count_structure_check',
    'c13_count_structure_check_stereo',
    'name_formula_mismatch',
    'name_formula_mass_delta',
    'cyrillic_letters_found',
    'name_not_found',
    'ea_include_oxygen',
    'ea_tolerance_exceeded',
    'formula_duplicate_elements',
    'formula_order_invalid',
    'ea_formula_element_missing_in_calcd',
    'c13_section_missing',
    'xnuc_duplicate_signals',
    'dp5_low_probability',
    'dp5_analysis_failed',

)


class ErrorCompatibilityReviewTests(unittest.TestCase):
    """Verify effective settings and exported report contracts together."""

    def test_boron_policy_defaults_preserve_warning_info_and_exact_wording(self):
        cfg = ConfigManager.load_factory()
        skip = AnalysisErrorCode.DP5_BORON_NOT_SUPPORTED
        absence = AnalysisErrorCode.C13_BORON_SIGNAL_NOT_OBSERVED
        self.assertEqual(get_error_severity(cfg, skip), ErrorSeverity.WARNING)
        self.assertEqual(get_error_severity(cfg, absence), ErrorSeverity.INFO)
        tracker = ErrorTracker()
        finding = emit_finding(cfg, tracker, skip)
        self.assertEqual(format_finding(finding).split('. ', 1)[1],
            'DP5q not calculated: boron-containing compounds are not supported (no validated geometry/prediction for boron)')
        self.assertIn('C–B carbon not observed, as is common', ERROR_TEMPLATES[absence]['default'])
        cfg.errors.overrides[skip] = ErrorSeverity.DISABLED
        disabled = ErrorTracker()
        emit_finding(cfg, disabled, skip)
        self.assertEqual(disabled.get_count(skip.value), 0)

    def test_legacy_xlsx_rows_stay_in_place_and_new_codes_are_appended(self):
        self.assertEqual(ERROR_NAMES[:len(LEGACY_REPORT_ROWS)], list(LEGACY_REPORT_ROWS))
        appended = ['dp5_not_applicable', 'formula_source_mismatch', 'formula_source_review_required',
                    'ea_isotope_calcd_mismatch', 'dp5_boron_not_supported', 'c13_boron_signal_not_observed',
                    'nmr_configuration_dependent', 'dp5_configuration_dependent']
        self.assertEqual(ERROR_NAMES[len(LEGACY_REPORT_ROWS):], appended)
        with TemporaryDirectory() as temporary:
            path = export_error_stats(ErrorTracker().to_dict(), 'input.pdf', str(Path(temporary) / 'stats.xlsx'), open_file=False)
            workbook = load_workbook(path, read_only=True)
            try:
                rows = [row[0] for row in workbook.active.iter_rows(min_row=2, values_only=True)]
                self.assertEqual(rows, [*LEGACY_REPORT_ROWS, *appended])
            finally:
                workbook.close()

    def test_alias_is_not_an_independent_error_or_editor_row(self):
        legacy = AnalysisErrorCode.NMR_SOLVENT_OR_FREQUENCY_MISSING
        self.assertIs(AnalysisErrorCode.NMR_SOLVENT_AND_FREQUENCY_MISSING, legacy)
        self.assertIs(AnalysisErrorCode('nmr_solvent_and_frequency_missing'), legacy)
        self.assertNotIn('nmr_solvent_and_frequency_missing', ERROR_NAMES)
        self.assertEqual(len(AnalysisErrorCode), 80)
        # This existing member controls EA policy and intentionally emits no text.
        self.assertEqual(set(ERROR_TEMPLATES), set(AnalysisErrorCode) - {AnalysisErrorCode.EA_INCLUDE_OXYGEN})

    def test_alias_normalizes_without_mutating_input_and_legacy_wins_both_orders(self):
        for overrides in [
            {'nmr_solvent_and_frequency_missing': 'warning'},
            {'nmr_solvent_and_frequency_missing': 'critical', 'nmr_solvent_or_frequency_missing': 'disabled'},
            {'nmr_solvent_or_frequency_missing': 'disabled', 'nmr_solvent_and_frequency_missing': 'critical'},
        ]:
            with self.subTest(overrides=overrides):
                before = deepcopy(overrides)
                settings = ErrorSettings(default_severity='info', overrides=overrides)
                self.assertEqual(overrides, before)
                expected = overrides.get('nmr_solvent_or_frequency_missing', overrides['nmr_solvent_and_frequency_missing'])
                self.assertEqual(settings.overrides, {AnalysisErrorCode.NMR_SOLVENT_OR_FREQUENCY_MISSING: ErrorSeverity(expected)})
                with TemporaryDirectory() as temporary:
                    path = Path(temporary) / 'settings.toml'
                    manager = ConfigManager(path)
                    manager.save(make_config(overrides))
                    rendered = path.read_text()
                    self.assertNotIn('nmr_solvent_and_frequency_missing', rendered)
                    self.assertEqual(manager.load().errors.overrides, {
                        **settings.overrides,
                        AnalysisErrorCode.NMR_CONFIGURATION_DEPENDENT: ErrorSeverity.WARNING,
                        AnalysisErrorCode.DP5_CONFIGURATION_DEPENDENT: ErrorSeverity.WARNING,
                    })

    def test_shipped_mmff_limitation_is_informational_and_overridable(self):
        settings_path = Path(__file__).resolve().parents[1] / 'settings.toml'
        cfg = ConfigManager(settings_path).load()
        self.assertEqual(cfg.errors.overrides[AnalysisErrorCode.DP5_NOT_APPLICABLE], ErrorSeverity.INFO)
        cfg.errors.overrides[AnalysisErrorCode.DP5_NOT_APPLICABLE] = ErrorSeverity.WARNING
        self.assertEqual(cfg.errors.overrides[AnalysisErrorCode.DP5_NOT_APPLICABLE], ErrorSeverity.WARNING)
