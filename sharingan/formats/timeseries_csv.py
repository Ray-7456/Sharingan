"""现场数据 CSV（Time + 数值列 + 备注）的忠实读写。

格式事实来自现场导出文件实测：

- GBK 编码、无 BOM、CRLF 换行、文件末尾有换行符；
- 表头形如 ``Time,<标签...>,备注``（16 个字段）；
- **数据行比表头多一个字段**：结尾有一个表头未命名的空列（Excel 导出残留），
  读取时原样保留、写回时原样输出；
- 时间文本不补零：``2026-7-17 0:00:00``；
- 数值文本为最短表示：``0`` / ``-1.8`` / ``135.6``；
- 数据区之后可能跟一行全空的占位行（原样保留）。

两条硬约束：

1. **未修改的单元格按原始文本原样写回**，保证人工复核时的 diff 最小；
2. 修改过的单元格按 :func:`format_number` 的规范化文本写入。

解析规则：从表头之后逐行读取，直到某行的首字段为空为止；该行及其之后的
所有物理行原样保存在 ``tail_lines`` 中，写回时不做任何改动。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TIME_COLUMN = "Time"
NOTE_COLUMN = "备注"

_TIME_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")


def format_time(moment: datetime) -> str:
    """按现场格式输出时间文本：年月日与小时不补零，分秒补零。

    例如 ``2026-7-17 0:00:00``、``2026-7-17 1:40:00``。
    """
    return (
        f"{moment.year}-{moment.month}-{moment.day} "
        f"{moment.hour}:{moment.minute:02d}:{moment.second:02d}"
    )


def parse_time(text: str) -> datetime | None:
    """解析现场时间文本；无法解析时返回 ``None``（不猜测）。"""
    match = _TIME_RE.match(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        )
    except ValueError:
        return None


def format_number(value: float | int | None) -> str:
    """把数值写成现场风格的文本：不使用科学计数法，整数不带小数点。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("布尔值不是合法的测点数值")
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"非法数值: {value!r}")
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e16:
        return str(int(number))
    text = repr(number)
    if "e" in text or "E" in text:
        # 现场文件不使用科学计数法；极小/极大值退化为定点表示
        text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text


def detect_encoding(raw: bytes) -> str:
    """按 BOM 与可解码性推断编码；现场文件通常为 GBK。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "gbk"
    return "utf-8"


@dataclass
class DataRow:
    """一行数据；``values`` 保存各列的**当前文本**，初始与 ``original`` 一致。"""

    time_text: str
    values: list[str]
    original: tuple[str, ...]
    note: str = ""
    original_note: str = ""
    time: datetime | None = None
    extra: tuple[str, ...] = ()  # 表头未命名的尾部列，原样保留

    @classmethod
    def create(
        cls, moment: datetime, values, note: str = "", extra: tuple[str, ...] = ()
    ) -> "DataRow":
        texts = [format_number(v) for v in values]
        return cls(
            time_text=format_time(moment),
            values=list(texts),
            original=tuple(texts),
            note=note,
            original_note=note,
            time=moment,
            extra=extra,
        )

    def value(self, index: int) -> float | None:
        """取第 index 个数值列的浮点值；空文本返回 ``None``，解析失败直接抛错。"""
        text = self.values[index].strip()
        if not text:
            return None
        return float(text)

    def set_value(self, index: int, value: float | int | None) -> None:
        """修改第 index 个数值列，按规范化文本写入并记为已修改。"""
        self.values[index] = format_number(value)

    def set_note(self, note: str) -> None:
        self.note = note

    def is_modified(self, index: int) -> bool:
        return self.values[index] != self.original[index]

    @property
    def is_note_modified(self) -> bool:
        return self.note != self.original_note

    @property
    def modified_indices(self) -> list[int]:
        return [i for i in range(len(self.values)) if self.is_modified(i)]

    @property
    def is_modified_any(self) -> bool:
        return bool(self.modified_indices)


@dataclass(frozen=True)
class Change:
    """一处修改记录，直接供 dry-run 变更清单与审计日志使用。"""

    row_index: int
    field_index: int  # 数值列下标；备注列为 -1
    field_name: str
    time_text: str
    before: str
    after: str


def _join_fields(fields: list[str]) -> str:
    for field in fields:
        if any(ch in field for ch in ',"\r\n'):
            raise ValueError(f"字段包含分隔符或引号，拒绝写出：{field!r}")
    return ",".join(fields)


class TimeSeriesFile:
    """一份数据文件：表头、数据行、尾部原样保留行。"""

    def __init__(
        self,
        *,
        header_line: str,
        columns: list[str],
        note_column: str,
        rows: list[DataRow],
        tail_lines: list[str] | None = None,
        newline: str = "\r\n",
        ends_with_newline: bool = True,
        encoding: str = "gbk",
    ) -> None:
        self.header_line = header_line
        self.columns = list(columns)
        self.note_column = note_column
        self.rows = rows
        self.tail_lines = list(tail_lines or [])
        self.newline = newline
        self.ends_with_newline = ends_with_newline
        self.encoding = encoding

    # ---------- 构造 ----------

    @classmethod
    def from_rows(
        cls,
        columns: list[str],
        rows: list[DataRow],
        *,
        note_column: str = NOTE_COLUMN,
        trailing_blank_row: bool = True,
        unnamed_trailing_columns: int = 1,
        newline: str = "\r\n",
        encoding: str = "gbk",
    ) -> "TimeSeriesFile":
        """由数据行构造新文件。

        ``unnamed_trailing_columns``：现场导出的数据行比表头多一个未命名的空列，
        这里默认同样生成，使夹具与真实格式一致（设为 0 可关闭）。
        """
        header_line = ",".join([TIME_COLUMN, *columns, note_column])
        expected_fields = len(columns) + 2 + unnamed_trailing_columns
        for row in rows:
            row.extra = ("",) * unnamed_trailing_columns
        tail = [",".join([""] * expected_fields)] if trailing_blank_row else []
        return cls(
            header_line=header_line,
            columns=columns,
            note_column=note_column,
            rows=rows,
            tail_lines=tail,
            newline=newline,
            ends_with_newline=True,
            encoding=encoding,
        )

    # ---------- 读 ----------

    @classmethod
    def read(cls, path: str | Path, *, encoding: str | None = None) -> "TimeSeriesFile":
        raw = Path(path).read_bytes()
        used_encoding = encoding or detect_encoding(raw)
        text = raw.decode(used_encoding)

        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        ends_with_newline = lines and lines[-1] == ""
        if ends_with_newline:
            lines.pop()
        if not lines:
            raise ValueError(f"文件为空：{path}")

        header_line = lines[0]
        header = next(csv.reader([header_line]))
        if len(header) < 3:
            raise ValueError(f"表头至少需要 时间列、一个数值列、备注列：{header!r}")
        columns = header[1:-1]
        note_column = header[-1]
        expected_fields = len(columns) + 2

        rows: list[DataRow] = []
        tail_lines: list[str] = []
        for offset, line in enumerate(lines[1:], start=2):
            fields = next(csv.reader([line]))
            if not fields or not fields[0].strip():
                tail_lines = lines[offset - 1 :]
                break
            if len(fields) < expected_fields:
                raise ValueError(
                    f"第 {offset} 行字段数为 {len(fields)}，少于表头声明的 {expected_fields}"
                )
            extra = tuple(fields[expected_fields:])
            if any(field.strip() for field in extra):
                raise ValueError(
                    f"第 {offset} 行存在表头未命名的非空列 {extra!r}，拒绝静默丢弃"
                )
            time_text = fields[0]
            values = fields[1 : expected_fields - 1]
            note = fields[expected_fields - 1]
            rows.append(
                DataRow(
                    time_text=time_text,
                    values=list(values),
                    original=tuple(values),
                    note=note,
                    original_note=note,
                    time=parse_time(time_text),
                    extra=extra,
                )
            )

        return cls(
            header_line=header_line,
            columns=columns,
            note_column=note_column,
            rows=rows,
            tail_lines=tail_lines,
            newline=newline,
            ends_with_newline=ends_with_newline,
            encoding=used_encoding,
        )

    # ---------- 写 ----------

    def _row_line(self, row: DataRow) -> str:
        return _join_fields([row.time_text, *row.values, row.note, *row.extra])

    def to_bytes(self, *, encoding: str | None = None) -> bytes:
        lines = [self.header_line]
        lines.extend(self._row_line(row) for row in self.rows)
        lines.extend(self.tail_lines)
        text = self.newline.join(lines) + (self.newline if self.ends_with_newline else "")
        return text.encode(encoding or self.encoding)

    def write(self, path: str | Path, *, encoding: str | None = None) -> None:
        Path(path).write_bytes(self.to_bytes(encoding=encoding))

    # ---------- 变更视图 ----------

    def changed_cells(self) -> list[Change]:
        """列出所有已修改的单元格，供 dry-run 报告与审计日志使用。"""
        changes: list[Change] = []
        for row_index, row in enumerate(self.rows):
            for field_index in row.modified_indices:
                changes.append(
                    Change(
                        row_index=row_index,
                        field_index=field_index,
                        field_name=self.columns[field_index],
                        time_text=row.time_text,
                        before=row.original[field_index],
                        after=row.values[field_index],
                    )
                )
            if row.is_note_modified:
                changes.append(
                    Change(
                        row_index=row_index,
                        field_index=-1,
                        field_name=self.note_column,
                        time_text=row.time_text,
                        before=row.original_note,
                        after=row.note,
                    )
                )
        return changes
