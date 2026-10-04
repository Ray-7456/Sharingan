"""``rules.json`` 的校验。

设计目标（见 docs/design.md §3）：

- 一条规则若没有 ``source``，就是草稿，不得进入生产运行；
- ``mode`` 只能是 ``auto`` / ``confirm-once`` / ``attended``；
- ``unmatched`` 必须是 ``halt``：未覆盖的情况一律停机，严禁猜测；
- 覆盖原始数据（``writeback.overwrite``）必须为 ``attended``，即每次都需人确认；
- 未知键一律告警，用于抓 ``limit`` 拼写错误这类静默事故。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .formula import FormulaError, parse, referenced

MODES = frozenset({"auto", "confirm-once", "attended"})
ENCODINGS = frozenset({"gbk", "utf-8", "utf-8-sig"})
# repair 段支持的动作（见 sharingan/analysis/repair.py 的模块文档）
REPAIR_ACTIONS = frozenset({"clip", "interpolate", "keep_and_flag", "reject"})

_COLUMN_RE = re.compile(r"^[A-Z]{1,2}$")
_RANGE_RE = re.compile(r"^([A-Z]{1,2}):([A-Z]{1,2})$")

_TOP_KEYS = {
    "version",
    "updated_at",
    "base_columns",
    "columns",
    "derived",
    "repair",
    "missing_data",
    "writeback",
    "unmatched",
}
_COLUMN_KEYS = {"limit", "source", "unit", "note", "description"}
_DERIVED_KEYS = {"formula", "limit", "source", "unit", "note", "description"}
_ACTION_KEYS = {"action", "params", "mode", "source", "description"}
_WRITEBACK_KEYS = {
    "range",
    "encoding",
    "date_format",
    "preserve_columns",
    "backup",
    "overwrite",
    "description",
}


@dataclass(frozen=True)
class Issue:
    level: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.path}: {self.message}"


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.errors.append(Issue("error", path, message))

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(Issue("warning", path, message))

    def format(self) -> str:
        lines = [str(issue) for issue in self.errors]
        lines.extend(str(issue) for issue in self.warnings)
        return "\n".join(lines) if lines else "校验通过，无问题。"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def _check_unknown_keys(
    result: ValidationResult, path: str, entry: dict, allowed: set[str]
) -> None:
    for key in entry:
        if key not in allowed:
            result.warn(
                f"{path}.{key}",
                f"未知字段 {key!r}（允许：{sorted(allowed)}），拼写错误会被静默忽略",
            )


def _require_source(
    result: ValidationResult, path: str, entry: dict, production: bool
) -> None:
    source = entry.get("source")
    if source is None or (isinstance(source, str) and not source.strip()):
        if production:
            result.error(f"{path}.source", "缺少 source：草稿规则不得进入生产运行")
        else:
            result.warn(f"{path}.source", "缺少 source，当前按草稿处理")
    elif not isinstance(source, str):
        result.error(f"{path}.source", "source 必须是字符串")


def _check_mode(result: ValidationResult, path: str, mode: Any) -> None:
    if mode not in MODES:
        result.error(f"{path}.mode", f"mode 必须是 {sorted(MODES)} 之一，收到 {mode!r}")


def _check_limit(result: ValidationResult, path: str, entry: dict) -> None:
    if "limit" in entry and entry["limit"] is not None and not _is_number(entry["limit"]):
        result.error(f"{path}.limit", f"limit 必须是数字或 null，收到 {entry['limit']!r}")


def _check_columns(result: ValidationResult, data: dict, production: bool) -> None:
    columns = data.get("columns")
    if columns is None:
        return
    if not isinstance(columns, dict):
        result.error("columns", "columns 必须是对象")
        return
    for name, entry in columns.items():
        path = f"columns.{name}"
        if not _COLUMN_RE.match(name):
            result.error(path, f"列名必须是 1-2 个大写字母，收到 {name!r}")
            continue
        if not isinstance(entry, dict):
            result.error(path, "列规则必须是对象")
            continue
        _check_unknown_keys(result, path, entry, _COLUMN_KEYS)
        _check_limit(result, path, entry)
        _require_source(result, path, entry, production)


def _check_base_columns(result: ValidationResult, data: dict) -> set[str] | None:
    base = data.get("base_columns")
    if base is None:
        return None
    if not isinstance(base, list) or not all(isinstance(x, str) for x in base):
        result.error("base_columns", "base_columns 必须是列名字符串数组")
        return None
    for name in base:
        if not _COLUMN_RE.match(name):
            result.error("base_columns", f"非法列名 {name!r}")
    return set(base)


def _derived_references(derived: dict) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for name, entry in derived.items():
        if not isinstance(entry, dict):
            continue
        formula = entry.get("formula")
        if not isinstance(formula, str):
            continue
        try:
            refs[name] = set(referenced(formula))
        except FormulaError:
            continue
    return refs


def _check_cycles(result: ValidationResult, refs: dict[str, set[str]]) -> None:
    state: dict[str, int] = {}

    def visit(name: str, stack: list[str]) -> None:
        current = state.get(name, 0)
        if current == 2:
            return
        if current == 1:
            cycle = " -> ".join([*stack[stack.index(name) :], name])
            result.error(f"derived.{name}", f"派生列存在循环引用：{cycle}")
            return
        state[name] = 1
        for ref in sorted(refs.get(name, ())):
            if ref in refs:
                visit(ref, [*stack, name])
        state[name] = 2

    for name in refs:
        visit(name, [])


def _check_derived(
    result: ValidationResult, data: dict, production: bool, base: set[str] | None
) -> None:
    derived = data.get("derived")
    if derived is None:
        return
    if not isinstance(derived, dict):
        result.error("derived", "derived 必须是对象")
        return
    for name, entry in derived.items():
        path = f"derived.{name}"
        if not _COLUMN_RE.match(name):
            result.error(path, f"列名必须是 1-2 个大写字母，收到 {name!r}")
            continue
        if not isinstance(entry, dict):
            result.error(path, "派生列规则必须是对象")
            continue
        _check_unknown_keys(result, path, entry, _DERIVED_KEYS)
        _require_source(result, path, entry, production)
        _check_limit(result, path, entry)
        formula = entry.get("formula")
        if not isinstance(formula, str) or not formula.strip():
            result.error(f"{path}.formula", "派生列必须提供 formula 字符串")
            continue
        try:
            parse(formula)
        except FormulaError as exc:
            result.error(f"{path}.formula", f"公式不合法：{exc}")
            continue
        if base is not None:
            for ref in sorted(referenced(formula)):
                if ref not in base and ref not in derived:
                    result.error(
                        f"{path}.formula",
                        f"引用了未声明的列 {ref}（不在 base_columns 或 derived 中）",
                    )
    _check_cycles(result, _derived_references(derived))


def _check_action_section(
    result: ValidationResult, data: dict, key: str, production: bool
) -> None:
    """校验 missing_data 这类"自由键 + 动作"的段。"""
    section = data.get(key)
    if section is None:
        return
    if not isinstance(section, dict):
        result.error(key, f"{key} 必须是对象")
        return
    for name, entry in section.items():
        path = f"{key}.{name}"
        if not isinstance(entry, dict):
            result.error(path, "规则必须是对象")
            continue
        _check_unknown_keys(result, path, entry, _ACTION_KEYS)
        _require_source(result, path, entry, production)
        _check_mode(result, path, entry.get("mode"))
        action = entry.get("action")
        if not isinstance(action, str) or not action.strip():
            result.error(f"{path}.action", "必须提供 action 字符串")
        params = entry.get("params")
        if params is not None and not isinstance(params, dict):
            result.error(f"{path}.params", "params 必须是对象")


def _check_repair(
    result: ValidationResult, data: dict, production: bool, base: set[str] | None
) -> None:
    """校验 repair 段：**以被修改的列字母为键**，动作必须在白名单内。"""
    repair = data.get("repair")
    if repair is None:
        return
    if not isinstance(repair, dict):
        result.error("repair", "repair 必须是对象")
        return
    derived = data.get("derived") if isinstance(data.get("derived"), dict) else {}
    for name, entry in repair.items():
        path = f"repair.{name}"
        if not _COLUMN_RE.match(name):
            result.error(
                path, f"repair 的键必须是被修改列的字母（如 I、T），收到 {name!r}"
            )
            continue
        if base is not None and name not in base and name not in derived:
            result.error(path, f"列 {name} 既不在 base_columns 也不在 derived 中，无法执行修复")
        if not isinstance(entry, dict):
            result.error(path, "修复规则必须是对象")
            continue
        _check_unknown_keys(result, path, entry, _ACTION_KEYS)
        _require_source(result, path, entry, production)

        action = entry.get("action")
        if action not in REPAIR_ACTIONS:
            result.error(
                f"{path}.action",
                f"action 必须是 {sorted(REPAIR_ACTIONS)} 之一，收到 {action!r}",
            )
            continue
        # reject 不产生任何修改，模式无意义；其余动作必须写明执行模式
        if action == "reject":
            if "mode" in entry:
                _check_mode(result, path, entry.get("mode"))
        else:
            _check_mode(result, path, entry.get("mode"))

        params = entry.get("params")
        if params is not None and not isinstance(params, dict):
            result.error(f"{path}.params", "params 必须是对象")
            params = None
        if action == "clip":
            target = (params or {}).get("to")
            if not _is_number(target):
                result.error(
                    f"{path}.params.to", "clip 动作必须提供数值型 params.to（截断到该值）"
                )
        if action == "keep_and_flag" and (params or {}).get("note") is not None:
            if not isinstance((params or {}).get("note"), str):
                result.error(f"{path}.params.note", "keep_and_flag 的 params.note 必须是字符串")
        target_column = (params or {}).get("target")
        if target_column is not None:
            if not isinstance(target_column, str) or not _COLUMN_RE.match(target_column):
                result.error(
                    f"{path}.params.target",
                    f"params.target 必须是列字母（要改写的原始测点），收到 {target_column!r}",
                )
            elif base is not None and target_column not in base:
                result.error(
                    f"{path}.params.target",
                    f"params.target 指向的列 {target_column} 不在 base_columns 中",
                )


def _check_writeback(result: ValidationResult, data: dict) -> None:
    writeback = data.get("writeback")
    if writeback is None:
        return
    if not isinstance(writeback, dict):
        result.error("writeback", "writeback 必须是对象")
        return
    _check_unknown_keys(result, "writeback", writeback, _WRITEBACK_KEYS)

    column_range = writeback.get("range")
    if column_range is not None:
        if not isinstance(column_range, str) or not _RANGE_RE.match(column_range):
            result.error("writeback.range", f"range 形如 'A:O'，收到 {column_range!r}")
        else:
            match = _RANGE_RE.match(column_range)
            assert match is not None
            if _column_index(match.group(1)) > _column_index(match.group(2)):
                result.error("writeback.range", f"range 起点在终点之后：{column_range!r}")

    encoding = writeback.get("encoding")
    if encoding is not None and encoding not in ENCODINGS:
        result.error(
            "writeback.encoding", f"encoding 必须是 {sorted(ENCODINGS)} 之一，收到 {encoding!r}"
        )

    preserve = writeback.get("preserve_columns")
    if preserve is not None and (
        not isinstance(preserve, list) or not all(isinstance(x, str) for x in preserve)
    ):
        result.error("writeback.preserve_columns", "preserve_columns 必须是字符串数组")

    if "backup" in writeback and not isinstance(writeback["backup"], bool):
        result.error("writeback.backup", "backup 必须是布尔值")

    overwrite = writeback.get("overwrite")
    if overwrite is not None:
        if not isinstance(overwrite, dict):
            result.error("writeback.overwrite", "overwrite 必须是对象")
        elif overwrite.get("mode") != "attended":
            result.error(
                "writeback.overwrite.mode",
                "覆盖原始数据必须为 'attended'（每次人工确认），不允许静默覆盖",
            )


def _check_unmatched(result: ValidationResult, data: dict) -> None:
    unmatched = data.get("unmatched")
    if unmatched is None:
        result.error("unmatched", "缺少 unmatched：必须显式声明未覆盖情况的处理方式")
        return
    if not isinstance(unmatched, dict):
        result.error("unmatched", "unmatched 必须是对象")
        return
    _check_unknown_keys(result, "unmatched", unmatched, {"mode", "description"})
    if unmatched.get("mode") != "halt":
        result.error(
            "unmatched.mode",
            "unmatched.mode 必须是 'halt'：规则未覆盖时必须停机，严禁猜测",
        )


def validate(data: Any, *, production: bool = True) -> ValidationResult:
    """校验规则数据。``production=True`` 时要求所有规则都带 source。"""
    result = ValidationResult()
    if not isinstance(data, dict):
        result.error("<root>", "规则文件顶层必须是对象")
        return result

    _check_unknown_keys(result, "<root>", data, _TOP_KEYS)

    version = data.get("version")
    if version is None:
        result.error("version", "缺少 version")
    elif not isinstance(version, int) or isinstance(version, bool) or version < 1:
        result.error("version", f"version 必须是不小于 1 的整数，收到 {version!r}")

    updated_at = data.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        result.error("updated_at", "updated_at 必须是字符串")

    base = _check_base_columns(result, data)
    _check_columns(result, data, production)
    _check_derived(result, data, production, base)
    _check_repair(result, data, production, base)
    _check_action_section(result, data, "missing_data", production)
    _check_writeback(result, data)
    _check_unmatched(result, data)
    return result


def load_rules(
    path: str | Path, *, production: bool = True
) -> tuple[dict | None, ValidationResult]:
    """读取并校验规则文件；JSON 解析失败时返回 ``(None, 结果)``。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        result = ValidationResult()
        result.error("<file>", f"JSON 解析失败：{exc}")
        return None, result
    result = validate(data, production=production)
    return data, result
