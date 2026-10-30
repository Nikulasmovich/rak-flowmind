from __future__ import annotations

from types import SimpleNamespace

import pytest

from cybercut.rendering.ffmpeg_renderer import (
    build_clip_args,
    build_drawtext_args,
)
from cybercut.rendering.qc import _detect_black, _detect_silence


def test_image_clip_gets_silent_aac_track() -> None:
    args = build_clip_args(
        clip_index=0, source_path="poster.png", media_kind="image",
        source_in_us=0, source_out_us=0, playback_duration_us=2_000_000,
        fps_num=30, fps_den=1, has_audio=False, output_path="clip.mp4",
    )

    assert "-an" not in args
    assert args[args.index("-map") + 1] == "0:v:0"
    assert "1:a:0" in args
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in args


def test_silent_video_clip_gets_silent_aac_track() -> None:
    args = build_clip_args(
        clip_index=0, source_path="silent.mp4", media_kind="video",
        source_in_us=0, source_out_us=2_000_000,
        playback_duration_us=2_000_000, fps_num=30, fps_den=1,
        has_audio=False, output_path="clip.mp4",
    )

    assert "1:a:0" in args
    assert "-af" in args and "apad" in args


def test_drawtext_time_uses_timeline_frame_rate() -> None:
    args = build_drawtext_args(
        input_path="in.mp4",
        text_intents=[{"start_frame": 25, "end_frame": 50, "text": "title"}],
        font_path="font.ttf", fps_num=25, fps_den=1, output_path="out.mp4",
    )

    assert "between(t,1.000,2.000)" in args[args.index("-vf") + 1]


class _Toolchain:
    def __init__(self, stderr: str = "", error: Exception | None = None) -> None:
        self.stderr = stderr
        self.error = error

    def run_ffmpeg(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return SimpleNamespace(stderr=self.stderr)


def test_silence_detection_pairs_start_and_end_and_closes_at_eof() -> None:
    toolchain = _Toolchain(
        "silence_start: 1.0\nsilence_end: 4.5 | silence_duration: 3.5\n"
        "silence_start: 8.0\n"
    )

    assert _detect_silence(toolchain, "out.mp4", 10.0) == [
        {"start": 1.0, "end": 4.5},
        {"start": 8.0, "end": 10.0},
    ]


@pytest.mark.parametrize("detector", [_detect_black, _detect_silence])
def test_qc_detection_failure_propagates(detector) -> None:
    toolchain = _Toolchain(error=RuntimeError("ffmpeg unavailable"))

    with pytest.raises(RuntimeError, match="ffmpeg unavailable"):
        if detector is _detect_black:
            detector(toolchain, "out.mp4")
        else:
            detector(toolchain, "out.mp4", 10.0)
