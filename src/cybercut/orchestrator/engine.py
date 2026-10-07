"""编排引擎：Run 生命周期、依赖推进、审批、失效与聚合（WB-01 §七）。

职责边界：
- **只**维护「业务事实」（Project/Run/NodeRun/Approval/Artifact 的编排语义）；
- **不**执行节点（由 `executor` 负责），**不**直接管理租约；
- 每次 attempt 结束后由 worker / 测试调用 `advance_run()` 推进整条 Run。

关键不变量：
1. 依赖未全部 `succeeded` 的节点**不会**被入队（`ready` 也不入队）；
2. 重跑/失效只影响被标记的节点 —— **已完成的前置节点不会重复执行**；
3. 所有状态迁移经 `contracts.state_machine` 校验，非法即抛。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cybercut.contracts.enums import (
    ApprovalStatus,
    AttemptStatus,
    ExecutorKind,
    NodeRunStatus,
    ProjectStatus,
    RunStatus,
)
from cybercut.contracts.errors import ConflictError, NotFoundError
from cybercut.orchestrator.pipeline import TEST_PIPELINE, PipelineSpec
from cybercut.storage import approvals as approvals_repo
from cybercut.storage import events as events_repo
from cybercut.storage import idempotency as idem
from cybercut.storage import node_attempts, node_runs, projects, runs

logger = logging.getLogger(__name__)

OPERATION_CREATE_PROJECT = "create_project"
ERROR_WORKER_LOST = "WORKER_LOST"

_ACTIVE_ATTEMPT_STATUSES = (
    AttemptStatus.QUEUED.value,
    AttemptStatus.CLAIMED.value,
    AttemptStatus.RUNNING.value,
)


class Orchestrator:
    """CyberCut 控制面编排器。"""

    def __init__(
        self,
        db: Any,
        *,
        pipeline: PipelineSpec = TEST_PIPELINE,
        data_root: Path | str | None = None,
        auto_retry_worker_lost: bool = True,
        max_auto_retries: int = 1,
    ) -> None:
        self._db = db
        self.pipeline = pipeline
        self.data_root = data_root
        self.auto_retry_worker_lost = auto_retry_worker_lost
        self.max_auto_retries = int(max_auto_retries)

    # ══════════════════════════════════════════════════════════════
    # Project
    # ══════════════════════════════════════════════════════════════

    def create_project(
        self,
        *,
        tenant_id: str,
        name: str,
        goal: dict | None = None,
        automation_policy: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict, bool]:
        """创建项目，返回 `(project, replayed)`。

        携带 `idempotency_key` 时：同键同请求 -> 返回既有项目（replayed=True）；
        同键不同请求 -> `IdempotencyConflictError`。
        """
        payload = {"name": name, "goal": goal or {},
                   "automation_policy": automation_policy or {}}
        req_hash = idem.request_hash(payload)
        with self._db.transaction() as conn:
            if idempotency_key:
                existing = idem.check_or_reserve(
                    conn, tenant_id, OPERATION_CREATE_PROJECT, idempotency_key, req_hash
                )
                if existing is not None:
                    # 规范重放：必须命中既有资源，绝不因记录不完整而新建（防重复 Project）
                    resource_id = existing.get("resource_id")
                    row = (
                        projects.get(conn, resource_id, tenant_id)
                        if resource_id else None
                    )
                    if row is None:
                        raise ConflictError(
                            "幂等键已存在但关联资源不可用（拒绝重复创建）",
                            details={"operation": OPERATION_CREATE_PROJECT,
                                     "idempotency_key": idempotency_key},
                        )
                    return row, True
            project = projects.create(
                conn,
                tenant_id=tenant_id,
                name=name,
                goal=goal,
                automation_policy=automation_policy,
                status=ProjectStatus.DRAFT,
                idempotency_key=idempotency_key,
            )
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="project.created",
                project_id=project["project_id"], payload={"name": name},
            )
            if idempotency_key:
                idem.complete(
                    conn, tenant_id, OPERATION_CREATE_PROJECT, idempotency_key,
                    response_json={"project_id": project["project_id"]},
                    resource_id=project["project_id"],
                )
        return project, False

    def get_project(self, project_id: str, tenant_id: str) -> dict:
        with self._db.transaction() as conn:
            row = projects.get(conn, project_id, tenant_id)
        if row is None:
            raise NotFoundError("project 不存在")
        return row

    # ══════════════════════════════════════════════════════════════
    # Run
    # ══════════════════════════════════════════════════════════════

    def start_run(
        self,
        *,
        project_id: str,
        tenant_id: str,
        pipeline_version: str | None = None,
        requested_from_node: str | None = None,
    ) -> dict:
        """启动 Run 并生成全部 NodeRun（root=ready，其余=blocked），随后推进一次。"""
        version = pipeline_version or self.pipeline.version
        with self._db.transaction() as conn:
            project = projects.get_for_update(conn, project_id, tenant_id)
            if project is None:
                raise NotFoundError("project 不存在")
            if project["status"] == ProjectStatus.DRAFT.value:
                projects.update_status(
                    conn, project_id, tenant_id, to_status=ProjectStatus.ACTIVE
                )
            elif project["status"] != ProjectStatus.ACTIVE.value:
                raise ConflictError(
                    f"项目已处于终态（{project['status']}），无法启动新 Run"
                )
            run = runs.create(
                conn, project_id=project_id, tenant_id=tenant_id,
                pipeline_version=version, status=RunStatus.QUEUED,
                requested_from_node=requested_from_node,
            )
            projects.set_current_run(conn, project_id, tenant_id, run["run_id"])
            self._bootstrap_node_runs(conn, run, tenant_id)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.started",
                project_id=project_id, run_id=run["run_id"],
                payload={"run_no": run["run_no"], "pipeline_version": version},
            )
        self.advance_run(run["run_id"], tenant_id)
        with self._db.transaction() as conn:
            return runs.get(conn, run["run_id"], tenant_id)

    def _bootstrap_node_runs(self, conn: Any, run: dict, tenant_id: str) -> list[dict]:
        created: list[dict] = []
        for spec in self.pipeline.nodes:
            status = NodeRunStatus.READY if not spec.depends_on else NodeRunStatus.BLOCKED
            node_run = node_runs.create(
                conn,
                run_id=run["run_id"],
                tenant_id=tenant_id,
                node_key=spec.node_key,
                status=status,
                depends_on=list(spec.depends_on),
                approval_required=spec.approval_required,
            )
            created.append(node_run)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="node_run.created",
                run_id=run["run_id"], node_run_id=node_run["node_run_id"],
                payload={"node_key": spec.node_key, "status": str(status)},
            )
        return created

    # ══════════════════════════════════════════════════════════════
    # 推进
    # ══════════════════════════════════════════════════════════════

    def advance_run(self, run_id: str, tenant_id: str) -> dict:
        """推进 Run：依赖放开 -> 重试策略 -> 入队 -> 取消 -> 聚合。"""
        enqueued: list[str] = []
        with self._db.transaction() as conn:
            run = runs.get_for_update(conn, run_id, tenant_id)
            if run is None:
                raise NotFoundError("run 不存在")
            if run["status"] in (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value,
                                 RunStatus.CANCELLED.value):
                return {"run": run, "enqueued": enqueued, "terminal": True,
                        "project": None}

            # 0) 已请求取消 -> 优先收敛，不再入队任何新 attempt
            if run["cancel_requested_at"] is not None:
                self._apply_cancellation(conn, run_id, tenant_id)
                result = self._aggregate(conn, run_id, tenant_id)
                return {"run": result["run"], "enqueued": enqueued,
                        "project": result.get("project")}

            # 1) 依赖放开：blocked / invalidated -> ready（依赖全部 succeeded）
            for node_run in node_runs.list_for_run(conn, run_id, tenant_id):
                if node_run["status"] not in (
                    NodeRunStatus.BLOCKED.value, NodeRunStatus.INVALIDATED.value
                ):
                    continue
                by_key = self._by_key(conn, run_id, tenant_id)
                if self._deps_succeeded(node_run, by_key):
                    clear = node_run["status"] == NodeRunStatus.INVALIDATED.value
                    node_runs.set_status(
                        conn, node_run["node_run_id"], tenant_id,
                        to_status=NodeRunStatus.READY, clear_invalidated=clear,
                    )
                    events_repo.append(
                        conn, tenant_id=tenant_id, event_type="node_run.ready",
                        run_id=run_id, node_run_id=node_run["node_run_id"],
                        payload={"node_key": node_run["node_key"]},
                    )

            # 2) worker 丢失重试策略：failed(WORKER_LOST) -> ready（新建 attempt）
            if self.auto_retry_worker_lost:
                self._apply_worker_lost_retry(
                    conn, run_id, tenant_id, self._by_key(conn, run_id, tenant_id)
                )

            # 3) 入队：ready + 依赖满足 + 无活动 attempt
            by_key = self._by_key(conn, run_id, tenant_id)
            for node_run in by_key.values():
                if node_run["status"] != NodeRunStatus.READY.value:
                    continue
                if not self._deps_succeeded(node_run, by_key):
                    continue
                if node_attempts.count_active(
                    conn, node_run["node_run_id"], tenant_id
                ) > 0:
                    continue
                attempt = self._enqueue_attempt(
                    conn, node_run, tenant_id, reason="ready"
                )
                enqueued.append(attempt["attempt_id"])

            # 4) 聚合
            result = self._aggregate(conn, run_id, tenant_id)
        return {"run": result["run"], "enqueued": enqueued,
                "project": result.get("project")}

    def _by_key(self, conn: Any, run_id: str, tenant_id: str) -> dict[str, dict]:
        return {
            nr["node_key"]: nr
            for nr in node_runs.list_for_run(conn, run_id, tenant_id)
        }

    @staticmethod
    def _deps_succeeded(node_run: dict, by_key: dict[str, dict]) -> bool:
        for dep in node_run.get("depends_on") or []:
            dep_row = by_key.get(dep)
            if dep_row is None or dep_row["status"] != NodeRunStatus.SUCCEEDED.value:
                return False
        return True

    def _enqueue_attempt(
        self, conn: Any, node_run: dict, tenant_id: str, *, reason: str
    ) -> dict:
        node_run_id = node_run["node_run_id"]
        attempt_no = node_runs.bump_attempt_no(conn, node_run_id, tenant_id)
        attempt = node_attempts.create(
            conn,
            node_run_id=node_run_id,
            tenant_id=tenant_id,
            attempt_no=attempt_no,
            executor_kind=ExecutorKind.CYBERCUT_WORKER,
            input_json={
                "node_key": node_run["node_key"],
                "depends_on": list(node_run.get("depends_on") or []),
                "reason": reason,
            },
        )
        events_repo.append(
            conn, tenant_id=tenant_id, event_type="attempt.enqueued",
            run_id=node_run["run_id"], node_run_id=node_run_id,
            payload={"attempt_id": attempt["attempt_id"], "attempt_no": attempt_no,
                     "node_key": node_run["node_key"], "reason": reason},
        )
        return attempt

    def _apply_worker_lost_retry(
        self, conn: Any, run_id: str, tenant_id: str, by_key: dict[str, dict]
    ) -> None:
        for node_run in list(by_key.values()):
            if node_run["status"] != NodeRunStatus.FAILED.value:
                continue
            attempts = node_attempts.list_for_node_run(
                conn, node_run["node_run_id"], tenant_id
            )
            if not attempts:
                continue
            last = attempts[-1]
            if last["error_code"] != ERROR_WORKER_LOST:
                continue
            if len(attempts) > self.max_auto_retries:
                continue
            node_runs.set_status(
                conn, node_run["node_run_id"], tenant_id,
                to_status=NodeRunStatus.READY, clear_invalidated=True,
            )
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="node_run.auto_retry",
                run_id=run_id, node_run_id=node_run["node_run_id"],
                payload={"reason": ERROR_WORKER_LOST, "attempts": len(attempts)},
            )

    def _apply_cancellation(self, conn: Any, run_id: str, tenant_id: str) -> None:
        """取消：先取消活动 attempt / 节点，再把 Run 收敛到 cancelled。

        锁顺序（WB-01R2 §三）：**run -> node_run -> node_attempt**。
        """
        current = runs.get_for_update(conn, run_id, tenant_id)
        for node_run in node_runs.list_for_run(conn, run_id, tenant_id):
            if node_run["status"] in (
                NodeRunStatus.RUNNING.value, NodeRunStatus.WAITING_APPROVAL.value
            ):
                node_runs.set_status(
                    conn, node_run["node_run_id"], tenant_id,
                    to_status=NodeRunStatus.CANCELLED,
                )
            for attempt in node_attempts.list_for_node_run(
                conn, node_run["node_run_id"], tenant_id
            ):
                if attempt["status"] in _ACTIVE_ATTEMPT_STATUSES:
                    node_attempts.cancel(conn, attempt["attempt_id"], tenant_id)

        if current is not None and current["status"] == RunStatus.QUEUED.value:
            current = runs.update_status(
                conn, run_id, tenant_id, to_status=RunStatus.RUNNING, mark_started=True
            )
        if current is not None and current["status"] in (
            RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value
        ):
            current = runs.update_status(
                conn, run_id, tenant_id, to_status=RunStatus.CANCELLING,
                cancel_requested=True,
            )
        if current is not None and current["status"] == RunStatus.CANCELLING.value:
            runs.update_status(conn, run_id, tenant_id, to_status=RunStatus.CANCELLED)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.cancelled", run_id=run_id,
                project_id=current["project_id"],
            )

    def _to_status(
        self, conn: Any, run_id: str, tenant_id: str, target: RunStatus,
        *, failure_code: str | None = None, failure_message: str | None = None,
    ) -> dict:
        """把 Run 迁移到 target（自动补 queued/waiting_approval -> running 中间步）。"""
        run = runs.get_for_update(conn, run_id, tenant_id)
        if run is None:  # pragma: no cover
            raise NotFoundError("run 不存在")
        if run["status"] == RunStatus.QUEUED.value and target != RunStatus.RUNNING:
            run = runs.update_status(
                conn, run_id, tenant_id, to_status=RunStatus.RUNNING, mark_started=True
            )
        if (run["status"] == RunStatus.WAITING_APPROVAL.value
                and target != RunStatus.RUNNING):
            run = runs.update_status(conn, run_id, tenant_id, to_status=RunStatus.RUNNING)
        if run["status"] != target.value:
            run = runs.update_status(
                conn, run_id, tenant_id, to_status=target,
                failure_code=failure_code, failure_message=failure_message,
            )
        return run

    def _aggregate(self, conn: Any, run_id: str, tenant_id: str) -> dict:
        run = runs.get_for_update(conn, run_id, tenant_id)
        if run is None:  # pragma: no cover
            raise NotFoundError("run 不存在")
        project_id = run["project_id"]
        statuses = [
            nr["status"] for nr in node_runs.list_for_run(conn, run_id, tenant_id)
        ]

        if run["status"] == RunStatus.CANCELLED.value:
            return {"run": run,
                    "project": self._finish_project(conn, project_id, tenant_id,
                                                    ProjectStatus.CANCELLED)}

        if all(s == NodeRunStatus.SUCCEEDED.value for s in statuses):
            run = self._to_status(conn, run_id, tenant_id, RunStatus.SUCCEEDED)
            project = self._finish_project(conn, project_id, tenant_id,
                                           ProjectStatus.COMPLETED)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.succeeded",
                project_id=project_id, run_id=run_id,
            )
            return {"run": run, "project": project}

        if any(s == NodeRunStatus.FAILED.value for s in statuses):
            run = self._to_status(
                conn, run_id, tenant_id, RunStatus.FAILED,
                failure_code="NODE_FAILED", failure_message="存在失败节点",
            )
            project = self._finish_project(conn, project_id, tenant_id,
                                           ProjectStatus.FAILED)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.failed",
                project_id=project_id, run_id=run_id,
            )
            return {"run": run, "project": project}

        if any(s == NodeRunStatus.WAITING_APPROVAL.value for s in statuses):
            if run["status"] == RunStatus.RUNNING.value:
                run = runs.update_status(
                    conn, run_id, tenant_id, to_status=RunStatus.WAITING_APPROVAL
                )
            return {"run": run, "project": None}

        has_active = any(
            s in (NodeRunStatus.READY.value, NodeRunStatus.RUNNING.value)
            for s in statuses
        )
        if run["status"] == RunStatus.QUEUED.value and has_active:
            run = runs.update_status(
                conn, run_id, tenant_id, to_status=RunStatus.RUNNING, mark_started=True
            )
        elif run["status"] == RunStatus.WAITING_APPROVAL.value and has_active:
            run = runs.update_status(conn, run_id, tenant_id, to_status=RunStatus.RUNNING)
        return {"run": run, "project": None}

    def _finish_project(
        self, conn: Any, project_id: str, tenant_id: str, target: ProjectStatus
    ) -> dict:
        project = projects.get_for_update(conn, project_id, tenant_id)
        if project is None:  # pragma: no cover
            raise NotFoundError("project 不存在")
        if project["status"] != ProjectStatus.ACTIVE.value:
            return project
        return projects.update_status(conn, project_id, tenant_id, to_status=target)

    # ══════════════════════════════════════════════════════════════
    # 审批
    # ══════════════════════════════════════════════════════════════

    def approve(
        self,
        node_run_id: str,
        tenant_id: str,
        *,
        expected_revision: int,
        decided_by: str,
        feedback: str | None = None,
    ) -> dict:
        return self._decide(
            node_run_id, tenant_id, approved=True, expected_revision=expected_revision,
            decided_by=decided_by, feedback=feedback,
        )

    def reject(
        self,
        node_run_id: str,
        tenant_id: str,
        *,
        expected_revision: int,
        decided_by: str,
        feedback: str | None = None,
    ) -> dict:
        """驳回：闸门回 ready；被驳上游失效重跑；其后代连锁失效。"""
        return self._decide(
            node_run_id, tenant_id, approved=False, expected_revision=expected_revision,
            decided_by=decided_by, feedback=feedback,
        )

    def _decide(
        self,
        node_run_id: str,
        tenant_id: str,
        *,
        approved: bool,
        expected_revision: int,
        decided_by: str,
        feedback: str | None,
    ) -> dict:
        with self._db.transaction() as conn:
            node_run = node_runs.get(conn, node_run_id, tenant_id)
            if node_run is None:
                raise NotFoundError("node_run 不存在")
            if node_run["status"] != NodeRunStatus.WAITING_APPROVAL.value:
                raise ConflictError(f"节点当前状态为 {node_run['status']}，不可审批")
            approval = approvals_repo.find_by_revision(
                conn, node_run_id, int(expected_revision), tenant_id
            )
            if approval is None:
                raise ConflictError(
                    "审批版本不匹配（expected_revision 过期）",
                    details={"expected_revision": int(expected_revision)},
                )
            if approval["status"] != ApprovalStatus.PENDING.value:
                raise ConflictError(f"该审批已处理（status={approval['status']}）")
            approvals_repo.decide(
                conn, approval["approval_id"], tenant_id,
                to_status=(
                    ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                ),
                decided_by=decided_by, feedback=feedback,
            )
            run_id = node_run["run_id"]

            if approved:
                node_runs.set_status(
                    conn, node_run_id, tenant_id, to_status=NodeRunStatus.SUCCEEDED
                )
                events_repo.append(
                    conn, tenant_id=tenant_id, event_type="approval.approved",
                    run_id=run_id, node_run_id=node_run_id,
                    payload={"expected_revision": int(expected_revision),
                             "decided_by": decided_by},
                )
            else:
                node_runs.set_status(
                    conn, node_run_id, tenant_id, to_status=NodeRunStatus.READY,
                    clear_invalidated=True,
                )
                invalidated = self._invalidate_on_reject(
                    conn, node_run, tenant_id, reason="approval_rejected"
                )
                events_repo.append(
                    conn, tenant_id=tenant_id, event_type="approval.rejected",
                    run_id=run_id, node_run_id=node_run_id,
                    payload={"expected_revision": int(expected_revision),
                             "decided_by": decided_by, "feedback": feedback,
                             "invalidated": invalidated},
                )
        self.advance_run(run_id, tenant_id)
        with self._db.transaction() as conn:
            return node_runs.get(conn, node_run_id, tenant_id)

    def _invalidate_on_reject(
        self, conn: Any, gate: dict, tenant_id: str, *, reason: str
    ) -> list[str]:
        """驳回连锁失效。返回被失效的 node_key 列表。"""
        run_id = gate["run_id"]
        invalidated: list[str] = []
        targets: list[str] = []
        for dep in gate.get("depends_on") or []:
            dep_row = node_runs.get_by_key(conn, run_id, dep, tenant_id)
            if dep_row and dep_row["status"] == NodeRunStatus.SUCCEEDED.value:
                targets.append(dep)

        for key in targets:
            dep_row = node_runs.get_by_key(conn, run_id, key, tenant_id)
            node_runs.set_status(
                conn, dep_row["node_run_id"], tenant_id,
                to_status=NodeRunStatus.INVALIDATED, invalidated_reason=reason,
            )
            node_runs.set_status(
                conn, dep_row["node_run_id"], tenant_id,
                to_status=NodeRunStatus.READY, clear_invalidated=True,
            )
            invalidated.append(key)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="node_run.invalidated",
                run_id=run_id, node_run_id=dep_row["node_run_id"],
                payload={"reason": reason, "node_key": key},
            )

        descendants: list[str] = []
        for key in targets:
            descendants.extend(self.pipeline.descendants_of(key))
        for key in dict.fromkeys(descendants):
            if key == gate["node_key"]:
                continue
            row = node_runs.get_by_key(conn, run_id, key, tenant_id)
            if row is None:
                continue
            if row["status"] == NodeRunStatus.SUCCEEDED.value:
                node_runs.set_status(
                    conn, row["node_run_id"], tenant_id,
                    to_status=NodeRunStatus.INVALIDATED, invalidated_reason=reason,
                )
                invalidated.append(key)
            elif row["status"] == NodeRunStatus.WAITING_APPROVAL.value:
                node_runs.set_status(
                    conn, row["node_run_id"], tenant_id,
                    to_status=NodeRunStatus.CANCELLED,
                )
                pending = approvals_repo.find_pending(conn, row["node_run_id"], tenant_id)
                if pending is not None:
                    approvals_repo.decide(
                        conn, pending["approval_id"], tenant_id,
                        to_status=ApprovalStatus.CANCELLED,
                        decided_by="system:invalidation",
                        feedback=f"上游失效（{reason}）",
                    )
                invalidated.append(key)
        return invalidated

    # ══════════════════════════════════════════════════════════════
    # 重跑 / 取消
    # ══════════════════════════════════════════════════════════════

    def retry_node(self, node_run_id: str, tenant_id: str) -> dict:
        """显式重试失败节点：`failed -> ready`（随后由 advance 新建 attempt）。"""
        with self._db.transaction() as conn:
            node_run = node_runs.get(conn, node_run_id, tenant_id)
            if node_run is None:
                raise NotFoundError("node_run 不存在")
            node_runs.set_status(
                conn, node_run_id, tenant_id, to_status=NodeRunStatus.READY,
                clear_invalidated=True,
            )
            run_id = node_run["run_id"]
        self.advance_run(run_id, tenant_id)
        with self._db.transaction() as conn:
            return node_runs.get(conn, node_run_id, tenant_id)

    def request_run_cancel(self, run_id: str, tenant_id: str) -> dict:
        """请求取消 Run（幂等）。"""
        with self._db.transaction() as conn:
            run = runs.get_for_update(conn, run_id, tenant_id)
            if run is None:
                raise NotFoundError("run 不存在")
            if run["status"] in (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value,
                                 RunStatus.CANCELLED.value):
                return run
            if run["cancel_requested_at"] is None:
                if run["status"] == RunStatus.QUEUED.value:
                    runs.update_status(
                        conn, run_id, tenant_id, to_status=RunStatus.RUNNING,
                        mark_started=True,
                    )
                runs.update_status(
                    conn, run_id, tenant_id, to_status=RunStatus.CANCELLING,
                    cancel_requested=True,
                )
        self.advance_run(run_id, tenant_id)
        with self._db.transaction() as conn:
            return runs.get(conn, run_id, tenant_id)

    def cancel_attempt(self, attempt_id: str, tenant_id: str) -> dict:
        """取消单条 attempt（queued/claimed/running -> cancelled）。"""
        with self._db.transaction() as conn:
            return node_attempts.cancel(conn, attempt_id, tenant_id)

    # ══════════════════════════════════════════════════════════════
    # 只读辅助
    # ══════════════════════════════════════════════════════════════

    def run_state(self, run_id: str, tenant_id: str) -> dict:
        with self._db.transaction() as conn:
            run = runs.get(conn, run_id, tenant_id)
            if run is None:
                raise NotFoundError("run 不存在")
            node_list = node_runs.list_for_run(conn, run_id, tenant_id)
        return {
            "run": run,
            "nodes": {nr["node_key"]: nr for nr in node_list},
            "statuses": {nr["node_key"]: nr["status"] for nr in node_list},
        }

    def attempts_of(self, node_run_id: str, tenant_id: str) -> list[dict]:
        with self._db.transaction() as conn:
            return node_attempts.list_for_node_run(conn, node_run_id, tenant_id)


__all__ = ["ERROR_WORKER_LOST", "OPERATION_CREATE_PROJECT", "Orchestrator"]
