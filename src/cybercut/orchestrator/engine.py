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
from cybercut.contracts.errors import ConflictError, NotFoundError, ValidationError
from cybercut.contracts.run_input import freeze_run_input, validate_start_assets
from cybercut.creative.policy import CREATIVE_NODE_KEYS, creative_descendants
from cybercut.orchestrator.pipeline import TEST_PIPELINE, PipelineSpec
from cybercut.storage import approvals as approvals_repo
from cybercut.storage import artifacts as artifacts_repo
from cybercut.storage import (
    creative_revisions,
    media_assets,
    node_attempts,
    node_runs,
    projects,
    runs,
)
from cybercut.storage import events as events_repo
from cybercut.storage import idempotency as idem

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
        asset_ids: list[str] | None = None,
        creative_input: dict | None = None,
        approval_policy: dict | None = None,
    ) -> dict:
        """启动 Run 并生成全部 NodeRun（root=ready，其余=blocked），随后推进一次。

        `asset_ids`（WB-02 §三.3/4）：显式输入素材列表；校验归属后**冻结**进
        `runs.input_json`，root attempt 的 `input_json` 也会携带同样的冻结内容。

        `creative_input` / `approval_policy`（WB-03）：创作层目标与审批策略，
        与素材一起冻结进 `runs.input_json`，并下发到**每个** attempt。
        """
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
            asset_id_list = list(asset_ids or [])
            frozen_input: dict | None = None
            if asset_id_list or self.pipeline.requires_assets:
                if not asset_id_list:
                    raise ValidationError(
                        f"管线 {self.pipeline.name!r} 至少需要一个素材",
                        details={"pipeline": self.pipeline.name},
                    )
                assets = media_assets.list_by_ids(
                    conn, project_id, tenant_id, list(asset_ids)
                )
                # WB-02R2 §二.4：只允许 ready 且正式文件存在
                for a in assets:
                    if a["status"] != "ready":
                        raise ValidationError(
                            f"素材 {a['asset_id']} 状态为 {a['status']}（仅 ready 可启动）",
                            details={"asset_id": a["asset_id"], "status": a["status"]},
                        )
                validate_start_assets(assets, data_root=self.data_root)
                frozen_input = freeze_run_input(
                    assets, require_assets=self.pipeline.requires_assets,
                    pipeline_name=self.pipeline.name,
                    creative_input=creative_input,
                    approval_policy=approval_policy,
                )
            elif creative_input is not None or approval_policy is not None:
                # 无素材管线（如 TEST_PIPELINE）也允许冻结创作层输入
                frozen_input = freeze_run_input(
                    [], require_assets=False, pipeline_name=self.pipeline.name,
                    creative_input=creative_input,
                    approval_policy=approval_policy,
                )
            run = runs.create(
                conn, project_id=project_id, tenant_id=tenant_id,
                pipeline_version=version, status=RunStatus.QUEUED,
                requested_from_node=requested_from_node, input_json=frozen_input,
            )
            projects.set_current_run(conn, project_id, tenant_id, run["run_id"])
            self._bootstrap_node_runs(conn, run, tenant_id)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.started",
                project_id=project_id, run_id=run["run_id"],
                payload={"run_no": run["run_no"], "pipeline_version": version,
                         "asset_ids": (frozen_input or {}).get("asset_ids", [])},
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
        """入队并**冻结输入**（WB-02 §三.4-9）。

        - root 节点：携带 Run 冻结的 `asset_ids` / `assets`；
        - 下游节点：把**直接依赖**的最新 ArtifactRevision 固化进
          `node_runs.input_revision_refs`（首次入队时写入，重试不漂移），
          并复制进 `node_attempts.input_json.input_artifacts`。
        """
        node_run_id = node_run["node_run_id"]
        attempt_no = node_runs.bump_attempt_no(conn, node_run_id, tenant_id)
        run = runs.get(conn, node_run["run_id"], tenant_id)
        run_input = (run or {}).get("input_json") or {}
        depends_on = list(node_run.get("depends_on") or [])

        # WB-02R §一.1：**每个** attempt 都携带 Run 冻结的 asset_ids/assets
        input_payload: dict[str, Any] = {
            "reason": reason,
            "asset_ids": list(run_input.get("asset_ids") or []),
            "assets": list(run_input.get("assets") or []),
        }
        # WB-03 §三：创作层输入与审批策略同样在每个 attempt 中下发
        if "creative_input" in run_input:
            input_payload["creative_input"] = dict(
                run_input.get("creative_input") or {})
        if "approval_policy" in run_input:
            input_payload["approval_policy"] = dict(
                run_input.get("approval_policy") or {})
        # WB-03R §1/§2：该节点的**定向修订请求**（人工反馈 / 新提示词）
        # 随新 attempt 一起冻结，保证重跑时输入不漂移。
        revision_request = (run_input.get("revision_requests") or {}).get(
            node_run["node_key"]
        )
        if revision_request:
            input_payload["revision_request"] = dict(revision_request)
        if depends_on:
            # §一.2/§一.6：下游另携带直接依赖的冻结 ArtifactRevision
            # （首次入队写入 node_runs.input_revision_refs，重试不漂移）
            refs = list(node_run.get("input_revision_refs") or [])
            if not refs:
                by_key = self._by_key(conn, node_run["run_id"], tenant_id)
                dep_ids = [
                    by_key[dep]["node_run_id"]
                    for dep in depends_on
                    if dep in by_key
                ]
                refs = artifacts_repo.refs_for_node_runs(
                    conn, run["project_id"], tenant_id, dep_ids
                )
                node_runs.set_input_revision_refs(
                    conn, node_run_id, tenant_id, refs
                )
            input_payload["input_artifacts"] = refs

        attempt = node_attempts.create(
            conn,
            node_run_id=node_run_id,
            tenant_id=tenant_id,
            attempt_no=attempt_no,
            executor_kind=ExecutorKind.CYBERCUT_WORKER,
            input_json={
                "node_key": node_run["node_key"],
                "depends_on": depends_on,
                **input_payload,
            },
        )
        events_repo.append(
            conn, tenant_id=tenant_id, event_type="attempt.enqueued",
            run_id=node_run["run_id"], node_run_id=node_run_id,
            payload={"attempt_id": attempt["attempt_id"], "attempt_no": attempt_no,
                     "node_key": node_run["node_key"], "reason": reason,
                     "input_artifacts": len(input_payload.get("input_artifacts", [])
                                            or [])},
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
                    "project": self._aggregate_project_status(
                        conn, project_id, tenant_id,
                        reason="run_cancelled")}

        if all(s == NodeRunStatus.SUCCEEDED.value for s in statuses):
            run = self._to_status(conn, run_id, tenant_id, RunStatus.SUCCEEDED)
            project = self._aggregate_project_status(
                conn, project_id, tenant_id, reason="run_succeeded")
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
            project = self._aggregate_project_status(
                conn, project_id, tenant_id, reason="run_failed")
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

    #: 处于活动状态（**非终态**）的 Run 状态：只要存在任一，项目就不得进入终态
    _RUN_ACTIVE_STATUSES = frozenset({
        RunStatus.QUEUED.value, RunStatus.RUNNING.value,
        RunStatus.WAITING_APPROVAL.value, RunStatus.CANCELLING.value,
    })

    def _aggregate_project_status(
        self, conn: Any, project_id: str, tenant_id: str, *, reason: str
    ) -> dict | None:
        """按**项目下所有 Run**聚合项目状态（WB-03R2-复审）。

        **两段式判定**，消除「Run 状态快照 → 新 Run 提交 → 聚合落终态」竞态：

        ① **快照**（未加项目锁）：仅用于提前短路 —— 若快照中已有活动 Run 则
           直接返回；该路径只会**避免**写终态，绝不会造成误标；
        ② **权威判定**：在与 `start_run` 一致的 **Project 锁**保护下
           **重新读取**全部 Run 状态并计算聚合结果。

        由此「新 Run 的创建」与「项目终态写入」被项目锁串行化：

        - 若新 Run 先提交，锁内重读会发现活动 Run ⇒ 项目保持 `active`；
        - 若终态写入先提交，`start_run` 会因项目已终态而被拒绝。

        聚合语义（已同步 ADR-0004 决策 34 与 `WB-03-creative-layer.md` §8）：

        - **Project 状态由项目下全部 Run 聚合得出，与 `current_run_id` 无关**
          （`current_run_id` 只是「最近一次启动的 Run」指针）；
        - 只要存在任一**非终态** Run（queued / running / waiting_approval /
          cancelling），项目就**不得**进入 `completed` / `failed` / `cancelled`；
        - 全部 Run 均为终态时：任一 `failed` ⇒ `failed`；否则任一 `cancelled`
          ⇒ `cancelled`；否则（全部 `succeeded`）⇒ `completed`；
        - 若先前项目已被误标为某终态而聚合结果不同（例如较早 Run 完成时
          误标 `completed`，随后当前 Run 失败），由
          `projects.apply_aggregate_status` 做**受控修正**，不得保持错误状态。

        返回聚合后的 Project 行；无变化或无 Run 时返回 `None`。
        """
        # ① Run 状态快照（未加项目锁）：只用于提前短路，不作为终态依据
        snapshot = runs.statuses_for_project(conn, project_id, tenant_id)
        if any(s in self._RUN_ACTIVE_STATUSES for s in snapshot):
            return None  # 快照已有活动 Run：无需写终态（安全短路）

        # ② 与 start_run 一致的 Project 锁：串行化「新 Run 创建」与「终态写入」，
        #    并在锁内**重新读取** Run 状态作为权威判定依据。
        projects.get_for_update(conn, project_id, tenant_id)
        statuses = runs.statuses_for_project(conn, project_id, tenant_id)
        if not statuses or any(s in self._RUN_ACTIVE_STATUSES for s in statuses):
            return None  # 锁内重读发现活动 Run：保持 active

        if any(s == RunStatus.FAILED.value for s in statuses):
            target = ProjectStatus.FAILED
        elif any(s == RunStatus.CANCELLED.value for s in statuses):
            target = ProjectStatus.CANCELLED
        elif all(s == RunStatus.SUCCEEDED.value for s in statuses):
            target = ProjectStatus.COMPLETED
        else:  # pragma: no cover - Run 状态枚举已全覆盖
            return None
        return projects.apply_aggregate_status(
            conn, project_id, tenant_id, to_status=target, reason=reason
        )

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
                # WB-03R2 §3：审批通过 ⇒ 当前版本 active，旧 active 版本 superseded
                activated = self._activate_creative_version(
                    conn, approval, tenant_id
                )
                events_repo.append(
                    conn, tenant_id=tenant_id, event_type="approval.approved",
                    run_id=run_id, node_run_id=node_run_id,
                    payload={"expected_revision": int(expected_revision),
                             "decided_by": decided_by,
                             "activated": activated},
                )
            else:
                # WB-03R §1：驳回**只重跑被驳节点自身**，且**只失效其创作层下游**；
                # 媒体层（probe/normalize/scene/transcribe/analysis）绝不失效或重跑。
                # 人工反馈冻结进新 attempt，新 attempt 产出**新版本**，
                # 旧版本与 rejected 审批记录均原样保留。
                # WB-03R2 §3：审批驳回 ⇒ 该版本置 rejected（既有 active 不变）
                rejected_version = self._reject_creative_version(
                    conn, approval, tenant_id
                )
                if node_run["node_key"] in CREATIVE_NODE_KEYS:
                    # 创作层闸门：只重跑自身 + 只失效创作层下游（WB-03R §1）
                    self._freeze_revision_request(
                        conn, run_id, tenant_id,
                        node_key=node_run["node_key"],
                        feedback=feedback, new_prompt=None,
                        expected_revision=int(expected_revision),
                    )
                    node_runs.set_status(
                        conn, node_run_id, tenant_id,
                        to_status=NodeRunStatus.READY, clear_invalidated=True,
                    )
                    node_runs.set_input_revision_refs(
                        conn, node_run_id, tenant_id, []
                    )
                    invalidated = self._invalidate_creative_stage(
                        conn, node_run, tenant_id, reason="approval_rejected"
                    )
                else:
                    # 非创作层闸门（如 TEST_PIPELINE 的 approval_gate）：
                    # 保持 WB-01 既有语义（重新打开上游依赖并级联失效后代）。
                    node_runs.set_status(
                        conn, node_run_id, tenant_id,
                        to_status=NodeRunStatus.READY, clear_invalidated=True,
                    )
                    invalidated = self._invalidate_on_reject(
                        conn, node_run, tenant_id, reason="approval_rejected"
                    )
                events_repo.append(
                    conn, tenant_id=tenant_id, event_type="approval.rejected",
                    run_id=run_id, node_run_id=node_run_id,
                    payload={"expected_revision": int(expected_revision),
                             "decided_by": decided_by, "feedback": feedback,
                             "invalidated": invalidated,
                             "rejected_version": rejected_version},
                )
        self.advance_run(run_id, tenant_id)
        with self._db.transaction() as conn:
            return node_runs.get(conn, node_run_id, tenant_id)

    # ══════════════════════════════════════════════════════════════
    # 定向修订（WB-03R §2）
    # ══════════════════════════════════════════════════════════════

    def revise_node(
        self,
        node_run_id: str,
        tenant_id: str,
        *,
        expected_revision: int,
        feedback: str | None = None,
        new_prompt: str | None = None,
        decided_by: str = "system:revision",
    ) -> dict:
        """对某个创作节点发起**定向修订**（不重跑媒体层与其无关节点）。

        - `expected_revision` 必须等于**目标 Run 内该节点**当前创作产物的
          `revision_no`（乐观守卫，按 Run 作用域比较；项目级版本号仅用于分配）；
        - 锁序严格为 Run -> NodeRun -> NodeAttempt -> Project，且节点状态在持锁后重读；
        - `feedback` / `new_prompt` 冻结进新 attempt；
        - 该节点自身重新入队；**只**失效其创作层下游并清空其冻结输入引用；
        - 未失效的节点不会重跑；历史版本与历史审批记录全部保留。
        """
        if not feedback and not new_prompt:
            raise ValidationError("定向修订必须提供 feedback 或 new_prompt")
        with self._db.transaction() as conn:
            # ── 锁序（WB-03R2 §1）：统一 Run -> NodeRun -> NodeAttempt -> Project ──
            # 先**非加锁**读取 run_id（仅用于确定加锁目标），
            # 再严格按全序加锁：先锁 Run，后锁 NodeRun；Project 由后续调用最内层加锁。
            probe = node_runs.get(conn, node_run_id, tenant_id)
            if probe is None:
                raise NotFoundError("node_run 不存在")
            run_row = runs.get_for_update(conn, probe["run_id"], tenant_id)
            if run_row is None:
                raise NotFoundError("run 不存在")
            # 在持锁状态下**重新读取**节点并校验其状态（不复用锁前快照）
            node_run = node_runs.get_for_update(conn, node_run_id, tenant_id)
            if node_run is None:  # pragma: no cover - 行不会在锁内消失
                raise NotFoundError("node_run 不存在")

            node_key = node_run["node_key"]
            kind = creative_revisions.ARTIFACT_KIND_MAP.get(node_key)
            if kind is None:
                raise ValidationError(
                    f"仅创作层节点支持定向修订：{node_key!r}",
                    details={"allowed": sorted(CREATIVE_NODE_KEYS)},
                )
            # ── 版本守卫（WB-03R2 §2）：必须以**目标 Run 内该节点**的当前产物为基准，
            #    不能用 project 级最新版本（多 Run 项目下后者会误判「过期」）。
            latest = creative_revisions.latest_for_run(
                conn, kind, run_row["project_id"], tenant_id,
                run_id=run_row["run_id"],
            )
            current = int(latest["revision_no"]) if latest else 0
            if int(expected_revision) != current:
                raise ConflictError(
                    "修订版本过期（expected_revision 与本 Run 当前版本不一致）",
                    details={"expected_revision": int(expected_revision),
                             "current_revision": current,
                             "run_id": run_row["run_id"],
                             "project_latest": self._project_latest_revision(
                                 conn, kind, run_row["project_id"], tenant_id)},
                )
            # 版本守卫通过后，才进行受控的 Run/Project 重开
            self._reopen_for_revision(
                conn, run_row, tenant_id, reason="revision_requested"
            )
            status = node_run["status"]
            if status == NodeRunStatus.SUCCEEDED.value:
                node_runs.set_status(
                    conn, node_run_id, tenant_id,
                    to_status=NodeRunStatus.INVALIDATED,
                    invalidated_reason="revision_requested",
                )
                node_runs.set_status(
                    conn, node_run_id, tenant_id, to_status=NodeRunStatus.READY,
                    clear_invalidated=True,
                )
            elif status in (NodeRunStatus.WAITING_APPROVAL.value,
                            NodeRunStatus.FAILED.value):
                node_runs.set_status(
                    conn, node_run_id, tenant_id, to_status=NodeRunStatus.READY,
                    clear_invalidated=True,
                )
            elif status == NodeRunStatus.READY.value:
                pass  # 已待执行，无需迁移
            else:
                raise ConflictError(
                    f"节点当前状态为 {status}，不可发起定向修订"
                )
            node_runs.set_input_revision_refs(conn, node_run_id, tenant_id, [])
            request = self._freeze_revision_request(
                conn, node_run["run_id"], tenant_id, node_key=node_key,
                feedback=feedback, new_prompt=new_prompt,
                expected_revision=current,
            )
            invalidated = self._invalidate_creative_stage(
                conn, node_run, tenant_id, reason="revision_requested"
            )
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="node_run.revision_requested",
                run_id=node_run["run_id"], node_run_id=node_run_id,
                payload={"node_key": node_key, "expected_revision": current,
                         "decided_by": decided_by,
                         "revision_index": request["revision_index"],
                         "invalidated": invalidated},
            )
            run_id = node_run["run_id"]
        self.advance_run(run_id, tenant_id)
        with self._db.transaction() as conn:
            return node_runs.get(conn, node_run_id, tenant_id)

    def revise_artifact(
        self,
        artifact_revision_id: str,
        tenant_id: str,
        *,
        expected_revision: int | None = None,
        feedback: str | None = None,
        new_prompt: str | None = None,
        decided_by: str = "system:revision",
    ) -> dict:
        """按**产物**发起定向修订（定位到产出它的节点后委托 `revise_node`）。

        `expected_revision` 缺省时取该产物的 `revision_no`。
        """
        with self._db.transaction() as conn:
            artifact = artifacts_repo.get(conn, artifact_revision_id, tenant_id)
            if artifact is None:
                raise NotFoundError("artifact_revision 不存在")
            node_run_id = artifact["node_run_id"]
            revision_no = int(artifact["revision_no"])
        return self.revise_node(
            node_run_id, tenant_id,
            expected_revision=(revision_no if expected_revision is None
                               else int(expected_revision)),
            feedback=feedback, new_prompt=new_prompt, decided_by=decided_by,
        )

    def _project_latest_revision(
        self, conn: Any, kind: str, project_id: str, tenant_id: str
    ) -> int:
        row = creative_revisions.latest(conn, kind, project_id, tenant_id)
        return int(row["revision_no"]) if row else 0

    def _creative_version_of_approval(
        self, conn: Any, approval: dict, tenant_id: str
    ) -> tuple[str, str] | None:
        """审批对应的**创作版本** `(kind, revision_id)`；非创作产物返回 `None`。"""
        revision_id = approval.get("artifact_revision_id")
        if not revision_id:
            return None
        artifact = artifacts_repo.get(conn, revision_id, tenant_id)
        if artifact is None:
            return None
        kind = creative_revisions.ARTIFACT_KIND_MAP.get(str(artifact["kind"]))
        if kind is None:
            return None
        return kind, str(revision_id)

    def _activate_creative_version(
        self, conn: Any, approval: dict, tenant_id: str
    ) -> dict | None:
        found = self._creative_version_of_approval(conn, approval, tenant_id)
        if found is None:
            return None
        kind, revision_id = found
        row = creative_revisions.activate(conn, kind, revision_id, tenant_id)
        return {"kind": kind, "revision_id": revision_id,
                "revision_no": int(row["revision_no"]), "status": row["status"]}

    def _reject_creative_version(
        self, conn: Any, approval: dict, tenant_id: str
    ) -> dict | None:
        found = self._creative_version_of_approval(conn, approval, tenant_id)
        if found is None:
            return None
        kind, revision_id = found
        row = creative_revisions.mark_rejected(conn, kind, revision_id, tenant_id)
        return {"kind": kind, "revision_id": revision_id,
                "revision_no": int(row["revision_no"]), "status": row["status"]}

    def _reopen_for_revision(
        self, conn: Any, run: dict, tenant_id: str, *, reason: str
    ) -> None:
        """把已完结的 Run / Project 重新打开（WB-03R §2 定向修订）。

        - Run `succeeded` -> `running`（并清空 `finished_at` 等终态元数据）；
        - Project `completed` -> `active`（并清空 `archived_at`）；
        通用状态迁移仍**拒绝**终态出边；本函数是唯一的受控例外。
        """
        if run["status"] == RunStatus.SUCCEEDED.value:
            # 受控专用路径：不走通用状态迁移（终态仍拒绝重开）
            runs.reopen_for_revision(conn, run["run_id"], tenant_id)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="run.reopened",
                project_id=run["project_id"], run_id=run["run_id"],
                payload={"reason": reason, "path": "revision"},
            )
        project = projects.get_for_update(conn, run["project_id"], tenant_id)
        if project is not None and project["status"] == ProjectStatus.COMPLETED.value:
            projects.reopen_for_revision(conn, run["project_id"], tenant_id)

    def _freeze_revision_request(
        self,
        conn: Any,
        run_id: str,
        tenant_id: str,
        *,
        node_key: str,
        feedback: str | None,
        new_prompt: str | None,
        expected_revision: int,
    ) -> dict:
        """把定向修订请求写入 `runs.input_json.revision_requests[node_key]`。

        已冻结的 `asset_ids` / `assets` / `creative_input` / `approval_policy`
        原样保留；仅在 `revision_requests` 子对象中追加/覆盖该节点的请求。
        """
        run = runs.get_for_update(conn, run_id, tenant_id)
        if run is None:
            raise NotFoundError("run 不存在")
        frozen = dict(run.get("input_json") or {})
        requests = dict(frozen.get("revision_requests") or {})
        previous = requests.get(node_key) or {}
        request = {
            "node_key": node_key,
            "feedback": feedback,
            "new_prompt": new_prompt,
            "expected_revision": int(expected_revision),
            "revision_index": int(previous.get("revision_index", 0)) + 1,
        }
        requests[node_key] = request
        frozen["revision_requests"] = requests
        runs.set_input_json(conn, run_id, tenant_id, frozen)
        return request

    def _invalidate_on_reject(
        self, conn: Any, gate: dict, tenant_id: str, *, reason: str
    ) -> list[str]:
        """**非创作层**闸门驳回时的连锁失效（保持 WB-01 既有语义）。

        重新打开上游依赖（`depends_on` 中已成功的节点）并级联失效其后代。
        创作层闸门**不**走此路径，见 `_invalidate_creative_stage`。
        """
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
                pending = approvals_repo.find_pending(
                    conn, row["node_run_id"], tenant_id
                )
                if pending is not None:
                    approvals_repo.decide(
                        conn, pending["approval_id"], tenant_id,
                        to_status=ApprovalStatus.CANCELLED,
                        decided_by="system:invalidation",
                        feedback=f"上游失效（{reason}）",
                    )
                invalidated.append(key)
        return invalidated

    def _invalidate_creative_stage(
        self, conn: Any, node_run: dict, tenant_id: str, *, reason: str
    ) -> list[str]:
        """失效该节点的**创作层下游**（仅创作链，绝不波及媒体层）。

        被失效节点同时清空 `input_revision_refs`，使其首次重新入队时
        冻结**新的**上游版本；未失效的节点不会重跑。
        """
        run_id = node_run["run_id"]
        invalidated: list[str] = []
        for key in creative_descendants(node_run["node_key"]):
            row = node_runs.get_by_key(conn, run_id, key, tenant_id)
            if row is None:
                continue
            status = row["status"]
            if status == NodeRunStatus.SUCCEEDED.value:
                node_runs.set_status(
                    conn, row["node_run_id"], tenant_id,
                    to_status=NodeRunStatus.INVALIDATED,
                    invalidated_reason=reason,
                )
            elif status == NodeRunStatus.WAITING_APPROVAL.value:
                node_runs.set_status(
                    conn, row["node_run_id"], tenant_id,
                    to_status=NodeRunStatus.CANCELLED,
                )
                pending = approvals_repo.find_pending(
                    conn, row["node_run_id"], tenant_id
                )
                if pending is not None:
                    approvals_repo.decide(
                        conn, pending["approval_id"], tenant_id,
                        to_status=ApprovalStatus.CANCELLED,
                        decided_by="system:invalidation",
                        feedback=f"上游失效（{reason}）",
                    )
            else:
                continue
            node_runs.set_input_revision_refs(
                conn, row["node_run_id"], tenant_id, []
            )
            invalidated.append(key)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="node_run.invalidated",
                run_id=run_id, node_run_id=row["node_run_id"],
                payload={"reason": reason, "node_key": key},
            )
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
