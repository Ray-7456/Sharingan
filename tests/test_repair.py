"""修复引擎测试：变更计划、停机纪律、备份与写盘正确性。"""

import json
import tempfile
import unittest
from pathlib import Path

from sharingan.analysis import apply_repairs, plan_repairs
from sharingan.analysis.summary import column_letter
from sharingan.fixtures import generate
from sharingan.formats import TimeSeriesFile
from sharingan.rules import validate

PRESERVE_NOTE = ["备注"]


def make_rules(
    repair: dict,
    *,
    derived: dict | None = None,
    preserve_columns: list[str] | None = None,
) -> dict:
    rules = {
        "version": 1,
        "base_columns": [column_letter(index) for index in range(15)],  # A..O
        "columns": {
            "B": {"limit": 85, "source": "测试：齿轮箱输入轴轴温"},
            "I": {"limit": 80, "source": "测试：发电机轴承B温度"},
        },
        "repair": repair,
        "writeback": {
            "range": "A:O",
            "encoding": "gbk",
            "preserve_columns": PRESERVE_NOTE if preserve_columns is None else preserve_columns,
            "backup": True,
            "overwrite": {"mode": "attended"},
        },
        "unmatched": {"mode": "halt"},
    }
    if derived is not None:
        rules["derived"] = derived
    return rules


CLIP_B = {"B": {"action": "clip", "params": {"to": 84.9}, "mode": "confirm-once", "source": "测试"}}
REJECT_I = {"I": {"action": "reject", "source": "测试：本列不改，人工判断"}}
DERIVED_T = {"T": {"formula": "abs(H-I)", "limit": 20, "source": "测试：待澄清#1"}}


class PlanRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.result = generate()
        self.csv_path, _ = self.result.write(self.directory)
        self.data_file = TimeSeriesFile.read(self.csv_path)

    def test_clip_base_column(self):
        # 夹具里 B 列有一个 88.5 的毛刺，阈值 85
        plan = plan_repairs(self.data_file, make_rules(dict(CLIP_B, **REJECT_I)))
        self.assertFalse(plan.halted, plan.halt_reasons)
        self.assertEqual(plan.action_count, 1)
        action = plan.actions[0]
        self.assertEqual(action.column_letter, "B")
        self.assertEqual(action.before, "88.5")
        self.assertEqual(action.after, "84.9")
        self.assertEqual(action.rule_key, "B")
        self.assertIn("截断", action.reason)

    def test_blocks_when_uncovered(self):
        # 只声明阈值不给修复规则 → 按 unmatched=halt 停机，不静默跳过
        plan = plan_repairs(self.data_file, make_rules({}))
        self.assertTrue(plan.halted)
        self.assertTrue(any("B 列" in reason for reason in plan.halt_reasons))
        self.assertTrue(any("I 列" in reason for reason in plan.halt_reasons))
        self.assertEqual(plan.action_count, 0)

    def test_reject_is_not_treated_as_missing(self):
        plan = plan_repairs(self.data_file, make_rules(dict(CLIP_B, **REJECT_I)))
        self.assertFalse(plan.halted, plan.halt_reasons)
        self.assertTrue(any("I 列" in item and "人工判断" in item for item in plan.skipped))

    def test_interpolate_single_spike(self):
        rules = make_rules(
            {
                "B": {
                    "action": "interpolate",
                    "mode": "confirm-once",
                    "source": "测试：单点毛刺按前后点插值",
                },
                **REJECT_I,
            }
        )
        plan = plan_repairs(self.data_file, rules)
        self.assertEqual(plan.action_count, 1)
        action = plan.actions[0]
        self.assertIsNotNone(action.value_after)
        self.assertLess(action.value_after, 85.0)
        # 插值结果应落在前后两点之间
        index = action.row_index
        before_point = self.data_file.rows[index - 1].value(0)
        after_point = self.data_file.rows[index + 1].value(0)
        low, high = sorted((before_point, after_point))
        self.assertGreaterEqual(action.value_after, low - 0.05)
        self.assertLessEqual(action.value_after, high + 0.05)

    def test_keep_and_flag_respects_preserve_columns(self):
        rules = make_rules(
            {
                "B": {
                    "action": "keep_and_flag",
                    "params": {"note": "超限"},
                    "mode": "auto",
                    "source": "测试",
                },
                **REJECT_I,
            }
        )
        plan = plan_repairs(self.data_file, rules)  # 默认 preserve_columns=["备注"]
        self.assertEqual(plan.action_count, 0)
        self.assertTrue(any("保留列" in item for item in plan.skipped))

    def test_keep_and_flag_writes_note_when_allowed(self):
        rules = make_rules(
            {
                "B": {
                    "action": "keep_and_flag",
                    "params": {"note": "超限"},
                    "mode": "auto",
                    "source": "测试",
                },
                **REJECT_I,
            },
            preserve_columns=[],
        )
        plan = plan_repairs(self.data_file, rules)
        self.assertEqual(plan.action_count, 1)
        action = plan.actions[0]
        self.assertEqual(action.field_index, -1)  # 备注列
        self.assertEqual(action.after, "超限")
        self.assertEqual(action.value_after, None)

    def test_derived_without_target_needs_human(self):
        rules = make_rules(
            {
                **CLIP_B,
                **REJECT_I,
                "T": {
                    "action": "clip",
                    "params": {"to": 19.9},
                    "mode": "confirm-once",
                    "source": "测试：未指定 target",
                },
            },
            derived=DERIVED_T,
        )
        plan = plan_repairs(self.data_file, rules)
        self.assertFalse(plan.halted, plan.halt_reasons)
        self.assertTrue(any("params.target" in item for item in plan.skipped))

    def test_derived_with_target_clips_base_column(self):
        rules = make_rules(
            {
                **CLIP_B,
                **REJECT_I,
                "T": {
                    "action": "clip",
                    "params": {"target": "I", "to": 55},
                    "mode": "confirm-once",
                    "source": "测试：T 超限时把 I 截断到 55",
                },
            },
            derived=DERIVED_T,
        )
        plan = plan_repairs(self.data_file, rules)
        self.assertFalse(plan.halted, plan.halt_reasons)
        clipped = [action for action in plan.actions if action.column_letter == "I" and action.after == "55"]
        self.assertGreaterEqual(len(clipped), 10)  # 夹具注入了 12 行派生列超限
        self.assertTrue(all(action.rule_key == "T" for action in clipped))
        # 毛刺行（I=84.2）也应被截断
        spike = [a for a in clipped if a.before == "84.2"]
        self.assertEqual(len(spike), 1)

    def test_rules_used_in_tests_pass_validator(self):
        variants = [
            make_rules(dict(CLIP_B, **REJECT_I)),
            make_rules(dict(CLIP_B, **REJECT_I), derived=DERIVED_T),
            make_rules({**CLIP_B, **REJECT_I, "T": {"action": "clip", "params": {"target": "I", "to": 55},
                                                    "mode": "confirm-once", "source": "测试"}},
                       derived=DERIVED_T),
        ]
        for rules in variants:
            result = validate(rules)
            self.assertTrue(result.ok, result.format())


class ApplyRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.result = generate()
        self.csv_path, _ = self.result.write(self.directory)
        self.original_bytes = self.csv_path.read_bytes()

    def _plan(self, rules: dict):
        return plan_repairs(TimeSeriesFile.read(self.csv_path), rules)

    def test_refuses_to_write_when_halted(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        plan = self._plan(make_rules({}))
        self.assertTrue(plan.halted)
        with self.assertRaises(RuntimeError):
            apply_repairs(data_file, plan, self.csv_path)
        self.assertEqual(self.csv_path.read_bytes(), self.original_bytes)

    def test_writes_backup_and_change_log(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        plan = self._plan(make_rules(dict(CLIP_B, **REJECT_I)))
        result = apply_repairs(data_file, plan, self.csv_path)

        self.assertEqual(result.change_count, 1)
        backup = Path(result.backup_path)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), self.original_bytes)  # 备份是原文件

        log = json.loads(Path(result.log_path).read_text(encoding="utf-8"))
        self.assertEqual(log["change_count"], 1)
        entry = log["changes"][0]
        self.assertEqual(entry["column"], "B")
        self.assertEqual(entry["before"], "88.5")
        self.assertEqual(entry["after"], "84.9")
        self.assertIn("rule", entry)
        self.assertIn("rule_source", entry)

    def test_changes_only_target_cells(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        plan = self._plan(make_rules(dict(CLIP_B, **REJECT_I)))
        target_row = plan.actions[0].row_index
        apply_repairs(data_file, plan, self.csv_path)

        old_lines = self.original_bytes.decode("gbk").split("\r\n")
        new_lines = self.csv_path.read_bytes().decode("gbk").split("\r\n")
        self.assertEqual(len(old_lines), len(new_lines))
        differing = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(differing, [target_row + 1])  # +1 是表头

    def test_apply_result_is_readable_and_consistent(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        plan = self._plan(make_rules(dict(CLIP_B, **REJECT_I)))
        apply_repairs(data_file, plan, self.csv_path)

        reloaded = TimeSeriesFile.read(self.csv_path)
        index = self.data_file_index = 0
        values = [row.value(index) for row in reloaded.rows]
        self.assertNotIn(88.5, values)
        self.assertEqual(max(values), 84.9)
        # 重读后不应再有任何"未保存的修改"记录
        self.assertEqual(reloaded.changed_cells(), [])


if __name__ == "__main__":
    unittest.main()
