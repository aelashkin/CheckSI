"""Common multiplicity and coupling-constant notations are read as valid descriptors."""

import unittest

from checksi.analysis.nmr import _check_constant_hnmr, _check_constant_nmr
from checksi.utils.error_handler import ErrorTracker
from tests.test_name_resolver import make_config


def active_counts(tracker):
    return {code: count for code, count in tracker.to_dict().items() if count}


class ProtonMultiplicityNotationTests(unittest.TestCase):
    def assert_clean(self, descriptor):
        tracker = ErrorTracker()
        findings = _check_constant_hnmr(make_config(), tracker, f"δ 3.06 ({descriptor})")
        self.assertEqual(active_counts(tracker), {}, descriptor)
        self.assertEqual(findings, [], descriptor)

    def test_long_multiplicity_abbreviations(self):
        for descriptor in ("quint, J = 6.0 Hz, 1H", "quin, J = 6.6 Hz, 1H",
                           "sext, J = 7.0 Hz, 1H", "sept, J = 6.9 Hz, 1H"):
            with self.subTest(descriptor=descriptor):
                self.assert_clean(descriptor)

    def test_apparent_prefixes(self):
        for descriptor in ("app dd, J = 13.4, 7.1 Hz, 1H", "app. t, J = 7.5 Hz, 2H",
                           "apparent d, J = 8.0 Hz, 2H", "pseudo-t, J = 7.5 Hz, 2H",
                           "ps t, J = 7.5 Hz, 2H", "app s, 1H"):
            with self.subTest(descriptor=descriptor):
                self.assert_clean(descriptor)

    def test_coupling_constants_with_nucleus_labels(self):
        for descriptor in ("q, 3JH-H = 6.7 Hz, 1H", "d, 2JH-F = 47.5 Hz, 2H",
                           "d, JHF = 8.6 Hz, 1H", "dd, J(H,F) = 8.6, 5.4 Hz, 2H",
                           "d, 3J = 5.3 Hz, 3H", "t, J_{HF} = 47.0 Hz, 1H"):
            with self.subTest(descriptor=descriptor):
                self.assert_clean(descriptor)

    def test_apparent_prefix_alone_is_not_a_multiplicity(self):
        tracker = ErrorTracker()
        _check_constant_hnmr(make_config(), tracker, "δ 3.06 (app, 1H)")
        self.assertEqual(tracker.get_count("hnmr_wrong_multiplicity"), 1)

    def test_multiplicity_without_constant_is_still_reported(self):
        for descriptor in ("quint, 1H", "app dd, 1H", "d, JHF, 1H"):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.06 ({descriptor})")
                self.assertEqual(tracker.get_count("hnmr_j_missing"), 1)


class CarbonCouplingNotationTests(unittest.TestCase):
    def test_carbon_heteronuclear_coupling_labels(self):
        section = ("δ 164.1 (d, 1JC-F = 258.4 Hz), 134.2 (d, 3JC-F = 9.2 Hz), "
                   "146.3 (d, JC-P = 4.0 Hz), 121.7 (q, JCF = 272 Hz), 125.5 (q, J = 3.9 Hz)")
        tracker = ErrorTracker()
        findings, coupled = _check_constant_nmr(make_config(), tracker, section, "13C")
        self.assertEqual((findings, active_counts(tracker)), ([], {}))
        self.assertEqual(coupled, 1)

    def test_carbon_multiplicity_without_constant_is_still_reported(self):
        tracker = ErrorTracker()
        _check_constant_nmr(make_config(), tracker, "δ 164.1 (d, JC-F Hz)", "13C")
        self.assertEqual(tracker.get_count("nmr_j_missing"), 1)


if __name__ == "__main__":
    unittest.main()
