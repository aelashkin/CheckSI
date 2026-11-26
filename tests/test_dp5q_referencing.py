"""A 13C description displaced as a whole against the other compounds of the document."""

import unittest

from checksi.dp5q.referencing import find_offsets

USUAL = [0.8, -1.5, 2.1, 0.3, -0.9, 1.4, -2.2, 0.6]


def document(**changed):
    data = {index: [value + 0.2 * index for value in USUAL] for index in range(6)}
    data.update({int(key[1:]): value for key, value in changed.items()})
    return data


class ReferencingTests(unittest.TestCase):
    def test_ordinary_document_has_no_offsets(self):
        self.assertEqual(find_offsets(document()), {})

    def test_displaced_description_is_found_with_its_offset(self):
        found = find_offsets(document(c3=[value - 5.5 for value in USUAL]))
        self.assertEqual(list(found), [3])
        self.assertAlmostEqual(found[3].offset, -5.9, places=1)
        self.assertEqual(found[3].carbons, 8)

    def test_scattered_errors_are_not_an_offset(self):
        wrong = [12.0, -9.0, 15.0, 6.5, -3.0, 8.0, 20.0, 5.0]
        self.assertEqual(find_offsets(document(c3=wrong)), {})

    def test_too_few_compounds_or_carbons_give_nothing(self):
        few = {index: values for index, values in document(c3=[v - 6 for v in USUAL]).items() if index >= 2}
        self.assertEqual(find_offsets(few), {})
        self.assertEqual(find_offsets(document(c3=[-6.0, -5.5, -6.2])), {})


if __name__ == '__main__':
    unittest.main()
