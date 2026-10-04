"""合成测试数据生成：数据文件 + 异常注入 + 真值清单。

夹具必须**完全合成**，禁止用真实数据替换（见 README 数据红线）。

异常剧本是固定的（不随 seed 变化），只有基础曲线噪声受 seed 影响，因此
给定 seed 时生成结果逐字节可复现。真值清单（manifest）记录每一处注入的
位置与数值，供测试断言，也供后续"脚本结果 vs 模板结果"对照使用。

注入的五类异常与用例文档一致：

===============  ==========================================================
gap              时间断档（对应现场 14 小时空档）
stale_run        连续陈旧值 + 备注"此时间点无数据"（对应现场复制上一行）
spike            单点毛刺，其中两处故意越过阈值
derived_overflow 迫使派生列 ``T=|H-I|`` 超限（对应现场唯一触发判定的情况）
float_artifact   浮点尾数（形如 ``53.80001``，对应现场 ``99.60001``）
===============  ==========================================================
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..formats import DataRow, TimeSeriesFile, format_time

NO_DATA_NOTE = "此时间点无数据"


@dataclass(frozen=True)
class Signal:
    tag: str
    base: float
    daily_amplitude: float
    noise: float
    limit: float | None = None
    load_driven: bool = False


SIGNALS: tuple[Signal, ...] = (
    Signal("gearbox_temp_input_shaft1", 45.0, 6.0, 0.5, 85),
    Signal("gearbox_temp_output_shaft2", 44.0, 6.0, 0.6, 85),
    Signal("gearbox_oil_temp_gearbox", 47.0, 5.0, 0.6, 75),
    Signal("generator_winding_temp_u1", 42.0, 9.0, 0.7, 135),
    Signal("generator_winding_temp_v1", 41.0, 9.0, 0.7, 135),
    Signal("generator_winding_temp_w1", 43.0, 9.0, 0.8, 135),
    Signal("generator_bearing_temp_a", 37.0, 3.0, 0.4, 80),
    Signal("generator_bearing_temp_b", 52.0, 6.0, 0.5, 80),
    Signal("nacelle_temp", 31.0, 5.0, 0.5, 50),
    Signal("grid_power", 700.0, 600.0, 60.0, None, load_driven=True),
    Signal("tower_base_cabinet_temperature", 33.0, 3.0, 0.4, 50),
    Signal("topbox_cabinet_temperature", 34.5, 3.5, 0.4, 50),
    Signal("main_bearing_rotor_side_temp", 37.0, 2.5, 0.4, 55),
    Signal("main_bearing_gearbox_side_temp", 41.0, 2.5, 0.4, 60),
)

TAG_INDEX = {signal.tag: index for index, signal in enumerate(SIGNALS)}

# ---- 异常剧本（固定，不随 seed 变化）----
GAP_OFFSET = timedelta(days=3, hours=18, minutes=50)
GAP_LENGTH = 84  # 84 × 10 分钟 = 14 小时
STALE_PLAN = (
    (timedelta(hours=1, minutes=40), 1),
    (timedelta(days=1, hours=16, minutes=40), 1),
    (timedelta(days=5, hours=18, minutes=50), 3),
)
SPIKE_PLAN = (
    (timedelta(days=1, hours=3, minutes=10), "gearbox_temp_input_shaft1", 88.5),
    (timedelta(days=5, hours=16, minutes=50), "generator_bearing_temp_b", 84.2),
    (timedelta(days=2, hours=9, minutes=20), "nacelle_temp", 46.5),
)
OVERFLOW_OFFSET = timedelta(days=6, hours=14)
OVERFLOW_LENGTH = 12
OVERFLOW_LIMIT = 20.0  # 派生列 T = |H-I|
FLOAT_PLAN = (
    (timedelta(days=4, hours=16, minutes=50), "generator_winding_temp_u1"),
    (timedelta(days=2, hours=11), "nacelle_temp"),
)


def _base_value(signal: Signal, moment: datetime, start: datetime, rng: random.Random) -> float:
    hours = (moment - start).total_seconds() / 3600.0
    diurnal = math.sin(2 * math.pi * ((hours % 24) - 6) / 24)
    if signal.load_driven:
        load = 0.55 + 0.45 * diurnal
        value = signal.base * max(0.0, load) + rng.uniform(-signal.noise, signal.noise)
        roll = rng.random()
        if roll < 0.08:
            value = 0.0
        elif roll < 0.14:
            value = -1.8
        return round(value, 1)
    slow = math.sin(2 * math.pi * hours / (24 * 4))
    value = (
        signal.base
        + signal.daily_amplitude * diurnal
        + 0.5 * signal.daily_amplitude * slow
        + rng.uniform(-signal.noise, signal.noise)
    )
    return round(value, 1)


@dataclass
class FixtureResult:
    data_file: TimeSeriesFile
    manifest: dict

    def write(self, directory: str | Path, *, stem: str = "synthetic_week") -> tuple[Path, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        csv_path = directory / f"{stem}.csv"
        manifest_path = directory / f"{stem}.manifest.json"
        self.data_file.write(csv_path)
        manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return csv_path, manifest_path


MIN_DAYS = 7  # 异常剧本按整周设计，短于 7 天放不下


def generate(
    *,
    seed: int = 20260105,
    days: int = 7,
    start: datetime = datetime(2026, 1, 5, 0, 0),
    step_minutes: int = 10,
) -> FixtureResult:
    """生成一份合成周数据，返回数据文件与真值清单。"""
    if days < MIN_DAYS:
        raise ValueError(
            f"days 至少为 {MIN_DAYS}：固定异常剧本按整周设计，{days} 天装不下"
        )
    # 这里刻意使用带种子的伪随机数：夹具要求逐字节可复现（不是密码学用途）
    rng = random.Random(seed)
    step = timedelta(minutes=step_minutes)
    total = days * 24 * 60 // step_minutes

    times: list[datetime] = [start + step * i for i in range(total)]
    value_matrix: list[list[float]] = [
        [_base_value(signal, moment, start, rng) for signal in SIGNALS] for moment in times
    ]

    # ---- gap：移除断档窗口 ----
    gap_start = start + GAP_OFFSET
    kept = [(t, v) for t, v in zip(times, value_matrix) if not (gap_start <= t < gap_start + step * GAP_LENGTH)]
    times = [t for t, _ in kept]
    value_matrix = [v for _, v in kept]
    index_of = {moment: index for index, moment in enumerate(times)}
    notes = [""] * len(times)

    anomalies: list[dict] = [
        {
            "kind": "gap",
            "rows_removed": GAP_LENGTH,
            "from": format_time(gap_start),
            "to": format_time(gap_start + step * (GAP_LENGTH - 1)),
        }
    ]

    # ---- stale_run：复制断档前一行，并标注无数据 ----
    for offset, length in STALE_PLAN:
        moment = start + offset
        index = index_of[moment]
        if index == 0:
            raise ValueError("stale_run 不能落在首行")
        stale = list(value_matrix[index - 1])
        for k in range(index, index + length):
            value_matrix[k] = list(stale)
            notes[k] = NO_DATA_NOTE
        anomalies.append(
            {
                "kind": "stale_run",
                "start_index": index,
                "length": length,
                "from": format_time(times[index]),
                "to": format_time(times[index + length - 1]),
                "note": NO_DATA_NOTE,
                "source_row_index": index - 1,
            }
        )

    # ---- spike：单点毛刺 ----
    for offset, tag, value in SPIKE_PLAN:
        moment = start + offset
        index = index_of[moment]
        column = TAG_INDEX[tag]
        before = value_matrix[index][column]
        value_matrix[index][column] = value
        limit = SIGNALS[column].limit
        anomalies.append(
            {
                "kind": "spike",
                "index": index,
                "time": format_time(moment),
                "column": tag,
                "before": before,
                "after": value,
                "limit": limit,
                "breaches_limit": limit is not None and value >= limit,
            }
        )

    # ---- derived_overflow：迫使 T = |H-I| 超限 ----
    overflow_index = index_of[start + OVERFLOW_OFFSET]
    column_a = TAG_INDEX["generator_bearing_temp_a"]
    column_b = TAG_INDEX["generator_bearing_temp_b"]
    max_diff = 0.0
    for k in range(overflow_index, overflow_index + OVERFLOW_LENGTH):
        raise_by = OVERFLOW_LIMIT + rng.uniform(1.0, 8.0)
        value_matrix[k][column_b] = round(value_matrix[k][column_a] + raise_by, 1)
        max_diff = max(max_diff, abs(value_matrix[k][column_b] - value_matrix[k][column_a]))
    anomalies.append(
        {
            "kind": "derived_overflow",
            "start_index": overflow_index,
            "length": OVERFLOW_LENGTH,
            "from": format_time(times[overflow_index]),
            "to": format_time(times[overflow_index + OVERFLOW_LENGTH - 1]),
            "derived": "T = |H-I|",
            "limit": OVERFLOW_LIMIT,
            "max_abs_diff": round(max_diff, 1),
        }
    )

    # ---- float_artifact：浮点尾数 ----
    for offset, tag in FLOAT_PLAN:
        index = index_of[start + offset]
        column = TAG_INDEX[tag]
        value_matrix[index][column] = round(value_matrix[index][column], 3) + 0.00001
        anomalies.append(
            {
                "kind": "float_artifact",
                "index": index,
                "time": format_time(start + offset),
                "column": tag,
                "text": f"{value_matrix[index][column]!r}",
            }
        )

    # ---- 物化为数据行（原始文本 == 当前文本，不产生"修改"记录）----
    rows = [
        DataRow.create(moment, values, note=note)
        for moment, values, note in zip(times, value_matrix, notes)
    ]
    data_file = TimeSeriesFile.from_rows(list(TAG_INDEX), rows)

    manifest = {
        "generator": "sharingan.fixtures.generator",
        "synthetic": True,
        "警告": "本文件为合成数据，禁止用真实数据替换后提交到公开仓库",
        "seed": seed,
        "params": {
            "days": days,
            "start": format_time(start),
            "step_minutes": step_minutes,
            "gap_length": GAP_LENGTH,
            "stale_runs": len(STALE_PLAN),
            "spikes": len(SPIKE_PLAN),
            "overflow_limit": OVERFLOW_LIMIT,
        },
        "columns": list(TAG_INDEX),
        "row_count": len(rows),
        "time_range": [format_time(times[0]), format_time(times[-1])],
        "anomalies": anomalies,
    }
    return FixtureResult(data_file=data_file, manifest=manifest)
