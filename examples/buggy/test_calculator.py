"""The same regression expectations are applied to buggy and fixed versions."""

import unittest

from calculator import add_tag, average, load_score, parse_score


class CalculatorTests(unittest.TestCase):
    def test_average_numbers(self):
        self.assertEqual(average([2, 4, 6]), 4.0)

    def test_empty_average(self):
        self.assertEqual(average([]), 0.0)

    def test_independent_default_lists(self):
        first = add_tag("first")
        second = add_tag("second")
        self.assertEqual(first, ["first"])
        self.assertEqual(second, ["second"])
        self.assertIsNot(first, second)

    def test_numeric_json(self):
        self.assertEqual(parse_score("12.5"), 12.5)

    def test_reject_expression(self):
        with self.assertRaises(ValueError):
            parse_score("1 + 2")

    def test_invalid_integer(self):
        with self.assertRaises(ValueError):
            load_score("invalid")


if __name__ == "__main__":
    unittest.main()
