"""NodeAttempt 运行时：领取 → running + 独立心跳 → handler（写暂存）→ 发布 → 单事务提交。

WB-01 / R1 / R2 / R3 累计模型：
1. **独立周期心跳**（`executor/heartbeat.py`），与 `progress_cb` 解耦；
2. **心跳窗口**：覆盖 `handler.execute` → staged 路径校验 → sha256/大小计算；
   进入提交事务前**停止并可靠 join**（失败则中断有界 I/O 后二次 join；仍失败 → 进程级致命错误）；
3. **暂存区模型（R3）**：handler 只能写 `NodeContext.attempt_dir`；
   `ArtifactSpec.staged_relative_path` 必须位于该目录内；**执行器**为每个 ArtifactRevision
   预生成 id 并分配正式不可变路径 `artifacts/{artifact_revision_id}/{safe_filename}`；
4. **原子发布**：同文件系统 `os.replace` 移动 staged → 正式；绝不覆盖既有文件/目录；
5. **单事务提交**：产物登记 / attempt 终态 / node_run 终态 / Approval / Event 同事务；
   失败回滚并**仅删除本次新分配的 artifact 目录**；
6. **统一锁顺序**：`run → node_run → node_attempt → project`。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cybercut.contracts.errors import (
    ArtifactOwnershipError,
    ArtifactPublishError,
    ConflictError,
    HandlerNotFoundError,
    HeartbeatStuckError,
    LeaseConflictError,
    NodeExecutionError,
    PathEscapeError,
    ValidationError,
)
from cybercut.contracts.node_handler import NodeContext, NodeResult, handler_key
from cybercut.executor.claim import claim_next_attempt
from cybercut.executor.heartbeat import HeartbeatMonitor, compute_interval
from cybercut.storage import approvals as approvals_repo
from cybercut.storage import artifacts as artifacts_repo
from cybercut.storage import creative_revisions, node_attempts, node_runs, paths
from cybercut.storage import events as events_repo
from cybercut.storage import projects as projects_repo
from cybercut.storage import runs as runs_repo
from cybercut.storage._util import new_id
from cybercut.storage.node_attempts import DEFAULT_LEASE_SECONDS, ERROR_WORKER_LOST

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024

#: 租约在提交前丢失时的错误码
ERROR_LEASE_LOST = "LEASE_LOST"
#: 心跳专用连接的语句超时（毫秒）——保证单次 beat 有界
BEAT_STATEMENT_TIMEOUT_MS = 5000


def _error_code_of(exc: BaseException) -> str:
    """取领域异常的**具体**错误码（`NodeExecutionError` 用实例级 `error_code`）。"""
    specific = getattr(exc, "error_code", None)
    if isinstance(specific, str) and specific:
        return specific
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "ARTIFACT_PREPARE_FAILED"


def sha256_of_file(path: Path) -> tuple[str, int]:
    """流式计算文件的 sha256 与字节数。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_json_payload(path: Path, kind: str) -> dict:
    """读取创作类产物的结构化负载（发布前读 staged；发布后读正式文件）。

    负载必须为 JSON 对象；否则抛错使整个提交事务回滚（**绝不发布非法产物**）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactPublishError(
            f"创作产物负载不可解析：{kind}",
            details={"kind": kind, "path": str(path)},
        ) from exc
    if not isinstance(data, dict):
        raise ArtifactPublishError(
            f"创作产物负载必须是 JSON 对象：{kind}",
            details={"kind": kind},
        )
    return data


def _write_json_payload(path: Path, payload: dict) -> tuple[str, int]:
    """以**确定性**序列化写回 staged 文件，返回新的 (sha256, size_bytes)。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), len(
        text.encode("utf-8")
    )


@dataclass(frozen=True)
class AttemptOutcome:
    """一次 attempt 的执行结果（供上层聚合/断言）。"""

    attempt_id: str
    node_run_id: str
    run_id: str
    project_id: str
    tenant_id: str
    node_key: str
    status: str
    error_code: str | None = None
    awaiting_approval: bool = False


@dataclass(frozen=True)
class _PreparedArtifact:
    """事务前准备好的发布素材（staged 文件已校验、哈希已计算）。"""

    kind: str
    staged_abs: Path
    staged_relative_path: str
    filename: str
    sha256: str
    size_bytes: int
    metadata: dict


@dataclass
class _Allocation:
    """发布分配结果（执行器分配的 id + 正式路径）。"""

    item: _PreparedArtifact
    artifact_revision_id: str
    dest: Path
    relative_path: str
    published_dir: Path | None = field(default=None)
    #: WB-03R：创作类产物在发布前被改写的**权威** payload 与版本号
    creative_kind: str | None = None
    creative_payload: dict | None = None
    revision_no: int | None = None


class _DedicatedBeat:
    """心跳专用**持久连接**：每拍一个独立事务，不与 handler / progress 共享连接。

    - 连接带有限 `connect_timeout`（由 `Database` 注入）与 `statement_timeout`，
      保证单次 beat 有界；
    - `interrupt()` 在 `stop()` 超时时**中断有界 I/O**（PostgreSQL cancel 请求 +
      置中断标志），使心跳线程尽快退出；中断后不再重连。
    """

    def __init__(self, db: Any, attempt_id: str, tenant_id: str, worker_id: str) -> None:
        self._db = db
        self._attempt_id = attempt_id
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._conn: Any = None
        self._interrupted = False

    def _ensure_conn(self) -> Any:
        if self._conn is None or getattr(self._conn, "closed", 1):
            conn = self._db.connect()
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(BEAT_STATEMENT_TIMEOUT_MS)}")
            conn.commit()
            self._conn = conn
        return self._conn

    def _beat_once(self) -> bool:
        conn = self._ensure_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.node_attempts SET heartbeat_at = now(),"
                " updated_at = now()"
                " WHERE attempt_id = %s AND claimed_by = %s"
                "   AND status IN ('claimed','running')"
                " RETURNING attempt_id",
                (self._attempt_id, self._worker_id),
            )
            row = cur.fetchone()
        conn.commit()
        return row is not None

    def __call__(self) -> bool:
        if self._interrupted:
            return False
        try:
            return self._beat_once()
        except Exception:  # noqa: BLE001 - 连接可能已失效：重建后重试一次
            if self._interrupted:
                return False
            logger.debug("心跳连接异常，重建后重试 attempt=%s", self._attempt_id,
                         exc_info=True)
            self._drop_conn()
            try:
                return self._beat_once()
            except Exception:  # noqa: BLE001 - 重试仍失败 -> 视为续租失败
                logger.warning("心跳续租失败 attempt=%s", self._attempt_id,
                               exc_info=True)
                self._drop_conn()
                return False

    def interrupt(self) -> None:
        """中断有界的 beat I/O（不关闭连接，避免与仍在使用它的线程竞争）。"""
        self._interrupted = True
        conn = self._conn
        if conn is None:
            return
        try:
            conn.cancel()  # PostgreSQL 取消请求：让正在执行的 beat 语句立即返回
        except Exception:  # noqa: BLE001 - 取消失败不应抛给调用方
            logger.debug("心跳取消请求失败 attempt=%s", self._attempt_id, exc_info=True)

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        for op in (conn.rollback, conn.close):
            try:
                op()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """释放专用连接（**只允许在心跳线程确已退出后调用**）。"""
        self._drop_conn()


class AttemptRunner:
    """单次 attempt 执行器。"""

    def __init__(
        self,
        db: Any,
        registry: Any,
        *,
        data_root: Path | str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_interval: float | None = None,
        heartbeat_probe: Any = None,
        hash_hook: Any = None,
        heartbeat_stop_timeout: float | None = None,
        run_id: str | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._data_root = data_root
        self._run_id = run_id
        self.lease_seconds = int(lease_seconds)
        self.heartbeat_interval = compute_interval(self.lease_seconds, heartbeat_interval)
        #: 单次 `stop()`/join 的等待上限（超时后中断 I/O 再试；仍失败则致命）
        self.heartbeat_stop_timeout = (
            max(2.0, self.heartbeat_interval * 2.0)
            if heartbeat_stop_timeout is None
            else float(heartbeat_stop_timeout)
        )
        #: 测试注入：替代数据库心跳的可调用对象（返回 bool；可带 `interrupt()`）
        self._heartbeat_probe = heartbeat_probe
        #: 测试注入：哈希前后回调（拉长「校验 + 哈希」窗口）
        self._hash_hook = hash_hook
        #: 最近一次执行的心跳监视器（可观测性 / 测试断言）
        self.last_monitor: HeartbeatMonitor | None = None

    # ── 对外入口 ──

    def run_once(self, worker_id: str) -> AttemptOutcome | None:
        claimed = claim_next_attempt(self._db, worker_id, run_id=self._run_id)
        if claimed is None:
            return None
        return self.execute(claimed, worker_id)

    # ── 生命周期 ──

    def execute(self, attempt_row: dict, worker_id: str) -> AttemptOutcome:
        attempt_id = attempt_row["attempt_id"]
        tenant_id = attempt_row["tenant_id"]
        node_run = self._require_node_run(attempt_row["node_run_id"], tenant_id)
        node_key = node_run["node_key"]
        run_id = node_run["run_id"]
        project_id = self._project_id_of(run_id, tenant_id)

        self._start(attempt_row, worker_id, node_run)

        data_root = (
            Path(self._data_root) if self._data_root else paths.default_data_root()
        )
        project_root = paths.ensure_project_dir(project_id, data_root)
        attempt_workdir = paths.ensure_attempt_dir(project_root, attempt_id)

        context = NodeContext(
            attempt_id=attempt_id,
            node_run_id=node_run["node_run_id"],
            run_id=run_id,
            project_id=project_id,
            tenant_id=tenant_id,
            node_key=node_key,
            attempt_no=int(attempt_row["attempt_no"]),
            project_root=project_root,
            data_root=data_root,
            worker_id=worker_id,
            attempt_dir=attempt_workdir,
            progress_cb=lambda progress, stage, message: self._report_progress(
                attempt_id, progress, stage, message
            ),
            cancel_check=lambda: self._is_cancelled(attempt_id, tenant_id),
        )

        monitor, beat = self._start_heartbeat(attempt_row, worker_id, node_run)
        self.last_monitor = monitor

        result: NodeResult | None = None
        prepared: list[_PreparedArtifact] | None = None
        failure: tuple[str, str, dict | None] | None = None
        try:
            handler = self._registry.get(node_key)
            if handler is None:
                failure = (
                    HandlerNotFoundError.code,
                    f"未知 node_key={node_key!r}（未注册 NodeHandler）",
                    None,
                )
            else:
                try:
                    produced = handler.execute(
                        context, dict(attempt_row.get("input_json") or {})
                    )
                except NodeExecutionError as exc:
                    failure = (exc.error_code, exc.message, exc.details)
                except Exception as exc:  # noqa: BLE001 - handler 任意异常统一落 failed
                    logger.exception(
                        "handler 执行异常 node_key=%s attempt=%s", node_key, attempt_id
                    )
                    failure = (
                        "HANDLER_EXCEPTION",
                        f"{type(exc).__name__}: {exc}",
                        None,
                    )
                else:
                    if not isinstance(produced, NodeResult):
                        failure = (
                            "HANDLER_BAD_RESULT",
                            f"handler {handler_key(handler)!r} 返回类型非法："
                            f"{type(produced).__name__}",
                            None,
                        )
                    else:
                        result = produced

            # 心跳窗口：staged 路径校验 + 归属校验 + sha256/大小计算
            if failure is None and result is not None:
                try:
                    prepared = self._prepare_artifacts(result, attempt_workdir)
                except (
                    PathEscapeError,
                    ValidationError,
                    ArtifactOwnershipError,
                    NodeExecutionError,
                ) as exc:
                    failure = (
                        _error_code_of(exc),
                        str(exc),
                        getattr(exc, "details", None),
                    )
        finally:
            # 进入最终数据库提交事务**之前**：停止并**可靠**结束心跳
            self._reliably_stop_heartbeat(monitor, beat)

        try:
            if failure is not None:
                error_code, message, details = failure
                return self._fail(
                    attempt_row, node_run, node_key, project_id,
                    error_code=error_code, message=message, details=details,
                )
            if monitor.lease_lost:
                return self._fail(
                    attempt_row, node_run, node_key, project_id,
                    error_code=ERROR_LEASE_LOST,
                    message="租约丢失（周期心跳失败），拒绝最终提交",
                )
            return self._succeed(
                attempt_row, node_run, node_key, project_id, run_id, project_root,
                prepared or [], worker_id,
                awaiting_approval=bool(result.awaiting_approval) if result else False,
            )
        finally:
            # 最后统一清理 attempt 独占暂存目录（正式产物已发布到正式区）
            self._cleanup_attempt_dir(attempt_workdir)

    # ── 心跳 ──

    def _start_heartbeat(
        self, attempt_row: dict, worker_id: str, node_run: dict
    ) -> tuple[HeartbeatMonitor, Any]:
        attempt_id = attempt_row["attempt_id"]
        tenant_id = attempt_row["tenant_id"]
        node_run_id = node_run["node_run_id"]
        run_id = node_run["run_id"]

        if self._heartbeat_probe is not None:
            beat: Any = self._heartbeat_probe
        else:
            beat = _DedicatedBeat(self._db, attempt_id, tenant_id, worker_id)

        def on_lease_lost() -> None:
            try:
                with self._db.transaction() as conn:
                    events_repo.append(
                        conn, tenant_id=tenant_id, event_type="attempt.lease_lost",
                        run_id=run_id, node_run_id=node_run_id,
                        payload={"attempt_id": attempt_id, "worker_id": worker_id},
                    )
            except Exception:  # noqa: BLE001 - 审计失败不影响主流程
                logger.warning("lease_lost 事件写入失败 attempt=%s", attempt_id,
                               exc_info=True)

        monitor = HeartbeatMonitor(
            beat, self.heartbeat_interval, on_lease_lost=on_lease_lost
        )
        monitor.start()
        return monitor, beat

    def _reliably_stop_heartbeat(self, monitor: HeartbeatMonitor, beat: Any) -> None:
        """停止并**确认**心跳线程退出；否则中断有界 I/O 再试；仍失败 → 进程级致命错误。

        - 绝不在心跳线程仍可能使用资源时 `close()`；
        - 真正无法停止时抛 `HeartbeatStuckError`（worker 必须退出，不得继续领取任务）。
        """
        if monitor.stop(timeout=self.heartbeat_stop_timeout):
            self._safe_close(beat)
            return

        logger.error("心跳首次 join 失败，尝试中断有界 I/O 后重试")
        self._safe_interrupt(beat)
        if monitor.stop(timeout=self.heartbeat_stop_timeout):
            self._safe_close(beat)
            return

        raise HeartbeatStuckError(
            "心跳线程无法停止（中断后仍未退出）；保留其占用资源，worker 不可继续领取任务",
            details={"beats": monitor.beats, "interval": monitor.interval},
        )

    @staticmethod
    def _safe_interrupt(beat: Any) -> None:
        interrupt = getattr(beat, "interrupt", None)
        if interrupt is None:
            return
        try:
            interrupt()
        except Exception:  # noqa: BLE001 - 中断失败不应掩盖原始问题
            logger.warning("心跳中断失败", exc_info=True)

    @staticmethod
    def _safe_close(beat: Any) -> None:
        close = getattr(beat, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.warning("心跳资源释放失败", exc_info=True)

    # ── 内部步骤 ──

    def _require_node_run(self, node_run_id: str, tenant_id: str) -> dict:
        with self._db.transaction() as conn:
            row = node_runs.get(conn, node_run_id, tenant_id)
        if row is None:
            raise NodeExecutionError(
                f"node_run 不存在: {node_run_id}", error_code="NODE_RUN_MISSING"
            )
        return row

    def _project_id_of(self, run_id: str, tenant_id: str) -> str:
        with self._db.transaction() as conn:
            run = runs_repo.get(conn, run_id, tenant_id)
        if run is None:
            raise NodeExecutionError(f"run 不存在: {run_id}", error_code="RUN_MISSING")
        return run["project_id"]

    def _start(self, attempt_row: dict, worker_id: str, node_run: dict) -> None:
        """claimed -> running，并把 run / node_run 提升为 running。

        锁顺序：**run -> node_run -> node_attempt**。
        """
        attempt_id = attempt_row["attempt_id"]
        tenant_id = attempt_row["tenant_id"]
        node_run_id = node_run["node_run_id"]
        run_id = node_run["run_id"]
        with self._db.transaction() as conn:
            run = runs_repo.get_for_update(conn, run_id, tenant_id)
            if run is not None and run["status"] == "queued":
                runs_repo.update_status(
                    conn, run_id, tenant_id, to_status="running", mark_started=True
                )
            current = node_runs.get_for_update(conn, node_run_id, tenant_id)
            if current is not None and current["status"] == "ready":
                node_runs.set_status(conn, node_run_id, tenant_id, to_status="running")
            node_attempts.mark_running(conn, attempt_id, tenant_id, worker_id)
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="attempt.started", run_id=run_id,
                node_run_id=node_run_id,
                payload={"attempt_id": attempt_id, "worker_id": worker_id,
                         "node_key": node_run["node_key"]},
            )

    def _report_progress(
        self, attempt_id: str, progress: float, stage: str, message: str
    ) -> None:
        try:
            with self._db.transaction() as conn:
                node_attempts.update_progress(
                    conn, attempt_id, progress=progress, stage=stage, message=message
                )
        except Exception:  # noqa: BLE001 - 进度上报失败不得影响执行
            logger.debug("进度上报失败 attempt=%s", attempt_id, exc_info=True)

    def _is_cancelled(self, attempt_id: str, tenant_id: str) -> bool:
        try:
            with self._db.transaction() as conn:
                row = node_attempts.get(conn, attempt_id, tenant_id)
        except Exception:  # noqa: BLE001
            return False
        return row is not None and row["status"] == "cancelled"

    def _cleanup_attempt_dir(self, attempt_workdir: Path) -> None:
        """删除 attempt 独占暂存目录（完全属于本 attempt）。"""
        try:
            shutil.rmtree(attempt_workdir, ignore_errors=True)
        except Exception:  # noqa: BLE001 - 尽力而为
            logger.debug("attempt 暂存目录清理失败：%s", attempt_workdir, exc_info=True)

    # ── 准备（事务外，受心跳覆盖）──

    def _prepare_artifacts(
        self, result: NodeResult, attempt_workdir: Path
    ) -> list[_PreparedArtifact]:
        """校验 staged 路径归属并计算哈希/大小（正式路径由执行器在提交阶段分配）。"""
        prepared: list[_PreparedArtifact] = []
        for spec in result.artifacts:
            try:
                staged_abs = paths.resolve_staged_path(
                    attempt_workdir, spec.staged_relative_path
                )
            except (PathEscapeError, ValidationError) as exc:
                raise ArtifactOwnershipError(
                    "staged 路径必须位于当前 attempt 的独占暂存目录内",
                    details={"staged_relative_path": spec.staged_relative_path,
                             "attempt_dir": str(attempt_workdir)},
                ) from exc
            if not staged_abs.is_file():
                raise NodeExecutionError(
                    f"staged 文件不存在：{spec.staged_relative_path}",
                    error_code="ARTIFACT_MISSING",
                )
            if self._hash_hook is not None:
                self._hash_hook(spec.staged_relative_path)
            sha256, size = sha256_of_file(staged_abs)
            if self._hash_hook is not None:
                self._hash_hook(spec.staged_relative_path)
            prepared.append(
                _PreparedArtifact(
                    kind=spec.kind,
                    staged_abs=staged_abs,
                    staged_relative_path=spec.staged_relative_path,
                    filename=paths.sanitize_filename(spec.staged_relative_path),
                    sha256=sha256,
                    size_bytes=size,
                    metadata=dict(spec.metadata),
                )
            )
        return prepared

    # ── 发布 + 成功提交（单事务；锁序 run -> node_run -> node_attempt -> project）──

    def _succeed(
        self,
        attempt_row: dict,
        node_run: dict,
        node_key: str,
        project_id: str,
        run_id: str,
        project_root: Path,
        prepared: list[_PreparedArtifact],
        worker_id: str,
        *,
        awaiting_approval: bool,
    ) -> AttemptOutcome:
        attempt_id = attempt_row["attempt_id"]
        tenant_id = attempt_row["tenant_id"]
        node_run_id = node_run["node_run_id"]
        published: list[Path] = []

        try:
            with self._db.transaction() as conn:
                # ① 锁序：run -> node_run -> node_attempt（project 由登记内部最后加锁）
                runs_repo.get_for_update(conn, run_id, tenant_id)
                node_runs.get_for_update(conn, node_run_id, tenant_id)
                node_attempts.require_running_owned(
                    conn, attempt_id, tenant_id, worker_id
                )

                # ② 分配 ArtifactRevision id 与正式不可变路径（执行器决定）
                allocations = self._allocate(project_root, prepared)

                # ②' WB-03R：用**数据库分配**的权威身份改写创作产物 payload
                #     （发布前、同事务；project 为锁序最内层）
                self._finalize_creative_payloads(
                    conn, allocations, node_run, project_id, tenant_id
                )

                # ③ 原子发布：同文件系统 os.replace（不覆盖任何既有文件/目录）
                for alloc in allocations:
                    dest_dir = alloc.dest.parent
                    if dest_dir.exists() or alloc.dest.exists():
                        raise ArtifactPublishError(
                            "正式产物路径已存在，拒绝覆盖",
                            details={"target": str(alloc.dest)},
                        )
                    dest_dir.mkdir(parents=True, exist_ok=False)
                    published.append(dest_dir)
                    alloc.published_dir = dest_dir
                    os.replace(alloc.item.staged_abs, alloc.dest)

                # ④ 登记产物（内部对 project 行加锁：锁序最内层）
                registered = self._register_artifacts(
                    conn, node_run, attempt_row, allocations, project_id, tenant_id,
                    awaiting_approval=awaiting_approval,
                )

                # ⑤ 同事务写入终态
                output: dict[str, Any] = {
                    "artifact_revision_ids": [
                        a["artifact_revision_id"] for a in registered
                    ],
                }
                node_attempts.finish(
                    conn, attempt_id, tenant_id, to_status="succeeded",
                    output_json=output,
                )
                current = node_runs.get(conn, node_run_id, tenant_id)
                if current is not None and current["status"] == "running":
                    target = "waiting_approval" if awaiting_approval else "succeeded"
                    node_runs.set_status(conn, node_run_id, tenant_id, to_status=target)
                    if awaiting_approval:
                        self._create_approval(
                            conn, node_run, attempt_row, registered, project_id,
                            tenant_id,
                        )
                events_repo.append(
                    conn, tenant_id=tenant_id, event_type="attempt.succeeded",
                    project_id=project_id, run_id=run_id, node_run_id=node_run_id,
                    payload={"attempt_id": attempt_id, "node_key": node_key,
                             "artifact_revisions": len(registered),
                             "awaiting_approval": awaiting_approval},
                )
        except (ConflictError, LeaseConflictError) as exc:
            self._cleanup_published(published)
            return self._fail(
                attempt_row, node_run, node_key, project_id,
                error_code=getattr(exc, "code", "COMMIT_REJECTED"),
                message=f"最终提交被拒绝：{exc}", details=getattr(exc, "details", None),
            )
        except Exception:
            self._cleanup_published(published)
            logger.exception("成功提交事务失败（已回滚）attempt=%s", attempt_id)
            return self._fail(
                attempt_row, node_run, node_key, project_id,
                error_code="COMMIT_FAILED", message="成功提交事务失败（已回滚）",
            )

        return AttemptOutcome(
            attempt_id=attempt_id, node_run_id=node_run_id, run_id=run_id,
            project_id=project_id, tenant_id=tenant_id, node_key=node_key,
            status="succeeded", awaiting_approval=awaiting_approval,
        )

    @staticmethod
    def _allocate(
        project_root: Path, prepared: list[_PreparedArtifact]
    ) -> list[_Allocation]:
        """为每个产物预生成 id 并分配正式不可变路径（同名 staged -> 不同正式路径）。"""
        allocations: list[_Allocation] = []
        for item in prepared:
            revision_id = new_id()
            dest = paths.artifact_publish_path(project_root, revision_id, item.filename)
            allocations.append(
                _Allocation(
                    item=item,
                    artifact_revision_id=revision_id,
                    dest=dest,
                    relative_path=paths.relative_to_project(project_root, dest),
                )
            )
        return allocations

    def _cleanup_published(self, published: list[Path]) -> None:
        """仅删除**本次新分配**的 artifact 目录；绝不触碰其它既有文件/目录。"""
        for directory in published:
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception:  # noqa: BLE001
                logger.warning("本次发布目录清理失败：%s", directory, exc_info=True)

    def _finalize_creative_payloads(
        self,
        conn: Any,
        allocations: list[_Allocation],
        node_run: dict,
        project_id: str,
        tenant_id: str,
    ) -> None:
        """把数据库分配的权威身份写进创作产物 payload（WB-03R，发布前）。

        权威身份 = 该产物 ArtifactRevision 的 `artifact_revision_id` 与 `revision_no`，
        因此「产物文件 / ArtifactRevision / 创作版本表 / payload」四者一一对应。
        改写后 staged 文件被**确定性重写**并重算哈希，保证登记哈希与磁盘一致。
        """
        creative = [a for a in allocations
                    if a.item.kind in creative_revisions.ARTIFACT_KIND_MAP]
        if not creative:
            return
        # 锁序唯一全序：run -> node_run -> node_attempt -> project（project 最内层）
        projects_repo.lock(conn, project_id, tenant_id)
        used: dict[str, int] = {}
        for alloc in creative:
            kind = creative_revisions.ARTIFACT_KIND_MAP[alloc.item.kind]
            if alloc.item.kind not in used:
                used[alloc.item.kind] = artifacts_repo.next_revision_no(
                    conn, project_id, tenant_id, alloc.item.kind
                )
            revision_no = used[alloc.item.kind]
            used[alloc.item.kind] = revision_no + 1

            payload = _read_json_payload(alloc.item.staged_abs, alloc.item.kind)
            finalized = creative_revisions.finalize_payload(
                kind, payload,
                revision_id=alloc.artifact_revision_id,
                revision_no=revision_no,
                project_id=project_id,
                run_id=node_run["run_id"],
            )
            creative_revisions.validate_payload(kind, finalized)
            sha, size = _write_json_payload(alloc.item.staged_abs, finalized)

            alloc.creative_kind = kind
            alloc.creative_payload = finalized
            alloc.revision_no = revision_no
            alloc.item = replace(alloc.item, sha256=sha, size_bytes=size)

    def _register_artifacts(
        self,
        conn: Any,
        node_run: dict,
        attempt_row: dict,
        allocations: list[_Allocation],
        project_id: str,
        tenant_id: str,
        *,
        awaiting_approval: bool = False,
    ) -> list[dict]:
        registered: list[dict] = []
        for alloc in allocations:
            row = artifacts_repo.register(
                conn,
                project_id=project_id,
                run_id=node_run["run_id"],
                node_run_id=node_run["node_run_id"],
                attempt_id=attempt_row["attempt_id"],
                tenant_id=tenant_id,
                kind=alloc.item.kind,
                relative_path=alloc.relative_path,
                sha256=alloc.item.sha256,
                size_bytes=alloc.item.size_bytes,
                metadata=alloc.item.metadata,
                artifact_revision_id=alloc.artifact_revision_id,
                revision_no=alloc.revision_no,
            )
            registered.append(row)
            # WB-03R：创作类产物在同一事务内登记为**版本化**创作产物，
            # 且强制 payload 身份 == ArtifactRevision 身份
            # （非创作 kind 为空操作；任一不一致则整体回滚）
            # WB-03R2：待审批的版本登记为 `draft`（**不得**标记 active），
            # 审批通过后再激活并取代旧 active 版本。
            creative_revisions.persist_for_artifact(
                conn, artifact_row=row, payload=alloc.creative_payload,
                revision_id=alloc.artifact_revision_id,
                revision_no=alloc.revision_no,
                status=(creative_revisions.STATUS_DRAFT if awaiting_approval
                        else creative_revisions.STATUS_ACTIVE),
            )
        return registered

    def _create_approval(
        self,
        conn: Any,
        node_run: dict,
        attempt_row: dict,
        registered: list[dict],
        project_id: str,
        tenant_id: str,
    ) -> None:
        node_run_id = node_run["node_run_id"]
        if approvals_repo.find_pending(conn, node_run_id, tenant_id) is not None:
            return
        expected = 1
        artifact_id = None
        if registered:
            artifact_id = registered[0]["artifact_revision_id"]
            expected = int(registered[0]["revision_no"])
        approvals_repo.create_pending(
            conn,
            project_id=project_id,
            run_id=node_run["run_id"],
            node_run_id=node_run_id,
            tenant_id=tenant_id,
            expected_revision=expected,
            artifact_revision_id=artifact_id,
            scope={"attempt_id": attempt_row["attempt_id"],
                   "node_key": node_run["node_key"]},
        )

    # ── 失败提交（锁序 run -> node_run -> node_attempt）──

    def _fail(
        self,
        attempt_row: dict,
        node_run: dict,
        node_key: str,
        project_id: str,
        *,
        error_code: str,
        message: str,
        details: dict | None = None,
    ) -> AttemptOutcome:
        attempt_id = attempt_row["attempt_id"]
        tenant_id = attempt_row["tenant_id"]
        node_run_id = node_run["node_run_id"]
        with self._db.transaction() as conn:
            runs_repo.get_for_update(conn, node_run["run_id"], tenant_id)
            nr = node_runs.get_for_update(conn, node_run_id, tenant_id)
            if nr is not None and nr["status"] == "running":
                node_runs.set_status(conn, node_run_id, tenant_id, to_status="failed")
            current = node_attempts.get_for_update(conn, attempt_id, tenant_id)
            if current is not None and current["status"] in ("claimed", "running"):
                node_attempts.finish(
                    conn, attempt_id, tenant_id, to_status="failed",
                    error_code=error_code, error_message=message,
                    error_details=details,
                )
            events_repo.append(
                conn, tenant_id=tenant_id, event_type="attempt.failed",
                project_id=project_id, run_id=node_run["run_id"],
                node_run_id=node_run_id,
                payload={"attempt_id": attempt_id, "node_key": node_key,
                         "error_code": error_code, "message": message},
            )
        return AttemptOutcome(
            attempt_id=attempt_id, node_run_id=node_run_id, run_id=node_run["run_id"],
            project_id=project_id, tenant_id=tenant_id, node_key=node_key,
            status="failed", error_code=error_code,
        )


__all__ = [
    "BEAT_STATEMENT_TIMEOUT_MS",
    "ERROR_LEASE_LOST",
    "ERROR_WORKER_LOST",
    "AttemptOutcome",
    "AttemptRunner",
    "sha256_of_file",
]
