"""WB-02 媒体契约测试（纯单元，无数据库/临时目录）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from cybercut.contracts.media import (
    ANALYSIS_SCHEMA_VERSION,
    MEDIA_SCHEMA_VERSION,
    AssetAnalysisReport,
    AssetSegment,
    MediaProbeReport,
    TranscriptSegment,
)
from cybercut.contracts.models import NodeRun
from cybercut.contracts.run_input import ArtifactRevisionRef
from cybercut.media.probe import build_probe_report, has_audio_stream

ROTATED_VIDEO_PROBE = {
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1920,
         "height": 1080, "pix_fmt": "yuv420p", "avg_frame_rate": "30000/1001",
         "r_frame_rate": "30000/1001", "time_base": "1/30000", "duration": "3.0",
         "nb_frames": "90",
         "side_data_list": [{"rotation": -90}]},
        {"index": 1, "codec_name": "aac", "codec_type": "audio",
         "sample_rate": "44100", "channels": 2, "duration": "3.0"},
    ],
    "format": {"format_name": "mov,mp4", "duration": "3.0", "size": "12345",
               "bit_rate": "99"},
}

IMAGE_PROBE = {
    "streams": [{"index": 0, "codec_name": "png", "codec_type": "video",
                 "width": 800, "height": 600, "pix_fmt": "rgb24"}],
    "format": {"format_name": "png", "size": "777"},
}


def test_probe_report_rotated_video() -> None:
    report = build_probe_report(ROTATED_VIDEO_PROBE, media_kind="video")
    assert isinstance(report, MediaProbeReport)
    assert report.media_kind == "video"
    assert report.has_video and report.has_audio
    assert report.rotation == -90
    assert report.video is not None
    assert report.video.avg_frame_rate == {"num": 30000, "den": 1001}
    assert report.video.time_base == {"num": 1, "den": 30000}
    assert report.duration_us == 3_000_000
    assert report.audio is not None and report.audio.sample_rate == 44100
    assert report.schema_version == MEDIA_SCHEMA_VERSION


def test_probe_report_image_has_no_duration() -> None:
    report = build_probe_report(IMAGE_PROBE, media_kind="image", source_size_bytes=777)
    assert report.media_kind == "image"
    assert report.duration_us is None, "图片不得伪造视频时长"
    assert report.has_audio is False
    assert report.size_bytes == 777


def test_probe_report_missing_fields_become_none() -> None:
    report = build_probe_report({"streams": [], "format": {}}, media_kind="video")
    assert report.video is None and report.audio is None
    assert report.duration_us is None
    assert report.has_video is False


def test_probe_report_vfr_rational_kept() -> None:
    data = {"streams": [{"index": 0, "codec_name": "h264", "codec_type": "video",
                         "width": 640, "height": 480,
                         "avg_frame_rate": "2997/100"}],
            "format": {}}
    report = build_probe_report(data, media_kind="video")
    assert report.video.avg_frame_rate == {"num": 2997, "den": 100}


def test_probe_report_invalid_rational_ignored() -> None:
    data = {"streams": [{"index": 0, "codec_name": "h264", "codec_type": "video",
                         "width": 640, "height": 480,
                         "avg_frame_rate": "0/0"}],
            "format": {}}
    report = build_probe_report(data, media_kind="video")
    assert report.video.avg_frame_rate is None


def test_probe_report_nan_duration_becomes_none() -> None:
    """NaN/Inf 由转换层吞掉（→ None），而不是写进报告。"""
    bad = {"streams": [{"index": 0, "codec_name": "h264", "codec_type": "video",
                        "width": 640, "height": 480, "duration": "NaN"}],
           "format": {"duration": "NaN"}}
    report = build_probe_report(bad, media_kind="video")
    assert report.duration_us is None
    assert report.video.duration_us is None


def test_transcript_confidence_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start_us=0, end_us=10, text="x", confidence=float("nan"))


def test_has_audio_stream() -> None:
    assert has_audio_stream(ROTATED_VIDEO_PROBE) is True
    assert has_audio_stream(IMAGE_PROBE) is False


def test_transcript_segment_half_open() -> None:
    seg = TranscriptSegment(start_us=1_000_000, end_us=2_000_000, text="hi")
    assert seg.end_us > seg.start_us
    with pytest.raises(ValidationError):
        TranscriptSegment(start_us=2_000_000, end_us=1_000_000, text="bad")


def test_asset_segment_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        AssetSegment(segment_index=0, start_us=-1, end_us=10, is_time_based=True,
                     source="scene_detect")


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        AssetSegment(segment_index=0, start_us=0, end_us=1, is_time_based=True,
                     source="scene_detect", llm_summary="不许有")


def test_analysis_report_defaults_and_schema() -> None:
    report = AssetAnalysisReport(
        asset_id="a1", media_kind="image",
        probe=build_probe_report(IMAGE_PROBE, media_kind="image"),
    )
    assert report.schema_version == ANALYSIS_SCHEMA_VERSION
    assert report.segments == [] and report.transcript == []
    data = report.model_dump_json()
    assert "schema_version" in data


def test_transcript_confidence_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start_us=0, end_us=10, text="x",
                          confidence=float("inf"))


# ══════════════════════════════════════════════════════════════════
# WB-02R5 §四/§五：ArtifactRevisionRef 与 NodeRun 引用契约
# ══════════════════════════════════════════════════════════════════

def _valid_ref() -> dict:
    return {
        "artifact_revision_id": "rev-1",
        "kind": "asset.probe.a1",
        "relative_path": "artifacts/rev-1/probe.json",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "node_run_id": "nr-1",
    }


def test_artifact_revision_ref_roundtrip() -> None:
    ref = ArtifactRevisionRef.model_validate(_valid_ref())
    assert ref.size_bytes == 12
    dumped = ref.model_dump(mode="json")
    assert set(dumped) == set(_valid_ref())
    assert ArtifactRevisionRef.model_validate(dumped) == ref


def test_artifact_revision_ref_rejects_non_hex_sha256() -> None:
    """WB-02R5 §五：sha256 必须严格为 64 位十六进制字符。"""
    for bad in ("z" * 64, "g" * 64, "a" * 63 + "/", " " * 64):
        with pytest.raises(ValidationError):
            ArtifactRevisionRef.model_validate({**_valid_ref(), "sha256": bad})


def test_artifact_revision_ref_normalizes_uppercase_sha256() -> None:
    ref = ArtifactRevisionRef.model_validate({**_valid_ref(), "sha256": "A" * 64})
    assert ref.sha256 == "a" * 64


def test_artifact_revision_ref_rejects_wrong_sha256_length() -> None:
    with pytest.raises(ValidationError):
        ArtifactRevisionRef.model_validate({**_valid_ref(), "sha256": "a" * 63})


def test_artifact_revision_ref_rejects_missing_and_extra_fields() -> None:
    missing = _valid_ref()
    del missing["node_run_id"]
    with pytest.raises(ValidationError):
        ArtifactRevisionRef.model_validate(missing)
    with pytest.raises(ValidationError):
        ArtifactRevisionRef.model_validate({**_valid_ref(), "extra": 1})


def test_artifact_revision_ref_rejects_empty_strings() -> None:
    for field in ("artifact_revision_id", "kind", "relative_path", "node_run_id"):
        with pytest.raises(ValidationError):
            ArtifactRevisionRef.model_validate({**_valid_ref(), field: ""})


def test_artifact_revision_ref_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        ArtifactRevisionRef.model_validate({**_valid_ref(), "size_bytes": -1})


def test_node_run_parses_valid_revision_refs() -> None:
    """WB-02R5 §四：NodeRun.input_revision_refs 为 list[ArtifactRevisionRef]。"""
    node = NodeRun(
        node_run_id="nr-2", run_id="r-1", tenant_id="t-1", node_key="n1",
        status="blocked", input_revision_refs=[_valid_ref()],
    )
    assert len(node.input_revision_refs) == 1
    assert isinstance(node.input_revision_refs[0], ArtifactRevisionRef)
    assert node.input_revision_refs[0].kind == "asset.probe.a1"


def test_node_run_defaults_to_empty_revision_refs() -> None:
    node = NodeRun(node_run_id="nr-3", run_id="r-1", tenant_id="t-1",
                   node_key="n1", status="blocked")
    assert node.input_revision_refs == []


def test_node_run_rejects_invalid_revision_refs() -> None:
    """非法引用必须被拒绝：非十六进制 sha256 / 缺字段 / 额外字段。"""
    bad_sha = _valid_ref()
    bad_sha["sha256"] = "z" * 64
    missing = _valid_ref()
    del missing["relative_path"]
    extra = {**_valid_ref(), "unexpected": "x"}
    for bad in (bad_sha, missing, extra):
        with pytest.raises(ValidationError):
            NodeRun(node_run_id="nr-4", run_id="r-1", tenant_id="t-1",
                    node_key="n1", status="blocked", input_revision_refs=[bad])
