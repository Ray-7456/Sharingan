"""采集层测试：PNG 写入、帧去重、会话落盘、录制调度与 Windows 适配器。

除最后一个类之外都不碰真实屏幕与真实输入——采集调度用合成源驱动，
这样整条管道可以在没有显示器、不产生任何真实操作的情况下被验证。
"""

import json
import platform
import tempfile
import unittest
from pathlib import Path

from sharingan.capture.dedup import difference, fingerprint
from sharingan.capture.png import bgra_to_rgb_rows, decode_png, encode_rgb
from sharingan.capture.recorder import Recorder, RecorderOptions, is_sensitive_title
from sharingan.capture.session import CaptureSession
from sharingan.capture.sources import (
    InputEvent,
    SyntheticEventHook,
    SyntheticFrameSource,
    WindowInfo,
)
from sharingan.platforms import current_platform, describe

IS_WINDOWS = platform.system() == "Windows"


class PngTest(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        rows = [
            bytes([255, 0, 0, 0, 255, 0]),  # 红 + 绿
            bytes([0, 0, 255, 10, 20, 30]),  # 蓝 + 深灰
        ]
        data = encode_rgb(2, 2, rows)  # 每行 6 字节 = 2 像素
        width, height, decoded = decode_png(data)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(decoded, rows)

    def test_rejects_mismatched_rows(self):
        with self.assertRaises(ValueError):
            encode_rgb(2, 2, [bytes(6)])
        with self.assertRaises(ValueError):
            encode_rgb(2, 2, [bytes(6), bytes(3)])

    def test_bgra_to_rgb(self):
        # 两个像素：BGR(A) = (10,20,30) 与 (40,50,60)
        data = bytes([10, 20, 30, 255, 40, 50, 60, 255])
        rows = bgra_to_rgb_rows(data, 2, 1)
        self.assertEqual(rows, [bytes([30, 20, 10, 60, 50, 40])])

    def test_bgra_to_rgb_with_step(self):
        # 4 像素一行，step=2 → 取第 0、2 个像素
        data = bytes(
            [1, 2, 3, 255, 9, 9, 9, 255, 4, 5, 6, 255, 9, 9, 9, 255]
        )
        rows = bgra_to_rgb_rows(data, 4, 1, step=2)
        self.assertEqual(rows, [bytes([3, 2, 1, 6, 5, 4])])


class DedupTest(unittest.TestCase):
    def _frame(self, level: int, width: int = 64, height: int = 48) -> list[bytes]:
        row = bytes([level, level, level] * width)
        return [row] * height

    def test_identical_frames_have_zero_difference(self):
        left = fingerprint(self._frame(40))
        right = fingerprint(self._frame(40))
        self.assertEqual(difference(left, right), 0.0)

    def test_changed_frame_exceeds_threshold(self):
        left = fingerprint(self._frame(40))
        right = fingerprint(self._frame(120))
        self.assertGreater(difference(left, right), 0.9)

    def test_small_local_change_stays_below_default_threshold(self):
        rows = self._frame(40, width=64, height=48)
        changed = [bytearray(row) for row in rows]
        for y in range(0, 6):  # 只改左上角一小块（约占 1/8 × 1/8）
            changed[y][0:12] = bytes([200] * 12)
        left = fingerprint(rows)
        right = fingerprint([bytes(row) for row in changed])
        self.assertLess(difference(left, right), 0.02)

    def test_different_grid_sizes_are_treated_as_different(self):
        self.assertEqual(difference(b"\x01" * 4, b"\x01" * 9), 1.0)


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "session"

    def test_layout_and_meta_lifecycle(self):
        session = CaptureSession(self.directory, meta={"tool": "test"})
        # 开始时就先写一份 meta（状态 recording），崩溃也能看出未完成
        meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "recording")
        self.assertEqual(meta["tool"], "test")

        session.add_event(InputEvent(ts=0.5, kind="mouse_down", x=1, y=2, button="left"))
        rows = [bytes([1, 2, 3] * 4)] * 2
        path = session.add_frame(0.6, "mouse_down", rows, 4, 2, b"\x00" * 1024)
        final = session.close(stats={"frames": 1})

        self.assertTrue(path.exists())
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["frames"], 1)
        self.assertEqual(final["stats"], {"frames": 1})
        # 帧能被自己的解码器读回
        width, height, decoded = decode_png(path.read_bytes())
        self.assertEqual((width, height), (4, 2))
        self.assertEqual(decoded, rows)
        # 事件流与索引都是 JSONL，逐行可解析
        event = json.loads((self.directory / "events.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(event["kind"], "mouse_down")
        self.assertIsNone(event.get("key"))  # 空字段不落盘
        index = json.loads(
            (self.directory / "frames" / "index.jsonl").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(index["reason"], "mouse_down")
        self.assertEqual(index["file"], "frames/000000.png")


class FakeClock:
    """让录制循环在测试里快速推进（每调用一次前进 fixed 步长）。"""

    def __init__(self, step: float = 0.5) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class RecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "session"

    def _run(self, source, hook, options, *, window_provider=None):
        session = CaptureSession(self.directory, meta={"test": True})
        recorder = Recorder(
            source,
            hook,
            session,
            options,
            window_provider=window_provider,
            clock=FakeClock(),
        )
        stats = recorder.run()
        return stats, session

    def _events(self, session):
        text = session.events_path.read_text(encoding="utf-8").strip()
        return [json.loads(line) for line in text.splitlines()] if text else []

    def _frames(self, session):
        text = session.index_path.read_text(encoding="utf-8").strip()
        return [json.loads(line) for line in text.splitlines()] if text else []

    def test_events_are_recorded_and_frames_attributed_to_actions(self):
        # 第 4 次抓帧开始画面变化：正好落在"按键按下"那次事件门控抓帧上
        source = SyntheticFrameSource(width=32, height=24, changes={4: 60})
        hook = SyntheticEventHook(
            events=[
                InputEvent(ts=0.1, kind="mouse_down", x=1, y=2, button="left"),
                InputEvent(ts=0.2, kind="mouse_up", x=1, y=2, button="left"),
                InputEvent(ts=0.3, kind="key_down", key="A"),
            ]
        )
        stats, session = self._run(source, hook, RecorderOptions(seconds=2.5, fps=2.0))

        self.assertEqual(stats.events, 3)
        self.assertEqual([event["kind"] for event in self._events(session)],
                         ["mouse_down", "mouse_up", "key_down"])
        # 第 1 帧（鼠标按下）与第 3 帧（画面变亮）落盘；第 2 帧与后面重复的被去重
        frames = self._frames(session)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["reason"], "mouse_down")
        self.assertEqual(frames[1]["reason"], "key_down")
        self.assertGreaterEqual(stats.skipped_frames, 1)
        for frame in frames:
            self.assertTrue((self.directory / frame["file"]).exists())

    def test_periodic_frames_only_when_screen_changes(self):
        source = SyntheticFrameSource(width=32, height=24, changes={4: -20})
        hook = SyntheticEventHook(events=[])
        stats, session = self._run(source, hook, RecorderOptions(seconds=3.0, fps=2.0))
        frames = self._frames(session)
        self.assertEqual([frame["reason"] for frame in frames], ["tick", "tick"])
        self.assertGreaterEqual(stats.skipped_frames, 1)

    def test_keys_can_be_disabled(self):
        source = SyntheticFrameSource()
        hook = SyntheticEventHook(events=[InputEvent(ts=0.1, kind="key_down", key="A")])
        stats, session = self._run(
            source, hook, RecorderOptions(seconds=1.0, record_keys=False)
        )
        self.assertEqual(stats.events, 0)
        self.assertEqual(self._events(session), [])

    def test_sensitive_window_masks_keys(self):
        source = SyntheticFrameSource()
        hook = SyntheticEventHook(
            events=[
                InputEvent(ts=0.1, kind="key_down", key="A"),
                InputEvent(ts=0.2, kind="mouse_down", x=5, y=5, button="left"),
            ]
        )
        provider = lambda: WindowInfo(title="登录 - 系统", process="chrome.exe")  # noqa: E731
        stats, session = self._run(
            source, hook, RecorderOptions(seconds=1.0), window_provider=provider
        )
        kinds = [event["kind"] for event in self._events(session)]
        self.assertIn("focus", kinds)
        self.assertIn("key_masked", kinds)
        self.assertNotIn("key_down", kinds)
        self.assertEqual(stats.masked_keys, 1)

    def test_window_switch_emits_focus_event(self):
        source = SyntheticFrameSource()
        hook = SyntheticEventHook(events=[])
        titles = iter([WindowInfo("编辑器 - a.txt", "code.exe")] * 20)
        stats, session = self._run(
            source,
            hook,
            RecorderOptions(seconds=1.5),
            window_provider=lambda: next(titles, None),
        )
        focus = [event for event in self._events(session) if event["kind"] == "focus"]
        self.assertEqual(len(focus), 1)  # 只在切换时记一条
        self.assertIn("code.exe", focus[0]["window"])

    def test_max_frames_stops_recording(self):
        source = SyntheticFrameSource(width=32, height=24, changes={2: 50, 3: 50, 4: 50})
        hook = SyntheticEventHook(events=[])
        stats, _ = self._run(
            source, hook, RecorderOptions(seconds=10.0, fps=4.0, max_frames=2)
        )
        self.assertEqual(stats.frames, 2)


class SensitiveTitleTest(unittest.TestCase):
    def test_hints(self):
        self.assertTrue(is_sensitive_title("登录 - 系统"))
        self.assertTrue(is_sensitive_title("Sign in to GitHub"))
        self.assertTrue(is_sensitive_title("Password manager"))
        self.assertFalse(is_sensitive_title("季度报表 - Excel"))
        self.assertFalse(is_sensitive_title(None))


class PlatformRegistryTest(unittest.TestCase):
    def test_current_platform_and_description(self):
        self.assertIn(current_platform(), {"windows", "macos", "linux", "unknown"})
        self.assertIsInstance(describe(), str)
        self.assertTrue(describe())


@unittest.skipUnless(IS_WINDOWS, "仅在 Windows 上验证适配器")
class WindowsAdapterTest(unittest.TestCase):
    def test_key_names(self):
        from sharingan.platforms.windows import key_name

        self.assertEqual(key_name(0x41), "A")
        self.assertEqual(key_name(0x0D), "ENTER")
        self.assertEqual(key_name(0x70), "F1")
        self.assertEqual(key_name(0x01), "vk_1")  # 未知码不猜名字

    def test_screen_size_without_reading_pixels(self):
        from sharingan.platforms.windows import WindowsScreenSource

        source = WindowsScreenSource()
        try:
            width, height = source.size()
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
        finally:
            source.close()

    def test_small_region_capture_plumbing(self):
        """只取 32×32 并核对缓冲区大小——**不检查内容、不落盘**，避免把屏幕内容带进测试。"""
        from sharingan.platforms.windows import WindowsScreenSource

        source = WindowsScreenSource((0, 0, 32, 32))
        try:
            self.assertEqual(source.size(), (32, 32))
            first = source.grab()
            second = source.grab()
            self.assertEqual(len(first), 32 * 32 * 4)
            self.assertEqual(len(second), len(first))
        finally:
            source.close()

    def test_active_window_shape(self):
        from sharingan.platforms.windows import active_window

        info = active_window()
        if info is not None:  # 无前台窗口时允许为 None
            self.assertIsInstance(info, WindowInfo)
            self.assertIsInstance(info.title, str)


if __name__ == "__main__":
    unittest.main()
