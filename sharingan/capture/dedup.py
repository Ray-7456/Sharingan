"""帧指纹与去重：判断"这一帧和上一张存下来的帧是否算同一个画面"。

思路与 screenpipe 那类工具一致：把帧降采样成 32×32 的灰度指纹，比较差异格子
比例。好处是无状态、快（1080p 一帧毫秒级），且对光标闪烁、时钟跳字这类
局部小变化不敏感——它们不该产生新帧。
"""

from __future__ import annotations

DEFAULT_GRID = 32
# 单个格子亮度差超过它才算"变了"，用来容忍渲染噪声与轻微动画
DEFAULT_TOLERANCE = 12


def fingerprint(rows: list[bytes], *, grid: int = DEFAULT_GRID) -> bytes:
    """把逐行 RGB 帧降采样成 ``grid × grid`` 的灰度指纹。"""
    if not rows:
        raise ValueError("空帧")
    height = len(rows)
    width = len(rows[0]) // 3
    if width == 0:
        raise ValueError("帧宽为 0")
    out = bytearray(grid * grid)
    for gy in range(grid):
        y = min(height - 1, int((gy + 0.5) * height / grid))
        row = rows[y]
        for gx in range(grid):
            x = min(width - 1, int((gx + 0.5) * width / grid))
            offset = x * 3
            red, green, blue = row[offset], row[offset + 1], row[offset + 2]
            out[gy * grid + gx] = (red * 299 + green * 587 + blue * 114) // 1000
    return bytes(out)


def difference(left: bytes, right: bytes, *, tolerance: int = DEFAULT_TOLERANCE) -> float:
    """两张指纹不同的格子占比（0.0 ~ 1.0）；尺寸不一致时视为完全不同。"""
    if len(left) != len(right):
        return 1.0
    if not left:
        return 0.0
    changed = sum(1 for a, b in zip(left, right) if abs(a - b) > tolerance)
    return changed / len(left)
