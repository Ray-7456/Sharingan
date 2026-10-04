"""rules.json 校验器测试：source 纪律、mode 取值、覆盖确认、未知键告警。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from sharingan.rules import load_rules, validate

VALID = {
    "version": 3,
    "updated_at": "2026-07-24",
    "base_columns": list("ABCDEFGHIJKLMNO"),
    "columns": {
        "B": {"limit": 85, "source": "模板表头(85) 与条件格式一致"},
        "D": {"limit": 75, "source": "模板表头(75)"},
        "N": {"limit": 55, "source": "模板表头(55)"},
        "O": {"limit": 60, "source": "模板表头(60)"},
    },
    "derived": {
        "T": {"formula": "abs(H-I)", "limit": 20, "source": "澄清#3：以表头 20 为准"},
    },
    "repair": {
        "T": {
            "action": "clip",
            "params": {"target": "I", "to": 19.9},
            "mode": "confirm-once",
            "source": "首次运行人工修正归纳",
        }
    },
    "missing_data": {
        "备注=此时间点无数据": {"action": "keep_and_flag", "mode": "auto", "source": "澄清#4"}
    },
    "writeback": {
        "range": "A:O",
        "encoding": "gbk",
        "preserve_columns": ["备注"],
        "backup": True,
        "overwrite": {"mode": "attended"},
    },
    "unmatched": {"mode": "halt"},
}


def mutate(**changes):
    data = copy.deepcopy(VALID)
    for key, value in changes.items():
        data[key] = value
    return data


class ValidRulesTest(unittest.TestCase):
    def test_valid_rules_pass_without_warnings(self):
        result = validate(VALID)
        self.assertTrue(result.ok, result.format())
        self.assertEqual(result.warnings, [], result.format())


class SourceDisciplineTest(unittest.TestCase):
    def test_missing_source_is_error_in_production(self):
        data = copy.deepcopy(VALID)
        del data["columns"]["B"]["source"]
        result = validate(data, production=True)
        self.assertFalse(result.ok)
        self.assertTrue(any("source" in issue.path for issue in result.errors))

    def test_missing_source_is_warning_in_draft(self):
        data = copy.deepcopy(VALID)
        del data["columns"]["B"]["source"]
        result = validate(data, production=False)
        self.assertTrue(result.ok)
        self.assertTrue(any("source" in issue.path for issue in result.warnings))

    def test_blank_source_rejected(self):
        data = copy.deepcopy(VALID)
        data["derived"]["T"]["source"] = "   "
        self.assertFalse(validate(data).ok)


class ModeTest(unittest.TestCase):
    def test_invalid_mode_rejected(self):
        data = copy.deepcopy(VALID)
        data["repair"]["T"]["mode"] = "ask-later"
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("mode" in issue.path for issue in result.errors))

    def test_missing_mode_rejected(self):
        data = copy.deepcopy(VALID)
        del data["missing_data"]["备注=此时间点无数据"]["mode"]
        self.assertFalse(validate(data).ok)


class WritebackTest(unittest.TestCase):
    def test_overwrite_must_be_attended(self):
        data = copy.deepcopy(VALID)
        data["writeback"]["overwrite"]["mode"] = "auto"
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("overwrite" in issue.path for issue in result.errors))

    def test_reversed_range_rejected(self):
        data = copy.deepcopy(VALID)
        data["writeback"]["range"] = "O:A"
        self.assertFalse(validate(data).ok)

    def test_unknown_encoding_rejected(self):
        data = copy.deepcopy(VALID)
        data["writeback"]["encoding"] = "cp936"
        self.assertFalse(validate(data).ok)

    def test_unknown_key_warns(self):
        data = copy.deepcopy(VALID)
        data["columns"]["B"]["limits"] = 85
        result = validate(data)
        self.assertTrue(result.ok)
        self.assertTrue(any("limits" in issue.path for issue in result.warnings))


class UnmatchedTest(unittest.TestCase):
    def test_missing_unmatched_rejected(self):
        data = copy.deepcopy(VALID)
        del data["unmatched"]
        self.assertFalse(validate(data).ok)

    def test_non_halt_unmatched_rejected(self):
        for mode in ("skip", "guess", "auto"):
            data = copy.deepcopy(VALID)
            data["unmatched"] = {"mode": mode}
            self.assertFalse(validate(data).ok, mode)


class DerivedTest(unittest.TestCase):
    def test_undeclared_reference_rejected(self):
        data = copy.deepcopy(VALID)
        data["derived"]["X"] = {"formula": "abs(R-S)", "source": "测试"}
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("未声明的列" in issue.message for issue in result.errors))

    def test_derived_may_reference_derived(self):
        data = copy.deepcopy(VALID)
        data["derived"]["P"] = {"formula": "abs(B-D)", "source": "测试"}
        data["derived"]["Q"] = {"formula": "abs(P)", "source": "测试"}
        self.assertTrue(validate(data).ok, validate(data).format())

    def test_cycle_detected(self):
        data = copy.deepcopy(VALID)
        data["derived"]["P"] = {"formula": "abs(Q)", "source": "测试"}
        data["derived"]["Q"] = {"formula": "abs(P)", "source": "测试"}
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("循环引用" in issue.message for issue in result.errors))

    def test_bad_formula_rejected(self):
        data = copy.deepcopy(VALID)
        data["derived"]["T"]["formula"] = "MAX(H:I)"
        result = validate(data)
        self.assertFalse(result.ok)

    def test_bad_limit_rejected(self):
        data = copy.deepcopy(VALID)
        data["columns"]["B"]["limit"] = "85"
        self.assertFalse(validate(data).ok)


class RepairSectionTest(unittest.TestCase):
    """repair 段：以被修改列字母为键，动作白名单，参数齐全性。"""

    def test_non_column_key_rejected(self):
        data = copy.deepcopy(VALID)
        data["repair"] = {"T_overflow": {"action": "reject", "source": "测试"}}
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("repair 的键" in issue.message for issue in result.errors))

    def test_unknown_action_rejected(self):
        data = copy.deepcopy(VALID)
        data["repair"] = {"T": {"action": "auto_fix", "mode": "auto", "source": "测试"}}
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("action" in issue.path for issue in result.errors))

    def test_clip_requires_numeric_to(self):
        data = copy.deepcopy(VALID)
        data["repair"] = {"T": {"action": "clip", "mode": "auto", "source": "测试"}}
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("params.to" in issue.path for issue in result.errors))

    def test_target_must_be_declared_base_column(self):
        data = copy.deepcopy(VALID)
        data["repair"] = {
            "T": {
                "action": "clip",
                "params": {"target": "Z", "to": 1},
                "mode": "auto",
                "source": "测试",
            }
        }
        result = validate(data)
        self.assertFalse(result.ok)
        self.assertTrue(any("params.target" in issue.path for issue in result.errors))

    def test_reject_needs_no_mode(self):
        data = copy.deepcopy(VALID)
        data["repair"] = {"T": {"action": "reject", "source": "测试：本列只报警不改"}}
        result = validate(data)
        self.assertTrue(result.ok, result.format())

    def test_valid_actions_accepted(self):
        for action, params in (
            ("clip", {"to": 1}),
            ("interpolate", {}),
            ("keep_and_flag", {"note": "超限"}),
        ):
            data = copy.deepcopy(VALID)
            data["repair"] = {
                "T": {"action": action, "params": params, "mode": "confirm-once", "source": "测试"}
            }
            result = validate(data)
            self.assertTrue(result.ok, f"{action}: {result.format()}")


class VersionTest(unittest.TestCase):
    def test_version_required_and_positive_integer(self):
        self.assertFalse(validate({}).ok)
        self.assertFalse(validate(mutate(version=0)).ok)
        self.assertFalse(validate(mutate(version="3")).ok)
        self.assertFalse(validate(mutate(version=True)).ok)


class LoadRulesTest(unittest.TestCase):
    def test_load_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
            data, result = load_rules(path)
            self.assertIsNotNone(data)
            self.assertTrue(result.ok, result.format())

    def test_malformed_json_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text("{ not json", encoding="utf-8")
            data, result = load_rules(path)
            self.assertIsNone(data)
            self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
