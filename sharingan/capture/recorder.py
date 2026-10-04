"""录制调度：把屏幕帧与输入事件合成一个采集会话。

抓帧策略（默认）：

1. **事件门控**——点击、滚轮、按键按下、窗口切换之后抓一帧。这才是"操作"发生
   的地方；上一张存下来的帧天然就是"操作前"的画面，两张合起来就是一次变化；
2. **定时兜底**——每 ``1/fps`` 秒查一次画面，有明显变化才存；
3. **去重**——与上一张已存帧比较指纹，差异小于阈值就丢弃（光标闪烁、时钟跳字
   不该产生新帧）。

隐私：

- ``record_keys=False`` 时完全不记按键；
- 前台窗口标题命中敏感词（登录、密码…）时，按键只记 `key_masked` 数量、不记键名。
  这是**启发式**，不是保证——真正的敏感输入请用 ``--no-keys``。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .dedup import DEFAULT_GRID, fingerprint, difference
from .png import bgra_to_rgb_rows
from .session import CaptureSession
from .sources import (
    FOCUS,
    KEY_DOWN,
    KEY_MASKED,
    MOUSE_MOVE,
    InputEvent,
    WindowInfo,
)

# 标题命中这些词就按敏感窗口处理（中英文都覆盖常见写法）
SENSITIVE_TITLE_HINTS = (
    "登录", "登陆", "密码", "口令", "凭据", "验证码",
    "login", "log in", "sign in", "signin", "password", "credential", "passcode",
)
# 事件门控要看重的类型（移动与抬键不抓帧）
_FRAME_TRIGGERS = {"mouse_down", "mouse_up", "mouse_wheel", "key_down", FOCUS}


def is_sensitive_title(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(hint in title or hint in lowered for hint in SENSITIVE_TITLE_HINTS)


@dataclass
class RecorderOptions:
    fps: float = 2.0
    seconds: float | None = 60.0
    step: int = 1  # >1 表示长宽各取 1/step（最近邻降采样）
    record_keys: bool = True
    record_moves: bool = False  # 记录鼠标移动（默认关，事件量很大）
    frame_on_events: bool = True
    change_threshold: float = 0.02  # 指纹差异超过它才算画面变了
    grid: int = DEFAULT_GRID
    max_frames: int | None = None
    window_poll: float = 0.25


@dataclass
class RecorderStats:
    seconds: float = 0.0
    frames: int = 0
    events: int = 0
    skipped_frames: int = 0
    masked_keys: int = 0
    dropped_moves: int = 0
    output: str = ""

    def summary(self) -> str:
        parts = [
            f"时长 {self.seconds:.1f}s",
            f"帧 {self.frames}",
            f"事件 {self.events}",
            f"去重跳过 {self.skipped_frames}",
        ]
        if self.masked_keys:
            parts.append(f"敏感窗口内隐藏按键 {self.masked_keys}")
        if self.dropped_moves:
            parts.append(f"忽略移动 {self.dropped_moves}")
        return "，".join(parts)


class Recorder:
    """把帧来源与事件钩子合成一个会话。"""

    def __init__(
        self,
        source,
        hook,
        session: CaptureSession,
        options: RecorderOptions | None = None,
        *,
        window_provider: Callable[[], WindowInfo | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.hook = hook
        self.session = session
        self.options = options or RecorderOptions()
        self.window_provider = window_provider
        self._clock = clock

        self._queue: deque[InputEvent] = deque()
        self._last_digest: bytes | None = None
        self._last_periodic = 0.0
        self._last_window_poll = 0.0
        self._current_window: str | None = None
        self._pending_reason: str | None = None
        self.stats = RecorderStats(output=str(session.directory))

    # ---------- 事件入口（可能来自钩子线程，必须极快返回） ----------

    def _on_event(self, event: InputEvent) -> None:
        self._queue.append(event)

    # ---------- 主循环 ----------

    def run(self, *, progress: Callable[[RecorderStats], None] | None = None) -> RecorderStats:
        options = self.options
        width, height = self.source.size()
        self.session.meta.update(
            {
                "source": type(self.source).__name__,
                "hook": type(self.hook).__name__,
                "screen": {"width": width, "height": height, "step": options.step},
                "options": {
                    "fps": options.fps,
                    "seconds": options.seconds,
                    "record_keys": options.record_keys,
                    "frame_on_events": options.frame_on_events,
                    "change_threshold": options.change_threshold,
                },
            }
        )

        started = self._clock()
        self._prime_window()
        self.hook.start(self._on_event)
        try:
            while True:
                elapsed = self._clock() - started
                if options.seconds is not None and elapsed >= options.seconds:
                    break
                if options.max_frames is not None and self.session.frame_count >= options.max_frames:
                    break
                self.hook.pump(0.01)
                self._drain_queue(elapsed)
                self._poll_window(elapsed)
                # 先处理事件门控的抓帧：这样"刚点完那一下"的画面会记在这次点击名下，
                # 而不是被定时抓帧抢先记成 tick
                if self._pending_reason is not None and options.frame_on_events:
                    reason = self._pending_reason
                    self._pending_reason = None
                    if self._capture(elapsed, reason):
                        self._last_periodic = elapsed
                self._periodic_frame(elapsed)
                if progress is not None:
                    progress(self.stats)
        except KeyboardInterrupt:
            pass
        finally:
            self.hook.stop()

        self.stats.seconds = self._clock() - started
        self.session.close(stats=self.stats.__dict__)
        return self.stats

    # ---------- 内部 ----------

    def _drain_queue(self, elapsed: float) -> None:
        while self._queue:
            event = self._queue.popleft()
            event = self._apply_privacy(event)
            if event is None:
                continue
            if event.kind == MOUSE_MOVE and not self.options.record_moves:
                self.stats.dropped_moves += 1
                continue
            if event.window is None:
                event = InputEvent(**{**event.__dict__, "window": self._current_window})
            self.session.add_event(event)
            self.stats.events += 1
            if event.kind in _FRAME_TRIGGERS:
                self._pending_reason = event.kind

    def _apply_privacy(self, event: InputEvent) -> InputEvent | None:
        """按键相关：按时开关与敏感窗口策略处理。"""
        if event.kind not in ("key_down", "key_up"):
            return event
        if not self.options.record_keys:
            return None
        if is_sensitive_title(self._current_window):
            if event.kind != KEY_DOWN:
                return None
            self.stats.masked_keys += 1
            return InputEvent(ts=event.ts, kind=KEY_MASKED, window=self._current_window)
        return event

    def _prime_window(self) -> None:
        """循环开始前先确定前台窗口。

        两个作用：把起始上下文记进事件流；以及让"敏感窗口内隐藏按键"从第一秒就
        生效——否则最开始几秒的输入会因为还不知道窗口而失去保护。
        """
        if self.window_provider is None:
            return
        info = self.window_provider()
        if info is None:
            return
        self._current_window = info.label()
        self.session.add_event(InputEvent(ts=0.0, kind=FOCUS, window=self._current_window))
        self.stats.events += 1

    def _poll_window(self, elapsed: float) -> None:
        if self.window_provider is None:
            return
        if elapsed - self._last_window_poll < self.options.window_poll:
            return
        self._last_window_poll = elapsed
        info = self.window_provider()
        if info is None:
            return
        label = info.label()
        if label != self._current_window:
            self._current_window = label
            self.session.add_event(
                InputEvent(ts=elapsed, kind=FOCUS, window=label)
            )
            self.stats.events += 1
            self._pending_reason = FOCUS

    def _periodic_frame(self, elapsed: float) -> None:
        if self.options.fps <= 0:
            return
        if elapsed - self._last_periodic < 1.0 / self.options.fps:
            return
        self._last_periodic = elapsed
        self._capture(elapsed, "tick")

    def _capture(self, elapsed: float, reason: str) -> bool:
        """抓一帧并按去重策略决定是否落盘。"""
        data = self.source.grab()
        width, height = self.source.size()
        rows = bgra_to_rgb_rows(data, width, height, step=self.options.step)
        out_width = len(rows[0]) // 3
        out_height = len(rows)
        digest = fingerprint(rows, grid=self.options.grid)
        if self._last_digest is not None:
            if difference(self._last_digest, digest) < self.options.change_threshold:
                self.stats.skipped_frames += 1
                return False
        self.session.add_frame(elapsed, reason, rows, out_width, out_height, digest)
        self._last_digest = digest
        self.stats.frames = self.session.frame_count
        return True
