"""规则编辑面板：查看/修改阈值与修复动作，保存前自动校验。

这里只做"人和规则文件之间的编辑器"，不碰数据——真正写数据的动作在变更清单页，
且必须人工确认。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

from ..rules import REPAIR_ACTIONS, validate

try:
    from PySide6.QtWidgets import (
        QComboBox,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    RULES_UI_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    RULES_UI_AVAILABLE = False

NO_ACTION = "（无）"
MODE_OPTIONS = ("", "auto", "confirm-once", "attended")
HEADERS = ("列", "名称 / 公式", "阈值", "修复动作", "参数", "执行模式", "来源")
SOURCE_FALLBACK = "界面编辑"
# 参数列写法：to=84.9、target=I; to=55、note=超限
_PARAM_SPLIT = re.compile(r"[;,，；]")
_NUMERIC_PARAMS = ("to",)
_COLUMN_PARAMS = ("target",)
_TEXT_PARAMS = ("note",)


def _letter_sort_key(letter: str) -> tuple[int, str]:
    return (len(letter), letter)


def format_params(params: dict | None) -> str:
    """把参数渲染成 ``target=I; to=55`` 这样的可编辑文本。"""
    if not params:
        return ""
    parts = []
    for key in (*_COLUMN_PARAMS, *_NUMERIC_PARAMS, *_TEXT_PARAMS):
        if key in params and params[key] is not None:
            parts.append(f"{key}={params[key]}")
    for key, value in params.items():  # 其他自定义参数原样展示
        if key not in _COLUMN_PARAMS + _NUMERIC_PARAMS + _TEXT_PARAMS:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def parse_params(text: str) -> dict:
    """解析参数文本；无法识别时抛 ``ValueError``（由调用方提示用户）。"""
    params: dict = {}
    for chunk in _PARAM_SPLIT.split(text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"参数片段 {chunk!r} 缺少 '='（示例：to=84.9）")
        key, raw = (piece.strip() for piece in chunk.split("=", 1))
        if key in _NUMERIC_PARAMS:
            params[key] = float(raw)
        elif key in _COLUMN_PARAMS:
            letter = raw.upper()
            if not re.fullmatch(r"[A-Z]{1,2}", letter):
                raise ValueError(f"{key} 必须是列字母，收到 {raw!r}")
            params[key] = letter
        elif key in _TEXT_PARAMS:
            params[key] = raw
        else:
            raise ValueError(f"未知参数 {key!r}（支持：to、target、note）")
    return params


def rules_rows(rules: dict) -> list[dict]:
    """把规则文件摊平成表格行（纯函数，便于测试）。"""
    columns = rules.get("columns") or {}
    derived = rules.get("derived") or {}
    repair = rules.get("repair") or {}
    rows: list[dict] = []
    for letter in sorted(set(columns) | set(derived) | set(repair), key=_letter_sort_key):
        column_entry = columns.get(letter) or {}
        derived_entry = derived.get(letter) or {}
        repair_entry = repair.get(letter) or {}
        if "limit" in derived_entry:
            limit = derived_entry["limit"]
        else:
            limit = column_entry.get("limit")
        sources = [
            entry.get("source")
            for entry in (repair_entry, derived_entry, column_entry)
            if entry.get("source")
        ]
        rows.append(
            {
                "letter": letter,
                "name": derived_entry.get("formula") or column_entry.get("name") or "",
                "limit": "" if limit is None else f"{limit:g}",
                "action": repair_entry.get("action") or NO_ACTION,
                "params": format_params(repair_entry.get("params")),
                "mode": repair_entry.get("mode") or "",
                "source": sources[0] if sources else "",
            }
        )
    return rows


def merge_rows_into_rules(rules: dict, rows: list[dict], *, today: str | None = None) -> dict:
    """把表格行的编辑结果写回规则字典（纯函数，便于测试）。

    - 阈值写回 ``columns`` 或 ``derived``（取决于该列原本属于哪边）；
    - 动作写回 ``repair``；选「（无）」则删除该列的修复规则；
    - 新增或改动过的规则若没有来源，补一条「界面编辑（日期）」作为出处；
    - ``version`` 自增、``updated_at`` 更新。
    """
    stamp = today or date.today().isoformat()
    data = copy.deepcopy(rules)
    columns = data.setdefault("columns", {})
    derived = data.setdefault("derived", {})
    repair = data.setdefault("repair", {})

    for row in rows:
        letter = row["letter"]
        target = derived if letter in derived else columns
        limit_text = str(row.get("limit", "")).strip()
        entry = target.setdefault(letter, {})
        if limit_text:
            value = float(limit_text)
            entry["limit"] = int(value) if value.is_integer() else value
            # 新增或缺来源的条目补一条出处，否则生产校验会拦下来
            entry.setdefault("source", f"{SOURCE_FALLBACK}（{stamp}）")
        elif letter in derived:
            entry.pop("limit", None)  # 派生列：只去掉阈值，保留公式定义
        else:
            target.pop(letter, None)  # 原始列：清空阈值即删除该列规则

        action = row.get("action") or NO_ACTION
        if action == NO_ACTION:
            repair.pop(letter, None)
            continue
        repair_entry = repair.setdefault(letter, {})
        repair_entry["action"] = action
        params = parse_params(str(row.get("params", "")))
        if action == "reject":
            repair_entry.pop("params", None)  # reject 不产生修改，参数无意义
        elif params:
            repair_entry["params"] = params
        mode = (row.get("mode") or "").strip()
        if mode:
            repair_entry["mode"] = mode
        elif action != "reject":
            repair_entry.setdefault("mode", "confirm-once")
        if not repair_entry.get("source"):
            repair_entry["source"] = f"{SOURCE_FALLBACK}（{stamp}）"

    data["version"] = int(data.get("version", 1)) + 1
    data["updated_at"] = stamp
    return data


if RULES_UI_AVAILABLE:

    class RulesPanel(QWidget):  # type: ignore[misc]
        """规则编辑页。``on_rules_changed`` 在文件打开或保存成功后调用。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.rules_path: Path | None = None
            self.rules_data: dict | None = None
            self.on_rules_changed = None

            self.status = QLabel("尚未加载规则文件。")
            self.status.setWordWrap(True)

            toolbar = QHBoxLayout()
            self.open_button = QPushButton("打开规则文件…")
            self.open_button.clicked.connect(self.open_file)
            self.save_button = QPushButton("保存修改")
            self.save_button.clicked.connect(self.save)
            self.save_button.setEnabled(False)
            toolbar.addWidget(self.open_button)
            toolbar.addWidget(self.save_button)
            toolbar.addWidget(self.status, 1)

            self.table = QTableWidget(0, len(HEADERS))
            self.table.setHorizontalHeaderLabels(list(HEADERS))
            for index, width in enumerate((50, 170, 70, 110, 140, 130)):
                self.table.setColumnWidth(index, width)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
            self.table.verticalHeader().setVisible(False)

            layout = QVBoxLayout(self)
            layout.addLayout(toolbar)
            layout.addWidget(self.table, 1)
            hint = QLabel(
                "阈值可直接编辑；修复动作与执行模式用下拉选择（「（无）」表示该列超限时不自动改，"
                "按 unmatched=halt 停机）。参数列写法：clip 填 to=84.9；要改派生列超限背后的原始测点，"
                "填 target=I; to=55；keep_and_flag 可填 note=超限。保存前会自动校验，校验不通过不会写盘。"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)

        # ---------- 加载 / 保存 ----------

        def open_file(self) -> None:
            start = str(self.rules_path.parent) if self.rules_path else ""
            path, _ = QFileDialog.getOpenFileName(
                self, "打开规则文件", start, "规则文件 (*.json);;所有文件 (*)"
            )
            if not path:
                return
            self.load(Path(path))

        def load(self, path: Path, *, notify: bool = True) -> bool:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                QMessageBox.critical(self, "读取失败", f"{type(exc).__name__}: {exc}")
                return False
            result = validate(data, production=False)
            self.rules_path = path
            self.rules_data = data
            self.fill_table()
            self.save_button.setEnabled(True)
            self._show_status(data, result)
            if notify and self.on_rules_changed:
                self.on_rules_changed()
            return True

        def fill_table(self) -> None:
            rows = rules_rows(self.rules_data or {})
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self.table.setItem(index, 0, QTableWidgetItem(row["letter"]))
                name_item = QTableWidgetItem(row["name"])
                name_item.setFlags(name_item.flags() & ~name_item.flags().ItemIsEditable)
                self.table.setItem(index, 1, name_item)
                self.table.setItem(index, 2, QTableWidgetItem(row["limit"]))

                action_box = QComboBox()
                action_box.addItems([NO_ACTION, *sorted(REPAIR_ACTIONS)])
                action_box.setCurrentText(row["action"])
                self.table.setCellWidget(index, 3, action_box)

                self.table.setItem(index, 4, QTableWidgetItem(row["params"]))

                mode_box = QComboBox()
                mode_box.addItems(list(MODE_OPTIONS))
                mode_box.setCurrentText(row["mode"])
                self.table.setCellWidget(index, 5, mode_box)

                source_item = QTableWidgetItem(row["source"])
                source_item.setFlags(source_item.flags() & ~source_item.flags().ItemIsEditable)
                source_item.setToolTip(row["source"])
                self.table.setItem(index, 6, source_item)

        def _collect_rows(self) -> list[dict]:
            rows: list[dict] = []
            for index in range(self.table.rowCount()):
                action_widget = self.table.cellWidget(index, 3)
                mode_widget = self.table.cellWidget(index, 5)

                def text(column: int) -> str:
                    item = self.table.item(index, column)
                    return item.text() if item else ""

                rows.append(
                    {
                        "letter": text(0),
                        "limit": text(2),
                        "action": action_widget.currentText() if action_widget else NO_ACTION,
                        "params": text(4),
                        "mode": mode_widget.currentText() if mode_widget else "",
                    }
                )
            return rows

        def save(self) -> bool:
            if self.rules_data is None or self.rules_path is None:
                return False
            rows = self._collect_rows()
            for row in rows:
                text = str(row["limit"]).strip()
                if text:
                    try:
                        float(text)
                    except ValueError:
                        QMessageBox.warning(
                            self, "阈值格式不对", f"{row['letter']} 列的阈值 {text!r} 不是数字。"
                        )
                        return False
            merged = None
            try:
                merged = merge_rows_into_rules(self.rules_data, rows)
            except ValueError as exc:
                QMessageBox.warning(self, "参数格式不对", str(exc))
                return False
            result = validate(merged)
            if not result.ok:
                QMessageBox.warning(
                    self, "规则校验不通过，未保存", result.format()
                )
                return False
            self.rules_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self.rules_data = merged
            self.fill_table()
            self._show_status(merged, result)
            if self.on_rules_changed:
                self.on_rules_changed()
            return True

        def _show_status(self, data: dict, result) -> None:
            warnings = len(result.warnings)
            text = (
                f"已加载：{self.rules_path}（version {data.get('version')}，"
                f"告警 {warnings} 条）"
            )
            if warnings:
                text += "；" + "；".join(str(issue) for issue in result.warnings[:3])
            self.status.setText(text)
