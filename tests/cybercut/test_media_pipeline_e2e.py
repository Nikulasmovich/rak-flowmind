"""WB-02R §七：fake E2E —— PostgreSQL Orchestrator + Worker 跑完五节点。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cybercut.executor.worker import Worker
from cybercut.media.ingest import ingest_asset
from cybercut.media.toolchain import SubprocessResult
from cybercut.nodes.media_registry import MEDIA_NODE_KEYS, build_media_registry
from cybercut.orchestrator.engine import Orchestrator
from cybercut.orchestrator.pipeline import ASSET_ANALYSIS_PIPELINE
from tests.cybercut.helpers import TENANT


class _FakeChain:
    def __init__(self, probe_by_path):
        self._probe = probe_by_path
        self.calls = []
        self.ffmpeg_path = "/fake/ffmpeg"
        self.ffprobe_path = "/fake/ffprobe"

    def ffprobe_json(self, media_path, *, timeout=None, cancel_check=None):
        key = str(media_path)
        if key in self._probe:
            return self._probe[key]
        # 代理/未知路径：按扩展名返回合理 probe
        if ".proxy.png" in key or key.endswith(".png"):
            return {"streams": [{"index": 0, "codec_type": "video",
                                 "codec_name": "png", "width": 800,
                                 "height": 600, "pix_fmt": "rgb24"}],
                    "format": {"duration": None, "size": "13"}}
        return PROBE

    def run_ffmpeg(self, args, *, timeout=None, cancel_check=None):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        out = argv[-1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"FAKE")
        return SubprocessResult(0, "", "")

    def version(self, *, which="ffmpeg"):
        return "fake 7.0"

    def encoders(self):
        return ["libx264", "aac"]

    def filters(self):
        return ["scale", "setsar"]


class _FakeDetector:
    backend = "fake"

    def detect_segments(self, media_path, *, duration_us):
        return [(0, 1_000_000), (1_000_000, 2_000_000)]


class _FakeAsr:
    backend = "fake"
    model = "fake-model"
    language = "zh"

    def transcribe(self, audio_path, *, duration_us):
        from cybercut.contracts.media import TranscriptSegment
        return [TranscriptSegment(start_us=0, end_us=500_000, text="fake")]


PROBE = {"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280,
     "height": 720, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
     "r_frame_rate": "30/1", "duration": "2.0"},
    {"index": 1, "codec_type": "audio", "codec_name": "aac",
     "sample_rate": "48000", "channels": 2, "duration": "2.0"},
], "format": {"duration": "2.0", "size": "12"}}

IMAGE_PROBE = {"streams": [{"index": 0, "codec_type": "video",
                            "codec_name": "png", "width": 800, "height": 600,
                            "pix_fmt": "rgb24"}],
               "format": {"duration": None, "size": "13"}}


@pytest.fixture
def e2e(db, data_root, source_root):
    from cybercut.storage import projects

    with db.transaction() as conn:
        p = projects.create(conn, tenant_id=TENANT, name="e2e-media")
    pid = p["project_id"]
    s1 = _make_source(source_root, "clip.mp4", b"VIDEO")
    s2 = _make_source(source_root, "poster.png", b"IMAGE")
    a1, _ = ingest_asset(db, project_id=pid, tenant_id=TENANT, source_path=s1,
                         data_root=data_root)
    a2, _ = ingest_asset(db, project_id=pid, tenant_id=TENANT, source_path=s2,
                         data_root=data_root)
    probe_map = {
        str(data_root / "projects" / pid / a1["source_relative_path"]): PROBE,
        str(data_root / "projects" / pid / a2["source_relative_path"]): IMAGE_PROBE,
    }
    chain = _FakeChain(probe_map)
    registry = build_media_registry(toolchain=chain, detector=_FakeDetector(),
                                    asr_adapter=_FakeAsr())
    orch = Orchestrator(db, data_root=data_root, pipeline=ASSET_ANALYSIS_PIPELINE)
    worker = Worker(db, registry, worker_id="w-e2e", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    return orch, worker, db, data_root, pid, [a1, a2], chain


def _make_source(root, name, payload):
    path = root / name
    path.write_bytes(payload)
    return str(path)




def _producing_node_run_id(orch, run_id, tenant, asset_id, kind_prefix):
    """按产物生产节点查询 node_run_id。"""
    mapping = {
        "asset.probe": "asset_probe",
        "asset.proxy": "asset_normalize",
        "asset.scenes": "scene_detect",
        "asset.keyframe": "scene_detect",
        "asset.asr": "speech_transcribe",
        "asset.analysis": "asset_analysis",
    }
    node_key = mapping[kind_prefix]
    return orch.run_state(run_id, tenant)["nodes"][node_key]["node_run_id"]

def test_e2e_five_nodes_succeed(e2e):
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    assert len(state["statuses"]) == 5
    assert all(s == "succeeded" for s in state["statuses"].values()), state["statuses"]


def test_e2e_per_asset_artifacts(e2e):
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    for a in assets:
        aid = a["asset_id"]
        for prefix in ("asset.probe", "asset.proxy", "asset.scenes",
                       "asset.asr", "asset.analysis"):
            nr = _producing_node_run_id(orch, run["run_id"], TENANT, aid, prefix)
            with db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) n FROM cybercut.artifact_revisions"
                        " WHERE node_run_id=%s AND kind=%s",
                        (nr, f"{prefix}.{aid}"))
                    assert cur.fetchone()["n"] >= 1, f"缺少 {prefix}.{aid}"


def test_e2e_video_keyframes(e2e):
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    nr = _producing_node_run_id(orch, run["run_id"], TENANT, assets[0]["asset_id"], "asset.keyframe")
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) n FROM cybercut.artifact_revisions"
                " WHERE node_run_id=%s AND kind LIKE %s",
                (nr, f"asset.keyframe.{assets[0]['asset_id']}.%"))
            assert cur.fetchone()["n"] >= 2


def test_e2e_image_proxy_is_png(e2e):
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    nr = _producing_node_run_id(orch, run["run_id"], TENANT, assets[1]["asset_id"], "asset.proxy")
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relative_path FROM cybercut.artifact_revisions"
                " WHERE node_run_id=%s AND kind = %s",
                (nr, f"asset.proxy.{assets[1]['asset_id']}"))
            assert cur.fetchone()["relative_path"].endswith(".proxy.png")


def test_e2e_analysis_report_pydantic(e2e):
    from cybercut.contracts.media import AssetAnalysisReport
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    a1 = assets[0]
    nr = _producing_node_run_id(orch, run["run_id"], TENANT, a1["asset_id"], "asset.analysis")
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relative_path FROM cybercut.artifact_revisions"
                " WHERE node_run_id=%s AND kind = %s",
                (nr, f"asset.analysis.{a1['asset_id']}"))
            rel = cur.fetchone()["relative_path"]
    data = json.loads((data_root / "projects" / pid / rel).read_text("utf-8"))
    AssetAnalysisReport.model_validate(data)


def test_e2e_normalize_input_is_original(e2e):
    """asset_normalize 的 FFmpeg 输入必须是**素材原文件**，不得是 probe JSON 或暂存文件。"""
    orch, worker, db, data_root, pid, assets, chain = e2e
    orch.start_run(project_id=pid, tenant_id=TENANT,
                   asset_ids=[a["asset_id"] for a in assets])
    worker.drain()

    # 收集每个 FFmpeg 调用中 `-i` 之后的输入路径
    inputs: list[str] = []
    for call in chain.calls:
        if "-i" not in call:
            continue
        idx = call.index("-i")
        assert idx + 1 < len(call), "FFmpeg 的 -i 之后必须跟随输入路径"
        inputs.append(str(call[idx + 1]))

    assert inputs, "应存在带 -i 的 FFmpeg 调用"

    for src in inputs:
        norm = src.replace("\\", "/")
        assert not norm.endswith(".probe.json"), "probe JSON 不得作为 FFmpeg 输入"
        assert "/attempts/" not in norm, "暂存区文件不得作为 FFmpeg 输入"

    normalize_inputs = [s for s in inputs if "/original/" in s.replace("\\", "/")]
    assert normalize_inputs, "asset_normalize 必须从素材原文件读取输入"
    root = str(data_root).replace("\\", "/")
    for src in normalize_inputs:
        assert root in src.replace("\\", "/"), "原文件必须位于项目正式目录"


def test_e2e_no_drift(e2e):
    orch, worker, db, data_root, pid, assets, chain = e2e
    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a["asset_id"] for a in assets])
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    nr = state["nodes"]["asset_normalize"]["node_run_id"]
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT input_revision_refs FROM cybercut.node_runs"
                        " WHERE node_run_id=%s", (nr,))
            refs = cur.fetchone()["input_revision_refs"] or []
    assert refs, "应有冻结引用"
    worker.drain()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT input_revision_refs FROM cybercut.node_runs"
                        " WHERE node_run_id=%s", (nr,))
            assert cur.fetchone()["input_revision_refs"] == refs


def test_registry_has_five_media_nodes():
    registry = build_media_registry()
    assert set(registry.keys()) == set(MEDIA_NODE_KEYS)
