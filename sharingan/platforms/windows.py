"""Windows 采集适配器：GDI 截屏 + 底层输入钩子 + 前台窗口。

全部通过 ctypes 调用 Win32，不引入第三方依赖。三件事：

- :class:`WindowsScreenSource`——BitBlt 抓屏，返回 BGRA 原始字节（行优先、自上而下）；
- :class:`WindowsEventHook`——``WH_MOUSE_LL`` / ``WH_KEYBOARD_LL`` 底层钩子；
- :func:`active_window`——前台窗口的标题与进程名。

两个必须知道的约束：

1. 底层钩子要在**安装它的线程**上泵消息，所以采集调度每轮都会调用 ``pump()``，
   并且这个循环不能长时间阻塞；
2. 钩子回调必须极快返回（Windows 对超时会直接摘掉钩子），因此回调只把事件塞进
   队列，抓帧与写盘都在主循环里做。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

from ..capture.sources import (
    KEY_DOWN,
    KEY_UP,
    MOUSE_DOWN,
    MOUSE_MOVE,
    MOUSE_UP,
    MOUSE_WHEEL,
    InputEvent,
    WindowInfo,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
PM_REMOVE = 0x0001

_MOUSE_MESSAGES = {
    WM_LBUTTONDOWN: (MOUSE_DOWN, "left"),
    WM_LBUTTONUP: (MOUSE_UP, "left"),
    WM_RBUTTONDOWN: (MOUSE_DOWN, "right"),
    WM_RBUTTONUP: (MOUSE_UP, "right"),
    WM_MBUTTONDOWN: (MOUSE_DOWN, "middle"),
    WM_MBUTTONUP: (MOUSE_UP, "middle"),
}
_KEY_MESSAGES = {
    WM_KEYDOWN: KEY_DOWN,
    WM_SYSKEYDOWN: KEY_DOWN,
    WM_KEYUP: KEY_UP,
    WM_SYSKEYUP: KEY_UP,
}

_VK_NAMES = {
    0x08: "BACKSPACE", 0x09: "TAB", 0x0D: "ENTER", 0x13: "PAUSE", 0x14: "CAPSLOCK",
    0x1B: "ESC", 0x20: "SPACE", 0x21: "PAGEUP", 0x22: "PAGEDOWN", 0x23: "END",
    0x24: "HOME", 0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT", 0x28: "DOWN",
    0x2C: "PRINTSCREEN", 0x2D: "INSERT", 0x2E: "DELETE",
    0x5B: "LWIN", 0x5C: "RWIN", 0x5D: "APPS",
    0x6A: "MULTIPLY", 0x6B: "ADD", 0x6D: "SUBTRACT", 0x6E: "DECIMAL", 0x6F: "DIVIDE",
    0x90: "NUMLOCK", 0x91: "SCROLLLOCK",
    0xA0: "LSHIFT", 0xA1: "RSHIFT", 0xA2: "LCTRL", 0xA3: "RCTRL", 0xA4: "LALT", 0xA5: "RALT",
}


def key_name(vk_code: int) -> str:
    """虚拟键码 → 可读键名；未知的退回 ``vk_<码>``（不猜）。"""
    if vk_code in _VK_NAMES:
        return _VK_NAMES[vk_code]
    if 0x30 <= vk_code <= 0x39 or 0x41 <= vk_code <= 0x5A:  # 0-9 / A-Z
        return chr(vk_code)
    if 0x70 <= vk_code <= 0x87:  # F1-F24
        return f"F{vk_code - 0x6F}"
    return f"vk_{vk_code}"


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


class _MouseHookStruct(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KeyboardHookStruct(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# 显式声明签名：不声明的话 ctypes 会把 64 位句柄截断成 32 位，
# 表现出来就是 SetWindowsHookExW 返回 ERROR_MOD_NOT_FOUND(126)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(_BitmapInfo),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def ensure_dpi_aware() -> None:
    """让进程 DPI 感知，否则在缩放显示器上抓到的画面会被系统拉伸。"""
    try:  # Windows 10 1703+
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:  # pragma: no cover - 老系统
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            user32.SetProcessDPIAware()


class WindowsScreenSource:
    """BitBlt 抓屏；DC 与位图复用，避免每帧重新分配。"""

    def __init__(self, region: tuple[int, int, int, int] | None = None) -> None:
        ensure_dpi_aware()
        self.region = region
        self._screen_dc = None
        self._memory_dc = None
        self._bitmap = None
        self._previous = None
        self._buffer = None
        self._width = 0
        self._height = 0
        self._prepare()

    # ---------- 尺寸与准备 ----------

    def _target_rect(self) -> tuple[int, int, int, int]:
        if self.region is not None:
            return self.region
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    def _prepare(self) -> None:
        x, y, width, height = self._target_rect()
        if width <= 0 or height <= 0:
            raise RuntimeError(f"取到的屏幕尺寸不合法：{width}×{height}")
        self._width, self._height = width, height
        self._origin = (x, y)
        self._screen_dc = user32.GetDC(0)
        if not self._screen_dc:
            raise OSError("GetDC 失败")
        self._memory_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._memory_dc:
            raise OSError("CreateCompatibleDC 失败")
        self._bitmap = gdi32.CreateCompatibleBitmap(self._screen_dc, width, height)
        if not self._bitmap:
            raise OSError("CreateCompatibleBitmap 失败")
        self._previous = gdi32.SelectObject(self._memory_dc, self._bitmap)
        self._buffer = ctypes.create_string_buffer(width * height * 4)

    def size(self) -> tuple[int, int]:
        return self._width, self._height

    # ---------- 抓帧 ----------

    def grab(self) -> bytes:
        x, y = self._origin
        if not gdi32.BitBlt(
            self._memory_dc, 0, 0, self._width, self._height, self._screen_dc, x, y, SRCCOPY
        ):
            raise OSError("BitBlt 失败")
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = self._width
        info.bmiHeader.biHeight = -self._height  # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        lines = gdi32.GetDIBits(
            self._memory_dc,
            self._bitmap,
            0,
            self._height,
            self._buffer,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if lines == 0:
            raise OSError("GetDIBits 失败")
        return self._buffer.raw

    def close(self) -> None:
        if self._memory_dc and self._previous:
            gdi32.SelectObject(self._memory_dc, self._previous)
        for handle, release in (
            (self._bitmap, gdi32.DeleteObject),
            (self._memory_dc, gdi32.DeleteDC),
        ):
            if handle:
                release(handle)
        if self._screen_dc:
            user32.ReleaseDC(0, self._screen_dc)
        self._screen_dc = self._memory_dc = self._bitmap = self._previous = None


class WindowsEventHook:
    """底层鼠标/键盘钩子；回调里只入队，不做任何耗时操作。"""

    def __init__(self, *, record_moves: bool = False) -> None:
        self.record_moves = record_moves
        self._callback = None
        self._mouse_proc = None
        self._keyboard_proc = None
        self._mouse_hook = None
        self._keyboard_hook = None
        self._started_at = 0.0
        self._moves_dropped = 0

    # ---------- 生命周期 ----------

    def start(self, on_event) -> None:
        self._callback = on_event
        self._started_at = time.monotonic()
        self._mouse_proc = _HOOKPROC(self._handle_mouse)
        self._keyboard_proc = _HOOKPROC(self._handle_keyboard)
        # 低层钩子（LL）从不会被注入到别的进程，按文档要求 hMod 必须为 NULL；
        # 传模块句柄会失败（实测返回 126 ERROR_MOD_NOT_FOUND）
        self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_proc, None, 0
        )
        if not self._mouse_hook or not self._keyboard_hook:
            self.stop()
            raise OSError(
                "安装底层钩子失败（错误码 %d）；可尝试以管理员身份运行"
                % ctypes.get_last_error()
            )

    def pump(self, timeout: float) -> None:
        """处理本线程的消息队列——底层钩子靠它才有回调。"""
        message = wintypes.MSG()
        deadline = time.monotonic() + timeout
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= deadline:
                break
        time.sleep(0.001)  # 让出 CPU，避免忙等

    def stop(self) -> None:
        for hook in (self._mouse_hook, self._keyboard_hook):
            if hook:
                user32.UnhookWindowsHookEx(hook)
        self._mouse_hook = self._keyboard_hook = None
        self._callback = None

    # ---------- 回调 ----------

    def _emit(self, event: InputEvent) -> None:
        if self._callback is not None:
            self._callback(event)

    def _elapsed(self) -> float:
        return time.monotonic() - self._started_at

    def _handle_mouse(self, code, wparam, lparam):
        if code >= 0 and self._callback is not None:
            data = ctypes.cast(lparam, ctypes.POINTER(_MouseHookStruct)).contents
            message = int(wparam)
            if message == WM_MOUSEMOVE:
                if self.record_moves:
                    self._emit(
                        InputEvent(ts=self._elapsed(), kind=MOUSE_MOVE, x=data.pt.x, y=data.pt.y)
                    )
                else:
                    self._moves_dropped += 1
            elif message in _MOUSE_MESSAGES:
                kind, button = _MOUSE_MESSAGES[message]
                self._emit(
                    InputEvent(
                        ts=self._elapsed(), kind=kind, x=data.pt.x, y=data.pt.y, button=button
                    )
                )
            elif message == WM_MOUSEWHEEL:
                delta = ctypes.c_short(data.mouseData >> 16).value
                self._emit(
                    InputEvent(
                        ts=self._elapsed(),
                        kind=MOUSE_WHEEL,
                        x=data.pt.x,
                        y=data.pt.y,
                        delta=delta,
                    )
                )
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _handle_keyboard(self, code, wparam, lparam):
        if code >= 0 and self._callback is not None:
            data = ctypes.cast(lparam, ctypes.POINTER(_KeyboardHookStruct)).contents
            message = int(wparam)
            kind = _KEY_MESSAGES.get(message)
            if kind is not None:
                self._emit(InputEvent(ts=self._elapsed(), kind=kind, key=key_name(data.vkCode)))
        return user32.CallNextHookEx(None, code, wparam, lparam)


def active_window() -> WindowInfo | None:
    """前台窗口的标题与进程名；取不到就返回 None（不猜）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(title=buffer.value, process=_process_name(pid.value))


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)
    return ""
