"""公式求值测试：语法正确性、缺失值传播、以及拒绝执行任意代码。"""

import unittest

from sharingan.rules import FormulaError, evaluate, referenced
from sharingan.rules.formula import check


class FormulaTest(unittest.TestCase):
    def test_abs_difference(self):
        self.assertAlmostEqual(evaluate("abs(H-I)", {"H": 45.3, "I": 74.6}), 29.3, places=6)

    def test_template_style_formula_with_rows_and_dollars(self):
        # 模板真实写法：=(ABS(O4-J4))-4.1000
        value = evaluate("=(ABS(O4-J4))-4.1000", {"O": 51.5, "J": 24.0})
        self.assertAlmostEqual(value, 23.4, places=6)

    def test_precedence_and_parens(self):
        self.assertEqual(evaluate("2+3*4", {}), 14)
        self.assertEqual(evaluate("(2+3)*4", {}), 20)
        self.assertEqual(evaluate("- -3", {}), 3)
        self.assertEqual(evaluate("3*-2", {}), -6)

    def test_missing_value_propagates(self):
        self.assertIsNone(evaluate("abs(H-I)", {"H": None, "I": 74.6}))
        self.assertIsNone(evaluate("abs(H-I)", {}))

    def test_referenced_columns(self):
        self.assertEqual(referenced("abs(H-I)+M"), frozenset({"H", "I", "M"}))
        self.assertEqual(referenced("O4"), frozenset({"O"}))
        self.assertEqual(referenced("85"), frozenset())

    def test_division_by_zero_raises(self):
        with self.assertRaises(FormulaError):
            evaluate("H/0", {"H": 1.0})

    def test_rejects_range_reference(self):
        with self.assertRaises(FormulaError):
            check("MAX(B4:B1029)")

    def test_rejects_unknown_function(self):
        with self.assertRaises(FormulaError):
            check("sqrt(H)")

    def test_rejects_code_like_input(self):
        for source in (
            "__import__('os')",
            "H.__class__",
            "open('x')",
            "eval(1)",
            "lambda: 1",
            "abs(H); H",
        ):
            with self.assertRaises(FormulaError, msg=source):
                check(source)

    def test_rejects_incomplete_expression(self):
        for source in ("H I", "H+", "(H", "abs H", ""):
            with self.assertRaises(FormulaError, msg=source):
                check(source)


if __name__ == "__main__":
    unittest.main()
