"""整数帧 ↔ 微秒换算（**纯整数运算**，确定性，无浮点中间值）。

约定：
- `fps = num / den`（帧/秒），`den > 0` 且已约分；
- `frames_for_duration_us(d)` = `floor(d * num / (den * 1_000_000))`，
  且**至少 1 帧**（时间线上不允许 0 帧片段）；
- `duration_us_for_frames(f)` = `floor(f * den * 1_000_000 / num)`；
- 两者互逆性不保证（有理数帧率下必然有舍入），偏差由调用方显式记录。
"""
from __future__ import annotations

from cybercut.contracts.editing import FrameRate

MICROS_PER_SECOND = 1_000_000


def frames_for_duration_us(duration_us: int, fps: FrameRate) -> int:
    """把微秒时长换算为**整数帧数**（floor，至少 1 帧）。"""
    if duration_us <= 0:
        raise ValueError(f"时长必须为正：{duration_us}")
    num, den = fps.as_tuple()
    raw = (duration_us * num) // (den * MICROS_PER_SECOND)
    return max(1, raw)


def duration_us_for_frames(frames: int, fps: FrameRate) -> int:
    """把整数帧数换算回微秒时长（floor）。"""
    if frames <= 0:
        raise ValueError(f"帧数必须为正：{frames}")
    num, den = fps.as_tuple()
    return (frames * den * MICROS_PER_SECOND) // num


def frames_for_interval(start_us: int, end_us: int, fps: FrameRate) -> int:
    """把源素材微秒区间（半开）换算为帧数（floor，至少 0）。"""
    if end_us < start_us:
        raise ValueError(f"区间非法：[{start_us}, {end_us})")
    num, den = fps.as_tuple()
    return ((end_us - start_us) * num) // (den * MICROS_PER_SECOND)


__all__ = [
    "MICROS_PER_SECOND",
    "duration_us_for_frames",
    "frames_for_duration_us",
    "frames_for_interval",
]
