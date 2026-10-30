"""FFmpeg 多阶段合成（确定性，通过 FFmpegToolchain 执行）。

合成策略（ADR-0006 §2）——三阶段：
  Pass 1  逐片段裁切 + 画幅适配 → 中间 MP4（统一 1080x1920/fps）
  Pass 2  concat 拼接全部片段 → 中间 MP4
  Pass 3  drawtext 文字覆盖 → 最终 MP4
  Cover   首帧截取 → JPG

每阶段独立可验证；中间文件由调用方清理。
`drawtext` 使用显式 `fontfile`，不依赖 fontconfig。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from cybercut.contracts.editing import TimelineSpec
from cybercut.contracts.errors import ValidationError
from cybercut.media.toolchain import FFmpegToolchain

COMPOSITE_PROMPT_VERSION = "render-composite.v1"
COMPOSITE_MODEL_ID = "deterministic:ffmpeg-render-v1"

#: 目标画幅
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920

#: 默认字体路径（Windows 中文黑体；运行时可通过参数覆盖）
DEFAULT_FONT_PATH = "C:/Windows/Fonts/simhei.ttf"


def _esc_drawtext(text: str) -> str:
    """转义 drawtext 特殊字符。"""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("\n", " ")
    )


def _esc_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def build_clip_args(
    *,
    clip_index: int,
    source_path: str,
    media_kind: str,
    source_in_us: int,
    source_out_us: int,
    playback_duration_us: int,
    fps_num: int,
    fps_den: int,
    has_audio: bool,
    output_path: str,
) -> list[str]:
    """构建 Pass 1（片段裁切 + 画幅适配）的 ffmpeg 参数。"""
    fps_str = f"{fps_num}/{fps_den}"
    vf = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"fps={fps_str}"
    )
    if media_kind == "image":
        dur_s = playback_duration_us / 1_000_000
        return [
            "-loop", "1", "-t", f"{dur_s:.6f}",
            "-i", source_path,
            "-f", "lavfi", "-t", f"{dur_s:.6f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-t", f"{dur_s:.6f}",
            output_path,
        ]
    # video
    ss_s = source_in_us / 1_000_000
    to_s = source_out_us / 1_000_000
    args = [
        "-ss", f"{ss_s:.6f}", "-to", f"{to_s:.6f}",
        "-i", source_path,
    ]
    if not has_audio:
        args.extend([
            "-f", "lavfi", "-t", f"{playback_duration_us / 1_000_000:.6f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0",
        ])
    args.extend([
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-af", "apad",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{playback_duration_us / 1_000_000:.6f}", output_path,
    ])
    return args


def build_concat_file(clips: list[str], concat_list_path: str) -> None:
    """构建 concat demuxer 列表文件。"""
    lines = []
    for clip_path in clips:
        escaped = clip_path.replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    Path(concat_list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_drawtext_args(
    *,
    input_path: str,
    text_intents: list[dict[str, Any]],
    font_path: str,
    fps_num: int,
    fps_den: int,
    output_path: str,
) -> list[str]:
    """构建 Pass 3（drawtext 文字覆盖）的 ffmpeg 参数。

    每个文本意图通过 `enable='between(t,start,end)'` 在指定帧窗口内显示。
    """
    filters = []
    for intent in text_intents:
        fps = fps_num / fps_den
        start_s = intent["start_frame"] / fps
        end_s = intent["end_frame"] / fps
        text = _esc_drawtext(str(intent.get("text", "")))
        font = _esc_path(font_path)
        filters.append(
            f"drawtext=fontfile='{font}'"
            f":text='{text}'"
            f":fontcolor=white:fontsize=48"
            f":x=(w-text_w)/2:y=h-text_h-200"
            f":box=1:boxcolor=black@0.5:boxborderw=8"
            f":enable='between(t,{start_s:.3f},{end_s:.3f})'"
        )
    vf = ",".join(filters) if filters else "null"
    return [
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        output_path,
    ]


def build_cover_args(*, input_path: str, output_path: str) -> list[str]:
    """封面导出（首帧截取）。"""
    return [
        "-i", input_path,
        "-frames:v", "1", "-q:v", "2",
        output_path,
    ]


def render_spec(
    *,
    spec: TimelineSpec,
    source_paths: dict[str, str],
    output_dir: Path,
    toolchain: FFmpegToolchain,
    font_path: str = DEFAULT_FONT_PATH,
    cancel_check: Any = None,
) -> dict[str, Any]:
    """执行三阶段合成，返回 `{output_path, cover_path, clip_paths, warnings}`。

    这是**确定性**合成：相同 spec + 相同源素材 ⇒ 相同输出。
    """
    if not spec.clips:
        raise ValidationError(
            "TimelineSpec 没有任何片段，无法渲染",
            details={"unresolved_shots": len(spec.unresolved_shots)},
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    # ── Pass 1: 逐片段裁切 + 画幅适配 ──
    clip_paths: list[str] = []
    for clip in spec.clips:
        source = source_paths.get(clip.asset_id)
        if not source or not Path(source).exists():
            raise ValidationError(
                f"片段 {clip.clip_id} 的源素材不存在：{clip.asset_id!r}",
                details={"source_path": source},
            )
        clip_out = str(output_dir / f"clip_{clip.sequence:04d}.mp4")
        has_audio = False
        if clip.media_kind == "video":
            probe = toolchain.ffprobe_json(Path(source))
            has_audio = any(
                stream.get("codec_type") == "audio"
                for stream in probe.get("streams", [])
            )
        args = build_clip_args(
            clip_index=clip.sequence,
            source_path=source,
            media_kind=clip.media_kind,
            source_in_us=clip.source_in_us,
            source_out_us=clip.source_out_us,
            playback_duration_us=clip.playback_duration_us,
            fps_num=spec.frame_rate.num,
            fps_den=spec.frame_rate.den,
            has_audio=has_audio,
            output_path=clip_out,
        )
        toolchain.run_ffmpeg(args, cancel_check=cancel_check)
        clip_paths.append(clip_out)

    # ── Pass 2: concat 拼接 ──
    concat_list = str(output_dir / "concat_list.txt")
    build_concat_file(clip_paths, concat_list)
    concat_out = str(output_dir / "concat_pass.mp4")
    toolchain.run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", concat_list,
         "-c:v", "libx264", "-preset", "fast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         concat_out],
        cancel_check=cancel_check,
    )

    # ── Pass 3: drawtext 文字覆盖 ──
    final_out = str(output_dir / "final.mp4")
    intents = [i.model_dump(mode="json") for i in spec.text_intents]
    if intents:
        dt_args = build_drawtext_args(
            input_path=concat_out,
            text_intents=intents,
            font_path=font_path,
            fps_num=spec.frame_rate.num,
            fps_den=spec.frame_rate.den,
            output_path=final_out,
        )
        toolchain.run_ffmpeg(dt_args, cancel_check=cancel_check)
    else:
        final_out = concat_out  # 无文字意图时直接使用

    # ── Cover ──
    cover_out = str(output_dir / "cover.jpg")
    toolchain.run_ffmpeg(
        build_cover_args(input_path=final_out, output_path=cover_out),
        cancel_check=cancel_check,
    )

    # ── 清理中间文件 ──
    for cp in clip_paths:
        Path(cp).unlink(missing_ok=True)
    Path(concat_list).unlink(missing_ok=True)
    if concat_out != final_out:
        Path(concat_out).unlink(missing_ok=True)

    return {
        "output_path": final_out,
        "cover_path": cover_out,
        "clip_paths": clip_paths,
        "warnings": sorted(set(warnings)),
    }


__all__ = [
    "COMPOSITE_MODEL_ID",
    "COMPOSITE_PROMPT_VERSION",
    "DEFAULT_FONT_PATH",
    "TARGET_HEIGHT",
    "TARGET_WIDTH",
    "build_clip_args",
    "build_concat_file",
    "build_cover_args",
    "build_drawtext_args",
    "render_spec",
]
