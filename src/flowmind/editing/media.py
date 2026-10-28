"""媒体层：ffprobe 元数据 / 场景切分 / 字幕 / 剪辑渲染（ffmpeg 子进程）。

约定：
- 所有 ffmpeg/ffprobe 子进程带 timeout（config.editing 无单独项时取 30min
  上限防挂死）；失败带 stderr 片段显式抛 MediaError（错误永不静默）。
- 渲染 v1 能力边界（见 docs/agent-video-editing.md §7）：视频裁剪 + 图片
  Ken Burns + xfade(fade/none) 转场 + 字幕烧录 + 口播音轨。音频策略 v1 =
  有口播用口播，否则静音轨（原声混音/BGM 自动选段留 v2）。
- mock=True：用 lavfi color 源产出真实可播放的占位 mp4（demo 冒烟用），
  不做字幕烧录。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from flowmind.editing.models import ShotInfo, TimelineDoc, TimelineShot

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_S = 1800.0   # 单条 ffmpeg 命令上限（30min，防挂死）
_PTS_RE = re.compile(r"pts_time:\s*([\d.]+)")


class MediaError(Exception):
    """媒体处理失败。category 语义与 errors.py 一致。"""

    def __init__(self, message: str, category: str = "environment",
                 retriable: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.retriable = retriable


def ensure_ffmpeg() -> str:
    """确认 ffmpeg/ffprobe 可用，返回 ffmpeg 路径；缺失显式报错。"""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaError(
            "未找到 ffmpeg/ffprobe（PATH 不可见）。请安装 ffmpeg 后重试。",
            category="environment")
    return ffmpeg


def classify_asset(path: str) -> str:
    """按扩展名判定素材类型 video/image/audio。无法识别显式报错。"""
    ext = Path(path).suffix.lower()
    if ext in (".mp4", ".mov", ".mkv", ".webm"):
        return "video"
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "image"
    if ext in (".mp3", ".wav", ".m4a", ".aac"):
        return "audio"
    raise MediaError(f"无法识别的素材扩展名：{ext}（{path}）",
                     category="video")


def _run(cmd: list[str], *, timeout_s: float = _SUBPROCESS_TIMEOUT_S) -> str:
    """执行命令，返回 stdout；非零退出带 stderr 片段抛 MediaError。"""
    logger.debug("media 子进程：%s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            f"媒体子进程超时（>{int(timeout_s)}s）：{cmd[0]}",
            category="transient", retriable=True) from exc
    except FileNotFoundError as exc:
        raise MediaError(f"找不到可执行文件：{cmd[0]}", category="environment") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-600:]
        raise MediaError(
            f"媒体子进程失败（exit={proc.returncode}）：{cmd[0]} …{tail}",
            category="video")
    return proc.stdout or ""


def probe(path: str) -> dict:
    """ffprobe 元数据（format + streams 原始 dict）。"""
    ensure_ffmpeg()
    out = _run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", path,
    ])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe 输出不可解析：{exc}") from exc


def probe_summary(path: str) -> dict:
    """精简元数据：{duration_s,width,height,fps,has_audio}。图片给 0 时长。"""
    kind = classify_asset(path)
    data = probe(path)
    fmt_dur = float(data.get("format", {}).get("duration") or 0.0)
    width = height = 0
    fps = 0.0
    has_audio = False
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not width:
            width = int(st.get("width") or 0)
            height = int(st.get("height") or 0)
            rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1"
            try:
                num, den = rate.split("/")
                fps = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            if not fmt_dur and st.get("duration"):
                fmt_dur = float(st["duration"])
        elif st.get("codec_type") == "audio":
            has_audio = True
    if kind == "image":
        fmt_dur = 0.0
    return {
        "kind": kind, "duration_s": fmt_dur, "width": width,
        "height": height, "fps": round(fps, 2), "has_audio": has_audio,
    }


def scene_shots(path: str, *, threshold: float = 0.3,
                max_shots: int = 12) -> list[ShotInfo]:
    """视频场景切分（ffmpeg select='gt(scene,T)' + showinfo 解析时间戳）。

    镜头数超上限时按时间均匀抽样边界（保节奏分布，不保每个镜头）。
    """
    ensure_ffmpeg()
    vf = f"select='gt(scene,{threshold})',showinfo"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-vf", vf,
         "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_S)
    # 有场景切分时 exit 仍为 0；非 0 视为素材损坏
    if proc.returncode != 0:
        raise MediaError(
            f"场景切分失败（exit={proc.returncode}）：{(proc.stderr or '')[-400:]}",
            category="video")
    times = [float(m) for m in _PTS_RE.findall(proc.stderr or "")]
    summary = probe_summary(path)
    duration = summary["duration_s"]
    if duration <= 0:
        raise MediaError(f"视频时长非法（{duration}）：{path}", category="video")
    # 去重 + 去掉贴边时间点
    times = sorted({round(t, 3) for t in times if 0.05 < t < duration - 0.05})
    if len(times) >= max_shots:   # 边界数 ≥ 上限：均匀抽样 max_shots-1 个
        step = len(times) / (max_shots - 1)
        times = [times[int(i * step)] for i in range(max_shots - 1)]
    bounds = [0.0] + times + [duration]
    return [
        ShotInfo(index=i, start_s=bounds[i], end_s=bounds[i + 1],
                 score=1.0 if 0 < i < len(bounds) - 1 else 0.0)
        for i in range(len(bounds) - 1)
    ]


def extract_frames(path: str, times: list[float], out_dir: Path, *,
                   width: int = 480, prefix: str = "frame") -> list[tuple[float, Path]]:
    """在指定时刻抽帧（jpeg），返回 [(t, 路径)]——vision 看图输入。"""
    ensure_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[float, Path]] = []
    for i, t in enumerate(times):
        dst = out_dir / f"{prefix}_{i:02d}.jpg"
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "4", str(dst),
        ], timeout_s=120)
        out.append((t, dst))
    return out


def extract_preview(video: str, out_dir: Path, *,
                    count: int = 6) -> list[Path]:
    """成片均匀抽帧（自检/给用户过目）：preview/frame_XX.jpg。"""
    info = probe_summary(video)
    duration = info["duration_s"]
    times = [duration * (i + 0.5) / count for i in range(count)]
    return [p for _, p in extract_frames(
        video, times, out_dir, width=360, prefix="preview")]


def build_srt(shots: list[TimelineShot]) -> str:
    """按镜头时间轴生成 SRT 文本（同 section 的连续镜头字幕不重复展开）。"""

    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    idx = 1
    for shot in shots:
        text = (shot.subtitle_text or "").strip()
        if not text:
            continue
        lines += [str(idx), f"{ts(shot.global_start_s)} --> {ts(shot.global_end_s)}",
                  text, ""]
        idx += 1
    return "\n".join(lines)


def _escape_filter_path(path: str) -> str:
    """ffmpeg filter 字符串里的文件路径转义（Windows 盘符冒号需反斜杠）。"""
    return path.replace("\\", "/").replace(":", "\\:")


def _x264_args(preset: str, bitrate: str) -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-b:v", bitrate,
            "-pix_fmt", "yuv420p"]


def render(timeline: TimelineDoc, *, workdir: Path, subtitle_srt: Path,
           mock: bool = False, transition_duration_s: float = 0.5,
           subtitle_font: str = "Noto Sans CJK SC",
           subtitle_font_size: int = 22,
           x264_preset: str = "medium",
           video_bitrate: str = "6M") -> Path:
    """按时间线渲染成片，返回 output.mp4 路径。"""
    ensure_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "output.mp4"
    dur = max(1.0, timeline.duration_s)

    if mock:
        _render_mock(timeline, out, duration=dur)
        return out

    segs = _normalize_segments(timeline, workdir)
    joined = _join_segments(segs, timeline, workdir,
                            transition_duration_s=transition_duration_s)
    _burn_and_mux(joined, timeline, subtitle_srt, out,
                  subtitle_font=subtitle_font,
                  subtitle_font_size=subtitle_font_size,
                  x264_preset=x264_preset, video_bitrate=video_bitrate)
    return out


def _render_mock(timeline: TimelineDoc, out: Path, *, duration: float) -> None:
    """mock 渲染：lavfi color 源 + 静音轨，产出真实可播放的占位 mp4。"""
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"color=c=0x1f6feb:s={timeline.width}x{timeline.height}:"
        f"d={duration}:r={timeline.fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-shortest", *_x264_args("ultrafast", "2M"), "-c:a", "aac",
        str(out),
    ], timeout_s=300)


def _normalize_segments(timeline: TimelineDoc, workdir: Path) -> list[Path]:
    """Pass 1：每个镜头归一化为同参数中间段（统一画幅/帧率/编码，-an）。"""
    segs: list[Path] = []
    vf_base = (
        "scale={w}:{h}:force_original_aspect_ratio=decrease,"
        "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )
    for i, shot in enumerate(timeline.shots):
        seg = workdir / f"seg_{i:03d}.mp4"
        vf = vf_base.format(w=timeline.width, h=timeline.height, fps=timeline.fps)
        if shot.mode == "image_kenburns":
            vf = _kenburns_vf(shot, timeline) + "," + vf
            _run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-t", f"{shot.duration_s:.3f}",
                "-i", shot.source_path, "-vf", vf, "-an",
                *_x264_args("veryfast", "4M"), str(seg),
            ])
        else:
            # v1.1 变速/定格：输出时长 = duration_s（含定格）；
            # 内容段 = duration_s − freeze_s，源窗口 = 内容段 × speed，
            # setpts 提速/慢放，tpad 克隆尾帧做定格
            content_s = max(0.2, shot.duration_s - shot.freeze_s)
            src_window = content_s * shot.speed
            clip_vf = vf
            if abs(shot.speed - 1.0) > 1e-3:
                clip_vf = f"setpts=PTS/{shot.speed}," + vf
            if shot.freeze_s > 0:
                clip_vf += (f",tpad=stop_mode=clone:"
                            f"stop_duration={shot.freeze_s:.3f}")
            _run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{shot.source_in:.3f}",
                "-t", f"{src_window:.3f}",
                "-i", shot.source_path, "-vf", clip_vf, "-an",
                *_x264_args("veryfast", "4M"), str(seg),
            ])
        segs.append(seg)
    return segs


def _kenburns_vf(shot: TimelineShot, timeline: TimelineDoc) -> str:
    """图片 Ken Burns（zoompan；先放大 2 倍防抖）。pan 决定运动方向。"""
    frames = max(2, int(shot.duration_s * timeline.fps))
    w, h = timeline.width, timeline.height
    # 输出分辨率给 2 倍再由下游 scale/pad 归一；zoom 0→0.15 缓动
    if shot.pan == "out":
        zoom = f"if(eq(on,1),1.15,max(1.15-0.15*on/{frames},1.0))"
    else:   # in / left / right / none 均用渐进放大
        zoom = f"min(1+0.15*on/{frames},1.15)"
    if shot.pan == "left":
        x = f"(iw-iw/zoom)*on/{frames}"
    elif shot.pan == "right":
        x = f"(iw-iw/zoom)*(1-on/{frames})"
    else:
        x = "iw/2-(iw/zoom/2)"
    y = "ih/2-(ih/zoom/2)"
    return (f"scale=8000:-2,zoompan=z='{zoom}':x='{x}':y='{y}'"
            f":d={frames}:s={w * 2}x{h * 2}:fps={timeline.fps}")


def transition_steps(shots: list[TimelineShot], *,
                     transition_duration_s: float,
                     fps: int) -> list[float]:
    """每个镜头前的转场耗时（xfade 重叠量）：首镜 0，其余按转场类型。

    none = 1 帧 epsilon（见 _join_segments 注释：duration=0 会触发截断）；
    step 以相邻两段半时长为上限防越界。时间线/字幕与成片对齐依赖此函数。

    全 none 时 _join_segments 走 concat demuxer（零重叠），steps 必须全 0，
    否则 SRT/时间线按重叠折算会逐镜漂移（16 镜实测尾部漂 0.5s）。
    """
    if all(s.transition == "none" for s in shots):
        return [0.0] * len(shots)
    eps = 1.0 / max(1, fps)
    steps = [0.0] * len(shots)
    for k in range(1, len(shots)):
        step = (transition_duration_s
                if shots[k].transition != "none" else eps)
        steps[k] = min(step, shots[k - 1].duration_s * 0.5,
                       shots[k].duration_s * 0.5)
    return steps


def effective_global_starts(shots: list[TimelineShot], *,
                            transition_duration_s: float,
                            fps: int) -> list[float]:
    """折算转场重叠后的成片时间轴起点（与 _join_segments 输出一致）。

    output 总时长 = Σduration − Σstep；SRT/timeline 用此对齐成片。
    """
    steps = transition_steps(
        shots, transition_duration_s=transition_duration_s, fps=fps)
    starts: list[float] = []
    cursor = 0.0
    for i, shot in enumerate(shots):
        cursor -= steps[i]
        starts.append(round(cursor, 3))
        cursor += shot.duration_s
    return starts


def _join_segments(segs: list[Path], timeline: TimelineDoc, workdir: Path,
                   *, transition_duration_s: float) -> Path:
    """Pass 2：拼接（none=concat demuxer；fade=xfade 链）。"""
    joined = workdir / "joined.mp4"
    if len(segs) == 1:
        shutil.copyfile(segs[0], joined)
        return joined
    shots = timeline.shots
    if all(s.transition == "none" for s in shots):
        lst = workdir / "concat.txt"
        lst.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in segs),
            encoding="utf-8")
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", str(lst),
              *_x264_args("veryfast", "4M"), str(joined)])
        return joined

    # xfade 链：offset_k = Σd(0..k-1) − k·D
    durs = [s.duration_s for s in shots]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in segs:
        cmd += ["-i", str(p)]
    filters: list[str] = []
    for i in range(len(segs)):
        filters.append(
            f"[{i}:v]fps={timeline.fps},"
            f"scale={timeline.width}:{timeline.height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,format=yuv420p[v{i}]")
    prev = "v0"
    offset = 0.0
    steps = transition_steps(
        shots, transition_duration_s=transition_duration_s,
        fps=timeline.fps)
    for k in range(1, len(segs)):
        offset += durs[k - 1]
        label = f"x{k}" if k < len(segs) - 1 else "vout"
        # "none"（硬切）不能传 duration=0 且 offset 顶到输入边界——ffmpeg 的
        # xfade 要求 offset 严格小于当前输入时长，等于边界会直接截断后续
        # 输入（实测 D=0/offset=2.0 输出停在 2.0s）。none 用 1 帧 epsilon
        # 交叉溶解，观感即硬切
        step = steps[k]
        offset -= step
        filters.append(
            f"[{prev}][v{k}]xfade=transition=fade:duration={step:.4f}:"
            f"offset={max(0.0, offset):.3f}[{label}]")
        prev = label
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]",
            *_x264_args("veryfast", "4M"), str(joined)]
    _run(cmd)
    return joined


def _burn_and_mux(joined: Path, timeline: TimelineDoc, subtitle_srt: Path,
                  out: Path, *, subtitle_font: str, subtitle_font_size: int,
                  x264_preset: str, video_bitrate: str) -> None:
    """Pass 3：字幕烧录 + 音轨（口播或静音）。"""
    force = (
        f"FontName={subtitle_font},FontSize={subtitle_font_size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,"
        "Outline=1.5,Alignment=2,MarginV=28"
    )
    # Windows 盘符冒号必须转义，且文件名要用单引号包住（否则滤镜解析器
    # 仍会把转义冒号后的内容当第二个选项，报 original_size 解析错误）
    vf = (f"subtitles=filename='{_escape_filter_path(str(subtitle_srt))}'"
          f":force_style='{force}'")
    # 注意：-vf 是输出选项，必须放在全部输入之后（放输入之间会被解析成
    # 输入选项，ffmpeg 直接报错）
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(joined)]
    if timeline.voiceover_path:
        cmd += ["-i", timeline.voiceover_path]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest",
            *_x264_args(x264_preset, video_bitrate),
            "-c:a", "aac", "-b:a", "192k", str(out)]
    _run(cmd)
