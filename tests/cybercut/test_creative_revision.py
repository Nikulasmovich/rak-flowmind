"""WB-03R · 定向修订、版本身份统一与 Project/Run 组合约束。

覆盖：
- 驳回只重跑被驳节点自身、只失效创作层下游（媒体层 attempt 数不变）；
- `revise_node` / `revise_artifact` 的失效范围与冻结输入更新；
- 版本身份统一（产物文件 / ArtifactRevision / 创作版本表 / payload 一一对应）；
- 同项目两个 Run、同节点多次修订；
- 0005 复合外键拒绝跨 Project 的 run 组合。
"""
from __future__ import annotations

import json

import pytest

from cybercut.contracts.enums import ApprovalStatus
from cybercut.contracts.errors import ConflictError, NotFoundError, ValidationError
from cybercut.creative.policy import AUTO_POLICY, HYBRID_POLICY
from cybercut.orchestrator.pipeline import CREATIVE_PIPELINE
from cybercut.storage import approvals, creative_revisions, projects, runs
from tests.cybercut.conftest import resolve_test_dsn
from tests.cybercut.helpers import TENANT
from tests.cybercut.test_creative_pipeline_e2e import (
    CREATIVE_INPUT,
    _build,
    _start,
)

KINDS = ("brief", "script", "storyboard", "match", "edit_plan", "template_bind")


# ══════════════════════════════════════════════════════════════════
# 夹具与工具
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def e2e(db, data_root, source_root):
    """auto 策略：跑完整九节点（供修订与身份测试使用）。"""
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)


@pytest.fixture
def hybrid_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, HYBRID_POLICY)


def _attempt_counts(db, run_id: str) -> dict[str, int]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.node_key AS node_key, count(a.attempt_id) AS n"
                "  FROM cybercut.node_runs n"
                "  LEFT JOIN cybercut.node_attempts a"
                "    ON a.node_run_id = n.node_run_id"
                " WHERE n.run_id = %s GROUP BY n.node_key ORDER BY n.node_key",
                (run_id,),
            )
            return {r["node_key"]: int(r["n"]) for r in cur.fetchall()}


MEDIA_NODES = ("asset_probe", "asset_normalize", "scene_detect",
               "speech_transcribe", "asset_analysis")


def _rows(db, project_id: str, kind: str) -> list[dict]:
    with db.transaction() as conn:
        return creative_revisions.list_for_project(conn, kind, project_id, TENANT)


def _node_run_id(orch, run_id: str, key: str) -> str:
    return orch.run_state(run_id, TENANT)["nodes"][key]["node_run_id"]


def _frozen_refs(db, node_run_id: str) -> list[dict]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT input_revision_refs FROM cybercut.node_runs"
                " WHERE node_run_id = %s", (node_run_id,))
            return cur.fetchone()["input_revision_refs"] or []


def _artifact_row(db, revision_id: str) -> dict:
    from cybercut.storage import artifacts

    with db.transaction() as conn:
        row = artifacts.get(conn, revision_id, TENANT)
    assert row is not None
    return row


# ══════════════════════════════════════════════════════════════════
# 1) 版本身份统一
# ══════════════════════════════════════════════════════════════════


def test_identity_is_consistent_across_plan_artifacts(e2e) -> None:
    """产物文件 / ArtifactRevision / 创作版本表 / payload 四者一一对应。"""
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()

    for kind in KINDS:
        rows = _rows(db, pid, kind)
        assert len(rows) == 1, kind
        row = rows[0]
        payload = row["payload_json"]
        id_field = creative_revisions.id_field(kind)

        assert row[id_field] == payload[id_field], (
            f"{kind} 版本表 id 与 payload id 不一致")
        assert row["revision_no"] == payload["revision_no"], (
            f"{kind} 版本号与 payload 版本号不一致")
        assert payload["project_id"] == pid
        assert payload["run_id"] == run["run_id"]

        artifact = _artifact_row(db, row[id_field])
        assert artifact["artifact_revision_id"] == row[id_field], (
            f"{kind} 创作版本 id 必须等于 ArtifactRevision id")
        assert artifact["revision_no"] == row["revision_no"]
        assert artifact["kind"] == creative_revisions.KINDS[kind][2]
        assert artifact["project_id"] == pid
        assert artifact["run_id"] == run["run_id"]

        # 正式产物文件内容与资料库 payload 完全一致
        on_disk = json.loads(
            (data_root / "projects" / pid / artifact["relative_path"])
            .read_text(encoding="utf-8"))
        assert on_disk == payload, f"{kind} 产物文件与 payload 不一致"


def test_payload_revision_no_is_not_attempt_no(e2e) -> None:
    """`attempt_no` 不得充当业务 `revision_no`：多版本必须严格递增且唯一。"""
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.revise_node(script_node, TENANT, expected_revision=1,
                     new_prompt="换一个钩子")
    worker.drain()

    rows = _rows(db, pid, "script")
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert len({r["script_revision_id"] for r in rows}) == 2
    for row in rows:
        assert row["payload_json"]["revision_no"] == row["revision_no"]
        assert row["payload_json"]["script_revision_id"] == row["script_revision_id"]


def test_two_runs_in_same_project_share_revision_sequence(db, data_root,
                                                          source_root) -> None:
    """同项目两个 Run：版本身份仍唯一且版本号在同 project 内连续。"""
    orch, worker, db, data_root, pid, assets, policy = _build(
        db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)
    run_a = _start(orch, pid, assets, policy=policy)
    run_b = _start(orch, pid, assets, policy=policy)   # 项目仍 active
    worker.drain()

    rows = _rows(db, pid, "brief")
    assert len(rows) == 2
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert len({r["brief_revision_id"] for r in rows}) == 2
    run_ids = {r["payload_json"]["run_id"] for r in rows}
    assert run_ids == {run_a["run_id"], run_b["run_id"]}
    for row in rows:
        assert row["payload_json"]["brief_revision_id"] == row["brief_revision_id"]
        assert row["payload_json"]["revision_no"] == row["revision_no"]


# ══════════════════════════════════════════════════════════════════
# 2) 驳回：只重跑自身 + 只失效创作层下游
# ══════════════════════════════════════════════════════════════════


def test_reject_script_only_reruns_script_and_keeps_media(hybrid_e2e) -> None:
    """驳回 script：媒体五节点与 brief 的 attempt 数不变，script 产出新版本。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    assert orch.run_state(run["run_id"], TENANT)["statuses"][
        "creative.script"] == "waiting_approval"

    before = _attempt_counts(db, run["run_id"])
    orch.reject(script_node, TENANT, expected_revision=1,
                decided_by="reviewer", feedback="文案需要重写")
    worker.drain()
    after = _attempt_counts(db, run["run_id"])

    for key in MEDIA_NODES:
        assert after[key] == before[key] == 1, f"媒体节点 {key} 不得重跑"
    assert after["creative.brief"] == before["creative.brief"] == 1, (
        "brief 不得重跑")
    assert after["creative.script"] == 2, "被驳节点必须重跑一次"

    rows = _rows(db, pid, "script")
    assert [r["revision_no"] for r in rows] == [1, 2], "旧版本必须保留"
    assert rows[1]["payload_json"]["revision_note"] == "文案需要重写", (
        "人工反馈必须冻结进新 attempt 并体现在新 payload")
    assert len(_rows(db, pid, "brief")) == 1, "未失效节点不得产生新版本"

    with db.transaction() as conn:
        approval = approvals.find_by_revision(conn, script_node, 1, TENANT)
    assert approval["status"] == ApprovalStatus.REJECTED.value
    assert approval["feedback"] == "文案需要重写"


def test_reject_storyboard_invalidates_match_only(hybrid_e2e) -> None:
    """驳回 storyboard：只失效 match，script 与 brief 不重跑。"""
    from cybercut.creative.policy import ApprovalMode, ApprovalPolicy

    policy = ApprovalPolicy(mode=ApprovalMode.HYBRID,
                            gates=("creative.storyboard",))
    orch, worker, db, data_root, pid, assets, _ = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    storyboard_node = _node_run_id(orch, run["run_id"], "creative.storyboard")
    before = _attempt_counts(db, run["run_id"])

    orch.reject(storyboard_node, TENANT, expected_revision=1,
                decided_by="reviewer", feedback="分镜节奏需调整")
    worker.drain()
    after = _attempt_counts(db, run["run_id"])

    assert after["creative.storyboard"] == 2
    assert after["creative.brief"] == before["creative.brief"]
    assert after["creative.script"] == before["creative.script"]
    for key in MEDIA_NODES:
        assert after[key] == before[key]


# ══════════════════════════════════════════════════════════════════
# 3) revise_node / revise_artifact
# ══════════════════════════════════════════════════════════════════


def test_revise_brief_invalidates_script_storyboard_match(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    before = _attempt_counts(db, run["run_id"])

    brief_node = _node_run_id(orch, run["run_id"], "creative.brief")
    orch.revise_node(brief_node, TENANT, expected_revision=1,
                     feedback="卖点顺序调整")
    worker.drain()
    after = _attempt_counts(db, run["run_id"])

    for key in MEDIA_NODES:
        assert after[key] == before[key], f"媒体节点 {key} 不得重跑"
    assert after["creative.brief"] == 2
    for key in ("creative.script", "creative.storyboard", "creative.match"):
        assert after[key] == 2, f"{key} 应被失效并重跑"
    for kind in KINDS:
        assert len(_rows(db, pid, kind)) == 2, f"{kind} 应产生新版本"


def test_revise_script_invalidates_storyboard_and_downstream(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    storyboard_node = _node_run_id(orch, run["run_id"], "creative.storyboard")

    orch.revise_node(script_node, TENANT, expected_revision=1,
                     new_prompt="更口语化")
    worker.drain()

    assert len(_rows(db, pid, "brief")) == 1, "brief 不得重跑"
    for kind in ("script", "storyboard", "match", "edit_plan", "template_bind"):
        assert len(_rows(db, pid, kind)) == 2, kind
    assert _attempt_counts(db, run["run_id"])["creative.brief"] == 1

    # storyboard 的冻结引用必须指向 script 的新版本
    refs = _frozen_refs(db, storyboard_node)
    script_ref = next(r for r in refs if r["kind"] == "creative.script")
    latest_script = _rows(db, pid, "script")[-1]
    assert script_ref["artifact_revision_id"] == latest_script["script_revision_id"]
    assert script_ref["artifact_revision_id"] != _rows(db, pid, "script")[0][
        "script_revision_id"]


def test_revise_storyboard_invalidates_downstream(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    storyboard_node = _node_run_id(orch, run["run_id"], "creative.storyboard")

    orch.revise_node(storyboard_node, TENANT, expected_revision=1,
                     feedback="镜头再多一点")
    worker.drain()

    assert len(_rows(db, pid, "brief")) == 1
    assert len(_rows(db, pid, "script")) == 1
    for kind in ("storyboard", "match", "edit_plan", "template_bind"):
        assert len(_rows(db, pid, kind)) == 2, kind
    counts = _attempt_counts(db, run["run_id"])
    assert counts["creative.brief"] == 1 and counts["creative.script"] == 1


def test_revise_match_invalidates_nothing_upstream(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    match_node = _node_run_id(orch, run["run_id"], "creative.match")

    orch.revise_node(match_node, TENANT, expected_revision=1,
                     new_prompt="提高匹配阈值")
    worker.drain()

    for kind in ("brief", "script", "storyboard"):
        assert len(_rows(db, pid, kind)) == 1, f"{kind} 不得重跑"
    for kind in ("match", "edit_plan", "template_bind"):
        assert len(_rows(db, pid, kind)) == 2, kind
    counts = _attempt_counts(db, run["run_id"])
    assert counts["creative.brief"] == 1
    assert counts["creative.script"] == 1
    assert counts["creative.storyboard"] == 1
    assert counts["creative.match"] == 2


def test_revise_artifact_resolves_node_and_revision(e2e) -> None:
    """按产物发起修订：`expected_revision` 缺省取该产物的 revision_no。"""
    orch, worker, db, data_root, pid, assets, policy = e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()

    script_row = _rows(db, pid, "script")[0]
    # 创作版本 id 即 ArtifactRevision id（四者一一对应）
    orch.revise_artifact(script_row["script_revision_id"], TENANT,
                         feedback="按产物修订")
    worker.drain()

    rows = _rows(db, pid, "script")
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert rows[1]["payload_json"]["revision_note"] == "按产物修订"
    assert len(_rows(db, pid, "storyboard")) == 2


def test_revise_rejects_stale_expected_revision(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    with pytest.raises(ConflictError):
        orch.revise_node(script_node, TENANT, expected_revision=99,
                         feedback="过期版本")


def test_revise_rejects_non_creative_node(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    media_node = _node_run_id(orch, run["run_id"], "asset_analysis")

    with pytest.raises(ValidationError):
        orch.revise_node(media_node, TENANT, expected_revision=1,
                         feedback="不应允许")


def test_revise_requires_feedback_or_prompt(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    with pytest.raises(ValidationError):
        orch.revise_node(script_node, TENANT, expected_revision=1)


def test_revise_artifact_missing_raises(e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = e2e
    with pytest.raises(NotFoundError):
        orch.revise_artifact("no-such-artifact", TENANT, feedback="x")


def test_revision_request_is_frozen_into_new_attempt(e2e) -> None:
    """修订请求必须冻结进 Run 输入，并出现在新 attempt 的 input_json 中。"""
    orch, worker, db, data_root, pid, assets, policy = e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.revise_node(script_node, TENANT, expected_revision=1,
                     feedback="冻结检查", new_prompt="更短")
    worker.drain()

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT input_json FROM cybercut.runs WHERE run_id = %s",
                        (run["run_id"],))
            frozen = cur.fetchone()["input_json"]
            cur.execute(
                "SELECT input_json FROM cybercut.node_attempts"
                " WHERE node_run_id = %s ORDER BY attempt_no", (script_node,))
            attempts = [r["input_json"] for r in cur.fetchall()]

    request = frozen["revision_requests"]["creative.script"]
    assert request["feedback"] == "冻结检查"
    assert request["new_prompt"] == "更短"
    assert request["revision_index"] == 1
    # 已冻结的素材与创作输入必须原样保留
    assert frozen["asset_ids"] == [a["asset_id"] for a in assets]
    assert frozen["creative_input"]["objective"] == CREATIVE_INPUT["objective"]

    assert len(attempts) == 2, "应有两次 attempt"
    assert "revision_request" not in attempts[0]
    assert attempts[1]["revision_request"]["new_prompt"] == "更短"


def test_media_pipeline_run_has_no_creative_revision_requests(db, data_root,
                                                              source_root) -> None:
    """非创作节点不产生修订请求字段（避免污染媒体层 run）。"""
    from cybercut.orchestrator.pipeline import ASSET_ANALYSIS_PIPELINE

    orch, worker, db, data_root, pid, assets, _ = _build(
        db, data_root, source_root, ASSET_ANALYSIS_PIPELINE, AUTO_POLICY)
    run = _start(orch, pid, assets)
    worker.drain()
    with db.transaction() as conn:
        frozen = runs.get(conn, run["run_id"], TENANT)["input_json"]
    assert "revision_requests" not in frozen


# ══════════════════════════════════════════════════════════════════
# 4) 0005：Project/Run 组合约束
# ══════════════════════════════════════════════════════════════════


_TABLES = {
    "brief": ("creative_brief_revisions", "brief_revision_id"),
    "script": ("script_revisions", "script_revision_id"),
    "storyboard": ("storyboard_revisions", "storyboard_revision_id"),
    "match": ("asset_match_revisions", "match_revision_id"),
}


@pytest.mark.parametrize("kind", list(_TABLES))
def test_cross_project_run_combination_is_rejected(db, kind: str) -> None:
    """同租户但 Project 不匹配的 run_id 必须被复合外键拒绝。"""
    with db.transaction() as conn:
        project_a = projects.create(conn, tenant_id=TENANT, name="proj-a")
        project_b = projects.create(conn, tenant_id=TENANT, name="proj-b")
        run_a = runs.create(conn, project_id=project_a["project_id"],
                            tenant_id=TENANT, pipeline_version="creative-v1")
        run_b = runs.create(conn, project_id=project_b["project_id"],
                            tenant_id=TENANT, pipeline_version="creative-v1")

    table, pk = _TABLES[kind]
    with pytest.raises(Exception) as excinfo:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO cybercut.{table} ({pk}, project_id, run_id,"
                    " tenant_id, revision_no, status, payload_json,"
                    " prompt_version, model_id)"
                    " VALUES (%s, %s, %s, %s, 1, 'active', '{}', 'p', 'm')",
                    ("x-1", project_a["project_id"], run_b["run_id"], TENANT))
    assert excinfo.value.__class__.__name__ == "ForeignKeyViolation", (
        f"{table} 必须拒绝跨 Project 的 run 组合")

    # 正确的组合必须成功
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO cybercut.{table} ({pk}, project_id, run_id,"
                " tenant_id, revision_no, status, payload_json,"
                " prompt_version, model_id)"
                " VALUES (%s, %s, %s, %s, 1, 'active', '{}', 'p', 'm')",
                ("x-2", project_a["project_id"], run_a["run_id"], TENANT))


def test_runs_unique_key_exists_for_composite_fk(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE schemaname = 'cybercut' AND tablename = 'runs'")
            names = {r["indexname"] for r in cur.fetchall()}
    assert "uq_runs_id_project_tenant" in names


def test_weak_run_tenant_fk_was_replaced(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'cybercut.script_revisions'::regclass"
                "   AND contype = 'f'")
            names = {r["conname"] for r in cur.fetchall()}
    assert "fk_script_run_project_tenant" in names
    assert "fk_script_run_tenant" not in names


def test_migration_resolves_test_dsn() -> None:
    assert resolve_test_dsn().startswith("postgresql://")
