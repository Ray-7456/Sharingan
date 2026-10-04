"""与界面无关的分析层：分析器与生成器都将建立在这一层之上。"""

from .repair import (
    ApplyResult,
    DryRun,
    RepairAction,
    apply_repairs,
    plan_repairs,
)
from .summary import (
    ColumnStat,
    FileFacts,
    Gap,
    NoteStat,
    Summary,
    build_summary,
    column_letter,
    data_column_letter,
    row_values,
)

__all__ = [
    "ApplyResult",
    "ColumnStat",
    "DryRun",
    "FileFacts",
    "Gap",
    "NoteStat",
    "RepairAction",
    "Summary",
    "apply_repairs",
    "build_summary",
    "column_letter",
    "data_column_letter",
    "plan_repairs",
    "row_values",
]
