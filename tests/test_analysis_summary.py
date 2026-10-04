"""分析层测试：统计口径、规则联动、派生列求值。"""

import json
import tempfile
import unittest
from pathlib import Path

from sharingan.analysis import build_summary, column_letter, data_column_letter, row_values
from sharingan.fixtures import generate

BASE_RULES = {
    "version": 1,
    "base_columns": [column_letter(index) for index in range(15)],  # A..O
    "columns": {"B": {"limit": 85, "source": "测试"}},
    "derived": {"T": {"formula": "abs(H-I)", "limit": 20, "source": "测试"}},
    "unmatched": {"mode": "halt"},
}


class LetterHelperTest(unittest.TestCase):
    def test_column_letters(self):
        self.assertEqual(column_letter(0), "A")
        self.assertEqual(column_letter(25), "Z")
        self.assertEqual(column_letter(26), "AA")
        self.assertEqual(data_column_letter(0), "B")

    def test_row_values_mapping(self):
        result = generate()
        row = result.data_file.rows[0]
        values = row_values(row, result.data_file.columns)
        self.assertIsNone(values["A"])
        self.assertEqual(values["B"], row.value(0))
        expected_keys = {"A"} | {
            data_column_letter(index) for index in range(len(result.data_file.columns))
        }
        self.assertEqual(set(values), expected_keys)

    def test_generate_rejects_short_window(self):
        with self.assertRaises(ValueError):
            generate(days=1)


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.result = generate()
        self.csv_path, _ = self.result.write(self.directory)

    def _write_rules(self, data: dict, name: str = "rules.json") -> Path:
        path = self.directory / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_without_rules(self):
        summary = build_summary(self.csv_path)
        self.assertEqual(summary.facts.row_count, 924)
        self.assertEqual(summary.facts.encoding, "gbk")
        self.assertEqual(summary.facts.unnamed_trailing_columns, 1)
        self.assertEqual(summary.facts.parse_failures, 0)
        self.assertEqual(len(summary.columns), 14)
        self.assertTrue(all(stat.limit is None for stat in summary.columns))
        self.assertEqual(summary.duplicate_rows, 5)
        self.assertEqual(len(summary.gaps), 1)
        self.assertAlmostEqual(summary.gaps[0].minutes, 850.0)
        self.assertEqual(len(summary.notes), 1)
        self.assertEqual(summary.notes[0].count, 5)

    def test_with_rules_adds_limits_and_derived(self):
        rules_path = self._write_rules(BASE_RULES)
        summary = build_summary(self.csv_path, rules_path=rules_path)
        self.assertTrue(summary.rule_ok, summary.rule_errors)
        self.assertEqual(len(summary.columns), 15)  # 14 原始 + 1 派生

        column_b = next(stat for stat in summary.columns if stat.letter == "B")
        self.assertEqual(column_b.limit, 85)
        self.assertEqual(column_b.breaches, 1)

        derived = next(stat for stat in summary.columns if stat.derived)
        self.assertEqual(derived.letter, "T")
        self.assertEqual(derived.limit, 20)
        self.assertEqual(derived.breaches, 13)  # 12 行注入 + 1 处毛刺波及
        self.assertEqual(summary.breach_total, 14)

    def test_rules_induce_no_false_breach_on_other_columns(self):
        summary = build_summary(self.csv_path, rules_path=self._write_rules(BASE_RULES))
        others = [s for s in summary.columns if s.letter not in {"B", "T"}]
        self.assertTrue(all(stat.breaches == 0 for stat in others))

    def test_bad_formula_reports_error_and_skips_stat(self):
        data = json.loads(json.dumps(BASE_RULES))
        data["derived"]["T"]["formula"] = "MAX(H:I)"
        summary = build_summary(self.csv_path, rules_path=self._write_rules(data))
        self.assertFalse(summary.rule_ok)
        self.assertTrue(any("formula" in error for error in summary.rule_errors))
        self.assertFalse(any(stat.derived for stat in summary.columns))

    def test_missing_rules_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            build_summary(self.csv_path, rules_path=self.directory / "nope.json")

    def test_malformed_rules_marks_not_loaded_but_keeps_base_stats(self):
        broken = self.directory / "broken.json"
        broken.write_text("{ bad json", encoding="utf-8")
        summary = build_summary(self.csv_path, rules_path=broken)
        self.assertFalse(summary.rule_loaded)
        self.assertFalse(summary.rule_ok)
        self.assertEqual(len(summary.columns), 14)
        self.assertEqual(summary.breach_total, 0)


if __name__ == "__main__":
    unittest.main()
