"""Bridge the public FlowMind edit_* workflow to the CyberCut control plane."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flowmind.editing.store import get_project_store
from flowmind.tasks import CancelledError, current_task_context

_STAGE_TO_NODE = {
    "copy": "creative.brief",
    "script": "creative.script",
    "storyboard": "creative.storyboard",
    "pre_render": "edit.plan",
}
_NODE_TO_STAGE = {value: key for key, value in _STAGE_TO_NODE.items()}
_PROGRESS_STAGE_TO_NODE = {
    "understand": "asset_analysis",
    "copy": "creative.brief",
    "script": "creative.script",
    "storyboard": "creative.storyboard",
    "render": "render.qc",
}


def _approval_policy(review_stages: list[str]):
    from cybercut.creative.policy import ApprovalMode, ApprovalPolicy

    gates = tuple(_STAGE_TO_NODE[stage] for stage in review_stages)
    return ApprovalPolicy(
        mode=ApprovalMode.HYBRID if gates else ApprovalMode.AUTO,
        gates=gates,
    )


def _services(project_id: str):
    from cybercut.executor.worker import Worker
    from cybercut.media.ingest import ingest_asset
    from cybercut.nodes.creative_registry import build_creative_full_registry
    from cybercut.orchestrator.engine import Orchestrator
    from cybercut.orchestrator.pipeline import RENDER_PIPELINE
    from cybercut.storage.db import Database

    store = get_project_store()
    project = store.get_project(project_id)
    if project is None:
        raise RuntimeError(f"FlowMind 项目不存在：{project_id}")
    tenant_id = project.tenant_id or "local"
    state = project.input.setdefault("cybercut", {})
    db = Database()
    data_root = os.environ.get("CYBERCUT_DATA_ROOT") or None
    creative = project.input
    policy = _approval_policy(project.review_stages)
    orchestrator = Orchestrator(db, pipeline=RENDER_PIPELINE, data_root=data_root)
    return store, project, state, tenant_id, db, data_root, creative, policy, orchestrator, Worker, ingest_asset, build_creative_full_registry


def _ensure_run(project_id: str, services: tuple[Any, ...]) -> tuple[dict, Any, Any, str]:
    (store, project, bridge, tenant_id, db, data_root, creative, policy,
     orchestrator, Worker, ingest_asset, build_registry) = services
    if not bridge.get("project_id"):
        cp, _ = orchestrator.create_project(
            tenant_id=tenant_id, name=f"FlowMind edit {project_id}",
            goal={"flowmind_project_id": project_id},
            idempotency_key=f"flowmind:{project_id}",
        )
        bridge["project_id"] = cp["project_id"]
        store.save_project(project)
    cybercut_project_id = bridge["project_id"]
    asset_ids = list(bridge.get("asset_ids") or [])
    if not asset_ids:
        paths = creative.get("asset_paths") or []
        watermark_bbox = creative.get("watermark_bbox")
        for index, raw_path in enumerate(paths):
            source = Path(raw_path).resolve(strict=True)
            suffix = source.suffix.lower()
            if suffix in {".mp3", ".wav", ".m4a", ".aac"}:
                raise ValueError("CyberCut 当前不支持独立音频素材；请先移除音频素材")
            if watermark_bbox is not None:
                source = _remove_fixed_watermark(
                    source, watermark_bbox, project_id, index,
                )
            asset, _ = ingest_asset(
                db, project_id=cybercut_project_id, tenant_id=tenant_id,
                source_path=source, data_root=data_root,
            )
            asset_ids.append(asset["asset_id"])
        bridge["asset_ids"] = asset_ids
        bridge["watermark_removed"] = bool(watermark_bbox)
        store.save_project(project)
    if not bridge.get("run_id"):
        cp = orchestrator.get_project(cybercut_project_id, tenant_id)
        if cp.get("current_run_id"):
            bridge["run_id"] = cp["current_run_id"]
        else:
            objective = creative.get("theme", "")
            extra_brief = creative.get("extra_brief")
            if extra_brief:
                objective = f"{objective}\n{extra_brief}"
            input_args = {
                "objective": objective,
                "visual_style": creative.get("style"),
                "aspect_ratio": creative.get("aspect_ratio", "16:9"),
            }
            if creative.get("target_duration_s") is not None:
                input_args["target_duration_us"] = round(
                    float(creative["target_duration_s"]) * 1_000_000)
            run = orchestrator.start_run(
                project_id=cybercut_project_id, tenant_id=tenant_id,
                asset_ids=asset_ids,
                creative_input={"brief_input": input_args,
                                "script_input": input_args,
                                "edit_input": input_args},
                approval_policy=policy.to_json(),
            )
            bridge["run_id"] = run["run_id"]
        store.save_project(project)
    return project, orchestrator, (db, data_root, policy, Worker, build_registry), tenant_id


def _remove_fixed_watermark(
    source: Path, bbox: tuple[float, float, float, float],
    project_id: str, asset_index: int,
) -> Path:
    from flowmind.editing.pipeline import project_dir
    from flowmind.skills import _inpaint, _media
    from flowmind.skills.localize_video import _watermark_region
    from flowmind.tasks.gpu import gpu_lane

    if not _inpaint.available():
        raise RuntimeError(
            "固定水印去除需要 simple-lama-inpainting；请按 environment.yml 注释安装"
        )
    _, width, height = _media.probe_media(str(source))
    regions = _watermark_region(bbox, width, height)
    workdir = project_dir(project_id) / "cybercut-preprocess" / f"asset-{asset_index}"
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "watermark-removed.mp4"
    with gpu_lane():
        _inpaint.erase_regions(
            str(source), [], str(output), str(workdir), solid_regions=regions,
        )
    return output


def run_pipeline(project_id: str):
    """Run queued CyberCut nodes and adapt its state to FlowMind's project record."""
    store = get_project_store()
    project = store.get_project(project_id)
    try:
        services = _services(project_id)
        project, orchestrator, runtime, tenant_id = _ensure_run(project_id, services)
        db, data_root, policy, Worker, build_registry = runtime
        run_id = project.input["cybercut"]["run_id"]
        worker = Worker(
            db, build_registry(policy=policy, require_asr=False),
            data_root=data_root, orchestrator=orchestrator, run_id=run_id,
        )
        context = current_task_context()
        for _ in range(100):
            if context is not None and context.cancel_check():
                orchestrator.request_run_cancel(run_id, tenant_id)
                break
            if worker.step() is None:
                break
        if context is not None and context.cancel_check():
            orchestrator.request_run_cancel(run_id, tenant_id)
        state = orchestrator.run_state(run_id, tenant_id)
        run = state["run"]
        statuses = state["statuses"]
        waiting = next((key for key, value in statuses.items()
                        if value == "waiting_approval"), None)
        project.state = "awaiting_review" if waiting else {
            "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled",
            "waiting_approval": "awaiting_review",
        }.get(run["status"], "running")
        project.stage = _NODE_TO_STAGE.get(waiting, waiting or "render")
        project.stages = {
            stage: ("done" if statuses.get(node) == "succeeded" else "pending")
            for stage, node in _PROGRESS_STAGE_TO_NODE.items()
        }
        if project.state == "failed":
            project.error = (run.get("failure_message") or run.get("failure_code")
                             or "CyberCut run failed")
        bridge = project.input["cybercut"]
        bridge["statuses"] = statuses
        bridge["run_status"] = run["status"]
        store.save_project(project)
        if run["status"] == "cancelled":
            raise CancelledError(f"CyberCut Run {run_id} 已取消")
        from types import SimpleNamespace
        return SimpleNamespace(
            state=project.state, stage=project.stage, stages=project.stages,
            review_stages=project.review_stages,
            artifact_dir=str(Path(data_root or ".cybercut-data") / "projects"
                             / project.input["cybercut"]["project_id"]),
        )
    except CancelledError:
        if project is not None:
            project.state = "cancelled"
            project.stage = "cancelled"
            store.save_project(project)
        raise
    except Exception as exc:
        if project is not None:
            project.state = "failed"
            project.stage = "cybercut"
            project.error = f"CyberCut 后端失败：{type(exc).__name__}: {exc}"
            store.save_project(project)
        raise


def apply_review(project_id: str, stage: str, action: str, feedback: str | None) -> None:
    """Apply an edit_review action to the pending CyberCut approval."""
    services = _services(project_id)
    project, orchestrator, runtime, tenant_id = _ensure_run(project_id, services)
    db, data_root, policy, Worker, build_registry = runtime
    run_id = project.input["cybercut"]["run_id"]
    state = orchestrator.run_state(run_id, tenant_id)
    node_key = _STAGE_TO_NODE[stage]
    node = state["nodes"].get(node_key)
    if not node or node["status"] != "waiting_approval":
        raise ValueError(f"CyberCut 节点 {node_key} 当前不在审批等待")
    from cybercut.storage import approvals
    with db.transaction() as conn:
        pending = approvals.find_pending(conn, node["node_run_id"], tenant_id)
    if pending is None:
        raise ValueError(f"CyberCut 节点 {node_key} 缺少待处理审批记录")
    revision = int(pending["expected_revision"])
    if action == "approve":
        orchestrator.approve(node["node_run_id"], tenant_id,
                             expected_revision=revision, decided_by="flowmind.edit_review",
                             feedback=feedback)
    elif action == "revise":
        orchestrator.reject(node["node_run_id"], tenant_id,
                            expected_revision=revision, decided_by="flowmind.edit_review",
                            feedback=feedback)
    elif action == "reject":
        orchestrator.request_run_cancel(run_id, tenant_id)
    else:
        raise ValueError(f"不支持的审批动作：{action}")


def live_status(project_id: str) -> dict[str, Any] | None:
    """Read the latest CyberCut node states for edit_status."""
    from cybercut.orchestrator.engine import Orchestrator
    from cybercut.orchestrator.pipeline import RENDER_PIPELINE
    from cybercut.storage.db import Database

    project = get_project_store().get_project(project_id)
    bridge = project.input.get("cybercut", {}) if project else {}
    run_id = bridge.get("run_id")
    if not run_id:
        return None
    tenant_id = project.tenant_id or "local"
    db = Database()
    data_root = os.environ.get("CYBERCUT_DATA_ROOT") or None
    orchestrator = Orchestrator(db, pipeline=RENDER_PIPELINE, data_root=data_root)
    state = orchestrator.run_state(run_id, tenant_id)
    from cybercut.storage.paths import resolve_official_artifact_path
    cybercut_project_id = bridge["project_id"]
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (kind) kind, relative_path "
                "FROM cybercut.artifact_revisions WHERE project_id=%s "
                "AND tenant_id=%s AND status='active' "
                "ORDER BY kind, revision_no DESC",
                (cybercut_project_id, tenant_id),
            )
            revisions = cur.fetchall()
    project_root = Path(data_root or ".cybercut-data") / "projects" / cybercut_project_id
    state["artifacts"] = {
        row["kind"]: str(resolve_official_artifact_path(project_root, row["relative_path"]))
        for row in revisions
    }
    return state


def stage_map() -> dict[str, str]:
    return dict(_PROGRESS_STAGE_TO_NODE)


def review_stage_map() -> dict[str, str]:
    return dict(_NODE_TO_STAGE)

