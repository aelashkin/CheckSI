"""Adjacent charge parsing must not allocate the unrelated paragraph tail."""

import tracemalloc
import unittest

from checksi.analysis.hrms_fields import _charge_suffix


class ChargeSuffixCostTests(unittest.TestCase):
    def test_small_charge_has_constant_working_memory_with_a_large_tail(self):
        text = ' 2+ ' + 'x' * (2 * 1024 * 1024)
        tracemalloc.start()
        try:
            result = _charge_suffix(text, 1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result, ('2+', 1, 3, True))
        self.assertLess(peak, 64 * 1024)
