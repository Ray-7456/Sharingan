"""图形界面冒烟测试：离屏（offscreen）构建窗口并校验渲染结果。

不需要真实显示器：Qt 的 offscreen 平台插件在本机可用，因此界面代码
也能进 CI。未安装 PySide6 时整个测试类跳过。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sharingan.analysis import build_summary, column_letter  # noqa: E402
from sharingan.fixtures import generate  # noqa: E402
from sharingan.ui import UI_AVAILABLE  # noqa: E402
from sharingan.ui import app as ui_app  # noqa: E402

RULES = {
    "version": 1,
    "base_columns": [column_letter(index) for index in range(15)],  # A..O
    "columns": {"B": {"limit": 85, "source": "测试"}},
    "derived": {"T": {"formula": "abs(H-I)", "limit": 20, "source": "测试"}},
    "unmatched": {"mode": "halt"},
}


@unittest.skipUnless(UI_AVAILABLE, "未安装 PySide6")
class UiSmokeTest(unittest.TestCase):
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
        self.rules_path = self.directory / "rules.json"
        self.rules_path.write_text(json.dumps(RULES, ensure_ascii=False), encoding="utf-8")

    def test_font_picker_returns_string(self):
        family = ui_app.pick_ui_font()
        self.assertIsInstance(family, str)

    def test_render_facts_html_is_pure_and_complete(self):
        summary = build_summary(self.csv_path, rules_path=self.rules_path)
        text = ui_app.render_facts_html(summary)
        for expected in ("文件概况", "924", "备注统计", "时间断档", "规则校验", "合计超限"):
            self.assertIn(expected, text, expected)

    def test_populate_fills_table_without_rules(self):
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path)
        self.assertEqual(window.table.columnCount(), 7)
        self.assertEqual(window.table.rowCount(), 14)
        self.assertEqual(window.table.item(0, 0).text(), "B")
        self.assertIn("synthetic_week.csv", window.windowTitle())
        self.assertIn("924 行", window.statusBar().currentMessage())

    def test_populate_adds_derived_row_and_marks_breach(self):
        window = ui_app.MainWindow()
        ui_app.populate(window, self.csv_path, self.rules_path)
        self.assertEqual(window.table.rowCount(), 15)
        letters = [
            window.table.item(row, 0).text() for row in range(window.table.rowCount())
        ]
        self.assertIn("T", letters)

        breach_rows = [
            row
            for row in range(window.table.rowCount())
            if window.table.item(row, 6).text() not in ("", "0")
        ]
        self.assertTrue(breach_rows)
        for row in breach_rows:
            color = window.table.item(row, 6).foreground().color().name()
            self.assertEqual(color, "#c62828")

    def test_window_title_and_menu_actions_exist(self):
        window = ui_app.MainWindow()
        self.assertIn("Sharingan", window.windowTitle())
        menu_titles = [action.text() for action in window.menuBar().actions()]
        self.assertTrue(any("文件" in title for title in menu_titles))
        self.assertTrue(any("帮助" in title for title in menu_titles))


if __name__ == "__main__":
    unittest.main()
