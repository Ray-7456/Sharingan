"""变更清单页：先算后写。

- 「生成变更清单」调用 :func:`sharingan.analysis.plan_repairs`——**只读**，不改文件；
- 清单逐条显示时间、列、原值、新值、依据规则与规则来源，便于人工复核；
- 「执行写入」会再确认一次，写盘前自动备份原文件，并落一份变更日志。

变更计划若因存在未覆盖情况而停机（``unmatched: halt``），写入按钮保持禁用——
这是"不猜"纪律在界面上的体现。
"""

from __future__ import annotations

from pathlib import Path

from ..analysis import apply_repairs, plan_repairs
from ..formats import TimeSeriesFile

try:
    from PySide6.QtWidgets import (
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )

    CHANGES_UI_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    CHANGES_UI_AVAILABLE = False

HEADERS = ("时间", "列", "原值", "新值", "依据规则", "说明")
HALT_COLOR = "#c62828"


if CHANGES_UI_AVAILABLE:

    class ChangesPanel(QWidget):  # type: ignore[misc]
        """变更清单与执行写入。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.data_file: TimeSeriesFile | None = None
            self.rules_data: dict | None = None
            self.data_path: str | None = None
            self.plan = None
            self.on_applied = None

            toolbar = QHBoxLayout()
            self.plan_button = QPushButton("生成变更清单")
            self.plan_button.clicked.connect(self.refresh)
            self.apply_button = QPushButton("执行写入…")
            self.apply_button.clicked.connect(self.apply)
            self.apply_button.setEnabled(False)
            toolbar.addWidget(self.plan_button)
            toolbar.addWidget(self.apply_button)
            toolbar.addStretch(1)

            self.summary = QLabel("尚未生成变更清单。")
            self.summary.setWordWrap(True)

            self.table = QTableWidget(0, len(HEADERS))
            self.table.setHorizontalHeaderLabels(list(HEADERS))
            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

            self.details = QTextBrowser()
            self.details.setMaximumHeight(140)

            layout = QVBoxLayout(self)
            layout.addLayout(toolbar)
            layout.addWidget(self.summary)
            layout.addWidget(self.table, 2)
            layout.addWidget(QLabel("停机原因与待人工判断："))
            layout.addWidget(self.details, 1)

        # ---------- 输入 ----------

        def set_inputs(
            self,
            data_file: TimeSeriesFile | None,
            rules_data: dict | None,
            data_path: str | None,
            *,
            on_applied=None,
        ) -> None:
            self.data_file = data_file
            self.rules_data = rules_data
            self.data_path = data_path
            if on_applied is not None:
                self.on_applied = on_applied
            self.plan = None
            self.table.setRowCount(0)
            self.apply_button.setEnabled(False)
            self.details.clear()
            if data_file is None or rules_data is None:
                self.plan_button.setEnabled(False)
                self.summary.setText("需要先打开数据文件与规则文件。")
            else:
                self.plan_button.setEnabled(True)
                self.summary.setText("点「生成变更清单」按规则算出要改哪些单元格（只读，不写文件）。")

        # ---------- 计算与执行 ----------

        def refresh(self) -> None:
            if self.data_file is None or self.rules_data is None:
                return
            self.plan = plan_repairs(self.data_file, self.rules_data)
            plan = self.plan
            self.summary.setText(plan.summary())
            if plan.halted:
                self.summary.setStyleSheet(f"color: {HALT_COLOR};")
            else:
                self.summary.setStyleSheet("")

            self.table.setRowCount(len(plan.actions))
            for row, action in enumerate(plan.actions):
                cells = (
                    action.time_text,
                    action.column_letter,
                    action.before,
                    action.after,
                    action.rule_key,
                    f"{action.reason}｜来源：{action.rule_source or '（未注明）'}",
                )
                for column, text in enumerate(cells):
                    self.table.setItem(row, column, QTableWidgetItem(text))
            for column in (0, 1, 2, 3, 4):
                self.table.resizeColumnToContents(column)

            lines: list[str] = []
            for reason in plan.halt_reasons:
                lines.append(f"· 停机：{reason}")
            lines.extend(f"· 待人工：{item}" for item in plan.skipped)
            self.details.setPlainText("\n".join(lines) if lines else "（无）")

            self.apply_button.setEnabled(not plan.halted and plan.action_count > 0)

        def apply(self) -> None:
            if self.plan is None or self.data_file is None or self.data_path is None:
                return
            plan = self.plan
            answer = QMessageBox.question(
                self,
                "确认写入",
                f"将修改 {plan.action_count} 个单元格。\n\n"
                f"文件：{self.data_path}\n"
                "写盘前会自动备份原文件，并生成变更日志。\n\n确定执行吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            writeback = (self.rules_data or {}).get("writeback") or {}
            try:
                result = apply_repairs(
                    self.data_file,
                    plan,
                    self.data_path,
                    backup=bool(writeback.get("backup", True)),
                )
            except Exception as exc:  # 写盘失败必须让人看到原因
                QMessageBox.critical(self, "写入失败", f"{type(exc).__name__}: {exc}")
                return
            QMessageBox.information(
                self,
                "写入完成",
                f"已修改 {result.change_count} 处。\n\n"
                f"文件：{result.written_path}\n"
                f"备份：{result.backup_path or '（未备份）'}\n"
                f"变更日志：{result.log_path}",
            )
            self.plan = None
            self.table.setRowCount(0)
            self.apply_button.setEnabled(False)
            self.summary.setText(f"已写入 {result.change_count} 处修改。")
            if self.on_applied:
                self.on_applied()
