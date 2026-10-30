"""WB-03 创作节点 + 持久化桥接测试（真实 PostgreSQL，无需 PG 的用例显式分层）。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.errors import NodeExecutionError, ValidationError
from cybercut.contracts.node_handler import NodeContext
from cybercut.creative.fake_provider import FakeCreativeProvider
from cybercut.creative.policy import (
    AUTO_POLICY,
    HYBRID_POLICY,
    ApprovalMode,
    ApprovalPolicy,
)
from cybercut.creative.provider import (
    GenerationRequest,
    GenerationResult,
    ProviderError,
)
from cybercut.creative.traces import assert_no_chain_of_thought
from cybercut.nodes.creative_nodes import (
    CANCELLED,
    FACT_UNSUPPORTED,
    INPUT_MISSING,
    SCHEMA_INVALID,
    CreativeBriefHandler,
    CreativeMatchHandler,
    CreativeScriptHandler,
    CreativeStoryboardHandler,
)
from cybercut.storage import creative_revisions, projects

TENANT = "t-wb03"
A1 = "a1" * 8
A2 = "b2" * 8
PROJECT = "proj-nodes"
RUN = "run-nodes"

STAGED_NAMES = {
    "creative.brief": "creative/brief.json",
    "creative.script": "creative/script.json",
    "creative.storyboard": "creative/storyboard.json",
    "creative.match": "creative/match.json",
}

CREATIVE_INPUT = {
    "target_platform": "douyin", "aspect_ratio": "9:16",
    "target_duration_us": 10_000_000, "language": "zh", "audience": "受众",
    "objective": "推广", "tone": "轻快", "visual_style": "实拍",
    "call_to_action": "了解详情", "match_threshold": 0.1,
}


def _analysis(asset_id: str, *, segments: int, duration_us: int | None,
              width: int = 1280, height: int = 720) -> dict:
    segs = []
    for index in range(segments):
        step = (duration_us // segments) if duration_us else 0
        segs.append({
            "segment_index": index,
            "start_us": index * step if duration_us else 0,
            "end_us": ((index + 1) * step) if duration_us else 0,
            "is_time_based": bool(duration_us),
            "source": "scene_detect" if duration_us else "single_frame",
            "keyframe_artifact_revision_id": f"kf-{index}" if duration_us else None,
        })
    return {
        "schema_version": "asset-analysis.v1", "asset_id": asset_id,
        "media_kind": "video" if duration_us else "image",
        "duration_us": duration_us,
        "probe": {"has_audio": bool(duration_us), "has_video": True,
                  "video": {"width": width, "height": height}},
        "segments": segs,
        "transcript": ([{"start_us": 0, "end_us": step, "text": "clip 产品演示"}]
                       if duration_us else []),
        "keyframe_artifact_revision_ids": [], "warnings": [],
    }


ASSETS = [
    {"asset_id": A1, "media_kind": "video", "original_filename": "clip.mp4",
     "source_relative_path": f"assets/{A1}/original/clip.mp4",
     "sha256": "a" * 64, "size_bytes": 10},
    {"asset_id": A2, "media_kind": "image", "original_filename": "poster.png",
     "source_relative_path": f"assets/{A2}/original/poster.png",
     "sha256": "b" * 64, "size_bytes": 20},
]

ANALYSES = {
    A1: _analysis(A1, segments=3, duration_us=10_000_000),
    A2: _analysis(A2, segments=1, duration_us=None, width=1920, height=1080),
}


class _Env:
    """一次 attempt 的临时环境：项目根 + 暂存目录 + 上游产物。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.project_root = root / "projects" / PROJECT
        self.attempt_dir = self.project_root / ".attempts" / "att-1"
        self.attempt_dir.mkdir(parents=True, exist_ok=True)
        self.refs: list[dict] = []
        for asset_id, payload in ANALYSES.items():
            self.add(payload, kind=f"asset.analysis.{asset_id}")

    def add(self, payload: dict, *, kind: str) -> dict:
        revision = uuid.uuid4().hex
        rel = f"artifacts/{revision}/{kind.replace('.', '_')}.json"
        path = self.project_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8")
        ref = {"artifact_revision_id": revision, "kind": kind,
               "relative_path": rel, "sha256": "0" * 64, "size_bytes": 1,
               "node_run_id": f"nr-{kind}"}
        self.refs.append(ref)
        return ref

    def context(self, node_key: str, *, attempt_no: int = 1,
                cancel: bool = False) -> NodeContext:
        return NodeContext(
            attempt_id="att-1", node_run_id=f"nr-{node_key}", run_id=RUN,
            project_id=PROJECT, tenant_id=TENANT, node_key=node_key,
            attempt_no=attempt_no, project_root=self.project_root,
            data_root=self.root, worker_id="w-1",
            attempt_dir=self.attempt_dir,
            cancel_check=(lambda: cancel),
        )

    def staged(self, kind: str) -> dict:
        path = self.attempt_dir / STAGED_NAMES[kind]
        return json.loads(path.read_text(encoding="utf-8"))

    def input_json(self, *, extra_refs: list[dict] | None = None,
                   creative_input: dict | None = None,
                   approval_policy: dict | None = None) -> dict:
        payload: dict = {
            "assets": ASSETS, "asset_ids": [A1, A2],
            "input_artifacts": [*self.refs, *(extra_refs or [])],
            "creative_input": (creative_input
                               if creative_input is not None else CREATIVE_INPUT),
        }
        if approval_policy is not None:
            payload["approval_policy"] = approval_policy
        return payload


@pytest.fixture
def env(data_root: Path) -> _Env:
    return _Env(data_root)


def _run_chain(env: _Env, *, policy: ApprovalPolicy = AUTO_POLICY,
               creative_input: dict | None = None) -> dict:
    """按管线顺序跑完四个节点，返回各节点结果。"""
    ci = creative_input if creative_input is not None else CREATIVE_INPUT
    ap = policy.to_json() if policy is not AUTO_POLICY else None
    results = {}
    results["brief"] = CreativeBriefHandler().execute(
        env.context("creative.brief"),
        env.input_json(creative_input=ci, approval_policy=ap))
    env.add(env.staged("creative.brief"), kind="creative.brief")
    results["script"] = CreativeScriptHandler().execute(
        env.context("creative.script"),
        env.input_json(creative_input=ci, approval_policy=ap))
    env.add(env.staged("creative.script"), kind="creative.script")
    results["storyboard"] = CreativeStoryboardHandler().execute(
        env.context("creative.storyboard"),
        env.input_json(creative_input=ci, approval_policy=ap))
    env.add(env.staged("creative.storyboard"), kind="creative.storyboard")
    results["match"] = CreativeMatchHandler().execute(
        env.context("creative.match"),
        env.input_json(creative_input=ci, approval_policy=ap))
    env.add(env.staged("creative.match"), kind="creative.match")
    return results


# ══════════════════════════════════════════════════════════════════
# 节点：正常路径
# ══════════════════════════════════════════════════════════════════


def test_brief_handler_publishes_valid_brief(env: _Env) -> None:
    result = CreativeBriefHandler().execute(
        env.context("creative.brief"), env.input_json())
    assert [a.kind for a in result.artifacts] == ["creative.brief"]
    assert result.artifacts[0].staged_relative_path == "creative/brief.json"
    brief = CreativeBrief.model_validate(env.staged("creative.brief"))
    assert brief.project_id == PROJECT and brief.run_id == RUN
    assert brief.source_asset_ids == [A1, A2]
    assert brief.decision_trace, "必须携带 DecisionTrace 摘要"
    assert_no_chain_of_thought(brief.decision_trace)


def test_full_handler_chain_produces_all_artifacts(env: _Env) -> None:
    results = _run_chain(env)
    brief = CreativeBrief.model_validate(env.staged("creative.brief"))
    script = ScriptRevision.model_validate(env.staged("creative.script"))
    story = StoryboardRevision.model_validate(env.staged("creative.storyboard"))
    match = AssetMatchResult.model_validate(env.staged("creative.match"))

    assert results["brief"].artifacts[0].kind == "creative.brief"
    assert results["script"].artifacts[0].kind == "creative.script"
    assert results["storyboard"].artifacts[0].kind == "creative.storyboard"
    assert results["match"].artifacts[0].kind == "creative.match"
    assert script.total_target_duration_us == brief.target_duration_us
    assert story.total_target_duration_us == script.total_target_duration_us
    assert len(match.shots) == len(story.shots)
    for shot in match.shots:
        assert shot.candidates, "每个 shot 必须给出候选与分数"
        for candidate in shot.candidates:
            assert candidate.breakdown is not None


def test_handlers_are_deterministic(env: _Env) -> None:
    first = CreativeBriefHandler().execute(
        env.context("creative.brief"), env.input_json())
    payload_a = env.staged("creative.brief")
    second = CreativeBriefHandler().execute(
        env.context("creative.brief"), env.input_json())
    payload_b = env.staged("creative.brief")
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b,
                                                              sort_keys=True)
    assert first.artifacts[0].kind == second.artifacts[0].kind


def test_match_node_is_deterministic_across_reruns(env: _Env) -> None:
    _run_chain(env)
    first = json.dumps(env.staged("creative.match"), sort_keys=True)
    _run_chain(env)
    second = json.dumps(env.staged("creative.match"), sort_keys=True)
    assert first == second


def test_match_uses_algorithm_version_and_no_llm(env: _Env) -> None:
    _run_chain(env)
    match = AssetMatchResult.model_validate(env.staged("creative.match"))
    assert match.model_id.startswith("deterministic:")
    assert match.prompt_version == "deterministic-match.v1"


# ══════════════════════════════════════════════════════════════════
# 节点：输入冻结与错误路径
# ══════════════════════════════════════════════════════════════════


def test_brief_requires_analysis_artifact(env: _Env) -> None:
    payload = env.input_json()
    payload["input_artifacts"] = [r for r in payload["input_artifacts"]
                                  if not r["kind"].endswith(A2)]
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler().execute(env.context("creative.brief"), payload)
    assert excinfo.value.error_code == INPUT_MISSING


def test_script_requires_brief_artifact(env: _Env) -> None:
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeScriptHandler().execute(
            env.context("creative.script"), env.input_json())
    assert excinfo.value.error_code == INPUT_MISSING


def test_storyboard_requires_script_artifact(env: _Env) -> None:
    env.add(CreativeBriefHandler().execute(
        env.context("creative.brief"), env.input_json()).artifacts and
        env.staged("creative.brief"), kind="creative.brief")
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeStoryboardHandler().execute(
            env.context("creative.storyboard"), env.input_json())
    assert excinfo.value.error_code == INPUT_MISSING


def test_upstream_payload_must_match_schema(env: _Env) -> None:
    env.add({"not": "a brief"}, kind="creative.brief")
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeScriptHandler().execute(
            env.context("creative.script"), env.input_json())
    assert excinfo.value.error_code == "INPUT_INVALID"


def test_handlers_honour_cancel_check(env: _Env) -> None:
    for handler, key in ((CreativeBriefHandler(), "creative.brief"),
                         (CreativeScriptHandler(), "creative.script")):
        with pytest.raises(NodeExecutionError) as excinfo:
            handler.execute(env.context(key, cancel=True), env.input_json())
        assert excinfo.value.error_code == CANCELLED


def test_brief_rejects_forbidden_claim(env: _Env) -> None:
    payload = env.input_json(creative_input={
        **CREATIVE_INPUT, "forbidden_claims": ["推广"]})
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler().execute(env.context("creative.brief"), payload)
    assert excinfo.value.error_code == FACT_UNSUPPORTED


def test_brief_is_grounded_in_frozen_assets(env: _Env) -> None:
    """简报只能引用**本次 Run 冻结**的素材（冻结里没有的素材不得出现）。"""
    payload = env.input_json()
    payload["assets"] = [ASSETS[0]]  # 只冻结 A1
    payload["asset_ids"] = [A1]
    result = CreativeBriefHandler().execute(
        env.context("creative.brief"), payload)
    assert result.artifacts[0].kind == "creative.brief"
    brief = CreativeBrief.model_validate(env.staged("creative.brief"))
    assert brief.source_asset_ids == [A1]
    assert {ref.asset_id for ref in brief.evidence_refs} == {A1}


def test_brief_requires_at_least_one_frozen_asset(env: _Env) -> None:
    payload = env.input_json()
    payload["assets"] = []
    payload["asset_ids"] = []
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler().execute(env.context("creative.brief"), payload)
    assert excinfo.value.error_code == INPUT_MISSING


def test_invalid_creative_input_rejected(env: _Env) -> None:
    payload = env.input_json()
    payload["creative_input"] = ["not", "a", "dict"]
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler().execute(env.context("creative.brief"), payload)
    assert excinfo.value.error_code == "INPUT_INVALID"


def test_invalid_match_threshold_maps_to_schema_error(env: _Env) -> None:
    _run_chain(env)
    ci = {**CREATIVE_INPUT, "match_threshold": 5.0}
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeMatchHandler().execute(
            env.context("creative.match"),
            env.input_json(creative_input=ci))
    assert excinfo.value.error_code == SCHEMA_INVALID


# ══════════════════════════════════════════════════════════════════
# 审批策略
# ══════════════════════════════════════════════════════════════════


def test_auto_policy_never_awaits_approval(env: _Env) -> None:
    results = _run_chain(env, policy=AUTO_POLICY)
    assert all(r.awaiting_approval is False for r in results.values())


def test_hybrid_policy_gates_script_only(env: _Env) -> None:
    results = _run_chain(env, policy=HYBRID_POLICY)
    assert results["script"].awaiting_approval is True
    assert results["brief"].awaiting_approval is False
    assert results["storyboard"].awaiting_approval is False
    assert results["match"].awaiting_approval is False


def test_run_frozen_policy_overrides_injected_default(env: _Env) -> None:
    handler = CreativeScriptHandler(policy=HYBRID_POLICY)
    _run_chain(env)  # 先建立 brief 产物
    result = handler.execute(
        env.context("creative.script"),
        env.input_json(approval_policy=AUTO_POLICY.to_json()))
    assert result.awaiting_approval is False, "Run 冻结策略优先"

    env.add(env.staged("creative.script"), kind="creative.script")
    hybrid = CreativeScriptHandler()
    result2 = hybrid.execute(
        env.context("creative.script"),
        env.input_json(approval_policy=HYBRID_POLICY.to_json()))
    assert result2.awaiting_approval is True


def test_custom_hybrid_gate(env: _Env) -> None:
    policy = ApprovalPolicy(mode=ApprovalMode.HYBRID,
                            gates=("creative.storyboard",))
    results = _run_chain(env, policy=policy)
    assert results["storyboard"].awaiting_approval is True
    assert results["script"].awaiting_approval is False


# ══════════════════════════════════════════════════════════════════
# Provider 注入与失败映射
# ══════════════════════════════════════════════════════════════════


class _FailingProvider:
    model_id = "failing"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise ProviderError("CREATIVE_PROVIDER_FAILED", "boom")


class _CrashingProvider:
    model_id = "crashing"

    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise RuntimeError("unexpected")


def test_provider_error_maps_to_stable_node_error(env: _Env) -> None:
    """WB-03R §7：ProviderError 必须在 handler 边界转换为稳定错误码。"""
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler(provider=_FailingProvider()).execute(
            env.context("creative.brief"), env.input_json())
    assert excinfo.value.error_code == "CREATIVE_PROVIDER_FAILED"
    assert excinfo.value.details["provider_code"] == "CREATIVE_PROVIDER_FAILED"


def test_unexpected_provider_failure_maps_to_stable_code(env: _Env) -> None:
    with pytest.raises(NodeExecutionError) as excinfo:
        CreativeBriefHandler(provider=_CrashingProvider()).execute(
            env.context("creative.brief"), env.input_json())
    assert excinfo.value.error_code == "CREATIVE_PROVIDER_FAILED"


def test_fake_provider_can_be_injected(env: _Env) -> None:
    handler = CreativeBriefHandler(provider=FakeCreativeProvider())
    assert handler.execute(
        env.context("creative.brief"), env.input_json()
    ).artifacts[0].kind == "creative.brief"


# ══════════════════════════════════════════════════════════════════
# 持久化桥接（需要数据库）
# ══════════════════════════════════════════════════════════════════


def test_persist_for_artifact_ignores_media_kinds(db) -> None:
    with db.transaction() as conn:
        result = creative_revisions.persist_for_artifact(
            conn, artifact_row={"kind": f"asset.analysis.{A1}",
                                "project_id": "p", "run_id": "r",
                                "tenant_id": TENANT},
            payload={"any": "payload"})
    assert result is None


def test_persist_for_artifact_validates_payload(db) -> None:
    with pytest.raises(ValidationError):
        with db.transaction() as conn:
            creative_revisions.persist_for_artifact(
                conn, artifact_row={"kind": "creative.brief",
                                    "project_id": "p", "run_id": "r",
                                    "tenant_id": TENANT},
                payload={"not": "a brief"})


def test_persist_for_artifact_accepts_valid_brief(db) -> None:
    """落库要求 payload 身份与 ArtifactRevision 身份完全一致。"""
    from cybercut.storage import runs

    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="persist")
    with db.transaction() as conn:
        run = runs.create(conn, project_id=project["project_id"],
                          tenant_id=TENANT, pipeline_version="creative-v1")

    from tests.cybercut.test_creative_contracts import brief_payload

    artifact_row = {"kind": "creative.brief",
                    "project_id": project["project_id"],
                    "run_id": run["run_id"], "tenant_id": TENANT,
                    "artifact_revision_id": "rev-0001", "revision_no": 3}
    payload = brief_payload(project_id=project["project_id"],
                            run_id=run["run_id"])
    payload = creative_revisions.finalize_payload(
        "brief", payload, revision_id="rev-0001", revision_no=3,
        project_id=project["project_id"], run_id=run["run_id"])
    with db.transaction() as conn:
        row = creative_revisions.persist_for_artifact(
            conn, artifact_row=artifact_row, payload=payload)
    assert row is not None
    assert row["brief_revision_id"] == "rev-0001"
    assert row["revision_no"] == 3
    assert row["prompt_version"] == payload["prompt_version"]
    assert row["model_id"] == payload["model_id"]
    assert row["created_by"] == "system:executor"
    assert row["payload_json"]["brief_revision_id"] == "rev-0001"
    assert row["payload_json"]["revision_no"] == 3
