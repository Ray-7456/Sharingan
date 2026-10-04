"""分析模板（xlsx）的自动解析与核对。

模板里同一个参数写在两处：**表头文字**（如「发电机轴承A温度(80)」）与
**条件格式规则**。真实的模板里这两处并不总是一致（例如同一参数表头写 20、条件格式写 25），
因此本模块把两个来源分别提取出来，交叉核对，冲突一律报出来交人裁决，而不是
自己挑一个用。

需要 ``openpyxl``（可选依赖）：``pip install "sharingan[workbook]"``。
"""

# 注意：模块名就叫 compare，这里显式把它导出的同名函数绑到包命名空间上，
# 否则 `from sharingan.workbook import compare` 拿到的是子模块而不是函数。
from .compare import Finding, compare, compare_with_rules, template_findings
from .reader import (
    OPENPYXL_AVAILABLE,
    ConditionalRule,
    TemplateColumn,
    TemplateProfile,
    read_template,
)

__all__ = [
    "OPENPYXL_AVAILABLE",
    "ConditionalRule",
    "Finding",
    "TemplateColumn",
    "TemplateProfile",
    "compare_with_rules",
    "read_template",
    "template_findings",
]
