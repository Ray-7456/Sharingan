"""极简 PNG 写入（纯标准库）。

采集层要写大量截图帧，而项目的核心原则是零第三方依赖，所以自己实现：
只支持 8 位 RGB / RGBA、固定滤波类型 0、zlib 压缩——足够用，且输出是标准 PNG，
任何看图工具都能打开。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_rgb(width: int, height: int, rows: list[bytes]) -> bytes:
    """把逐行 RGB 字节编码成 PNG（颜色类型 2）。"""
    if len(rows) != height:
        raise ValueError(f"行数 {len(rows)} 与高 {height} 不一致")
    expected = width * 3
    for index, row in enumerate(rows):
        if len(row) != expected:
            raise ValueError(f"第 {index} 行字节数 {len(row)}，应为 {expected}")
    raw = b"".join(b"\x00" + row for row in rows)  # 每行滤波类型 0
    return (
        _SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def write_rgb(path: str | Path, width: int, height: int, rows: list[bytes]) -> int:
    """写 PNG 文件，返回字节数。"""
    data = encode_rgb(width, height, rows)
    Path(path).write_bytes(data)
    return len(data)


def bgra_to_rgb_rows(
    data: bytes, width: int, height: int, *, step: int = 1
) -> list[bytes]:
    """把 BGRA（32 位，行优先）转成逐行 RGB，可按整数倍降采样。

    用切片而非逐像素循环：1080p 一帧的转换在毫秒级。
    ``step=2`` 表示长宽各取一半（最近邻）。
    """
    if step < 1:
        raise ValueError("step 必须 ≥ 1")
    row_bytes = width * 4
    if len(data) < row_bytes * height:
        raise ValueError(
            f"数据长度 {len(data)} 小于 {width}×{height}×4={row_bytes * height}"
        )
    rows: list[bytes] = []
    for y in range(0, height, step):
        line = data[y * row_bytes : (y + 1) * row_bytes]
        blue = line[0:: 4 * step]
        green = line[1:: 4 * step]
        red = line[2:: 4 * step]
        row = bytearray(len(red) * 3)
        row[0::3] = red
        row[1::3] = green
        row[2::3] = blue
        rows.append(bytes(row))
    return rows


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    """解析本模块写出的 PNG，返回 (宽, 高, 逐行 RGB)。

    只用于测试与自检：贴在 ``encode_rgb`` 的另一端，保证写的确实是标准 PNG。
    """
    if not data.startswith(_SIGNATURE):
        raise ValueError("不是 PNG 数据")
    position = len(_SIGNATURE)
    width = height = 0
    idat = bytearray()
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        tag = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length
        if tag == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if (depth, color, interlace) != (8, 2, 0):
                raise ValueError("只支持 8 位 RGB 无隔行")
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    stride = width * 3 + 1
    rows = []
    for y in range(height):
        chunk = raw[y * stride : (y + 1) * stride]
        if chunk[0] != 0:
            raise ValueError(f"第 {y} 行滤波类型不是 0")
        rows.append(chunk[1:])
    return width, height, rows
