"""Linux 采集适配器——**尚未实现**。

实现要点（X11 与 Wayland 差别很大，别把它们当成一回事）：

- **屏幕**：
  - X11：``XGetImage``（python-xlib）或 ``XShmGetImage`` 抓根窗口；
  - Wayland：没有全局抓屏 API，必须走 ``xdg-desktop-portal`` 的
    ``org.freedesktop.portal.ScreenCast``（用户每次会话都要确认），
    GNOME/KDE 的实现细节还不一致；
  - 折中方案：调用 ``grim``（Wayland）或 ``import``/``scrot``（X11）等外部命令，
    把"平台差异"限制在一个可替换的探针里。
- **输入事件**：X11 用 ``XRecord``（推荐，不干扰事件流）或 XTEST；
  Wayland 下普通应用**读不到全局按键**，只有 ``libinput`` + ``evdev`` 直接读
  ``/dev/input/event*``（需要把用户加入 ``input`` 组或以 root 运行），
  这一条要在界面里明确告知用户。
- **前台窗口**：X11 用 ``_NET_ACTIVE_WINDOW``；Wayland 下需要通过各桌面环境的
  D-Bus 接口，实现不一致。

结论：Linux 先支持 X11（可直接实现），Wayland 只做"能力探测 + 明确告知不支持"。
"""

from __future__ import annotations

from ..capture.sources import InputEvent, WindowInfo

_HINT = (
    "Linux 采集适配器尚未实现。计划：X11 使用 XRecord + XGetImage（可直接实现）；"
    "Wayland 需要 xdg-desktop-portal 的 ScreenCast（屏幕）与 evdev 直接读 /dev/input（输入），"
    "且普通用户读全局按键需要加入 input 组。实现位置：sharingan/platforms/linux.py"
)


def screen_source(region: tuple[int, int, int, int] | None = None):
    raise NotImplementedError(_HINT)


def event_hook(*, record_moves: bool = False):
    raise NotImplementedError(_HINT)


def active_window() -> WindowInfo | None:
    return None
