"""WB-02R §七：媒体节点单元测试（fake toolchain / detector / ASR，无数据库）。"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from cybercut.contracts.errors import NodeExecutionError
from cybercut.contracts.media import AssetAnalysisReport, AssetSegment
from cybercut.media.asr import AsrDependencyMissing, TranscriptSegment
from cybercut.media.scenes import SceneDetectDependencyMissing
from cybercut.media.toolchain import FFmpegToolchain, MediaToolError, SubprocessResult
from cybercut.nodes.media_nodes import (
    AssetAnalysisHandler,
    AssetNormalizeHandler,
    AssetProbeHandler,
    SceneDetectHandler,
    SpeechTranscribeHandler,
)
from cybercut.storage.paths import resolve_official_asset_path

TENANT = "tenant-a"
PROJECT = "proj-nodes"
ASSET_A = "aaaa1111-1111-4111-8111-111111111111"
ASSET_B = "bbbb2222-2222-4222-8222-222222222222"

FAKE_SOURCE_PROBE = {
    "streams": [
        {"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1920,
         "height": 1080, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
         "r_frame_rate": "30/1", "duration": "2.0"},
        {"index": 1, "codec_name": "aac", "codec_type": "audio",
         "sample_rate": "44100", "channels": 2, "duration": "2.0"},
    ],
    "format": {"format_name": "mov,mp4", "duration": "2.0", "size": "4096"},
}

FAKE_IMAGE_PROBE = {
    "streams": [{"index": 0, "codec_name": "png", "codec_type": "video",
                 "width": 800, "height": 600, "pix_fmt": "rgb24"}],
    "format": {"format_name": "png", "size": "888"},
}


@pytest.fixture
def tmp_root():
    from tests.cybercut.helpers import make_source_dir
    return make_source_dir()


def make_context(tmp_root: Path):
    project_root = tmp_root / "projects" / PROJECT
    attempt_dir = project_root / ".attempts" / "att-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    class _Ctx:
        pass

    from cybercut.contracts.node_handler import NodeContext

    return NodeContext(
        attempt_id="att-1", node_run_id="nr-1", run_id="run-1",
        project_id=PROJECT, tenant_id=TENANT, node_key="x", attempt_no=1,
        project_root=project_root, data_root=tmp_root, worker_id="w",
        attempt_dir=attempt_dir,
        progress_cb=lambda *a, **k: None,
        cancel_check=lambda: False,
    )


def make_fake_chain(probe_by_path: dict[str, dict]):
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append(argv)
        if argv[0].endswith("ffprobe"):
            data = probe_by_path.get(argv[-1], {"streams": [], "format": {}})
            return SubprocessResult(0, json.dumps(data), "")
        if argv[0].endswith("ffmpeg"):
            out = argv[-1]
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"FAKE-MEDIA-OUTPUT")
            return SubprocessResult(0, "", "")
        if "-version" in argv:
            return SubprocessResult(0, "ffmpeg version 7.0-fake", "")
        if "-encoders" in argv:
            return SubprocessResult(0, " V libx264 fake\n", "")
        if "-filters" in argv:
            return SubprocessResult(0, " V V->V scale fake\n", "")
        return SubprocessResult(0, "", "")

    return FFmpegToolchain(ffmpeg_path="/fake/ffmpeg", ffprobe_path="/fake/ffprobe",
                           runner=runner), calls


def make_assets() -> list[dict]:
    return [
        {"asset_id": ASSET_A, "media_kind": "video",
         "original_filename": "clip.mp4",
         "source_relative_path": f"assets/{ASSET_A}/original/clip.mp4",
         "sha256": "a" * 64, "size_bytes": 4096, "status": "ready"},
        {"asset_id": ASSET_B, "media_kind": "image",
         "original_filename": "poster.png",
         "source_relative_path": f"assets/{ASSET_B}/original/poster.png",
         "sha256": "b" * 64, "size_bytes": 2048, "status": "ready"},
    ]


def _materialise_sources(tmp_root: Path, assets: list[dict],
                         payload: bytes = b"ORIGINAL") -> None:
    for asset in assets:
        src = resolve_official_asset_path(
            tmp_root / "projects" / PROJECT, asset["source_relative_path"]
        )
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(payload)


def test_probe_handler_publishes_one_report_per_asset(tmp_root: Path) -> None:
    context = make_context(tmp_root)
    assets = make_assets()
    _materialise_sources(tmp_root, assets)
    probe_by_path = {
        str(resolve_official_asset_path(
            context.project_root, a["source_relative_path"])): FAKE_SOURCE_PROBE
        for a in assets[:1]
    }
    probe_by_path[str(resolve_official_asset_path(
        context.project_root, assets[1]["source_relative_path"]))] = FAKE_IMAGE_PROBE
    chain, calls = make_fake_chain(probe_by_path)

    result = AssetProbeHandler(toolchain=chain).execute(
        context, {"asset_ids": [a["asset_id"] for a in assets], "assets": assets}
    )

    assert result.artifacts and len(result.artifacts) == 2
    kinds = [s.kind for s in result.artifacts]
    assert kinds == [f"asset.probe.{ASSET_A}", f"asset.probe.{ASSET_B}"]
    for spec in result.artifacts:
        staged = context.attempt_dir / spec.staged_relative_path
        data = json.loads(staged.read_text(encoding="utf-8"))
        assert data["schema_version"] == "media.v1"
    # ffprobe 读取的是**原素材**路径
    assert all("original" in c[-1] for c in calls if c[0].endswith("ffprobe"))


def test_normalize_reads_original_and_uses_expected_args(tmp_root: Path) -> None:
    context = make_context(tmp_root)
    assets = make_assets()
    _materialise_sources(tmp_root, assets)
    probe_by_path = {
        str(resolve_official_asset_path(
            context.project_root, a["source_relative_path"])): FAKE_SOURCE_PROBE
        for a in assets
    }
    chain, calls = make_fake_chain(probe_by_path)

    result = AssetNormalizeHandler(toolchain=chain).execute(
        context, {"asset_ids": [a["asset_id"] for a in assets], "assets": assets}
    )

    kinds = [s.kind for s in result.artifacts]
    assert kinds == [f"asset.proxy.{ASSET_A}", f"asset.proxy.{ASSET_B}"]
    video_calls = [c for c in calls if c[0].endswith("ffmpeg")
                   and c[-1].endswith(".proxy.mp4")]
    image_calls = [c for c in calls if c[0].endswith("ffmpeg")
                   and c[-1].endswith(".proxy.png")]
    assert video_calls, "视频代理应输出 MP4"
    for c in video_calls:
        joined = " ".join(c)
        assert "-r" in c and "30" in c
        assert "yuv420p" in joined and "setsar=1" in joined
        assert "libx264" in joined and "aac" in joined
    assert image_calls, "图片代理应输出 PNG"
    for c in image_calls:
        assert "-frames:v" in c
        assert c[-1].endswith(".proxy.png")
    # normalize 的输入确实是**原素材**（original/ 路径）
    for c in calls:
        if c[0].endswith("ffmpeg"):
            assert "original" in " ".join(c)




def _setup_proxy(tmp_root: Path, assets: list[dict]) -> dict[str, dict]:
    """为每个素材建立代理正式文件与引用；返回 input_artifacts。"""
    refs = []
    # 代理探测 JSON 由调用方按需注入
    proxy_probe = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280,
         "height": 720, "duration": "2.0"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "sample_rate": "48000", "channels": 2, "duration": "2.0"},
    ], "format": {"duration": "2.0"}}
    del proxy_probe  # 占位：真实场景由 fake runner 注入
    for asset in assets:
        rev = f"rev-{asset['asset_id'][:8]}"
        rel = f"artifacts/{rev}/{asset['asset_id']}.proxy.mp4"
        path = tmp_root / "projects" / PROJECT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PROXY")
        refs.append({"artifact_revision_id": rev, "kind": f"asset.proxy.{asset['asset_id']}",
                     "relative_path": rel, "sha256": "0" * 64, "size_bytes": 6,
                     "node_run_id": "nr-normalize"})
    return refs

def test_scene_detect_publishes_monotonic_segments_and_keyframes(
    tmp_root: Path,
) -> None:
    context = make_context(tmp_root)

    class FakeDetector:
        backend = "fake"

        def detect_segments(self, media_path, *, duration_us):
            return [(0, 800_000), (800_000, 1_600_000)]

    chain, _calls = make_fake_chain({})
    handler = SceneDetectHandler(detector=FakeDetector(), toolchain=chain)
    refs = _setup_proxy(tmp_root, make_assets()[:1])
    result = handler.execute(context, {"asset_ids": [ASSET_A],
                                       "assets": make_assets()[:1],
                                       "input_artifacts": refs})

    scenes = [s for s in result.artifacts if s.kind == f"asset.scenes.{ASSET_A}"]
    keyframes = [s for s in result.artifacts
                 if s.kind.startswith(f"asset.keyframe.{ASSET_A}.")]
    assert len(scenes) == 1 and len(keyframes) == 2
    staged = json.loads((context.attempt_dir / scenes[0].staged_relative_path)
                        .read_text(encoding="utf-8"))
    segs = staged["segments"]
    assert [s["segment_index"] for s in segs] == [0, 1]
    assert segs[0]["end_us"] <= segs[1]["start_us"], "镜头必须有序、不重叠"
    assert segs[-1]["end_us"] <= 2_000_000, "镜头不得越过媒体时长"


def test_scene_detect_dependency_missing_maps_to_stable_code(tmp_root: Path) -> None:
    context = make_context(tmp_root)
    assets = make_assets()[:1]
    _materialise_sources(tmp_root, assets)

    class MissingDetector:
        backend = "pyscenedetect"

        def detect_segments(self, media_path, *, duration_us):
            raise SceneDetectDependencyMissing("PySceneDetect 不可用")

    refs = _setup_proxy(tmp_root, assets)
    chain, _calls = make_fake_chain({})
    with pytest.raises(NodeExecutionError) as excinfo:
        SceneDetectHandler(detector=MissingDetector(), toolchain=chain).execute(
            context, {"asset_ids": [ASSET_A], "assets": assets,
                      "input_artifacts": refs}
        )
    assert excinfo.value.error_code == "SCENE_DEPENDENCY_MISSING"


def test_transcribe_without_audio_returns_empty_with_warning(tmp_root: Path) -> None:
    context = make_context(tmp_root)

    class NoAudioChain:
        def ffprobe_json(self, media_path, **kwargs):
            return {"streams": [{"index": 0, "codec_type": "video",
                                 "codec_name": "h264"}], "format": {}}

        def run_ffmpeg(self, args, **kwargs):
            return SubprocessResult(0, "", "")

    refs = _setup_proxy(tmp_root, make_assets()[:1])
    handler = SpeechTranscribeHandler(asr_adapter=None, toolchain=NoAudioChain(),
                                      require_asr=True)
    result = handler.execute(
        context, {"asset_ids": [ASSET_A], "assets": make_assets()[:1],
                  "input_artifacts": refs}
    )
    assert any("NO_AUDIO_STREAM" in w for w in (result.warnings or []))
    asr_specs = [s for s in result.artifacts if s.kind == f"asset.asr.{ASSET_A}"]
    data = json.loads((context.attempt_dir / asr_specs[0].staged_relative_path)
                      .read_text(encoding="utf-8"))
    assert data["segments"] == []


def test_transcribe_missing_adapter_is_stable_error(tmp_root: Path) -> None:
    context = make_context(tmp_root)

    class AudioChain:
        def ffprobe_json(self, media_path, **kwargs):
            return {"streams": [{"index": 0, "codec_type": "audio",
                                 "codec_name": "aac", "sample_rate": "48000"}],
                    "format": {}}

    refs = _setup_proxy(tmp_root, make_assets()[:1])
    with pytest.raises(NodeExecutionError) as excinfo:
        SpeechTranscribeHandler(asr_adapter=None, toolchain=AudioChain(),
                                require_asr=True).execute(
            context, {"asset_ids": [ASSET_A], "assets": make_assets()[:1],
                      "input_artifacts": refs}
        )
    assert excinfo.value.error_code == "ASR_NOT_CONFIGURED"


def test_transcribe_dependency_missing_is_stable_error(tmp_root: Path) -> None:
    context = make_context(tmp_root)

    class MissingAsr:
        backend = "funasr"
        model = "/models/paraformer"
        language = "zh"

        def transcribe(self, audio_path, *, duration_us):
            raise AsrDependencyMissing("FunASR 不可用")

    class AudioChain:
        def ffprobe_json(self, media_path, **kwargs):
            return {"streams": [{"index": 0, "codec_type": "audio",
                                 "codec_name": "aac", "sample_rate": "48000"}],
                    "format": {}}

    refs = _setup_proxy(tmp_root, make_assets()[:1])
    with pytest.raises(NodeExecutionError) as excinfo:
        SpeechTranscribeHandler(asr_adapter=MissingAsr(), toolchain=AudioChain()).execute(
            context, {"asset_ids": [ASSET_A], "assets": make_assets()[:1],
                      "input_artifacts": refs}
        )
    assert excinfo.value.error_code == "ASR_DEPENDENCY_MISSING"


def test_transcribe_normal_timestamps(tmp_root: Path) -> None:
    context = make_context(tmp_root)

    class FakeAsr:
        backend = "fake"
        model = "fake-model"
        language = "zh"

        def transcribe(self, audio_path, *, duration_us):
            return [TranscriptSegment(start_us=0, end_us=1_000_000, text="hello")]

    class AudioChain:
        def ffprobe_json(self, media_path, **kwargs):
            return {"streams": [{"index": 0, "codec_type": "audio",
                                 "codec_name": "aac", "sample_rate": "48000"}],
                    "format": {}}

    refs = _setup_proxy(tmp_root, make_assets()[:1])
    result = SpeechTranscribeHandler(asr_adapter=FakeAsr(), toolchain=AudioChain()).execute(
        context, {"asset_ids": [ASSET_A], "assets": make_assets()[:1],
                  "input_artifacts": refs}
    )
    asr_spec = [s for s in result.artifacts if s.kind == f"asset.asr.{ASSET_A}"][0]
    data = json.loads((context.attempt_dir / asr_spec.staged_relative_path)
                      .read_text(encoding="utf-8"))
    assert data["segments"][0]["start_us"] == 0
    assert data["segments"][0]["end_us"] == 1_000_000


def test_analysis_merges_deterministic_facts(tmp_root: Path) -> None:
    context = make_context(tmp_root)
    assets = make_assets()[:1]
    probe_rel = f"artifacts/r1/{ASSET_A}.probe.json"
    scenes_rel = f"artifacts/r2/{ASSET_A}.scenes.json"
    asr_rel = f"artifacts/r3/{ASSET_A}.asr.json"
    kf_id = "kf-0001"
    kf_rel = f"artifacts/kf/{ASSET_A}.keyframe.jpg"

    for rel, payload in (
        (probe_rel, build_probe_report_payload()),
        (scenes_rel, {"asset_id": ASSET_A, "duration_us": 2_000_000,
                      "segments": [AssetSegment(segment_index=0, start_us=0,
                                                end_us=1_000_000,
                                                is_time_based=True,
                                                source="scene_detect")
                                   .model_dump(mode="json")]}),
        (asr_rel, {"asset_id": ASSET_A, "segments": [
            {"start_us": 0, "end_us": 500_000, "text": "hi"}]}),
    ):
        path = context.project_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    input_json = {
        "asset_ids": [ASSET_A], "assets": assets,
        "input_artifacts": [
            {"artifact_revision_id": "r1", "kind": f"asset.probe.{ASSET_A}",
             "relative_path": probe_rel, "sha256": "0" * 64, "size_bytes": 1,
             "node_run_id": "nr-probe"},
            {"artifact_revision_id": "r2", "kind": f"asset.scenes.{ASSET_A}",
             "relative_path": scenes_rel, "sha256": "0" * 64, "size_bytes": 1,
             "node_run_id": "nr-scenes"},
            {"artifact_revision_id": "r3", "kind": f"asset.asr.{ASSET_A}",
             "relative_path": asr_rel, "sha256": "0" * 64, "size_bytes": 1,
             "node_run_id": "nr-asr"},
            {"artifact_revision_id": kf_id,
             "kind": f"asset.keyframe.{ASSET_A}.0000",
             "relative_path": kf_rel, "sha256": "0" * 64, "size_bytes": 1,
             "node_run_id": "nr-scenes"},
        ],
    }
    result = AssetAnalysisHandler().execute(context, input_json)

    analysis_spec = [s for s in result.artifacts
                     if s.kind == f"asset.analysis.{ASSET_A}"][0]
    data = json.loads((context.attempt_dir / analysis_spec.staged_relative_path)
                      .read_text(encoding="utf-8"))
    report = AssetAnalysisReport.model_validate(data)  # Pydantic 校验
    assert report.schema_version == "asset-analysis.v1"
    assert report.probe.media_kind == "video"
    assert report.segments[0].keyframe_artifact_revision_id == kf_id
    assert report.transcript[0].text == "hi"
    assert report.keyframe_artifact_revision_ids == [kf_id]
    # 无 LLM 字段
    assert not any("llm" in k.lower() or "summary" in k.lower()
                   for k in data.keys())


def build_probe_report_payload() -> dict:
    from cybercut.media.probe import build_probe_report

    return build_probe_report(FAKE_SOURCE_PROBE, media_kind="video").model_dump(
        mode="json")


def test_cancel_propagates_to_scene_detect(tmp_root):
    """取消检查置位后，scene_detect 不得继续生成后续关键帧（WB-02R5 §三）。

    验证三件事：
    1. `ffprobe_json` 与关键帧 `run_ffmpeg` 均收到 `context.cancel_check`；
    2. 取消被映射为稳定错误码 `TOOL_CANCELLED`（不降级为 HANDLER_EXCEPTION）；
    3. 取消后不再有后续关键帧命令执行（两次镜头只尝试 1 次）。
    """
    context = make_context(tmp_root)
    assets = make_assets()[:1]
    _materialise_sources(tmp_root, assets)
    refs = _setup_proxy(tmp_root, assets)

    seen: dict[str, Any] = {"ffprobe_cancel": None, "keyframe_calls": 0}
    context = dataclasses.replace(context, cancel_check=lambda: True)

    class Chain:
        def ffprobe_json(self, media_path, *, timeout=None, cancel_check=None):
            seen["ffprobe_cancel"] = cancel_check
            return {"streams": [{"index": 0, "codec_type": "video",
                                 "codec_name": "h264", "width": 1280,
                                 "height": 720, "duration": "2.0"}],
                    "format": {"duration": "2.0"}}

        def run_ffmpeg(self, args, *, timeout=None, cancel_check=None):
            assert cancel_check is not None, "run_ffmpeg 必须收到 cancel_check"
            assert cancel_check() is True, "cancel_check 应为已置位的取消函数"
            seen["keyframe_calls"] += 1
            raise MediaToolError("TOOL_CANCELLED", "命令被取消")

    with pytest.raises(NodeExecutionError) as excinfo:
        SceneDetectHandler(
            detector=_make_scene_detector(), toolchain=Chain()
        ).execute(context, {"asset_ids": [ASSET_A], "assets": assets,
                            "input_artifacts": refs})

    assert seen["ffprobe_cancel"] is not None, "ffprobe 必须收到 cancel_check"
    assert excinfo.value.error_code == "TOOL_CANCELLED", (
        f"应为稳定错误码 TOOL_CANCELLED，实际 {excinfo.value.error_code}")
    # 两个镜头 → 最多尝试 1 次关键帧（第 1 次即取消，不得继续）
    assert seen["keyframe_calls"] == 1, "取消后不得继续生成后续关键帧"
    assert not list(context.attempt_dir.rglob("scene-0001*")), (
        "取消后不得生成第二个关键帧")


def _make_scene_detector():
    class D:
        backend = "fake"
        def detect_segments(self, p, *, duration_us):
            return [(0, 800_000), (800_000, 1_600_000)]
    return D()
