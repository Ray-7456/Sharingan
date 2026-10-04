"""采集会话的目录结构。

会话目录（分析器的输入）：

.. code-block:: text

    session/
      meta.json          元信息：开始/结束时间、平台、屏幕尺寸、参数、统计
      events.jsonl       事件流，每行一个 JSON（时间戳为会话相对秒数）
      frames/000000.png  屏幕帧（PNG，可降采样）
      frames/index.jsonl 帧索引：序号、时间戳、触发原因、文件、指纹

写入策略：``meta.json`` 在开始时先写一份（状态 recording），正常结束时补上
最终统计——这样即使中途崩溃，也能看出会话未完成，而不是被误当成完整数据。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from .png import write_rgb


class CaptureSession:
    """一次采集的落盘目标。"""

    def __init__(self, directory: str | Path, meta: dict) -> None:
        self.directory = Path(directory)
        self.frames_directory = self.directory / "frames"
        self.frames_directory.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.directory / "meta.json"
        self.events_path = self.directory / "events.jsonl"
        self.index_path = self.frames_directory / "index.jsonl"

        self.meta = dict(meta)
        self.meta.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
        self.meta["status"] = "recording"
        self._write_meta()

        self._events_file = self.events_path.open("w", encoding="utf-8", newline="\n")
        self._index_file = self.index_path.open("w", encoding="utf-8", newline="\n")
        self.frame_count = 0
        self.event_count = 0
        self.bytes_written = 0

    # ---------- 写入 ----------

    def add_event(self, event) -> None:
        payload = asdict(event) if is_dataclass(event) else dict(event)
        payload = {key: value for key, value in payload.items() if value is not None}
        self._events_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.event_count += 1

    def add_frame(
        self, ts: float, reason: str, rows: list[bytes], width: int, height: int, digest: bytes
    ) -> Path:
        index = self.frame_count
        path = self.frames_directory / f"{index:06d}.png"
        self.bytes_written += write_rgb(path, width, height, rows)
        self._index_file.write(
            json.dumps(
                {
                    "index": index,
                    "ts": round(ts, 3),
                    "reason": reason,
                    "file": f"frames/{path.name}",
                    "width": width,
                    "height": height,
                    "digest": digest.hex(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.frame_count += 1
        return path

    # ---------- 收尾 ----------

    def _write_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def close(self, *, stats: dict | None = None) -> dict:
        for handle in (self._events_file, self._index_file):
            if not handle.closed:
                handle.close()
        self.meta["status"] = "complete"
        self.meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.meta["frames"] = self.frame_count
        self.meta["events"] = self.event_count
        self.meta["bytes"] = self.bytes_written
        if stats:
            self.meta["stats"] = stats
        self._write_meta()
        return self.meta
