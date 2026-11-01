"""Carbon annotations count overlapping signals without cloning DP5 observations."""

from dataclasses import asdict
import json
import re
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.nmr import calculate_nmr_number_count, check_nmr, preprocess_signals
from checksi.analysis.stereochem import check_streo_desc
from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import DP5PreparationSkip, _compound_request
from checksi.dp5q.observation_policy import derive_observation_policy
from checksi.utils.error_handler import ErrorTracker


def config(disabled=False, *, disabled_codes=()):
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "disabled" if disabled else "warning",
                   "overrides": {code: "disabled" for code in disabled_codes}},
    })


def carbon(smiles, formula, shifts, **kwargs):
    structure = analyze_molecular_structure_detailed(Chem.AddHs(Chem.MolFromSmiles(smiles)))
    tracker = ErrorTracker()
    policy = derive_observation_policy(Chem.AddHs(Chem.MolFromSmiles(smiles)), structure=structure)
    checked = check_nmr(config(kwargs.pop("disabled", False),
                               disabled_codes=kwargs.pop("disabled_codes", ())), tracker,
        f"13C NMR (100 MHz, CDCl3): δ {shifts}.", "13C", 1,
        structure.carbon_count, 400, structure.stereocenter_count,
        structure.one_pair_center_count, formula, 0, 0,
        structure.c13_count_alternatives, carbon_groups=structure.carbon_groups,
        c13_count_alternatives_w_groups=structure.c13_alternatives,
        stereogenic_signal_flag=structure.stereogenic_signal_flag,
        structure_partition_complete=structure.partition_complete,
        structure_partition_reason=structure.partition_reason,
        coincident_environments=policy.coincident_environments,
        contains_boron=policy.contains_boron, configuration_status=policy.configuration_status,
        direct_boron_groups=tuple(policy.groups[i].labels for i in policy.direct_boron_environments), **kwargs)
    return checked, tracker, structure


class CarbonObservationTests(unittest.TestCase):
    def test_substitution_counts_select_formal_and_merged_groups_for_dp5(self):
        for disabled in (False, True):
            for shifts, expected_count, expected_key, eligible in (
                ("170.0, 60.0, 30.0, 20.0, 10.0", 5, None, True),
                ("170.0, 60.0, 30.0, 20.0", 5, "c13_count_structure_check_stereo", True),
                ("170.0, 60.0, 30.0", 3, "c13_count_structure_check", False),
            ):
                with self.subTest(disabled=disabled, shifts=shifts):
                    checked, tracker, structure = carbon(
                        "CC(C)[C@H](N)C(=O)O", "C5H11NO2", shifts, disabled=disabled,
                    )
                    self.assertEqual(structure.carbon_count, 5)
                    self.assertEqual([alt.name for alt in structure.c13_alternatives],
                                     ["formal", "diastereotopic_merged"])
                    self.assertEqual(checked[6], eligible)
                    if eligible:
                        selected = next(alt for alt in structure.c13_alternatives
                                        if alt.count == expected_count)
                        self.assertEqual(checked[5], selected.groups_dp5)
                        self.assertEqual(len(checked[7]), len(shifts.split(',')))
                    for key in ("c13_count_structure_check", "c13_count_structure_check_stereo"):
                        # A mismatched chiral compound also retains the existing
                        # diastereotopic note under the same diagnostic identity.
                        expected_occurrences = 2 if key == "c13_count_structure_check" else 1
                        self.assertEqual(tracker.get_count(key),
                                         expected_occurrences if not disabled and key == expected_key else 0)
                    if expected_key == "c13_count_structure_check" and not disabled:
                        self.assertTrue(any("5 or 4" in line for line in checked[0]))

    def test_manual_coincidence_preserves_full_groups_and_supported_sharing(self):
        for smiles, formula, shifts, count in (
                ('CC(C)[C@H](N)C(=O)O', 'C5H11NO2', '170.0, 60.0, 30.0, 20.0', 5),
                ('C[S@](=O)C(C)C', 'C4H10OS', '50.0, 30.0, 20.0', 4),
                ('CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C', 'C10H18O6',
                 '170.0, 70.0, 60.0, 20.0', 5)):
            for disabled in (False, True):
                with self.subTest(smiles=smiles, disabled=disabled):
                    details, scope = {}, {}
                    checked, _, structure = carbon(smiles, formula, shifts,
                        disabled=disabled, dp5_refusal_details=details, carbon_scope_out=scope)
                    self.assertTrue(checked[6])
                    self.assertEqual(checked[5], structure.carbon_groups)
                    self.assertEqual(details, {})
                    self.assertEqual(scope['full_environment_count'], count)
                    self.assertEqual(scope['observation_count'], count - 1)
                    self.assertEqual(len(checked[7]), count - 1)

    def test_incomplete_structure_partition_retains_full_observations_for_geometry(self):
        details = {}
        with patch('checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule', return_value=-1):
            checked, _, structure = carbon('CC(C)[C@H](N)C(=O)O', 'C5H11NO2',
                '170.0, 60.0, 30.0, 20.0, 10.0', dp5_refusal_details=details)
        self.assertFalse(structure.partition_complete)
        self.assertTrue(checked[6])
        self.assertEqual(checked[5], structure.carbon_groups)
        self.assertEqual(details, {})
        self.assertEqual(structure.partition_reason, 'substitution_incomplete')

    def test_annotations_count_signals_and_preserve_single_peak_dp5_evidence(self):
        for smiles, formula, plain, annotated, fraction, count, expected in (
            ("CC(=O)C", "C3H6O", "207.1, 30.9", "207.1, 30.9 (2C)", 2 / 3, 3, 2),
            ("Cc1ccccc1", "C7H8", "137.9, 129.1, 128.3, 125.3, 21.5",
             "137.9, 129.1 (2C), 128.3 (2C), 125.3, 21.5", 5 / 7, 7, 5),
            ("c1ccccc1", "C6H6", "128.4", "128.4 (6C)", 1 / 6, 6, 1),
        ):
            with self.subTest(smiles=smiles):
                before, plain_tracker, _ = carbon(smiles, formula, plain)
                details = {}
                checked, tracker, structure = carbon(smiles, formula, annotated, dp5_refusal_details=details)
                self.assertTrue(before[6])
                # Symmetry atoms are one environment, so these author annotations
                # raise the diagnostic count but cannot constrain a DP5 group.
                self.assertFalse(checked[6])
                self.assertEqual(checked[7], before[7])
                self.assertEqual(checked[5], structure.carbon_groups)
                self.assertAlmostEqual(before[4], fraction)
                self.assertEqual(checked[4], 1.0)
                self.assertEqual(plain_tracker.get_count("c13_count_structure_check"), 0)
                self.assertEqual(tracker.get_count("c13_count_structure_check"), 1)
                result_note = next(line for line in checked[0] if line.startswith("C NMR Result:"))
                self.assertEqual(sum(map(int, re.findall(r'\] (\d+)', result_note))), count)
                self.assertIn(
                    f"❗ c13_count_structure_check. Check the number of C signals in 13C NMR: {count}; "
                    f"probably, it should be {expected}. Also check the parsed name. If the molecule has "
                    "groups that are diastereotopic, shows hindered rotation, exists as E/Z (cis/trans) "
                    "stereoisomers, or other factors, the number of signals can be higher than expected.",
                    checked[0])
                disabled_details = {}
                disabled, disabled_tracker, _ = carbon(smiles, formula, annotated,
                    disabled_codes=("c13_count_structure_check",), dp5_refusal_details=disabled_details)
                self.assertEqual(disabled[5:], checked[5:])
                self.assertEqual(details, {"observed_count": expected, "prepared_shift_count": expected,
                                           "required_count": expected, "cause": "annotation_partition_mismatch"})
                self.assertEqual(disabled_details, details)
                self.assertEqual(disabled_tracker.get_count("c13_count_structure_check"), 0)

    def test_annotated_and_listed_signal_totals_preserve_structural_stereo(self):
        cases = (("139.8, 128.1 (2C), 127.1, 126.1, 125.1, 124.1, 78.0", 7),
                 ("139.8, 128.1, 128.1, 127.1, 126.1, 125.1, 124.1, 78.0", 8))
        stereo_smiles = "CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C"
        for severity in ErrorSeverity:
            for trees_count in (None, 8):
                for shifts, peaks in cases:
                    with self.subTest(severity=severity, trees_count=trees_count, peaks=peaks):
                        cfg = config()
                        cfg.errors.default_severity = severity
                        checked = check_nmr(cfg, ErrorTracker(),
                            f"13C NMR (100 MHz, CDCl3): δ {shifts}.",
                            "13C", 1, trees_count, 400, 2, 0, "C14H14O2", 0, 0,
                            carbon_groups=[])
                        self.assertAlmostEqual(checked[4], 8 / 14)
                        self.assertEqual(len(checked[7]), peaks)
                        expected_note = ("❗ Check equivalent  C: 14 - 8 = 6 " if trees_count is None
                                         else "equivalent C in 13C NMR: 14 - 8 = 6 ")
                        self.assertIn(expected_note, checked[0])
                        tracker = ErrorTracker()
                        stereo = check_streo_desc(cfg, tracker, "(1R,2S)", checked[4], 0, 2,
                                                  smiles=stereo_smiles)
                        occurrence, = tracker.occurrences
                        self.assertEqual(occurrence.variant, "structural_non_meso")
                        self.assertEqual(occurrence.severity, severity)
                        self.assertEqual(occurrence.context["meso_status"], "confirmed_non_meso")
                        self.assertEqual(occurrence.context["stereocenter_count"], 2)
                        self.assertNotIn("eq_c_fraction", occurrence.context)
                        self.assertEqual(tracker.get_count("stereo_check_required"),
                                         int(severity is not ErrorSeverity.DISABLED))
                        # The same specified L-tartrate structure stays non-meso
                        # even at fractions that previously changed the guess.
                        for fraction in (.01, .99):
                            self.assertEqual(check_streo_desc(cfg, ErrorTracker(),
                                "(1R,2S)", fraction, 0, 2, smiles=stereo_smiles), stereo)
                        racemate_tracker = ErrorTracker()
                        self.assertEqual(check_streo_desc(cfg, racemate_tracker,
                            "(1RS,2SR)", checked[4], 0, 2, smiles=stereo_smiles), [])
                        self.assertEqual(racemate_tracker.get_count("stereo_descriptor_invalid"), 0)

    def test_catalogue_144_counts_ten_signals_against_nine_environments(self):
        shifts = ("144.4 (2C), 129.7, 126.7, 113.1, 50.1–50.0 (br. m), "
                  "39.5, 36.3, 29.6, 20.3")
        molecule = Chem.AddHs(Chem.MolFromSmiles(
            "Cc1ccc(NC[C@H]2C[C@@H](CNc3ccc(C)cc3)CC2)cc1"))
        structure = analyze_molecular_structure_detailed(molecule)
        self.assertEqual(structure.carbon_count, 9)
        tracker = ErrorTracker()
        details = {}
        checked = check_nmr(config(), tracker,
            f"13C NMR (101 MHz, CDCl3) δ {shifts}.", "13C", 1, 9,
            400, structure.stereocenter_count, structure.one_pair_center_count,
            "C21H28N2", 0, 0, structure.c13_count_alternatives,
            carbon_groups=structure.carbon_groups,
            c13_count_alternatives_w_groups=structure.c13_alternatives,
            stereogenic_signal_flag=structure.stereogenic_signal_flag,
            dp5_refusal_details=details)
        self.assertEqual(calculate_nmr_number_count(
            config(), ErrorTracker(), shifts, "13C", 1, 1, None, 0, 0)[0], 10)
        self.assertEqual(tracker.get_count("c13_count_structure_check"), 2)
        self.assertTrue(any("10; probably, it should be 9" in line for line in checked[0]))
        self.assertEqual(len(checked[7]), 9)
        self.assertFalse(checked[6])
        self.assertEqual(details['cause'], 'annotation_partition_mismatch')
        self.assertIn("C NMR Result: 10. Signals: [>170] 0, [120-170] 4, [80-120] 1, [<80] 5", checked[0])

    def test_annotation_evidence_retains_unknown_counts_and_original_tokens(self):
        observations = []
        shifts = "207.1, 30.9 (2 C)"
        checked, _, _ = carbon("CC(=O)C", "C3H6O", shifts, observations_out=observations)
        self.assertFalse(checked[6])
        self.assertEqual([item.represented_atoms for item in observations], [None, 2])
        for item in observations:
            self.assertEqual(shifts[item.start_offset:item.end_offset], item.raw_token)
        json.dumps([asdict(item) for item in observations], allow_nan=False)
        observations.append(observations[0])
        check_nmr(config(), ErrorTracker(), "No carbon data", "13C", 1, None, 400,
                  0, 0, None, 0, 0, observations_out=observations)
        self.assertEqual(observations, [])

    def test_range_endpoint_policy_retains_complete_source_span_and_atom_count(self):
        observations = []
        shifts = "130.0–131.0 (2C)"
        checked, tracker, _ = carbon("COC", "C2H6O", shifts, observations_out=observations)
        self.assertFalse(checked[6])
        self.assertEqual(checked[7], [130.0])
        self.assertEqual(observations[0].represented_atoms, 2)
        self.assertEqual(observations[0].raw_token, "130.0-131.0 (2C)")
        self.assertEqual((observations[0].start_offset, observations[0].end_offset), (0, len(shifts)))
        self.assertTrue(any("2; probably, it should be 1." in line for line in checked[0]))
        self.assertEqual(tracker.get_count("c13_count_structure_check"), 1)
        self.assertIn("C NMR Result: 2. Signals: [>170] 0, [120-170] 2, [80-120] 0, [<80] 0", checked[0])

    def test_rejected_annotations_count_each_peak_once_even_when_format_finding_is_disabled(self):
        for annotation in ("-2C", "+2C", "- 2C", "+ 2C", "-\t2C", "+\t 2C",
                           "2C, 3C", "0C", "1.4C", "1e4C"):
            for disabled in (False, True):
                with self.subTest(annotation=annotation, disabled=disabled):
                    observations, details = [], {}
                    checked, tracker, _ = carbon("CC(=O)C", "C3H6O", f"207.1, 30.9 ({annotation})",
                        disabled_codes=("nmr_signal_format",) if disabled else (),
                        observations_out=observations, dp5_refusal_details=details)
                    self.assertIsNone(observations[1].represented_atoms)
                    self.assertIn("C NMR Result: 2. Signals: [>170] 1, [120-170] 0, [80-120] 0, [<80] 1", checked[0])
                    self.assertEqual(tracker.get_count("c13_count_structure_check"), 0)
                    self.assertEqual(tracker.get_count("nmr_signal_format"), int(not disabled))
                    self.assertFalse(checked[6])
                    self.assertEqual(details, {"observed_count": 2, "prepared_shift_count": 2,
                                              "required_count": 2, "cause": "invalid_carbon_annotation"})

    def test_invalid_zero_annotation_cannot_hide_a_real_structure_mismatch(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                checked, tracker, _ = carbon("Cc1ccc(F)cc1", "C7H7F",
                    "161.0, 133.5, 130.5, 128.0 (0C), 114.9, 20.8",
                    disabled_codes=("nmr_signal_format",) if disabled else ())
                self.assertIn("C NMR Result: 6. Signals: [>170] 0, [120-170] 4, [80-120] 1, [<80] 1", checked[0])
                self.assertEqual(tracker.get_count("c13_count_structure_check"), 1)
                self.assertTrue(any("6; probably, it should be 5." in line for line in checked[0]))

    def test_coupling_isotope_labels_do_not_become_carbon_annotations(self):
        for label in ("J13C-19F", "1J 13C,19F", "1J 19F,13C", "1J 13C,13C",
                      "1J 19F-13C", "1J 13C-13C", "1J(19F,13C)", "1J (13C,13C)",
                      "1J((19F,13C))", "1J(19F)-(13C)"):
            with self.subTest(label=label):
                observations, details = [], {}
                checked, tracker, _ = carbon("Cc1ccc(F)cc1", "C7H7F",
                    f"161.0, 133.5, 130.5, 114.9 (d, {label} = 21.0 Hz), 20.8",
                    observations_out=observations, dp5_refusal_details=details)
                self.assertEqual([item.represented_atoms for item in observations], [None] * 5)
                self.assertEqual(observations[3].raw_token, f"114.9 (d, {label} = 21.0 Hz)")
                self.assertIn("C NMR Result: 5. Signals: [>170] 0, [120-170] 3, [80-120] 1, [<80] 1", checked[0])
                self.assertEqual(tracker.get_count("c13_count_structure_check"), 0)
                self.assertFalse(any("Carbon atom annotation" in line for line in checked[0]))
                if "(" in label:
                    # Nested descriptors retain the separator check's existing refusal.
                    self.assertFalse(checked[6])
                    self.assertEqual(details, {"observed_count": 5, "prepared_shift_count": 5,
                                              "required_count": 5, "cause": "malformed_separators"})
                else:
                    self.assertEqual(tracker.get_count("nmr_signal_format"), 0)
                    self.assertTrue(checked[6])
                    self.assertEqual(details, {})

    def test_coupling_nuclei_allow_independent_mass_labels_in_either_order(self):
        for other in ("F", "19F", "H", "1H", "C", "13C"):
            for carbon_label in ("C", "13C"):
                for left, right in ((other, carbon_label), (carbon_label, other)):
                    for separator in (",", "-"):
                        label = f"1J {left}{separator}{right}"
                        with self.subTest(label=label):
                            observations, details = [], {}
                            checked, tracker, _ = carbon("Cc1ccc(F)cc1", "C7H7F",
                                f"161.0, 133.5, 130.5, 114.9 (d, {label} = 21.0 Hz), 20.8",
                                observations_out=observations, dp5_refusal_details=details)
                            self.assertEqual([item.represented_atoms for item in observations], [None] * 5)
                            self.assertEqual(observations[3].raw_token, f"114.9 (d, {label} = 21.0 Hz)")
                            self.assertIn("C NMR Result: 5. Signals: [>170] 0, [120-170] 3, [80-120] 1, [<80] 1", checked[0])
                            self.assertEqual(tracker.get_count("c13_count_structure_check"), 0)
                            self.assertEqual(tracker.get_count("nmr_signal_format"), 0)
                            self.assertTrue(checked[6])
                            self.assertEqual(checked[7], [161.0, 133.5, 130.5, 114.9, 20.8])
                            self.assertEqual(details, {})

    def test_coupling_labels_preserve_explicit_valid_and_rejected_annotations(self):
        for label in ("1J 19F,13C", "1J 13C,13C", "1J 19F-13C", "1J(19F,13C)", "1J19F,13C",
                      "1J F,13C", "1J 13C,F", "1J H-13C", "1J13C", "1JC"):
            for annotation, accepted in (("2C", 2), ("-2C", None), ("0C", None), ("2C, 3C", None)):
                for description in (f"{annotation}, {label} = 21.0 Hz", f"{label} = 21.0 Hz, {annotation}",
                                    f"{annotation},{label}", f"{label},{annotation}"):
                    for disabled in (False, True):
                        with self.subTest(description=description, disabled=disabled):
                            observations, details = [], {}
                            checked, tracker, _ = carbon("Cc1ccc(F)cc1", "C7H7F",
                                f"161.0, 133.5, 130.5, 114.9 (d, {description}), 20.8",
                                disabled_codes=("nmr_signal_format",) if disabled else (),
                                observations_out=observations, dp5_refusal_details=details)
                            self.assertEqual([item.represented_atoms for item in observations],
                                             [None, None, None, accepted, None])
                            expected_count = 6 if accepted else 5
                            self.assertTrue(any(line.startswith(f"C NMR Result: {expected_count}.")
                                                for line in checked[0]))
                            self.assertEqual(tracker.get_count("c13_count_structure_check"), int(bool(accepted)))
                            self.assertEqual(sum("Carbon atom annotation" in line for line in checked[0]),
                                             int(accepted is None and not disabled))
                            if "(" not in label:
                                self.assertEqual(tracker.get_count("nmr_signal_format"),
                                                 int(accepted is None and not disabled))
                            self.assertFalse(checked[6])
                            self.assertEqual(checked[7], [161.0, 133.5, 130.5, 114.9, 20.8])
                            cause = ("malformed_separators" if "(" in label else
                                     "invalid_carbon_annotation" if accepted is None else "annotation_partition_mismatch")
                            self.assertEqual(details, {} if cause is None else {
                                "observed_count": 5, "prepared_shift_count": 5,
                                "required_count": 5, "cause": cause})

    def test_explicit_one_and_unknown_both_fit_one_equivalent_environment(self):
        before, _, _ = carbon("CCOCC", "C4H10O", "65.9, 15.4")
        self.assertTrue(before[6])
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                details = {}
                checked, tracker, _ = carbon("CCOCC", "C4H10O", "65.9 (1C), 15.4",
                                              disabled=disabled, dp5_refusal_details=details)
                self.assertTrue(checked[6])
                self.assertEqual(details, {})
                self.assertEqual(tracker.get_count("c13_count_structure_check"), 0)

    def test_fitting_total_is_insufficient_and_impossible_inventory_remains_refused(self):
        for smiles, formula, shifts in (
            ("Cc1ccccc1", "C7H8", "137.9 (3C), 129.1, 128.3, 125.3, 21.5"),
            ("CC(O)C(C)(C)C", "C6H14O", "100.0 (2C), 90.0 (2C), 80.0 (1C), 70.0 (1C)"),
            ("CC(=O)C", "C3H6O", "207.1, 30.9 (3C)"),
            ("CC(=O)C", "C3H6O", "207.1, 30.9 (0C)"),
        ):
            with self.subTest(smiles=smiles, shifts=shifts):
                checked, _, _ = carbon(smiles, formula, shifts)
                self.assertFalse(checked[6])

    def test_negative_annotation_cannot_be_read_as_a_positive_count(self):
        checked, _, _ = carbon("CC(=O)C", "C3H6O", "207.1, 30.9 (-2C)")
        self.assertFalse(checked[6])

    def test_zero_annotation_totals_still_validate_the_observed_peaks(self):
        for shifts, expected in (("100.0 (0C)", [100.0]),
                                 ("100.0 (0C), 30.0 (0C)", [100.0, 30.0])):
            for disabled in (False, True):
                with self.subTest(shifts=shifts, disabled=disabled):
                    details = {}
                    checked, tracker, _ = carbon("CC(=O)C", "C3H6O", shifts,
                        disabled=disabled, dp5_refusal_details=details)
                    self.assertFalse(checked[6])
                    self.assertEqual(checked[7], expected)
                    self.assertEqual(details["cause"], "invalid_carbon_annotation")
                    self.assertEqual(tracker.get_count("nmr_signal_format"), int(not disabled))
                    self.assertEqual(tracker.get_count("nmr_section_missing"), 0)

    def test_noninteger_count_continuations_cannot_supply_an_integer_suffix(self):
        for annotation in ("1.4C", "1e4C"):
            with self.subTest(annotation=annotation):
                checked, _, _ = carbon("CC(C)(C)C", "C5H12", f"100.0, 30.9 ({annotation})")
                self.assertFalse(checked[6])

    def test_nonfinite_numeric_source_retains_json_safe_evidence_without_scoring(self):
        observations = []
        checked, _, _ = carbon("c1ccccc1", "C6H6", "9" * 400 + ".0", observations_out=observations)
        self.assertFalse(checked[6])
        self.assertIsNone(observations[0].shift)
        json.dumps([asdict(item) for item in observations], allow_nan=False)

    def test_coincident_inequivalent_entries_are_not_cloned(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                details = {}
                checked, _, _ = carbon("CCCO", "C3H8O", "63.0, 25.0 (2C)",
                    disabled_codes=("c13_count_structure_check",) if disabled else (),
                    dp5_refusal_details=details)
                self.assertTrue(checked[6])
                self.assertEqual(checked[5], [])
                self.assertEqual(checked[7], [63.0, 25.0])
                self.assertEqual(details, {})

    def test_raw_lower_count_keeps_full_partition_before_annotations(self):
        for smiles, formula in (("CC(O)C(C)C", "C5H12O"), ("CC(C)=CC", "C5H10")):
            with self.subTest(smiles=smiles):
                checked, _, structure = carbon(smiles, formula, "100.0 (2C), 90.0, 80.0, 70.0")
                alternative = next(item for item in structure.c13_alternatives if item.count == 4)
                self.assertTrue(alternative.manual_check_only)
                self.assertEqual(alternative.groups_dp5, [])
                self.assertTrue(checked[6])
                self.assertEqual(checked[5], structure.carbon_groups)
                self.assertEqual(checked[7], [100.0, 90.0, 80.0, 70.0])
                self.assertEqual(checked[4], 1.0)
                refused, _, _ = carbon(smiles, formula, "100.0 (1C), 90.0 (1C), 80.0 (1C), 70.0 (1C)")
                self.assertFalse(refused[6])

    def test_legacy_expansion_and_current_spelling_policy_remain_available(self):
        for annotation in ("2C", "2 C", "2С"):
            with self.subTest(annotation=annotation):
                shifts = f"207.1, 30.9 ({annotation})"
                self.assertEqual(preprocess_signals(shifts), [207.1, 30.9, 30.9])
                checked, _, _ = carbon("CC(=O)C", "C3H6O", shifts)
                self.assertFalse(checked[6])
                self.assertEqual(checked[7], [207.1, 30.9])

    def test_controller_and_adapter_retain_json_safe_observation_constraints(self):
        description = ("1H NMR (400 MHz, CDCl3): δ 2.17 (s, 6H). "
                       "13C NMR (100 MHz, CDCl3): δ 207.1, 30.9 (1C).")
        with patch("checksi.controller._smiles_and_name_from_paragraph",
                   return_value=("CC(=O)C", "acetone", "1", {}, [])):
            result = CheckSIController(config()).run_analysis(["acetone (1)", description])
        compound = result["compounds"][0]
        self.assertEqual([item["represented_atoms"] for item in compound["carbon_observations"]], [None, 1])
        json.dumps(compound["carbon_observations"], allow_nan=False)
        request = _compound_request(config(), compound, 1)
        self.assertEqual(request.carbon_shifts, (207.1, 30.9))
        self.assertEqual(request.observation_atom_counts, (None, 1))
        compound["carbon_observations"] = list(reversed(compound["carbon_observations"]))
        with self.assertRaises(DP5PreparationSkip):
            _compound_request(config(), compound, 1)


if __name__ == "__main__":
    unittest.main()
