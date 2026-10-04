"""采集层的接口定义与合成源（测试用）。

真实屏幕与输入钩子在各平台的适配器里（``sharingan/platforms/``），
采集调度只依赖这里的两个协议：

- :class:`FrameSource`：给出一帧屏幕的 BGRA 原始字节；
- :class:`EventHook`：把鼠标键盘事件回调出来（Windows 需要消息泵，因此有 ``pump``）。

合成源让整条采集管道可以在没有显示器、没有真实输入的环境里被测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

# 事件类型（平台适配器要把系统消息映射到这些名字）
MOUSE_DOWN = "mouse_down"
MOUSE_UP = "mouse_up"
MOUSE_MOVE = "mouse_move"
MOUSE_WHEEL = "mouse_wheel"
KEY_DOWN = "key_down"
KEY_UP = "key_up"
FOCUS = "focus"
KEY_MASKED = "key_masked"  # 敏感窗口里只记次数，不记具体按键


@dataclass(frozen=True)
class InputEvent:
    """一次输入事件；坐标是屏幕绝对坐标，时间戳为会话相对秒数。"""

    ts: float
    kind: str
    x: int | None = None
    y: int | None = None
    button: str | None = None
    key: str | None = None
    delta: int | None = None
    window: str | None = None

    @property
    def is_significant(self) -> bool:
        """是否值得为它抓一帧（移动与按键抬起不值得）。"""
        return self.kind in (MOUSE_DOWN, MOUSE_UP, MOUSE_WHEEL, KEY_DOWN, FOCUS)


@dataclass(frozen=True)
class WindowInfo:
    title: str
    process: str = ""

    def label(self) -> str:
        return f"{self.process}｜{self.title}" if self.process else self.title


@runtime_checkable
class FrameSource(Protocol):
    """屏幕帧来源。"""

    def size(self) -> tuple[int, int]:
        """返回 (宽, 高)，单位为像素。"""
        ...

    def grab(self) -> bytes:
        """抓一帧，返回 BGRA 字节（每行 宽×4 字节）。"""
        ...


@runtime_checkable
class EventHook(Protocol):
    """输入事件来源。"""

    def start(self, on_event: Callable[[InputEvent], None]) -> None:
        """开始监听，事件通过回调送出（回调必须极快返回）。"""
        ...

    def pump(self, timeout: float) -> None:
        """处理一次系统消息（Windows 的底层钩子需要消息泵）。"""
        ...

    def stop(self) -> None:
        """停止监听并释放资源。"""
        ...


# ---------------- 合成源（测试与演示） ----------------


@dataclass
class SyntheticFrameSource:
    """可编程的假屏幕：按脚本改变画面，用于验证抓帧与去重策略。"""

    width: int = 64
    height: int = 48
    base: int = 40
    _frame_index: int = field(default=0, init=False)
    changes: dict[int, int] = field(default_factory=dict)  # 第 N 帧起整体亮度变化

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def grab(self) -> bytes:
        self._frame_index += 1
        level = self.base
        for start, delta in sorted(self.changes.items()):
            if self._frame_index >= start:
                level = self.base + delta
        pixel = bytes([level, level, level, 255])  # B G R A
        return pixel * (self.width * self.height)

    @property
    def grab_count(self) -> int:
        return self._frame_index


@dataclass
class SyntheticEventHook:
    """按脚本"发生"输入事件的假钩子。"""

    events: list[InputEvent] = field(default_factory=list)
    _callback: Callable[[InputEvent], None] | None = None
    _cursor: int = 0

    def start(self, on_event: Callable[[InputEvent], None]) -> None:
        self._callback = on_event
        self._cursor = 0

    def pump(self, timeout: float) -> None:
        """每次泵出一个待发事件（模拟"这一刻发生了一个动作"）。"""
        if self._callback is None or self._cursor >= len(self.events):
            return
        event = self.events[self._cursor]
        self._cursor += 1
        self._callback(event)

    def stop(self) -> None:
        self._callback = None
