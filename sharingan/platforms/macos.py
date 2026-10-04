"""macOS 采集适配器——**尚未实现**。

这里写下实现要点，免得以后再从头调研（与 Windows 侧保持同一套接口）：

- **屏幕**：``ScreenCaptureKit``（macOS 12.3+，推荐）或 ``CGDisplayStream``；
  需要用户在「系统设置 → 隐私与安全性 → 屏幕录制」里授权，未授权时系统会弹窗，
  API 返回空帧而不是报错，实现时要把"空帧"识别成"未授权"并给出明确提示。
- **输入事件**：``CGEventTap``（Quartz Event Services），监听
  ``kCGEventLeftMouseDown`` / ``kCGEventKeyDown`` 等；需要「辅助功能」权限。
  ``CGEventTap`` 的回调同样要求快速返回，与 Windows 钩子一致。
- **前台窗口**：``NSWorkspace.sharedWorkspace().frontmostApplication()`` 取进程名，
  ``CGWindowListCopyWindowInfo`` 取窗口标题。
- **坐标**：Quartz 的坐标原点在左下角，而采集帧通常自上而下，需要翻转 y。

在实现之前，下面的工厂函数会抛出 ``NotImplementedError``，消息里写清缺什么，
让上层（CLI / 界面）能直接把它展示给用户，而不是抛一个看不懂的栈。
"""

from __future__ import annotations

from ..capture.sources import InputEvent, WindowInfo

_HINT = (
    "macOS 采集适配器尚未实现。需要的系统接口："
    "屏幕用 ScreenCaptureKit（需「屏幕录制」权限）、"
    "输入用 CGEventTap（需「辅助功能」权限）、"
    "前台窗口用 NSWorkspace / CGWindowList。实现位置：sharingan/platforms/macos.py"
)


def screen_source(region: tuple[int, int, int, int] | None = None):
    raise NotImplementedError(_HINT)


def event_hook(*, record_moves: bool = False):
    raise NotImplementedError(_HINT)


def active_window() -> WindowInfo | None:
    return None
