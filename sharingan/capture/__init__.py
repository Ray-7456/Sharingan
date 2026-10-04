"""采集层：把"人做一遍工作"的过程录下来（屏幕帧 + 键鼠事件 + 窗口上下文）。

这一层是平台中立的调度与格式，真实的屏幕抓取与输入钩子在
``sharingan/platforms/`` 里（见 docs/capture.md）。
"""

from .dedup import difference, fingerprint
from .png import bgra_to_rgb_rows, decode_png, encode_rgb, write_rgb
from .recorder import Recorder, RecorderOptions, RecorderStats, is_sensitive_title
from .session import CaptureSession
from .sources import (
    FOCUS,
    KEY_DOWN,
    KEY_MASKED,
    KEY_UP,
    MOUSE_DOWN,
    MOUSE_MOVE,
    MOUSE_UP,
    MOUSE_WHEEL,
    EventHook,
    FrameSource,
    InputEvent,
    SyntheticEventHook,
    SyntheticFrameSource,
    WindowInfo,
)

__all__ = [
    "FOCUS",
    "KEY_DOWN",
    "KEY_MASKED",
    "KEY_UP",
    "MOUSE_DOWN",
    "MOUSE_MOVE",
    "MOUSE_UP",
    "MOUSE_WHEEL",
    "CaptureSession",
    "EventHook",
    "FrameSource",
    "InputEvent",
    "Recorder",
    "RecorderOptions",
    "RecorderStats",
    "SyntheticEventHook",
    "SyntheticFrameSource",
    "WindowInfo",
    "bgra_to_rgb_rows",
    "decode_png",
    "difference",
    "encode_rgb",
    "fingerprint",
    "is_sensitive_title",
    "write_rgb",
]
