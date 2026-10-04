"""跨平台图形界面（PySide6 / Qt for Python）。

设计约束（见 docs/design.md §7）：

- 界面层只渲染 :mod:`sharingan.analysis` 产出的 ``Summary``，自身不含任何规则判断逻辑；
- 核心保持零第三方依赖，PySide6 是**可选依赖**（``pip install "sharingan[ui]"``）；
- 不使用平台专用 API，Qt 在 Windows / macOS / Linux 上表现一致；
- 未安装 PySide6 时不影响 CLI 使用，本模块仍可安全导入。
"""

from __future__ import annotations

import html
from pathlib import Path

from ..analysis import Summary, build_summary
from ..formats import TimeSeriesFile
from ..rules import load_rules
from .changes_panel import CHANGES_UI_AVAILABLE, ChangesPanel
from .rules_panel import RULES_UI_AVAILABLE, RulesPanel

try:  # PySide6 为可选依赖
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QColor
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QHeaderView,
        QMainWindow,
        QMessageBox,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextBrowser,
    )

    UI_AVAILABLE = True
    UI_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    UI_AVAILABLE = False
    UI_IMPORT_ERROR = exc

WINDOW_TITLE = "Sharingan —— 数据与规则查看器"
TABLE_HEADERS = ("列", "名称", "样本", "最小", "最大", "阈值", "超限")
BREACH_COLOR = QColor(198, 40, 40) if UI_AVAILABLE else None

# 应用图标：所有由本项目生成的自动化工具统一使用这一套（见 README 图标规范）
ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
ICON_SIZES = (16, 32, 48, 64, 128, 256)

# 各平台常见的中文字体，按优先级挑选（Linux 精简环境常缺中文字体，会显示方块）
FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "SimHei",
)


def app_icon():
    """装配多尺寸应用图标；资源缺失或未装 Qt 时返回 None（不影响启动）。"""
    if not UI_AVAILABLE:
        return None
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for size in ICON_SIZES:
        path = ICON_DIR / f"sharingan-{size}.png"
        if path.exists():
            icon.addFile(str(path))
    return None if icon.isNull() else icon


def apply_app_icon(application) -> bool:
    """设置应用级图标（任务栏/窗口管理器用）；成功返回 True。"""
    icon = app_icon()
    if icon is None:
        return False
    application.setWindowIcon(icon)
    return True


def pick_ui_font() -> str:
    """从候选里挑一个当前系统可用的中文字体；没有可用项时返回空串。"""
    if not UI_AVAILABLE:
        return ""
    from PySide6.QtGui import QFontDatabase

    available = set(QFontDatabase.families())
    for family in FONT_CANDIDATES:
        if family in available:
            return family
    return ""


def apply_ui_font(application) -> str:
    """把挑中的字体应用到整个界面；返回所选字体名（可能为空串）。"""
    family = pick_ui_font()
    if family:
        from PySide6.QtGui import QFont

        application.setFont(QFont(family, application.font().pointSize()))
    return family


def _fact_row(label: str, value: str) -> str:
    return (
        f"<tr><td style='color:#666;padding-right:12px'>{html.escape(label)}</td>"
        f"<td><b>{html.escape(value)}</b></td></tr>"
    )


def render_facts_html(summary: Summary) -> str:
    """把概况渲染成 HTML；纯函数，便于测试。"""
    facts = summary.facts
    rows = [
        _fact_row("文件", facts.path),
        _fact_row("编码 / 换行", f"{facts.encoding} / {facts.newline!r}"),
        _fact_row("数值列 / 数据行", f"{facts.column_count} / {facts.row_count}"),
        _fact_row("时间范围", f"{facts.time_range[0]} → {facts.time_range[1]}"),
    ]
    if facts.tail_line_count:
        rows.append(_fact_row("尾部保留行", f"{facts.tail_line_count} 行（原样写回）"))
    if facts.unnamed_trailing_columns:
        rows.append(
            _fact_row("行尾未命名列", f"{facts.unnamed_trailing_columns} 个（原样保留）")
        )
    if facts.parse_failures:
        rows.append(_fact_row("时间无法解析的行", str(facts.parse_failures)))

    parts = ["<h3 style='margin:4px 0'>文件概况</h3>", "<table cellspacing='0'>"]
    parts.extend(rows)
    parts.append("</table>")

    if summary.notes:
        parts.append("<h3 style='margin:14px 0 4px'>备注统计</h3><ul style='margin:0'>")
        parts.extend(
            f"<li>{html.escape(note.text)} —— {note.count} 行</li>" for note in summary.notes
        )
        parts.append("</ul>")
    if summary.duplicate_rows:
        parts.append(
            f"<p style='margin:10px 0 0'>与上一行完全重复的行："
            f"<b>{summary.duplicate_rows}</b>（常见于无数据时的复制占位）</p>"
        )
    if summary.gaps:
        parts.append("<h3 style='margin:14px 0 4px'>时间断档</h3><ul style='margin:0'>")
        for gap in summary.gaps[:20]:
            parts.append(
                f"<li>{html.escape(gap.before)} → {html.escape(gap.after)}"
                f"（{gap.hours:.1f} 小时）</li>"
            )
        if len(summary.gaps) > 20:
            parts.append(f"<li>…… 其余 {len(summary.gaps) - 20} 处</li>")
        parts.append("</ul>")

    if summary.rule_path:
        parts.append("<h3 style='margin:14px 0 4px'>规则校验</h3>")
        if summary.rule_ok and not summary.rule_warnings:
            parts.append("<p style='margin:0'>校验通过，无问题。</p>")
        for warning in summary.rule_warnings:
            parts.append(
                f"<p style='margin:0;color:#8a6d00'>告警：{html.escape(warning)}</p>"
            )
        for error in summary.rule_errors:
            parts.append(f"<p style='margin:0;color:#c62828'>错误：{html.escape(error)}</p>")
        if summary.rule_ok:
            parts.append(
                f"<p style='margin:8px 0 0'>合计超限：<b>{summary.breach_total}</b> 处</p>"
            )
    return "".join(parts)


def fill_table(table: "QTableWidget", summary: Summary) -> None:
    """把逐列统计填进表格；超限行标红。"""
    stats = [stat for stat in summary.columns if stat.count]
    table.setRowCount(len(stats))
    for row, stat in enumerate(stats):
        cells = [
            stat.letter,
            stat.name,
            str(stat.count),
            "" if stat.minimum is None else f"{stat.minimum:.1f}",
            "" if stat.maximum is None else f"{stat.maximum:.1f}",
            "" if stat.limit is None else f"{stat.limit:g}",
            str(stat.breaches),
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if stat.breaches:
                item.setForeground(BREACH_COLOR)
                item.setToolTip("存在超限数据")
            table.setItem(row, column, item)


def populate(
    window: "MainWindow",
    data_path: str | Path,
    rules_path: str | Path | None = None,
    *,
    gap_minutes: int = 10,
) -> Summary:
    """加载数据文件（与可选规则文件）并刷新界面；返回统计结果。

    与对话框解耦，便于在无界面环境下测试。
    """
    summary = build_summary(data_path, rules_path=rules_path, gap_minutes=gap_minutes)
    window.summary = summary
    window.data_path = str(data_path)
    if rules_path is not None:
        window.rules_path = str(rules_path)
    window.facts_view.setHtml(render_facts_html(summary))
    fill_table(window.table, summary)
    window.setWindowTitle(f"{WINDOW_TITLE} —— {Path(data_path).name}")
    headline = (
        f"{Path(data_path).name}：{summary.facts.row_count} 行，"
        f"{len(summary.columns)} 列统计"
    )
    if summary.rule_path:
        headline += f"，合计超限 {summary.breach_total} 处"
    window.statusBar().showMessage(headline)

    # 变更清单页需要原始数据对象与规则字典（只读使用）
    window.data_file = TimeSeriesFile.read(data_path)
    window.rules_data = None
    if rules_path is not None:
        rules_data, _ = load_rules(rules_path, production=False)
        window.rules_data = rules_data
    if getattr(window, "changes_panel", None) is not None:
        window.changes_panel.set_inputs(
            window.data_file, window.rules_data, str(data_path), on_applied=window._after_apply
        )
    if getattr(window, "rules_panel", None) is not None and rules_path is not None:
        panel_path = window.rules_panel.rules_path
        if panel_path is None or Path(panel_path) != Path(rules_path):
            window.rules_panel.load(Path(rules_path), notify=False)
    return summary


if UI_AVAILABLE:

    class MainWindow(QMainWindow):  # type: ignore[misc]
        """主窗口：左侧概况、右侧逐列统计。"""

        def __init__(self) -> None:
            super().__init__()
            self.data_path: str | None = None
            self.rules_path: str | None = None
            self.summary: Summary | None = None
            self.data_file: TimeSeriesFile | None = None
            self.rules_data: dict | None = None

            self.setWindowTitle(WINDOW_TITLE)
            self.resize(1180, 720)
            icon = app_icon()
            if icon is not None:
                self.setWindowIcon(icon)

            self.facts_view = QTextBrowser()
            self.facts_view.setOpenExternalLinks(False)
            self.table = QTableWidget(0, len(TABLE_HEADERS))
            self.table.setHorizontalHeaderLabels(list(TABLE_HEADERS))
            self.table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.addWidget(self.facts_view)
            splitter.addWidget(self.table)
            splitter.setStretchFactor(0, 2)
            splitter.setStretchFactor(1, 3)

            self.tabs = QTabWidget()
            self.tabs.addTab(splitter, "数据概况")
            self.rules_panel = RulesPanel() if RULES_UI_AVAILABLE else None
            if self.rules_panel is not None:
                self.rules_panel.on_rules_changed = self._rules_changed
                self.tabs.addTab(self.rules_panel, "规则")
            self.changes_panel = ChangesPanel() if CHANGES_UI_AVAILABLE else None
            if self.changes_panel is not None:
                self.changes_panel.on_applied = self._after_apply
                self.tabs.addTab(self.changes_panel, "变更清单")
            self.setCentralWidget(self.tabs)

            file_menu = self.menuBar().addMenu("文件(&F)")
            self._action(file_menu, "打开数据文件…", self.open_data_file, "Ctrl+O")
            self._action(file_menu, "打开规则文件…", self.open_rules_file, "Ctrl+R")
            self._action(file_menu, "打开合成示例", self.open_sample, "Ctrl+Shift+O")
            self._action(file_menu, "生成变更清单", self.generate_plan, "Ctrl+D")
            file_menu.addSeparator()
            self._action(file_menu, "退出", self.close, "Ctrl+Q")

            help_menu = self.menuBar().addMenu("帮助(&H)")
            self._action(help_menu, "关于", self.show_about)

            toolbar = self.addToolBar("操作")
            toolbar.setMovable(False)
            for text, slot in (
                ("打开数据文件", self.open_data_file),
                ("打开规则文件", self.open_rules_file),
                ("合成示例", self.open_sample),
                ("生成变更清单", self.generate_plan),
            ):
                action = QAction(text, self)
                action.triggered.connect(slot)
                toolbar.addAction(action)

            self.statusBar().showMessage("请先打开一个数据文件（或点『合成示例』试用）")

        def _action(self, menu, text: str, slot, shortcut: str | None = None) -> QAction:
            action = QAction(text, self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(shortcut)
            menu.addAction(action)
            return action

        # ---------- 动作 ----------

        def open_data_file(self) -> None:
            start = str(Path(self.data_path).parent) if self.data_path else ""
            path, _ = QFileDialog.getOpenFileName(
                self, "打开数据文件", start, "数据文件 (*.csv *.txt);;所有文件 (*)"
            )
            if not path:
                return
            self._load(path, self.rules_path)

        def open_rules_file(self) -> None:
            if self.rules_panel is not None:  # 规则编辑页有自己的文件对话框
                self.rules_panel.open_file()
                return
            start = str(Path(self.rules_path).parent) if self.rules_path else ""
            path, _ = QFileDialog.getOpenFileName(
                self, "打开规则文件", start, "规则文件 (*.json);;所有文件 (*)"
            )
            if not path:
                return
            if self.data_path is None:
                self._load_sample(rules_path=path)
            else:
                self._load(self.data_path, path)

        def open_sample(self) -> None:
            self._load_sample()

        def generate_plan(self) -> None:
            """切到变更清单页并计算（只读，不写文件）。"""
            if self.changes_panel is None:
                QMessageBox.information(self, "暂不可用", "当前环境没有加载界面组件。")
                return
            self.tabs.setCurrentWidget(self.changes_panel)
            self.changes_panel.refresh()

        # ---------- 联动 ----------

        def _rules_changed(self) -> None:
            """规则被打开或保存后：同步路径并重新统计。"""
            if self.rules_panel is not None and self.rules_panel.rules_path is not None:
                self.rules_path = str(self.rules_panel.rules_path)
            self._reload()

        def _after_apply(self) -> None:
            """写入完成后重新读取文件，确认盘上结果。"""
            self._reload()

        def _reload(self) -> None:
            if self.data_path:
                self._load(self.data_path, self.rules_path)

        def show_about(self) -> None:
            QMessageBox.information(
                self,
                "关于 Sharingan",
                "Sharingan（写轮眼）：读懂规则、复制操作、生成自动化工具。\n\n"
                "三个页签：数据概况（只读）、规则（编辑阈值与修复动作）、"
                "变更清单（先算后写，写盘前必须人工确认，并自动备份原文件）。",
            )

        # ---------- 内部 ----------

        def _load_sample(self, rules_path: str | None = None) -> None:
            import tempfile

            from ..fixtures import generate

            directory = Path(tempfile.mkdtemp(prefix="sharingan-sample-"))
            csv_path, _ = generate().write(directory, stem="synthetic_week")
            self._load(str(csv_path), rules_path)

        def _load(self, data_path: str, rules_path: str | None) -> None:
            try:
                populate(self, data_path, rules_path)
            except Exception as exc:  # 读取失败要让人看到原因，而不是静默
                QMessageBox.critical(self, "读取失败", f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    """启动图形界面。``argv`` 可传一个数据文件路径与可选的 ``--rules`` 参数。"""
    if not UI_AVAILABLE:
        print("未安装 PySide6，无法启动图形界面。")
        print('安装：pip install PySide6   或   pip install "sharingan[ui]"')
        print(f"（导入错误：{UI_IMPORT_ERROR}）")
        return 2

    arguments = list(argv or [])
    data_path = None
    rules_path = None
    while arguments:
        token = arguments.pop(0)
        if token == "--rules" and arguments:
            rules_path = arguments.pop(0)
        elif data_path is None:
            data_path = token

    app = QApplication.instance() or QApplication([])
    if not apply_ui_font(app):
        print(
            "提示：未找到常见中文字体，界面文字可能显示为方块。\n"
            "       Linux 可安装 fonts-noto-cjk（Debian/Ubuntu：apt install fonts-noto-cjk）。"
        )
    apply_app_icon(app)
    window = MainWindow()
    if data_path:
        window._load(data_path, rules_path)
    elif rules_path:
        window.rules_path = rules_path
    window.show()
    return app.exec()
