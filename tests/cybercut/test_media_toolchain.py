"""WB-02 媒体工具层测试（fake runner + 真实超时路径）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cybercut.media.toolchain import (
    FFmpegToolchain,
    MediaToolError,
    SubprocessResult,
    subprocess_runner,
    truncate_error,
)

FAKE_PROBE = {
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1920,
         "height": 1080, "pix_fmt": "yuv420p", "avg_frame_rate": "30000/1001",
         "r_frame_rate": "30/1", "time_base": "1/30000", "duration": "3.0",
         "nb_frames": "90",
         "side_data_list": [{"rotation": -90}]},
        {"index": 1, "codec_name": "aac", "codec_type": "audio",
         "sample_rate": "44100", "channels": 2, "time_base": "1/44100",
         "duration": "3.0"},
    ],
    "format": {"format_name": "mov,mp4,m4a", "duration": "3.0", "size": "12345",
               "bit_rate": "99"},
}


def _chain(runner, **kw) -> FFmpegToolchain:
    return FFmpegToolchain(ffmpeg_path="/fake/ffmpeg", ffprobe_path="/fake/ffprobe",
                           runner=runner, **kw)


def _fake_runner(handler):
    def _run(args, **kwargs):
        argv = [str(a) for a in args]
        return handler(argv, kwargs)
    return _run


def test_ffprobe_json_parses(source_root: Path) -> None:
    media = source_root / "a.mp4"
    media.write_bytes(b"x")

    def handler(argv, kwargs):
        assert argv[0] == "/fake/ffprobe"
        assert "-print_format" in argv and "json" in argv
        assert "-show_format" in argv and "-show_streams" in argv
        assert argv[-1] == str(media).replace("\\", "/")
        return SubprocessResult(0, json.dumps(FAKE_PROBE), "")

    report = _chain(_fake_runner(handler)).ffprobe_json(media)
    assert report["streams"][0]["codec_name"] == "h264"


def test_ffprobe_nonzero_exit_is_probe_failed(source_root: Path) -> None:
    media = source_root / "a.mp4"
    media.write_bytes(b"x")

    def handler(argv, kwargs):
        return SubprocessResult(1, "", "boom: invalid data")

    with pytest.raises(MediaToolError) as excinfo:
        _chain(_fake_runner(handler)).ffprobe_json(media)
    assert excinfo.value.code == "PROBE_FAILED"
    assert "boom" in excinfo.value.stderr


def test_ffprobe_invalid_json_is_probe_failed(source_root: Path) -> None:
    media = source_root / "a.mp4"
    media.write_bytes(b"x")

    def handler(argv, kwargs):
        return SubprocessResult(0, "{not-json", "")

    with pytest.raises(MediaToolError) as excinfo:
        _chain(_fake_runner(handler)).ffprobe_json(media)
    assert excinfo.value.code == "PROBE_FAILED"


def test_tool_not_found_reported(source_root: Path) -> None:
    def handler(argv, kwargs):
        raise MediaToolError("TOOL_NOT_FOUND", "可执行文件不存在：/fake/ffprobe")

    chain = _chain(_fake_runner(handler))
    assert chain.version(which="ffprobe") is None
    report = chain.capability_report()
    assert report.available is False
    assert report.missing_reason


def test_ffmpeg_failure_is_normalize_failed(source_root: Path) -> None:
    out = source_root / "out.mp4"

    def handler(argv, kwargs):
        return SubprocessResult(1, "", "encoder error")

    with pytest.raises(MediaToolError) as excinfo:
        _chain(_fake_runner(handler)).run_ffmpeg(["-i", "in.mp4", str(out)])
    assert excinfo.value.code == "NORMALIZE_FAILED"
    assert not out.exists()


@pytest.fixture
def source_root():
    from tests.cybercut.helpers import make_source_dir
    return make_source_dir()


def test_real_timeout_path(source_root: Path) -> None:
    """真实子进程超时（>0.2s）→ TOOL_TIMEOUT。"""
    runner = subprocess_runner(default_timeout=0.2)
    sleeper = Path(sys.executable)
    with pytest.raises(MediaToolError) as excinfo:
        runner([str(sleeper), "-c", "import time;time.sleep(2)"])
    assert excinfo.value.code == "TOOL_TIMEOUT"


def test_real_cancel_path(source_root: Path) -> None:
    """取消检查置位 → TOOL_CANCELLED 且子进程被 kill。"""
    runner = subprocess_runner(default_timeout=10, poll_interval=0.05)
    flag = {"cancelled": False}

    def cancel():
        flag["cancelled"] = True
        return flag["cancelled"]

    flag["cancelled"] = True
    with pytest.raises(MediaToolError) as excinfo:
        runner([Path(sys.executable), "-c", "import time;time.sleep(3)"], cancel=cancel)
    assert excinfo.value.code == "TOOL_CANCELLED"


def test_truncate_error_keeps_tail() -> None:
    text = "A" * 3000 + "TAIL"
    trimmed = truncate_error(text, limit=100)
    assert trimmed.endswith("TAIL")
    assert "截断" in trimmed


def test_large_output_no_deadlock():
    """>64KB stdout 输出不得导致 PIPE 死锁（WB-02R3 §5）。"""
    runner = subprocess_runner(default_timeout=10, poll_interval=0.05)
    result = runner([Path(sys.executable), "-c",
                     "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"])
    assert len(result.stdout.strip()) >= 200_000
    assert result.returncode == 0


def test_large_stderr_no_deadlock():
    """>64KB stderr 输出不得导致 PIPE 死锁（WB-02R3 §5）。"""
    runner = subprocess_runner(default_timeout=10, poll_interval=0.05)
    result = runner([Path(sys.executable), "-c",
                     "import sys; sys.stderr.write('e' * 200000); sys.stderr.flush()"])
    assert len(result.stderr.strip()) >= 200_000
    assert result.returncode == 0


def test_subprocess_output_decodes_utf8() -> None:
    runner = subprocess_runner(default_timeout=10, poll_interval=0.05)
    result = runner([
        Path(sys.executable), "-c",
        "import sys;sys.stdout.buffer.write('中文路径'.encode('utf-8'))",
    ])

    assert result.stdout == "中文路径"
