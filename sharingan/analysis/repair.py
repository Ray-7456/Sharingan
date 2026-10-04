"""变更计划（dry-run）与执行写入。

设计纪律（见 docs/design.md）：

- **先算后写**：:func:`plan_repairs` 只产出"该改哪个单元格、改成什么、依据哪条规则"，
  绝不碰文件；确认后才由 :func:`apply_repairs` 备份并写盘；
- **不猜**：某列出现超限但规则里没有对应的修复条目时，按 ``unmatched: halt`` 停机，
  而不静默跳过；派生列超限若没指定要改哪个原始测点，同样记为待人工判断；
- **可追溯**：每处修改都带触发规则与规则来源，执行时落成变更日志。

修复规则写在 ``rules.json`` 的 ``repair`` 段，**以被修改的列字母为键**：

.. code-block:: json

    "repair": {
      "I": { "action": "clip", "params": { "to": 55 }, "mode": "confirm-once",
             "source": "澄清#3：轴承B温度不应超过 55℃" },
      "T": { "action": "clip", "params": { "target": "I", "to": 55 },
             "mode": "confirm-once", "source": "以派生指标 T 超限为触发，改原始测点 I" }
    }

支持的动作：

===============  ==========================================================
clip             截断：新值 = min(旧值, params.to)
interpolate      用前后最近的未超限点按时间线性插值
keep_and_flag    不改数值，把 params.note（默认「超限」）追加到备注列
reject           明确决定"不改，交人工判断"——只登记，不算遗漏
===============  ==========================================================
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..formats import TimeSeriesFile, format_number
from ..rules import FormulaError, evaluate
from .summary import data_column_letter, row_values

DEFAULT_FLAG_NOTE = "超限"


@dataclass(frozen=True)
class RepairAction:
    """一处待执行的修改。"""

    row_index: int
    field_index: int  # 数值列下标；-1 表示备注列
    column_letter: str
    column_name: str
    time_text: str
    before: str
    after: str
    value_before: float | None
    value_after: float | None
    rule_key: str
    rule_source: str
    reason: str

    def describe(self) -> str:
        return f"{self.time_text} {self.column_letter} {self.before} → {self.after}（{self.reason}）"


@dataclass
class DryRun:
    """一次 dry-run 的结果；只有 ``halted=False`` 才允许执行写入。"""

    actions: list[RepairAction] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    halted: bool = False
    halt_reasons: list[str] = field(default_factory=list)
    rules_version: int | None = None

    @property
    def action_count(self) -> int:
        return len(self.actions)

    def counts_by_column(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.column_letter] = counts.get(action.column_letter, 0) + 1
        return counts

    def summary(self) -> str:
        if self.halted:
            return "已停机：" + "；".join(self.halt_reasons)
        if not self.actions:
            return "没有需要修改的单元格。"
        parts = [f"{col} 列 {count} 处" for col, count in sorted(self.counts_by_column().items())]
        text = f"共 {self.action_count} 处修改：" + "，".join(parts)
        if self.skipped:
            text += f"；另有 {len(self.skipped)} 处需人工判断"
        return text


@dataclass
class ApplyResult:
    written_path: str
    backup_path: str | None
    change_count: int
    log_path: str


def _letter_to_index(letter: str) -> int:
    """列字母 → 数值列下标（数据文件 A 列是时间，故 B → 0）。"""
    index = 0
    for char in letter.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 2


def _column_limits(rules_data: dict) -> dict[str, float]:
    return {
        letter: spec["limit"]
        for letter, spec in (rules_data.get("columns") or {}).items()
        if isinstance(spec, dict) and spec.get("limit") is not None
    }


def _derived_rules(rules_data: dict) -> dict[str, dict]:
    return {
        letter: spec
        for letter, spec in (rules_data.get("derived") or {}).items()
        if isinstance(spec, dict) and spec.get("formula") and spec.get("limit") is not None
    }


def _interpolate(
    data_file: TimeSeriesFile, field_index: int, row_index: int
) -> float | None:
    """用前后最近的可用点按时间线性插值；任一侧取不到就返回 None（不猜）。"""
    rows = data_file.rows
    target_time = rows[row_index].time
    if target_time is None:
        return None

    def find(step: int) -> tuple[float, float] | None:
        index = row_index + step
        while 0 <= index < len(rows):
            value = rows[index].value(field_index)
            moment = rows[index].time
            if value is not None and moment is not None:
                return (moment.timestamp(), value)
            index += step
        return None

    before = find(-1)
    after = find(1)
    if before is None or after is None:
        return None
    span = after[0] - before[0]
    if span <= 0:
        return None
    ratio = (target_time.timestamp() - before[0]) / span
    return before[1] + (after[1] - before[1]) * ratio


def _plan_for_column(
    run: DryRun,
    data_file: TimeSeriesFile,
    rules_data: dict,
    *,
    column_letter: str,
    trigger_indices: list[int],
    rule: dict,
    field_index: int,
    note_allowed: bool,
    rule_key: str | None = None,
) -> None:
    """把某列的触发点按规则转换成修改项（或登记为需人工判断）。

    ``column_letter`` 是要改写的列；``rule_key`` 是触发这条修复的规则键——
    派生列超限时两者不同（例如 T 列超限、改写 I 列），日志要记录后者。
    """
    action = rule.get("action")
    params = rule.get("params") or {}
    source = rule.get("source", "")
    column_name = data_file.columns[field_index]
    triggered_by = rule_key or column_letter

    if action == "reject":
        run.skipped.append(f"{column_letter} 列 {len(trigger_indices)} 处触发，规则明确不改（人工判断）")
        return

    if action == "keep_and_flag":
        if not note_allowed:
            run.skipped.append(
                f"{column_letter} 列 {len(trigger_indices)} 处触发，但备注列被声明为保留列"
                "（writeback.preserve_columns），无法标注"
            )
            return
        flag = str(params.get("note", DEFAULT_FLAG_NOTE))
        for row_index in trigger_indices:
            row = data_file.rows[row_index]
            if flag in row.note:
                continue
            new_note = f"{row.note}；{flag}" if row.note else flag
            run.actions.append(
                RepairAction(
                    row_index=row_index,
                    field_index=-1,
                    column_letter=data_file.note_column,
                    column_name=data_file.note_column,
                    time_text=row.time_text,
                    before=row.note,
                    after=new_note,
                    value_before=None,
                    value_after=None,
                    rule_key=triggered_by,
                    rule_source=source,
                    reason=f"按规则标注「{flag}」，不改数值",
                )
            )
        return

    if action not in ("clip", "interpolate"):
        run.skipped.append(f"{column_letter} 列触发但动作 {action!r} 无法执行，需人工判断")
        return

    for row_index in trigger_indices:
        row = data_file.rows[row_index]
        before_value = row.value(field_index)
        if before_value is None:
            run.skipped.append(f"{row.time_text} {column_letter} 列为空值，跳过")
            continue
        if action == "clip":
            limit_to = params.get("to")
            if not isinstance(limit_to, (int, float)):
                run.skipped.append(f"{column_letter} 列的 clip 缺少数值型 params.to，需人工判断")
                continue
            new_value = min(before_value, float(limit_to))
            reason = f"截断到 {format_number(limit_to)}"
            if new_value == before_value:
                run.skipped.append(
                    f"{row.time_text} {column_letter} 列 {format_number(before_value)} "
                    f"未超过截断值 {format_number(limit_to)}，无需修改"
                )
                continue
        else:
            new_value = _interpolate(data_file, field_index, row_index)
            if new_value is None:
                run.skipped.append(
                    f"{row.time_text} {column_letter} 列 {format_number(before_value)} "
                    "前后缺少可用于插值的点，需人工判断"
                )
                continue
            reason = "按前后点线性插值"

        run.actions.append(
            RepairAction(
                row_index=row_index,
                field_index=field_index,
                column_letter=column_letter,
                column_name=column_name,
                time_text=row.time_text,
                before=row.values[field_index],
                after=format_number(round(new_value, 6)),
                value_before=before_value,
                value_after=round(new_value, 6),
                rule_key=triggered_by,
                rule_source=source,
                reason=reason,
            )
        )


def plan_repairs(data_file: TimeSeriesFile, rules_data: dict) -> DryRun:
    """计算变更计划，不修改任何文件。"""
    run = DryRun(rules_version=rules_data.get("version"))
    repair_rules: dict[str, dict] = rules_data.get("repair") or {}
    preserve = set((rules_data.get("writeback") or {}).get("preserve_columns") or [])
    note_allowed = data_file.note_column not in preserve

    # ---- 原始列超限 ----
    for letter, limit in _column_limits(rules_data).items():
        field_index = _letter_to_index(letter)
        if not 0 <= field_index < len(data_file.columns):
            continue
        triggers = [
            row_index
            for row_index, row in enumerate(data_file.rows)
            if (value := row.value(field_index)) is not None and value >= limit
        ]
        if not triggers:
            continue
        rule = repair_rules.get(letter)
        if rule is None:
            run.halted = True
            run.halt_reasons.append(
                f"{letter} 列（{data_file.columns[field_index]}）有 {len(triggers)} 处超限，"
                "但 repair 段没有对应规则（unmatched=halt）"
            )
            continue
        _plan_for_column(
            run,
            data_file,
            rules_data,
            column_letter=letter,
            trigger_indices=triggers,
            rule=rule,
            field_index=field_index,
            note_allowed=note_allowed,
        )

    # ---- 派生列超限 ----
    for letter, spec in _derived_rules(rules_data).items():
        formula = spec["formula"]
        limit = float(spec["limit"])
        try:
            series = [
                evaluate(formula, row_values(row, data_file.columns))
                for row in data_file.rows
            ]
        except FormulaError as exc:
            run.halted = True
            run.halt_reasons.append(f"派生列 {letter} 公式求值失败：{exc}")
            continue
        triggers = [
            index
            for index, value in enumerate(series)
            if value is not None and value >= limit
        ]
        if not triggers:
            continue
        rule = repair_rules.get(letter)
        if rule is None:
            run.halted = True
            run.halt_reasons.append(
                f"派生列 {letter}（{formula}）有 {len(triggers)} 处超限，"
                "但 repair 段没有对应规则（unmatched=halt）"
            )
            continue
        target = (rule.get("params") or {}).get("target")
        target_index = _letter_to_index(str(target)) if target else -1
        if not target or not 0 <= target_index < len(data_file.columns):
            run.skipped.append(
                f"派生列 {letter} 有 {len(triggers)} 处超限，但规则未指定要修改哪个原始测点"
                "（params.target），需人工判断"
            )
            continue
        _plan_for_column(
            run,
            data_file,
            rules_data,
            column_letter=str(target).upper(),
            trigger_indices=triggers,
            rule=rule,
            field_index=target_index,
            note_allowed=note_allowed,
            rule_key=letter,
        )

    return run


def apply_repairs(
    data_file: TimeSeriesFile,
    run: DryRun,
    target_path: str | Path,
    *,
    backup: bool = True,
    log_dir: str | Path | None = None,
) -> ApplyResult:
    """执行写入：先备份，再改文件，最后落变更日志。

    ``run.halted`` 为真时拒绝写盘——这是"不猜"纪律的最后一环。
    """
    if run.halted:
        raise RuntimeError("变更计划已标记停机（存在未覆盖情况），拒绝写盘：" + "；".join(run.halt_reasons))

    target = Path(target_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    backup_path: Path | None = None
    if backup and target.exists():
        backup_path = target.with_name(f"{target.stem}.bak-{timestamp}{target.suffix}")
        shutil.copy2(target, backup_path)

    for action in run.actions:
        row = data_file.rows[action.row_index]
        if action.field_index == -1:
            row.set_note(action.after)
        else:
            row.set_value(action.field_index, action.value_after)

    data_file.write(target)

    log_directory = Path(log_dir) if log_dir else target.parent
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"{target.stem}.changes-{timestamp}.json"
    log_path.write_text(
        json.dumps(
            {
                "target": str(target),
                "backup": str(backup_path) if backup_path else None,
                "applied_at": timestamp,
                "rules_version": run.rules_version,
                "change_count": run.action_count,
                "changes": [
                    {
                        "row_index": action.row_index,
                        "time": action.time_text,
                        "column": action.column_letter,
                        "name": action.column_name,
                        "before": action.before,
                        "after": action.after,
                        "rule": action.rule_key,
                        "rule_source": action.rule_source,
                        "reason": action.reason,
                    }
                    for action in run.actions
                ],
                "skipped": run.skipped,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    return ApplyResult(
        written_path=str(target),
        backup_path=str(backup_path) if backup_path else None,
        change_count=run.action_count,
        log_path=str(log_path),
    )
