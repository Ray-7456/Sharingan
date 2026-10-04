"""平台中立性护栏：核心代码不得出现 Windows/单一平台专用写法。

项目要求 Windows / macOS / Linux 三平台可通用（见 docs/design.md §7）。
平台相关的代码只能出现在采集层适配器里，核心模块必须保持纯标准库实现。

这个测试通过源码扫描防止无意间的平台耦合回潮。
"""

import re
import unittest
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent / "sharingan"

# 允许的采集层适配器目录（当前尚未创建，先留出口）
ADAPTER_MARKERS = ("platforms", "capture")

FORBIDDEN_PATTERNS = {
    r"\bimport\s+winreg\b": "Windows 注册表",
    r"\bimport\s+msvcrt\b": "Windows 专用控制台",
    r"\bimport\s+win32\w*": "pywin32",
    r"ctypes\.windll": "Windows API 调用",
    r"\bos\.startfile\b": "Windows 专用打开文件",
    r"shell\s*=\s*True": "shell=True（跨平台行为不一致）",
    r"['\"][A-Za-z]:[\\/]": "写死的盘符路径",
    r"\\\\\?\\": "Windows 扩展路径前缀",
    r"\bC:\\\\": "写死的 C 盘路径",
}


def iter_core_sources():
    for path in CORE_ROOT.rglob("*.py"):
        if any(marker in path.parts for marker in ADAPTER_MARKERS):
            continue
        yield path


class PlatformNeutralityTest(unittest.TestCase):
    def test_core_sources_exist(self):
        self.assertGreater(len(list(iter_core_sources())), 5)

    def test_no_platform_specific_constructs(self):
        offenders = []
        for path in iter_core_sources():
            text = path.read_text(encoding="utf-8")
            for pattern, description in FORBIDDEN_PATTERNS.items():
                for match in re.finditer(pattern, text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(
                        f"{path.relative_to(CORE_ROOT.parent)}:{line} "
                        f"命中 {description}（{match.group(0)!r}）"
                    )
        self.assertEqual(offenders, [], "核心模块出现平台耦合：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
