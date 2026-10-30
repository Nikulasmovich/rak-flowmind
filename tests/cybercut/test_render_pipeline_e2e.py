"""WB-05 渲染层端到端测试（真实 FFmpeg + PostgreSQL + 可播放素材）。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cybercut.creative.policy import AUTO_POLICY
from cybercut.orchestrator.pipeline import RENDER_PIPELINE
from tests.cybercut.helpers import TENANT

FFMPEG_DIR = Path(os.environ.get(
    "CYBERCUT_FFMPEG_DIR",
    "C:/Users/黄嘉林/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-9.0.2-full_build/bin",
))
FFMPEG = str(FFMPEG_DIR / "ffmpeg.exe")
FFPROBE = str(FFMPEG_DIR / "ffprobe.exe")

CREATIVE_INPUT = {
    "target_platform": "douyin", "aspect_ratio": "9:16",
    "target_duration_us": 6_000_000, "language": "zh",
    "audience": "测试", "objective": "冒烟", "tone": "中性",
    "visual_style": "testsrc", "call_to_action": "结束",
    "match_threshold": 0.05,
    "edit_input": {"template_id": "brand_story"},
}


def _make_video(root: Path, name: str, *, dur: int = 3, freq: int = 440,
                pattern: str = "testsrc2") -> str:
    path = root / name
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", f"{pattern}=duration={dur}:size=1080x1920:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "28",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(path)],
        check=True, capture_output=True, timeout=60)
    return str(path)


def _make_image(root: Path, name: str) -> str:
    path = root / name
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", "color=c=blue:size=1080x1920:duration=0.1",
         "-frames:v", "1", str(path)],
        check=True, capture_output=True, timeout=30)
    return str(path)


def _probe(path: str) -> dict:
    result = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def _build_render_env(db, data_root, source_root):
    from cybercut.executor.worker import Worker
    from cybercut.media.ingest import ingest_asset
    from cybercut.media.toolchain import FFmpegToolchain
    from cybercut.nodes.creative_registry import (
        NodeRegistry,
        build_creative_registry,
        build_media_registry,
        build_render_registry,
    )
    from cybercut.orchestrator.engine import Orchestrator
    from cybercut.storage import projects

    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="e2e-render")
    pid = project["project_id"]

    s1 = _make_video(source_root, "clip_a.mp4", dur=3, freq=440, pattern="testsrc2")
    s2 = _make_video(source_root, "clip_b.mp4", dur=3, freq=880, pattern="testsrc")
    s3 = _make_image(source_root, "poster.png")

    a1, _ = ingest_asset(db, project_id=pid, tenant_id=TENANT,
                         source_path=s1, data_root=data_root)
    a2, _ = ingest_asset(db, project_id=pid, tenant_id=TENANT,
                         source_path=s2, data_root=data_root)
    a3, _ = ingest_asset(db, project_id=pid, tenant_id=TENANT,
                         source_path=s3, data_root=data_root)

    class _FakeDetector:
        backend = "fake"
        def detect_segments(self, media_path, *, duration_us):
            if duration_us is None:
                return []
            step = duration_us // 3
            return [(i * step, (i + 1) * step) for i in range(3)]

    class _FakeAsr:
        backend = "fake"
        model = "fake"
        language = "zh"
        def transcribe(self, audio_path, *, duration_us):
            from cybercut.contracts.media import TranscriptSegment
            return [TranscriptSegment(start_us=0, end_us=1_000_000, text="test")]

    real_chain = FFmpegToolchain(ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE)
    media = build_media_registry(toolchain=real_chain,
                                 detector=_FakeDetector(),
                                 asr_adapter=_FakeAsr())
    creative = build_creative_registry(policy=AUTO_POLICY)
    render = build_render_registry(policy=AUTO_POLICY)
    registry = NodeRegistry()
    for r in (media, creative, render):
        for key in r.keys():
            registry.register(r.get(key))

    orch = Orchestrator(db, data_root=data_root, pipeline=RENDER_PIPELINE)
    worker = Worker(db, registry, worker_id="w-render", data_root=data_root,
                    lease_seconds=60, orchestrator=orch)
    return orch, worker, db, data_root, pid, [a1, a2, a3], AUTO_POLICY


@pytest.fixture
def r_e2e(db, data_root, source_root):
    return _build_render_env(db, data_root, source_root)


class TestRenderE2E:
    """WB-05 端到端：真实 FFmpeg 合成 9:16 MP4。"""

    def test_all_14_nodes_succeed(self, r_e2e) -> None:
        orch, worker, db, dr, pid, assets, _ = r_e2e
        run = orch.start_run(
            project_id=pid, tenant_id=TENANT,
            asset_ids=[a["asset_id"] for a in assets],
            creative_input=CREATIVE_INPUT,
            approval_policy=AUTO_POLICY.to_json())
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        assert len(state["statuses"]) == 14
        assert set(state["statuses"]) == set(RENDER_PIPELINE.keys())
        assert all(s == "succeeded" for s in state["statuses"].values()), \
            state["statuses"]

    def test_output_mp4_is_playable_1080x1920(self, r_e2e) -> None:
        orch, worker, db, dr, pid, assets, _ = r_e2e
        run = orch.start_run(
            project_id=pid, tenant_id=TENANT,
            asset_ids=[a["asset_id"] for a in assets],
            creative_input=CREATIVE_INPUT,
            approval_policy=AUTO_POLICY.to_json())
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        qc_node = state["nodes"]["render.qc"]["node_run_id"]
        with db.transaction() as conn:
            from cybercut.storage import artifacts
            rows = artifacts.list_for_node_run(conn, qc_node, TENANT)
        mp4 = [r for r in rows if r["kind"] == "render.mp4"]
        cover = [r for r in rows if r["kind"] == "render.cover.jpg"]
        assert mp4
        assert cover
        mp4_path = dr / "projects" / pid / mp4[0]["relative_path"]
        assert mp4_path.exists() and mp4_path.stat().st_size > 0
        probe = _probe(str(mp4_path))
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        assert video["codec_name"] == "h264"
        assert video["width"] == 1080 and video["height"] == 1920
        assert audio["codec_name"] == "aac"
        assert float(probe["format"]["duration"]) > 0

        composite_node = state["nodes"]["render.composite"]["node_run_id"]
        with db.transaction() as conn:
            composite_rows = artifacts.list_for_node_run(
                conn, composite_node, TENANT)
        assert any(r["kind"] == "render.intermediate.mp4"
                   for r in composite_rows)
        assert not any(r["kind"] == "render.mp4" for r in composite_rows)

    def test_narration_status_is_not_available(self, r_e2e) -> None:
        orch, worker, db, dr, pid, assets, _ = r_e2e
        run = orch.start_run(
            project_id=pid, tenant_id=TENANT,
            asset_ids=[a["asset_id"] for a in assets],
            creative_input=CREATIVE_INPUT,
            approval_policy=AUTO_POLICY.to_json())
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        nar = state["nodes"]["render.narration"]["node_run_id"]
        with db.transaction() as conn:
            from cybercut.storage import artifacts
            rows = artifacts.list_for_node_run(conn, nar, TENANT)
        nar_rows = [r for r in rows if r["kind"] == "render.narration"]
        assert nar_rows
        payload = json.loads(
            (dr / "projects" / pid / nar_rows[0]["relative_path"])
            .read_text(encoding="utf-8"))
        assert payload["status"] == "not_available"

    def test_accepted_false_blocks_render(self, db, data_root, source_root) -> None:
        """accepted=False 时渲染被阻止。"""
        from cybercut.executor.worker import Worker
        from cybercut.media.ingest import ingest_asset
        from cybercut.media.toolchain import FFmpegToolchain
        from cybercut.nodes.creative_registry import (
            NodeRegistry,
            build_creative_registry,
            build_media_registry,
            build_render_registry,
        )
        from cybercut.orchestrator.engine import Orchestrator
        from cybercut.storage import projects

        with db.transaction() as conn:
            project = projects.create(conn, tenant_id=TENANT, name="render-block")
        pid = project["project_id"]
        a1, _ = ingest_asset(
            db, project_id=pid, tenant_id=TENANT,
            source_path=_make_video(source_root, "clip.mp4", dur=3),
            data_root=data_root)

        real_chain = FFmpegToolchain(ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE)
        media = build_media_registry(toolchain=real_chain)
        creative = build_creative_registry(policy=AUTO_POLICY)
        render = build_render_registry(policy=AUTO_POLICY)
        registry = NodeRegistry()
        for r in (media, creative, render):
            for key in r.keys():
                registry.register(r.get(key))

        # match_threshold=0.999 → 全部 unmatched → binding accepted=False
        ci = {**CREATIVE_INPUT, "match_threshold": 0.999}
        orch = Orchestrator(db, data_root=data_root, pipeline=RENDER_PIPELINE)
        worker = Worker(db, registry, worker_id="w-block", data_root=data_root,
                        lease_seconds=60, orchestrator=orch)
        run = orch.start_run(
            project_id=pid, tenant_id=TENANT, asset_ids=[a1["asset_id"]],
            creative_input=ci, approval_policy=AUTO_POLICY.to_json())
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        for key in ("render.composite", "render.qc"):
            assert state["statuses"][key] != "succeeded", key
