"""QC 检查逻辑（ffprobe + FFmpeg 检测滤镜）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from cybercut.contracts.editing import TimelineSpec
from cybercut.media.toolchain import FFmpegToolchain


def probe_format(toolchain: FFmpegToolchain, path: str) -> dict[str, Any]:
    """ffprobe JSON 读取 format + streams。"""
    return toolchain.ffprobe_json(Path(path))


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"check_id": name, "passed": passed, "detail": detail}


def run_qc(
    *,
    toolchain: FFmpegToolchain,
    output_path: str,
    spec: TimelineSpec,
) -> dict[str, Any]:
    """执行技术 QC 检查，返回原始结果 dict（未包装为 QcReport 模型）。

    检查项：分辨率、帧率、时长偏差、音视频流、黑帧、异常静音。
    """
    probe = probe_format(toolchain, output_path)
    fmt = probe.get("format", {})
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration_s = float(fmt.get("duration", 0))
    duration_us = int(duration_s * 1_000_000)

    checks: list[dict] = []

    # 分辨率
    w = video.get("width", 0) if video else 0
    h = video.get("height", 0) if video else 0
    checks.append(_check(
        "resolution", w == 1080 and h == 1920,
        f"{w}x{h}（期望 1080x1920）"))

    # 帧率
    fps_num = spec.frame_rate.num
    fps_den = spec.frame_rate.den
    expected_fps = fps_num / fps_den
    actual_fps = 0.0
    if video and video.get("avg_frame_rate"):
        parts = str(video["avg_frame_rate"]).split("/")
        if len(parts) == 2:
            actual_fps = int(parts[0]) / max(1, int(parts[1]))
    checks.append(_check(
        "frame_rate", abs(actual_fps - expected_fps) < 0.5,
        f"actual={actual_fps:.2f} expected={expected_fps:.2f}"))

    # 时长偏差（允许帧舍入误差 ±2 帧）
    expected_dur_us = spec.playback_duration_us
    drift_us = duration_us - expected_dur_us
    frame_us = fps_den * 1_000_000 // fps_num if fps_num else 33333
    tolerance = 2 * frame_us
    checks.append(_check(
        "duration", abs(drift_us) <= tolerance,
        f"actual={duration_us}us expected={expected_dur_us}us drift={drift_us}us"))

    # 音视频流
    checks.append(_check(
        "has_video", video is not None and video.get("codec_name") == "h264",
        f"codec={video.get('codec_name') if video else 'N/A'}"))
    checks.append(_check(
        "has_audio", audio is not None and audio.get("codec_name") == "aac",
        f"codec={audio.get('codec_name') if audio else 'N/A'}"))

    # 黑帧检测
    black_frames = _detect_black(toolchain, output_path)
    checks.append(_check(
        "black_frames", len(black_frames) == 0,
        f"{len(black_frames)} black segment(s): {black_frames[:3]}"))

    # 异常静音（仅当有音轨时检查）
    if audio:
        silences = _detect_silence(toolchain, output_path, duration_s)
        total_silence = sum(s.get("end", 0) - s.get("start", 0) for s in silences)
        checks.append(_check(
            "abnormal_silence", total_silence < duration_s * 0.9,
            f"total_silence={total_silence:.1f}s / {duration_s:.1f}s"))
    else:
        checks.append(_check(
            "abnormal_silence", True, "无音轨，跳过静音检查"))

    vcodec = video.get("codec_name", "unknown") if video else "unknown"
    acodec = audio.get("codec_name", "unknown") if audio else "none"

    return {
        "width": w, "height": h,
        "frame_rate_num": fps_num, "frame_rate_den": fps_den,
        "duration_us": duration_us,
        "has_video": video is not None, "has_audio": audio is not None,
        "video_codec": vcodec, "audio_codec": acodec,
        "checks": checks,
        "all_passed": all(c["passed"] for c in checks),
    }


def _detect_black(toolchain: FFmpegToolchain, path: str) -> list[dict]:
    """使用 blackdetect 滤镜检测黑帧。"""
    result = toolchain.run_ffmpeg(
        ["-i", path, "-vf", "blackdetect=d=0.5:pix_th=0.10",
         "-an", "-f", "null", "-"],
    )
    stderr = result.stderr if hasattr(result, "stderr") else ""
    segments = []
    for line in stderr.splitlines():
        if "blackdetect" in line and "black_start" in line:
            start = _extract_field(line, "black_start")
            end = _extract_field(line, "black_end")
            if start is not None and end is not None:
                segments.append({"start": float(start), "end": float(end)})
    return segments


def _detect_silence(
    toolchain: FFmpegToolchain, path: str, duration_s: float,
) -> list[dict]:
    """使用 silencedetect 滤镜检测静音。"""
    result = toolchain.run_ffmpeg(
        ["-i", path, "-af", "silencedetect=n=-50dB:d=2",
         "-f", "null", "-"],
    )
    stderr = result.stderr if hasattr(result, "stderr") else ""
    segments = []
    open_start = None
    for line in stderr.splitlines():
        if "silence_start" in line:
            raw_start = _extract_field(line, "silence_start")
            if raw_start is not None:
                open_start = float(raw_start)
        if "silence_end" in line and open_start is not None:
            raw_end = _extract_field(line, "silence_end")
            if raw_end is not None:
                segments.append({"start": open_start, "end": float(raw_end)})
                open_start = None
    if open_start is not None:
        segments.append({"start": open_start, "end": duration_s})
    return segments


def _extract_field(line: str, field: str) -> str | None:
    idx = line.find(field)
    if idx < 0:
        return None
    rest = line[idx + len(field):].lstrip(" :=")
    token = rest.split(" ")[0].split("\r")[0]
    return token if token else None


def extract_cover(
    *, toolchain: FFmpegToolchain, video_path: str, output_path: str,
    cancel_check: Any = None,
) -> None:
    """从视频首帧导出封面 JPG。"""
    toolchain.run_ffmpeg(
        ["-i", video_path, "-frames:v", "1", "-q:v", "2", output_path],
        cancel_check=cancel_check,
    )


__all__ = ["extract_cover", "probe_format", "run_qc"]
