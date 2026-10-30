"""ffprobe JSON → MediaProbeReport 的**纯函数**转换（可独立单测）。"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.media import (
    AudioStreamInfo,
    MediaProbeReport,
    VideoStreamInfo,
)


def _rational(text: Any) -> dict[str, int] | None:
    """把 ffprobe 的 `num/den` 字符串转成有理数；非法/为 0 分母返回 None。"""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or "/" not in text:
        return None
    num, _, den = text.partition("/")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return None
    if d == 0:
        return None
    return {"num": n, "den": d}


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "", "N/A"):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return int(round(result))


def _rotation(stream: dict[str, Any], tags: dict[str, str]) -> int | None:
    side = stream.get("side_data_list") or []
    for item in side:
        if "rotation" in item:
            try:
                return int(float(item["rotation"]))
            except (TypeError, ValueError):
                pass
    disp = tags.get("rotate")
    if disp:
        try:
            return int(float(disp))
        except (TypeError, ValueError):
            return None
    return None


def build_probe_report(
    data: dict[str, Any], *, media_kind: str, source_size_bytes: int | None = None
) -> MediaProbeReport:
    """从 ffprobe JSON 构造 MediaProbeReport（缺字段 → None，不臆造）。"""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    tags = {str(k): str(v) for k, v in (fmt.get("tags") or {}).items()}

    video_stream: VideoStreamInfo | None = None
    audio_stream: AudioStreamInfo | None = None
    rotation: int | None = None
    for index, stream in enumerate(streams):
        ctype = stream.get("codec_type")
        if ctype == "video" and video_stream is None:
            rotation = _rotation(stream, {str(k): str(v)
                                          for k, v in (stream.get("tags") or {}).items()})
            video_stream = VideoStreamInfo(
                index=int(stream.get("index", index)),
                codec_name=str(stream.get("codec_name") or "unknown"),
                codec_type="video",
                width=_int_or_none(stream.get("width")) or 0,
                height=_int_or_none(stream.get("height")) or 0,
                pix_fmt=stream.get("pix_fmt"),
                avg_frame_rate=_rational(stream.get("avg_frame_rate")),
                r_frame_rate=_rational(stream.get("r_frame_rate")),
                time_base=_rational(stream.get("time_base")),
                duration_us=_int_or_none(stream.get("duration")) and int(
                    round(float(stream["duration"]) * 1_000_000)
                ),
                nb_frames=_int_or_none(stream.get("nb_frames")),
                rotation=rotation,
            )
        elif ctype == "audio" and audio_stream is None:
            audio_stream = AudioStreamInfo(
                index=int(stream.get("index", index)),
                codec_name=str(stream.get("codec_name") or "unknown"),
                codec_type="audio",
                sample_rate=_int_or_none(stream.get("sample_rate")),
                channels=_int_or_none(stream.get("channels")),
                time_base=_rational(stream.get("time_base")),
                duration_us=_int_or_none(stream.get("duration")) and int(
                    round(float(stream["duration"]) * 1_000_000)
                ),
            )

    duration_us = _int_or_none(fmt.get("duration"))
    if duration_us is not None:
        duration_us = int(round(duration_us * 1_000_000))
    if video_stream is not None and video_stream.duration_us is not None:
        duration_us = video_stream.duration_us

    return MediaProbeReport(
        media_kind=media_kind,  # type: ignore[arg-type]
        format_name=fmt.get("format_name"),
        duration_us=duration_us,
        size_bytes=source_size_bytes
        if source_size_bytes is not None
        else _int_or_none(fmt.get("size")),
        bit_rate=_int_or_none(fmt.get("bit_rate")),
        video=video_stream,
        audio=audio_stream,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        rotation=rotation,
        raw_format_tags=tags,
    )


def has_audio_stream(probe_json: dict[str, Any]) -> bool:
    return any(s.get("codec_type") == "audio" for s in (probe_json.get("streams") or []))


__all__ = ["build_probe_report", "has_audio_stream"]
