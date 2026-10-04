"""读取模板 xlsx：提取阈值（表头与条件格式两个来源）、派生公式与统计窗口。

只做"提取"，不做判断——冲突与不一致交给 :mod:`sharingan.workbook.compare` 报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..rules.formula import FormulaError, constants, normalize
from ..rules.formula import parse as parse_formula

try:
    import openpyxl
    from openpyxl.utils import get_column_letter

    OPENPYXL_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    OPENPYXL_AVAILABLE = False

# 表头文字末尾的阈值，如「发电机轴承A温度(80)」「发电机前后差值（20）」
_HEADER_LIMIT_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*[)）]\s*$")
# 第 3 行的最大值公式，如 =MAX(B$4:B$1029)
_MAX_RE = re.compile(
    r"^=\s*MAX\(\s*\$?([A-Z]{1,2})\$?(\d+)\s*:\s*\$?([A-Z]{1,2})\$?(\d+)\s*\)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConditionalRule:
    """一条条件格式规则（阈值来源之一）。"""

    ranges: str  # 原始 sqref，便于回查
    row: int
    letters: tuple[str, ...]
    operator: str
    thresholds: tuple[float, ...]


@dataclass(frozen=True)
class TemplateColumn:
    letter: str
    index: int
    header: str
    tag: str
    header_limit: float | None
    cf_limits: tuple[float, ...] = ()
    expression: str | None = None  # 规范化后的表达式（该列第 4、5 行是公式时）
    raw_expression: str | None = None
    window: tuple[int, int] | None = None
    window_formula: str | None = None
    constants: tuple[float, ...] = ()

    @property
    def cf_limit(self) -> float | None:
        """条件格式给出的阈值；多档时取最小值（判定用的最严档）。"""
        return min(self.cf_limits) if self.cf_limits else None

    @property
    def limit(self) -> float | None:
        """有效阈值：优先表头文字（它同时是参数名），否则用条件格式。"""
        return self.header_limit if self.header_limit is not None else self.cf_limit

    @property
    def is_derived(self) -> bool:
        return self.expression is not None


@dataclass
class TemplateProfile:
    path: str
    sheet: str
    header_row: int
    tag_row: int
    summary_row: int
    first_data_row: int
    last_data_row: int | None
    columns: list[TemplateColumn] = field(default_factory=list)
    conditional_rules: list[ConditionalRule] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def column(self, letter: str) -> TemplateColumn | None:
        for column in self.columns:
            if column.letter == letter:
                return column
        return None

    @property
    def base_letters(self) -> list[str]:
        return [column.letter for column in self.columns if not column.is_derived]

    @property
    def derived_letters(self) -> list[str]:
        return [column.letter for column in self.columns if column.is_derived]

    @property
    def windows(self) -> dict[str, tuple[int, int]]:
        return {
            column.letter: column.window
            for column in self.columns
            if column.window is not None
        }


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _header_limit(header: str) -> float | None:
    match = _HEADER_LIMIT_RE.search(header)
    return float(match.group(1)) if match else None


def _expression_of(ws, row: int, column: int) -> tuple[str | None, str | None, tuple[float, ...]]:
    """读取某列的表达式（规范化 / 原文 / 硬编码常量）。"""
    value = ws.cell(row, column).value
    if not isinstance(value, str) or not value.startswith("="):
        return None, None, ()
    raw = value
    try:
        normalized = normalize(raw)
    except FormulaError:
        return None, raw, ()
    try:
        literal_constants = constants(raw)
    except FormulaError:
        literal_constants = ()
    return normalized, raw, literal_constants


def _window_of(value: object) -> tuple[tuple[int, int] | None, str | None]:
    if not isinstance(value, str):
        return None, None
    match = _MAX_RE.match(value)
    if not match:
        return None, value
    first_letter, first_row, second_letter, second_row = match.groups()
    if first_letter.upper() != second_letter.upper():
        return None, value
    return (int(first_row), int(second_row)), value


def _extract_conditional_rules(ws, summary_row: int, notes: list[str]) -> list[ConditionalRule]:
    rules: list[ConditionalRule] = []
    for conditional in ws.conditional_formatting:
        for rule in conditional.rules:
            thresholds = tuple(
                float(value)
                for value in (rule.formula or [])
                if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit())
            )
            for cell_range in conditional.sqref.ranges:
                letters = tuple(
                    get_column_letter(index)
                    for index in range(cell_range.min_col, cell_range.max_col + 1)
                )
                rules.append(
                    ConditionalRule(
                        ranges=str(conditional.sqref),
                        row=cell_range.min_row,
                        letters=letters,
                        operator=str(rule.operator or rule.type or ""),
                        thresholds=thresholds,
                    )
                )
                if not (cell_range.min_row == summary_row == cell_range.max_row):
                    notes.append(
                        f"条件格式 {conditional.sqref} 作用于第 {cell_range.min_row} 行"
                        f"（不是最大值行 {summary_row}），未纳入阈值提取"
                    )
    return rules


def read_template(
    path: str | Path,
    *,
    sheet: str | None = None,
    header_row: int = 1,
    tag_row: int = 2,
    summary_row: int = 3,
    first_data_row: int = 4,
) -> TemplateProfile:
    """读取模板，返回提取结果（不改动任何文件）。"""
    if not OPENPYXL_AVAILABLE:
        raise RuntimeError('需要 openpyxl 才能解析模板：pip install "sharingan[workbook]"')

    workbook = openpyxl.load_workbook(path, data_only=False)
    worksheet = workbook[sheet] if sheet else workbook.worksheets[0]

    notes: list[str] = []
    conditional_rules = _extract_conditional_rules(worksheet, summary_row, notes)

    limits_by_letter: dict[str, list[float]] = {}
    for rule in conditional_rules:
        if rule.row != summary_row:
            continue
        for letter in rule.letters:
            limits_by_letter.setdefault(letter, []).extend(rule.thresholds)

    last_data_row: int | None = None
    for row in range(first_data_row, worksheet.max_row + 1):
        if worksheet.cell(row, 1).value not in (None, ""):
            last_data_row = row
    if last_data_row is None:
        notes.append("第 1 列没有找到任何数据，模板可能是空的")

    columns: list[TemplateColumn] = []
    for index in range(1, worksheet.max_column + 1):
        letter = get_column_letter(index)
        header = _text(worksheet.cell(header_row, index).value)
        tag = _text(worksheet.cell(tag_row, index).value)
        expression, raw_expression, literal_constants = _expression_of(
            worksheet, first_data_row, index
        )
        window, window_formula = _window_of(worksheet.cell(summary_row, index).value)
        cf_limits = tuple(sorted(set(limits_by_letter.get(letter, ()))))
        if not any((header, tag, expression, window, cf_limits)):
            continue  # 空列
        columns.append(
            TemplateColumn(
                letter=letter,
                index=index,
                header=header,
                tag=tag,
                header_limit=_header_limit(header),
                cf_limits=cf_limits,
                expression=expression,
                raw_expression=raw_expression,
                window=window,
                window_formula=window_formula,
                constants=literal_constants,
            )
        )

    return TemplateProfile(
        path=str(path),
        sheet=worksheet.title,
        header_row=header_row,
        tag_row=tag_row,
        summary_row=summary_row,
        first_data_row=first_data_row,
        last_data_row=last_data_row,
        columns=columns,
        conditional_rules=conditional_rules,
        notes=notes,
    )
