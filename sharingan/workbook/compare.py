"""把模板提取结果与 ``rules.json`` 交叉核对，产出需要人看的问题清单。

判断原则：**工具只负责发现不一致，不替人挑一个用**。表头与条件格式冲突、
模板与规则冲突，一律以 ``error`` 报出；可能是有意为之的（多档阈值、硬编码
常量、统计窗口差异）以 ``warn`` 报出；纯提示以 ``info`` 报出。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..formats import format_number
from ..rules.formula import FormulaError, normalize
from .reader import TemplateProfile


@dataclass(frozen=True)
class Finding:
    level: str  # error / warn / info
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.subject}：{self.message}"


def _numbers(values) -> list[str]:
    return [format_number(value) for value in values]


def template_findings(profile: TemplateProfile) -> list[Finding]:
    """不依赖规则的自检：模板内部是否自洽。"""
    findings: list[Finding] = []

    for column in profile.columns:
        if (
            column.header_limit is not None
            and column.cf_limit is not None
            and abs(column.header_limit - column.cf_limit) > 1e-9
        ):
            findings.append(
                Finding(
                    "error",
                    f"{column.letter} 列阈值口径冲突",
                    f"表头写 {format_number(column.header_limit)}，条件格式写 "
                    f"{format_number(column.cf_limit)}——同一参数两处不一致，必须由人裁决后再写进规则",
                )
            )
        if len(column.cf_limits) > 1:
            findings.append(
                Finding(
                    "info",
                    f"{column.letter} 列条件格式",
                    f"存在多档阈值 {_numbers(column.cf_limits)}，"
                    f"本工具取最严的 {format_number(column.cf_limit)} 作为判定边界",
                )
            )
        if column.constants:
            findings.append(
                Finding(
                    "warn",
                    f"{column.letter} 列公式含常量",
                    f"硬编码常量 {_numbers(column.constants)}，请确认是否有意为之："
                    f"{column.raw_expression}",
                )
            )
        if column.raw_expression and column.expression is None:
            findings.append(
                Finding(
                    "warn",
                    f"{column.letter} 列公式",
                    f"超出支持的语法，未纳入自动比对：{column.raw_expression}",
                )
            )

    windows = profile.windows
    if windows:
        most_common, _ = Counter(windows.values()).most_common(1)[0]
        outliers = {
            letter: window for letter, window in windows.items() if window != most_common
        }
        if outliers:
            detail = "，".join(
                f"{letter} 列只统计 {window[0]}:{window[1]}"
                for letter, window in sorted(outliers.items())
            )
            findings.append(
                Finding(
                    "warn",
                    "统计窗口不一致",
                    f"多数列统计第 {most_common[0]}:{most_common[1]} 行，但 {detail}（疑似笔误）",
                )
            )
        widest_end = max(window[1] for window in windows.values())
        if profile.last_data_row and profile.last_data_row > widest_end:
            findings.append(
                Finding(
                    "warn",
                    "统计窗口盖不住数据区",
                    f"数据到第 {profile.last_data_row} 行，而最大值最多统计到第 {widest_end} 行，"
                    "窗口外的数据不参与判定",
                )
            )

    for note in profile.notes:
        findings.append(Finding("info", "模板结构", note))
    return findings


def compare_with_rules(profile: TemplateProfile, rules_data: dict) -> list[Finding]:
    """模板与规则的逐项比对。"""
    findings: list[Finding] = []

    declared = rules_data.get("base_columns")
    if isinstance(declared, list):
        template_base = set(profile.base_letters)
        missing = sorted(template_base - set(declared))
        extra = sorted(set(declared) - template_base)
        if missing:
            findings.append(
                Finding("warn", "base_columns", f"模板中有这些列，但规则未声明：{missing}")
            )
        if extra:
            findings.append(
                Finding("warn", "base_columns", f"规则声明了这些列，但模板中没有：{extra}")
            )

    for section, label in (("columns", "列阈值"), ("derived", "派生列")):
        for letter, spec in (rules_data.get(section) or {}).items():
            if not isinstance(spec, dict):
                continue
            column = profile.column(letter)
            if column is None:
                findings.append(
                    Finding("warn", f"{letter} {label}", "规则里有该列，但模板中没有对应列")
                )
                continue

            if section == "derived":
                if not column.expression:
                    findings.append(
                        Finding(
                            "warn",
                            f"{letter} 派生列",
                            "模板中该列第 4 行不是公式，无法比对公式定义",
                        )
                    )
                else:
                    try:
                        rules_formula = normalize(str(spec.get("formula", "")))
                    except FormulaError as exc:
                        rules_formula = None
                        findings.append(
                            Finding(
                                "error",
                                f"{letter} 派生列公式",
                                f"规则里的公式无法解析（{exc}）：{spec.get('formula')!r}",
                            )
                        )
                    if rules_formula is not None and rules_formula != column.expression:
                        findings.append(
                            Finding(
                                "error",
                                f"{letter} 派生列公式",
                                f"模板为 {column.expression}，规则为 {rules_formula}（公式不一致）",
                            )
                        )

            limit = spec.get("limit")
            if limit is None:
                continue
            template_limit = column.limit
            if template_limit is None:
                findings.append(
                    Finding(
                        "warn",
                        f"{letter} {label}",
                        f"规则设了阈值 {format_number(limit)}，但模板里没有该列的阈值",
                    )
                )
            elif abs(float(template_limit) - float(limit)) > 1e-9:
                source = "表头" if column.header_limit is not None else "条件格式"
                findings.append(
                    Finding(
                        "error",
                        f"{letter} {label}",
                        f"模板（{source}）写 {format_number(template_limit)}，"
                        f"规则写 {format_number(limit)}（不一致）",
                    )
                )
    return findings


def compare(profile: TemplateProfile, rules_data: dict | None = None) -> list[Finding]:
    """一次跑完模板自检与规则比对。"""
    findings = template_findings(profile)
    if rules_data is not None:
        findings.extend(compare_with_rules(profile, rules_data))
    return findings
