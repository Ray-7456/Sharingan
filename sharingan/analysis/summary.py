"""文件概况与统计结果的数据结构（与界面无关，CLI 与 GUI 共用）。

这一层的存在意义：规则判断逻辑只写一次。CLI 与图形界面都只是渲染
:class:`Summary`，不许各自复制一遍统计口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..formats import TimeSeriesFile
from ..rules import FormulaError, evaluate, load_rules


def column_letter(index: int) -> str:
    """0 -> A，25 -> Z，26 -> AA。"""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def data_column_letter(index: int) -> str:
    """数据文件中第 index 个数值列的列字母（A 列固定为 Time）。"""
    return column_letter(index + 1)


def row_values(row, columns) -> dict[str, float | None]:
    """把一行展开成 ``列字母 -> 数值`` 的映射，供公式求值使用。"""
    values: dict[str, float | None] = {"A": None}
    for index in range(len(columns)):
        values[data_column_letter(index)] = row.value(index)
    return values


@dataclass(frozen=True)
class FileFacts:
    path: str
    encoding: str
    newline: str
    column_count: int
    row_count: int
    time_range: tuple[str, str]
    tail_line_count: int
    unnamed_trailing_columns: int
    parse_failures: int


@dataclass(frozen=True)
class NoteStat:
    text: str
    count: int


@dataclass(frozen=True)
class Gap:
    before: str
    after: str
    minutes: float

    @property
    def hours(self) -> float:
        return self.minutes / 60


@dataclass(frozen=True)
class ColumnStat:
    letter: str
    name: str
    count: int
    minimum: float | None
    maximum: float | None
    limit: float | None = None
    breaches: int = 0
    derived: str | None = None


@dataclass
class Summary:
    facts: FileFacts
    notes: list[NoteStat] = field(default_factory=list)
    duplicate_rows: int = 0
    gaps: list[Gap] = field(default_factory=list)
    columns: list[ColumnStat] = field(default_factory=list)
    rule_path: str | None = None
    rule_loaded: bool = True
    rule_ok: bool = True
    rule_errors: list[str] = field(default_factory=list)
    rule_warnings: list[str] = field(default_factory=list)

    @property
    def breach_total(self) -> int:
        return sum(stat.breaches for stat in self.columns)


def _stat_for(
    letter: str, name: str, values: list[float | None], limit: float | None, derived: str | None
) -> ColumnStat:
    present = [value for value in values if value is not None]
    return ColumnStat(
        letter=letter,
        name=name,
        count=len(present),
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
        limit=limit,
        breaches=sum(1 for value in present if value >= limit) if limit is not None else 0,
        derived=derived,
    )


def build_summary(
    path: str | Path,
    *,
    rules_path: str | Path | None = None,
    gap_minutes: int = 10,
    production: bool = False,
) -> Summary:
    """读取数据文件（可选配合规则文件），产出界面无关的统计结果。

    ``production=False``：按草稿标准校验规则，允许尚未确认口径的条目存在——
    本函数用于构建期分析，生产严格性由运行期负责。
    """
    data_file = TimeSeriesFile.read(path)
    rows = data_file.rows

    rules_data: dict | None = None
    rule_errors: list[str] = []
    rule_warnings: list[str] = []
    limits: dict[str, float] = {}
    derived_rules: dict[str, dict] = {}
    rule_loaded = True

    if rules_path is not None:
        rules_data, rules_result = load_rules(rules_path, production=production)
        rule_errors = [str(issue) for issue in rules_result.errors]
        rule_warnings = [str(issue) for issue in rules_result.warnings]
        rule_loaded = rules_data is not None
        if rules_data is not None:
            limits = {
                letter: spec["limit"]
                for letter, spec in (rules_data.get("columns") or {}).items()
                if isinstance(spec, dict) and spec.get("limit") is not None
            }
            derived_rules = {
                letter: spec
                for letter, spec in (rules_data.get("derived") or {}).items()
                if isinstance(spec, dict)
            }

    facts = FileFacts(
        path=str(path),
        encoding=data_file.encoding,
        newline=data_file.newline,
        column_count=len(data_file.columns),
        row_count=len(rows),
        time_range=(rows[0].time_text, rows[-1].time_text) if rows else ("", ""),
        tail_line_count=len(data_file.tail_lines),
        unnamed_trailing_columns=len(rows[0].extra) if rows else 0,
        parse_failures=sum(1 for row in rows if row.time is None),
    )

    note_counts: dict[str, int] = {}
    duplicate_rows = 0
    previous = None
    for row in rows:
        if row.note.strip():
            note_counts[row.note] = note_counts.get(row.note, 0) + 1
        if previous is not None and row.values == previous.values:
            duplicate_rows += 1
        previous = row

    gaps = []
    for before, after in zip(rows, rows[1:]):
        if before.time and after.time:
            minutes = (after.time - before.time).total_seconds() / 60
            if minutes > gap_minutes:
                gaps.append(Gap(before.time_text, after.time_text, minutes))

    columns_stats: list[ColumnStat] = []
    for index, name in enumerate(data_file.columns):
        letter = data_column_letter(index)
        values = [row.value(index) for row in rows]
        columns_stats.append(_stat_for(letter, name, values, limits.get(letter), None))

    for letter, spec in derived_rules.items():
        formula = spec.get("formula")
        if not isinstance(formula, str):
            continue
        series: list[float | None] = []
        for row in rows:
            try:
                series.append(evaluate(formula, row_values(row, data_file.columns)))
            except FormulaError as exc:
                rule_errors.append(f"派生列 {letter} 公式求值失败：{exc}")
                series = []
                break
        if not series:
            continue
        limit = spec.get("limit")
        columns_stats.append(
            _stat_for(letter, f"派生列 {formula}", series, limit, formula)
        )

    return Summary(
        facts=facts,
        notes=[NoteStat(text=text, count=count) for text, count in sorted(note_counts.items())],
        duplicate_rows=duplicate_rows,
        gaps=gaps,
        columns=columns_stats,
        rule_path=str(rules_path) if rules_path is not None else None,
        rule_loaded=rule_loaded,
        rule_ok=not rule_errors,
        rule_errors=rule_errors,
        rule_warnings=rule_warnings,
    )
