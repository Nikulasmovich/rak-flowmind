"""WB-03 创作管线端到端测试（真实 PostgreSQL + Orchestrator + Worker）。

媒体层使用可注入 fake toolchain / detector / ASR（本机无 ffmpeg），
创作层使用确定性 `FakeCreativeProvider`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.creative.policy import AUTO_POLICY, EDIT_NODE_KEYS, HYBRID_POLICY
from cybercut.executor.worker import Worker
from cybercut.media.ingest import ingest_asset
from cybercut.media.toolchain import SubprocessResult
from cybercut.nodes.creative_registry import (
    CREATIVE_NODE_KEYS,
    build_creative_full_registry,
    build_creative_registry,
)
from cybercut.orchestrator.engine import Orchestrator
from cybercut.orchestrator.pipeline import (
    CREATIVE_PIPELINE,
    CREATIVE_PIPELINE_HYBRID,
)
from cybercut.storage import creative_revisions
from tests.cybercut.helpers import TENANT

CREATIVE_INPUT = {
    "target_platform": "douyin", "aspect_ratio": "9:16",
    "target_duration_us": 10_000_000, "language": "zh", "audience": "年轻用户",
    "objective": "推广新品", "tone": "轻快", "visual_style": "实拍",
    "call_to_action": "立即了解新品", "match_threshold": 0.05,
}

PROBE = {"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
     "height": 1080, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
     "r_frame_rate": "30/1", "duration": "10.0"},
    {"index": 1, "codec_type": "audio", "codec_name": "aac",
     "sample_rate": "48000", "channels": 2, "duration": "10.0"},
], "format": {"duration": "10.0", "size": "12"}}

IMAGE_PROBE = {"streams": [{"index": 0, "codec_type": "video",
                            "codec_name": "png", "width": 1920, "height": 1080,
                            "pix_fmt": "rgb24"}],
               "format": {"duration": None, "size": "13"}}


class _FakeChain:
    """可注入的 fake toolchain（不调用任何外部进程）。"""

    def __init__(self, probe_by_path: dict[str, dict]) -> None:
        self._probe = probe_by_path
        self.calls: list[list[str]] = []
        self.ffmpeg_path = "/fake/ffmpeg"
        self.ffprobe_path = "/fake/ffprobe"

    def ffprobe_json(self, media_path, *, timeout=None, cancel_check=None):
        key = str(media_path)
        if key in self._probe:
            return self._probe[key]
        if key.endswith(".png"):
            return IMAGE_PROBE
        return PROBE

    def run_ffmpeg(self, args, *, timeout=None, cancel_check=None):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKE")
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
        # 10s -> 6 段，保证创作层有足够候选素材
        return [(i * 1_600_000, (i + 1) * 1_600_000) for i in range(6)]


class _FakeAsr:
    backend = "fake"
    model = "fake-model"
    language = "zh"

    def transcribe(self, audio_path, *, duration_us):
        from cybercut.contracts.media import TranscriptSegment

        return [TranscriptSegment(start_us=0, end_us=1_600_000,
                                 text="clip 产品演示")]


def _make_source(root: Path, name: str, payload: bytes) -> str:
    path = root / name
    path.write_bytes(payload)
    return str(path)


def _build(db, data_root: Path, source_root: Path, pipeline, policy):
    from cybercut.storage import projects

    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="e2e-creative")
    pid = project["project_id"]
    a1, _ = ingest_asset(
        db, project_id=pid, tenant_id=TENANT,
        source_path=_make_source(source_root, "clip.mp4", b"VIDEO"),
        data_root=data_root)
    a2, _ = ingest_asset(
        db, project_id=pid, tenant_id=TENANT,
        source_path=_make_source(source_root, "poster.png", b"IMAGE"),
        data_root=data_root)
    probe_map = {
        str(data_root / "projects" / pid / a1["source_relative_path"]): PROBE,
        str(data_root / "projects" / pid / a2["source_relative_path"]): IMAGE_PROBE,
    }
    registry = build_creative_full_registry(
        policy=policy, toolchain=_FakeChain(probe_map),
        detector=_FakeDetector(), asr_adapter=_FakeAsr())
    orch = Orchestrator(db, data_root=data_root, pipeline=pipeline)
    worker = Worker(db, registry, worker_id="w-creative", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    return orch, worker, db, data_root, pid, [a1, a2], policy


@pytest.fixture
def auto_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)


@pytest.fixture
def hybrid_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE_HYBRID,
                  HYBRID_POLICY)


def _start(orch, pid, assets, *, creative_input=None, policy=AUTO_POLICY):
    return orch.start_run(
        project_id=pid, tenant_id=TENANT,
        asset_ids=[a["asset_id"] for a in assets],
        creative_input=creative_input or CREATIVE_INPUT,
        approval_policy=policy.to_json())


def _creative_rows(db, pid: str, kind: str) -> list[dict]:
    with db.transaction() as conn:
        return creative_revisions.list_for_project(conn, kind, pid, TENANT)


def _staged_payload(db, pid: str, node_run_id: str, kind: str) -> dict:
    from cybercut.storage import artifacts

    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_run_id, TENANT)
    target = [r for r in rows if r["kind"] == kind]
    assert target, f"缺少产物 {kind}"
    return target[0]


# ══════════════════════════════════════════════════════════════════
# 全自动（auto）策略
# ══════════════════════════════════════════════════════════════════


def test_registry_contains_creative_and_media_nodes() -> None:
    registry = build_creative_registry()
    assert set(registry.keys()) == set(CREATIVE_NODE_KEYS)
    full = build_creative_full_registry()
    assert set(CREATIVE_NODE_KEYS) | set(EDIT_NODE_KEYS) <= set(full.keys())
    assert {"asset_probe", "asset_analysis"} <= set(full.keys())


def test_auto_pipeline_completes_all_eleven_nodes(auto_e2e) -> None:
    """WB-04：管线扩展为 11 节点（创作四 + 媒体五 + 编辑二）。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    assert len(state["statuses"]) == 11
    assert set(state["statuses"]) == set(CREATIVE_PIPELINE.keys())
    assert all(s == "succeeded" for s in state["statuses"].values()), \
        state["statuses"]


def test_auto_pipeline_publishes_six_artifact_kinds(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    for key in (*CREATIVE_NODE_KEYS, *EDIT_NODE_KEYS):
        node_run_id = state["nodes"][key]["node_run_id"]
        with db.transaction() as conn:
            from cybercut.storage import artifacts

            kinds = {r["kind"] for r in
                     artifacts.list_for_node_run(conn, node_run_id, TENANT)}
        assert kinds == {key}, f"{key} 产物 kind 不匹配：{kinds}"


def test_creative_tables_are_populated_once(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    for kind in ("brief", "script", "storyboard", "match"):
        rows = _creative_rows(db, pid, kind)
        assert len(rows) == 1, f"{kind} 应恰好一条版本记录"
        assert rows[0]["revision_no"] == 1
        assert rows[0]["status"] == "active"
        assert rows[0]["payload_json"]
        assert rows[0]["prompt_version"]


def test_creative_payloads_pass_pydantic(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    payloads = {kind: _creative_rows(db, pid, kind)[0]["payload_json"]
                for kind in ("brief", "script", "storyboard", "match")}
    brief = CreativeBrief.model_validate(payloads["brief"])
    script = ScriptRevision.model_validate(payloads["script"])
    storyboard = StoryboardRevision.model_validate(payloads["storyboard"])
    match = AssetMatchResult.model_validate(payloads["match"])

    assert brief.target_duration_us == CREATIVE_INPUT["target_duration_us"]
    assert script.total_target_duration_us == brief.target_duration_us
    assert storyboard.total_target_duration_us == script.total_target_duration_us
    assert len(match.shots) == len(storyboard.shots)
    assert [s.sequence_no for s in storyboard.shots] == \
        list(range(len(storyboard.shots)))
    assert state["statuses"]["creative.match"] == "succeeded"


def test_match_selects_unique_segments(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    match = AssetMatchResult.model_validate(
        _creative_rows(db, pid, "match")[0]["payload_json"])
    selected = [s.selected_segment_id for s in match.shots
                if s.selected_segment_id is not None]
    assert selected, "至少应有一个镜头选出素材"
    assert len(set(selected)) == len(selected), "同一 segment 不得被重复选中"
    for shot in match.shots:
        assert shot.candidates, "每个镜头必须给出候选与分数"
        for candidate in shot.candidates:
            assert candidate.breakdown is not None
            assert 0.0 <= candidate.score <= 1.0
        if shot.selected_segment_id is None:
            assert shot.warnings or match.warnings, "未选中必须给出 warning"


def test_match_reports_threshold_shortfall_never_fabricates(auto_e2e) -> None:
    """阈值极高时不得伪造选择，必须显式报告。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets,
           creative_input={**CREATIVE_INPUT, "match_threshold": 0.999})
    worker.drain()
    match = AssetMatchResult.model_validate(
        _creative_rows(db, pid, "match")[0]["payload_json"])
    assert all(s.selected_segment_id is None for s in match.shots)
    assert any("MATCH_" in w for w in match.warnings)


def test_creative_brief_is_reproducible_from_frozen_inputs(auto_e2e) -> None:
    """端到端确定性：用同一冻结输入重算简报，结果必须与已发布产物逐字段一致。"""
    from cybercut.creative.fake_provider import FakeCreativeProvider
    from cybercut.creative.prompts import prompt_version
    from cybercut.creative.provider import GenerationRequest

    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    published = _creative_rows(db, pid, "brief")[0]["payload_json"]

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_json FROM cybercut.runs WHERE run_id = %s",
                (run["run_id"],))
            frozen = cur.fetchone()["input_json"]
            cur.execute(
                "SELECT input_revision_refs FROM cybercut.node_runs"
                " WHERE node_run_id = %s",
                (state["nodes"]["creative.brief"]["node_run_id"],))
            refs = cur.fetchone()["input_revision_refs"]

    analyses = []
    for ref in sorted(refs, key=lambda r: r["kind"]):
        path = data_root / "projects" / pid / ref["relative_path"]
        analyses.append(json.loads(path.read_text(encoding="utf-8")))

    regenerated = FakeCreativeProvider().generate(GenerationRequest(
        kind="brief", prompt_version=prompt_version("brief"),
        context={"project_id": pid, "run_id": run["run_id"],
                 "assets": frozen["assets"], "analyses": analyses,
                 "brief_input": frozen["creative_input"]})).payload

    for field in ("selling_points", "core_message", "source_asset_ids",
                  "target_duration_us", "objective", "aspect_ratio"):
        assert regenerated[field] == published[field], field
    assert frozen["asset_ids"] == [a["asset_id"] for a in assets]


def test_downstream_inputs_are_frozen_and_do_not_drift(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    node_run_id = state["nodes"]["creative.storyboard"]["node_run_id"]
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_revision_refs FROM cybercut.node_runs"
                " WHERE node_run_id = %s", (node_run_id,))
            refs = cur.fetchone()["input_revision_refs"]
    kinds = {ref["kind"] for ref in refs}
    assert "creative.script" in kinds
    assert "creative.brief" in kinds
    assert any(kind.startswith("asset.analysis.") for kind in kinds)

    # 反复推进不得改变已冻结的输入引用（重试不漂移）
    refs = _frozen_refs(db, node_run_id)
    for _ in range(3):
        orch.advance_run(run["run_id"], TENANT)
        worker.drain()
        assert _frozen_refs(db, node_run_id) == refs, "冻结引用不得漂移"

    # 冻结引用必须指向真实存在且属于本项目的产物
    from cybercut.storage import artifacts

    with db.transaction() as conn:
        for ref in refs:
            row = artifacts.get(conn, ref["artifact_revision_id"], TENANT)
            assert row is not None, f"冻结引用指向不存在的产物：{ref}"
            assert row["project_id"] == pid


def _frozen_refs(db, node_run_id: str) -> list[dict]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_revision_refs FROM cybercut.node_runs"
                " WHERE node_run_id = %s", (node_run_id,))
            return cur.fetchone()["input_revision_refs"]


def test_decision_trace_is_summary_only(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    for kind in ("script", "storyboard", "match"):
        payload = _creative_rows(db, pid, kind)[0]["payload_json"]
        trace = payload.get("decision_trace") or []
        assert trace, f"{kind} 必须携带决策摘要"
        for item in trace:
            assert set(item) <= {
                "step", "decision", "rationale_code", "inputs_digest",
                "outputs_digest", "evidence_count", "notes",
            }, f"{kind} 摘要含未允许字段：{item}"


# ══════════════════════════════════════════════════════════════════
# 混合（hybrid）审批策略
# ══════════════════════════════════════════════════════════════════


def test_hybrid_pipeline_pauses_at_script(hybrid_e2e) -> None:
    from cybercut.contracts.enums import RunStatus

    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    assert state["statuses"]["creative.brief"] == "succeeded"
    assert state["statuses"]["creative.script"] == "waiting_approval"
    assert state["statuses"]["creative.storyboard"] == "blocked"
    assert state["statuses"]["creative.match"] == "blocked"
    assert state["run"]["status"] == RunStatus.WAITING_APPROVAL.value


def test_hybrid_approval_resumes_pipeline(hybrid_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    node_run_id = state["nodes"]["creative.script"]["node_run_id"]

    orch.approve(node_run_id, TENANT, expected_revision=1,
                 decided_by="reviewer")
    worker.drain()
    state_after = orch.run_state(run["run_id"], TENANT)
    assert all(s == "succeeded" for s in state_after["statuses"].values()), \
        state_after["statuses"]
    assert len(_creative_rows(db, pid, "match")) == 1


def test_hybrid_rejection_blocks_downstream(hybrid_e2e) -> None:
    """驳回：审批落 `rejected`，下游不得产出创作产物。"""
    from cybercut.contracts.enums import ApprovalStatus, RunStatus
    from cybercut.storage import approvals

    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    node_run_id = state["nodes"]["creative.script"]["node_run_id"]

    orch.reject(node_run_id, TENANT, expected_revision=1,
                decided_by="reviewer", feedback="文案需修改")
    worker.drain()

    with db.transaction() as conn:
        approval = approvals.find_by_revision(conn, node_run_id, 1, TENANT)
    assert approval is not None
    assert approval["status"] == ApprovalStatus.REJECTED.value
    assert approval["feedback"] == "文案需修改"

    state_after = orch.run_state(run["run_id"], TENANT)
    assert state_after["statuses"]["creative.match"] != "succeeded"
    assert _creative_rows(db, pid, "match") == []
    assert state_after["run"]["status"] != RunStatus.SUCCEEDED.value


def test_hybrid_policy_matches_pipeline_definition() -> None:
    gates = {spec.node_key for spec in CREATIVE_PIPELINE_HYBRID.nodes
             if spec.approval_required}
    assert gates == set(HYBRID_POLICY.gates)
    assert all(not spec.approval_required for spec in CREATIVE_PIPELINE.nodes)


def test_frozen_policy_is_delivered_to_every_attempt(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_json FROM cybercut.runs WHERE run_id = %s",
                (run["run_id"],))
            frozen = cur.fetchone()["input_json"]
    assert frozen["creative_input"]["objective"] == CREATIVE_INPUT["objective"]
    assert frozen["asset_ids"] == [a["asset_id"] for a in assets]
    worker.drain()

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.input_json FROM cybercut.node_attempts a"
                " JOIN cybercut.node_runs n ON n.node_run_id = a.node_run_id"
                " WHERE n.run_id = %s", (run["run_id"],))
            attempts = cur.fetchall()
    assert attempts
    for row in attempts:
        payload = row["input_json"]
        assert "creative_input" in payload, "所有 attempt 都必须携带创作输入"
        assert payload["asset_ids"] == frozen["asset_ids"]


def test_creative_payload_json_is_published_as_artifact(auto_e2e) -> None:
    """结构化负载与正式产物文件内容一致（可追溯）。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    row = _staged_payload(db, pid, state["nodes"]["creative.brief"]["node_run_id"],
                          "creative.brief")
    path = data_root / "projects" / pid / row["relative_path"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    stored = _creative_rows(db, pid, "brief")[0]["payload_json"]
    assert on_disk == stored


# ══════════════════════════════════════════════════════════════════
# WB-03R §6：manual 模式（四节点全部审批）
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def manual_e2e(db, data_root, source_root):
    from cybercut.creative.policy import MANUAL_POLICY
    from cybercut.orchestrator.pipeline import CREATIVE_PIPELINE_MANUAL

    return _build(db, data_root, source_root, CREATIVE_PIPELINE_MANUAL,
                  MANUAL_POLICY)


def test_manual_pipeline_gates_every_creative_node(manual_e2e) -> None:
    """manual 策略下四个创作节点依次要求人工审批。"""
    from cybercut.contracts.enums import RunStatus
    from cybercut.creative.policy import MANUAL_POLICY, PLAN_NODE_KEYS

    orch, worker, db, data_root, pid, assets, policy = manual_e2e
    run = orch.start_run(
        project_id=pid, tenant_id=TENANT,
        asset_ids=[a["asset_id"] for a in assets],
        creative_input={**CREATIVE_INPUT, "match_threshold": 0.05},
        approval_policy=MANUAL_POLICY.to_json())

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT input_json FROM cybercut.runs WHERE run_id = %s",
                        (run["run_id"],))
            frozen = cur.fetchone()["input_json"]
    assert frozen["approval_policy"]["mode"] == "manual"
    assert set(frozen["approval_policy"]["gates"]) == set(PLAN_NODE_KEYS)

    order = ("creative.brief", "creative.script",
             "creative.storyboard", "creative.match",
             "edit.plan", "template.bind")
    for index, key in enumerate(order):
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        assert state["statuses"][key] == "waiting_approval", (
            f"{key} 应等待审批（第 {index + 1} 道闸门）")
        assert state["run"]["status"] == RunStatus.WAITING_APPROVAL.value
        if index + 1 < len(order):
            nxt = order[index + 1]
            assert state["statuses"][nxt] == "blocked"
        orch.approve(state["nodes"][key]["node_run_id"], TENANT,
                     expected_revision=1, decided_by="reviewer")

    worker.drain()
    final = orch.run_state(run["run_id"], TENANT)
    assert all(s == "succeeded" for s in final["statuses"].values()), \
        final["statuses"]
    for kind in ("brief", "script", "storyboard", "match",
                 "edit_plan", "template_bind"):
        assert len(_creative_rows(db, pid, kind)) == 1


def test_manual_policy_gates_match_pipeline_definition() -> None:
    from cybercut.creative.policy import MANUAL_POLICY, PLAN_NODE_KEYS
    from cybercut.orchestrator.pipeline import CREATIVE_PIPELINE_MANUAL

    gates = {spec.node_key for spec in CREATIVE_PIPELINE_MANUAL.nodes
             if spec.approval_required}
    assert set(MANUAL_POLICY.gates) == set(PLAN_NODE_KEYS)
    assert gates == set(MANUAL_POLICY.gates) & set(CREATIVE_PIPELINE_MANUAL.keys())


# ══════════════════════════════════════════════════════════════════
# WB-03R §7：ProviderError → attempt.failure_code
# ══════════════════════════════════════════════════════════════════


class _FailingProvider:
    """总是抛 ProviderError 的 provider（用于验证错误码边界转换）。"""

    model_id = "failing-provider-v1"

    def generate(self, request):  # noqa: ANN001, ANN201
        from cybercut.creative.provider import ProviderError

        raise ProviderError("UPSTREAM_UNAVAILABLE", "模拟上游不可用")


def test_provider_error_becomes_stable_failure_code(db, data_root, source_root):
    """ProviderError 不得退化为 HANDLER_EXCEPTION，必须是稳定错误码。"""
    from cybercut.media.ingest import ingest_asset
    from cybercut.nodes.creative_registry import build_creative_full_registry
    from cybercut.storage import projects

    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="e2e-provider-fail")
    pid = project["project_id"]

    with db.transaction() as conn:
        a1, _ = ingest_asset(
            db, project_id=pid, tenant_id=TENANT,
            source_path=_make_source(source_root, "clip.mp4", b"VIDEO"),
            data_root=data_root)
        a2, _ = ingest_asset(
            db, project_id=pid, tenant_id=TENANT,
            source_path=_make_source(source_root, "poster.png", b"IMAGE"),
            data_root=data_root)
    probe_map = {
        str(data_root / "projects" / pid / a1["source_relative_path"]): PROBE,
        str(data_root / "projects" / pid / a2["source_relative_path"]): IMAGE_PROBE,
    }
    registry = build_creative_full_registry(
        provider=_FailingProvider(), policy=AUTO_POLICY,
        toolchain=_FakeChain(probe_map), detector=_FakeDetector(),
        asr_adapter=_FakeAsr())
    orch = Orchestrator(db, data_root=data_root, pipeline=CREATIVE_PIPELINE)
    worker = Worker(db, registry, worker_id="w-fail", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)

    run = orch.start_run(project_id=pid, tenant_id=TENANT,
                         asset_ids=[a1["asset_id"], a2["asset_id"]],
                         creative_input={**CREATIVE_INPUT,
                                         "match_threshold": 0.05},
                         approval_policy=AUTO_POLICY.to_json())
    worker.drain()

    state = orch.run_state(run["run_id"], TENANT)
    assert state["statuses"]["creative.brief"] == "failed"
    node_run_id = state["nodes"]["creative.brief"]["node_run_id"]
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_code, error_message, status"
                "  FROM cybercut.node_attempts"
                " WHERE node_run_id = %s ORDER BY attempt_no", (node_run_id,))
            attempts = [dict(r) for r in cur.fetchall()]
    assert attempts
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "CREATIVE_PROVIDER_FAILED", (
        f"必须映射为稳定错误码，实际 {attempts[0]['error_code']}")
    assert attempts[0]["error_code"] != "HANDLER_EXCEPTION"

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM cybercut.events"
                " WHERE run_id = %s AND event_type = 'attempt.failed'",
                (run["run_id"],))
            codes = {r["payload_json"]["error_code"] for r in cur.fetchall()}
    assert "CREATIVE_PROVIDER_FAILED" in codes
