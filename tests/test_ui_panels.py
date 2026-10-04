"""界面面板测试：规则页的纯函数逻辑 + 三个页签的离屏冒烟。

纯函数部分不需要 Qt；界面部分在 offscreen 平台下构建窗口，未装 PySide6 时跳过。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sharingan.analysis.summary import column_letter  # noqa: E402
from sharingan.fixtures import generate  # noqa: E402
from sharingan.rules import validate  # noqa: E402
from sharingan.ui import UI_AVAILABLE  # noqa: E402
from sharingan.ui import app as ui_app  # noqa: E402
from sharingan.ui.rules_panel import (  # noqa: E402
    NO_ACTION,
    format_params,
    merge_rows_into_rules,
    parse_params,
    rules_rows,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DEMO_RULES = EXAMPLES / "rules.fixture-demo.json"

# 停机场景用的规则：有阈值但没有任何修复规则 → 按 unmatched=halt 停机
HALTING_RULES = {
    "version": 1,
    "base_columns": [column_letter(index) for index in range(15)],  # A..O
    "columns": {
        "B": {"limit": 85, "source": "测试：齿轮箱输入轴轴温"},
        "I": {"limit": 80, "source": "测试：发电机轴承B温度"},
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


class RulesRowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = json.loads(DEMO_RULES.read_text(encoding="utf-8"))

    def test_rows_cover_columns_derived_and_repair(self):
        rows = {row["letter"]: row for row in rules_rows(self.rules)}
        self.assertIn("B", rows)
        self.assertIn("T", rows)  # 派生列
        self.assertEqual(rows["B"]["limit"], "85")
        self.assertEqual(rows["B"]["action"], "clip")
        self.assertEqual(rows["T"]["name"], "abs(H-I)")
        self.assertEqual(rows["T"]["action"], "reject")
        self.assertEqual(rows["K"]["action"], NO_ACTION) if "K" in rows else None

    def test_merge_updates_limit_and_bumps_version(self):
        rows = rules_rows(self.rules)
        for row in rows:
            if row["letter"] == "B":
                row["limit"] = "86.5"
        merged = merge_rows_into_rules(self.rules, rows, today="2026-10-05")
        self.assertEqual(merged["version"], self.rules["version"] + 1)
        self.assertEqual(merged["updated_at"], "2026-10-05")
        self.assertEqual(merged["columns"]["B"]["limit"], 86.5)
        self.assertTrue(validate(merged).ok, validate(merged).format())
        # 原对象不被修改
        self.assertEqual(self.rules["columns"]["B"]["limit"], 85)

    def test_merge_creates_and_removes_repair_entries(self):
        rows = rules_rows(self.rules)
        for row in rows:
            if row["letter"] == "B":
                row["action"] = NO_ACTION  # 取消修复规则
            if row["letter"] == "I":
                row["action"] = "clip"
                row["params"] = "to=84.9"
                row["mode"] = "confirm-once"
        merged = merge_rows_into_rules(self.rules, rows, today="2026-10-05")
        self.assertNotIn("B", merged["repair"])
        self.assertEqual(merged["repair"]["I"]["action"], "clip")
        self.assertEqual(merged["repair"]["I"]["params"], {"to": 84.9})
        self.assertIn("source", merged["repair"]["I"])
        self.assertTrue(validate(merged).ok, validate(merged).format())

    def test_merge_reject_drops_params(self):
        rows = rules_rows(self.rules)
        for row in rows:
            if row["letter"] == "B":
                row["action"] = "reject"
                row["params"] = "to=84.9"  # 残留参数应被清掉
        merged = merge_rows_into_rules(self.rules, rows, today="2026-10-05")
        self.assertNotIn("params", merged["repair"]["B"])
        self.assertTrue(validate(merged).ok, validate(merged).format())


class ParamsTextTest(unittest.TestCase):
    def test_parse_supported_params(self):
        self.assertEqual(parse_params("to=84.9"), {"to": 84.9})
        self.assertEqual(parse_params("target=i; to=55"), {"target": "I", "to": 55.0})
        self.assertEqual(parse_params("note=超限；to=1"), {"note": "超限", "to": 1.0})
        self.assertEqual(parse_params(""), {})

    def test_parse_rejects_unknown_or_malformed(self):
        for text in ("87", "limits=1", "target=AB1", "to="):
            with self.assertRaises(ValueError, msg=text):
                parse_params(text)

    def test_format_roundtrip(self):
        params = {"target": "I", "to": 55.0}
        self.assertEqual(parse_params(format_params(params)), params)

    def test_format_empty(self):
        self.assertEqual(format_params(None), "")
        self.assertEqual(format_params({}), "")

    def test_merge_clearing_limit_drops_empty_entry(self):
        rules = {
            "version": 1,
            "base_columns": ["A", "B"],
            "columns": {"B": {"limit": 85, "source": "测试"}},
            "unmatched": {"mode": "halt"},
        }
        rows = [{"letter": "B", "limit": "", "action": NO_ACTION, "mode": ""}]
        merged = merge_rows_into_rules(rules, rows, today="2026-10-05")
        self.assertNotIn("B", merged.get("columns", {}))
        self.assertTrue(validate(merged).ok, validate(merged).format())


@unittest.skipUnless(UI_AVAILABLE, "未安装 PySide6")
class PanelsSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.result = generate()
        self.csv_path, _ = self.result.write(self.directory)

    def test_window_has_three_tabs(self):
        window = ui_app.MainWindow()
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        self.assertEqual(titles, ["数据概况", "规则", "变更清单"])

    def test_rules_panel_loads_and_shows_rows(self):
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path, DEMO_RULES)
        self.assertIsNotNone(window.rules_panel)
        self.assertGreater(window.rules_panel.table.rowCount(), 0)
        letters = [
            window.rules_panel.table.item(row, 0).text()
            for row in range(window.rules_panel.table.rowCount())
        ]
        self.assertIn("B", letters)
        self.assertIn("T", letters)

    def test_changes_panel_plans_and_allows_apply(self):
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path, DEMO_RULES)
        panel = window.changes_panel
        self.assertIsNotNone(panel)
        self.assertTrue(panel.plan_button.isEnabled())
        panel.refresh()
        self.assertEqual(panel.plan.action_count, 2)
        self.assertFalse(panel.plan.halted)
        self.assertTrue(panel.apply_button.isEnabled())
        self.assertEqual(panel.table.rowCount(), 2)
        self.assertIn("2 处修改", panel.summary.text())

    def test_changes_panel_disables_apply_when_halted(self):
        halting_path = self.directory / "halting.json"
        halting_path.write_text(
            json.dumps(HALTING_RULES, ensure_ascii=False), encoding="utf-8"
        )
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path, halting_path)
        panel = window.changes_panel
        panel.refresh()
        self.assertTrue(panel.plan.halted)
        self.assertFalse(panel.apply_button.isEnabled())
        self.assertIn("已停机", panel.summary.text())
        self.assertIn("停机", panel.details.toPlainText())

    def test_generate_plan_switches_tab(self):
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path, DEMO_RULES)
        window.generate_plan()
        self.assertIs(window.tabs.currentWidget(), window.changes_panel)
        self.assertEqual(window.changes_panel.plan.action_count, 2)


if __name__ == "__main__":
    unittest.main()
