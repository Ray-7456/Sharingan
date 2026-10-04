"""平台适配器注册表：采集层只通过这里拿当前平台的实现。

约定（见 docs/design.md §8）：**只有这个包允许出现平台专用代码**，其余模块保持
平台中立，由 ``tests/test_platform_neutrality.py`` 守线。

各平台实现状态：

============  ==========================================  ====================
平台          屏幕 / 输入 / 前台窗口                          状态
============  ==========================================  ====================
Windows       GDI BitBlt / 底层钩子 / GetForegroundWindow  已实现
macOS         ScreenCaptureKit / CGEventTap / NSWorkspace   待实现（见 macos.py）
Linux         X11 或 Portal / evdev 或 XTEST                待实现（见 linux.py）
============  ==========================================  ====================
"""

from __future__ import annotations

import platform as _platform

from ..capture.sources import EventHook, FrameSource, WindowInfo

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"
UNKNOWN = "unknown"


def current_platform() -> str:
    system = _platform.system()
    if system == "Windows":
        return WINDOWS
    if system == "Darwin":
        return MACOS
    if system == "Linux":
        return LINUX
    return UNKNOWN


def screen_source(region: tuple[int, int, int, int] | None = None) -> FrameSource:
    """按平台返回屏幕帧来源；未实现或未知平台时抛出带说明的错误。"""
    name = current_platform()
    if name == WINDOWS:
        from .windows import WindowsScreenSource

        return WindowsScreenSource(region)
    if name == MACOS:
        from .macos import screen_source as implement

        return implement(region)
    if name == LINUX:
        from .linux import screen_source as implement

        return implement(region)
    raise NotImplementedError(
        f"不支持的平台 {_platform.system()!r}：采集层目前只实现了 Windows，"
        "macOS 与 Linux 的适配器接口已留好（sharingan/platforms/）"
    )


def event_hook(*, record_moves: bool = False) -> EventHook:
    """按平台返回输入事件钩子。"""
    name = current_platform()
    if name == WINDOWS:
        from .windows import WindowsEventHook

        return WindowsEventHook(record_moves=record_moves)
    if name == MACOS:
        from .macos import event_hook as implement

        return implement(record_moves=record_moves)
    if name == LINUX:
        from .linux import event_hook as implement

        return implement(record_moves=record_moves)
    raise NotImplementedError(f"不支持的平台 {_platform.system()!r}")


def active_window() -> WindowInfo | None:
    """前台窗口信息；未实现的平台返回 None（调用方按"无窗口上下文"处理）。"""
    name = current_platform()
    if name == WINDOWS:
        from .windows import active_window as implement

        return implement()
    if name == MACOS:
        from .macos import active_window as implement

        return implement()
    if name == LINUX:
        from .linux import active_window as implement

        return implement()
    return None


def describe() -> str:
    """一句话说明当前平台的采集能力，供 CLI 提示。"""
    name = current_platform()
    if name == WINDOWS:
        return "Windows：屏幕（GDI）、键鼠（底层钩子）、前台窗口 均已实现"
    if name == MACOS:
        return "macOS：适配器尚未实现（需要「屏幕录制」与「辅助功能」权限，见 platforms/macos.py）"
    if name == LINUX:
        return "Linux：适配器尚未实现（X11 直接可用，Wayland 需要 Portal，见 platforms/linux.py）"
    return f"未知平台：{_platform.system()}（采集不可用）"
