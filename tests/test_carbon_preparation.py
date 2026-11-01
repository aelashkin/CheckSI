"""Carbon preparation reuses one count/capacity decision across its stages."""

import unittest
from unittest.mock import patch

from checksi.dp5q.observation_assignment import validate_observation_coverage
from tests.test_carbon_observations import carbon


class CarbonPreparationTests(unittest.TestCase):
    def test_count_and_annotation_stages_share_one_coverage_check(self):
        for shifts, eligible in (('60.0, 20.0', True), ('60.0', False)):
            with self.subTest(shifts=shifts), patch(
                    'checksi.analysis.nmr.validate_observation_coverage',
                    wraps=validate_observation_coverage) as validate:
                checked, _, _ = carbon('CCO', 'C2H6O', shifts)
            self.assertEqual(checked[6], eligible)
            self.assertEqual(validate.call_count, 1)


if __name__ == '__main__':
    unittest.main()
