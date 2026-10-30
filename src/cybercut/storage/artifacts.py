"""`cybercut.artifact_revisions` 仓储（不可变登记）。

不可变性：
- 表上有 `BEFORE UPDATE OR DELETE` 触发器（`cybercut.forbid_mutation`），数据库层面拒绝改写；
- 应用层亦不提供任何 update/delete 接口；
- 版本递进用**新行**表达（`revision_no` 递增 + `supersedes_id` 指向旧行），
  旧行永久保留（可审计、可对比）。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import ArtifactStatus
from cybercut.contracts.errors import ArtifactImmutableError
from cybercut.storage import projects
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "artifact_revision_id, project_id, run_id, node_run_id, attempt_id, tenant_id,"
    " kind, revision_no, relative_path, sha256, size_bytes, metadata_json,"
    " supersedes_id, status, created_at, updated_at"
)


def next_revision_no(conn: Any, project_id: str, tenant_id: str, kind: str) -> int:
    """项目内某 kind 的下一个版本号（单调递增）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(revision_no), 0) + 1 AS n FROM cybercut.artifact_revisions"
            " WHERE project_id = %s AND tenant_id = %s AND kind = %s",
            (project_id, tenant_id, kind),
        )
        return int(cur.fetchone()["n"])


def latest_for_kind(
    conn: Any, project_id: str, tenant_id: str, kind: str
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.artifact_revisions"
            " WHERE project_id = %s AND tenant_id = %s AND kind = %s"
            " ORDER BY revision_no DESC LIMIT 1",
            (project_id, tenant_id, kind),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get(conn: Any, artifact_revision_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.artifact_revisions"
            " WHERE artifact_revision_id = %s AND tenant_id = %s",
            (artifact_revision_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_node_run(conn: Any, node_run_id: str, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.artifact_revisions"
            " WHERE node_run_id = %s AND tenant_id = %s ORDER BY kind, revision_no",
            (node_run_id, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def register(
    conn: Any,
    *,
    project_id: str,
    run_id: str,
    node_run_id: str,
    attempt_id: str,
    tenant_id: str,
    kind: str,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    metadata: dict | None = None,
    supersedes_id: Any = UNSET,
    revision_no: int | None = None,
    artifact_revision_id: str | None = None,
) -> dict:
    """登记一条产物版本（哈希与大小由调用方在登记时计算后传入）。

    - `revision_no` 缺省时自动取 `(project, kind)` 的下一个版本号；
    - `supersedes_id` 缺省（`UNSET`）时自动指向同 `(project, kind)` 的上一版本
      （显式传 `None` 表示不建立版本链）；
    - 重复 `(project_id, kind, revision_no)` -> `ArtifactImmutableError`（不可覆盖）。
    """
    arid = artifact_revision_id or new_id()
    if revision_no is None:
        # WB-01R3：串行化同 (project, kind) 的版本号分配 ——
        # 先锁 Project 行。锁序中 Project 是**最内层**（唯一全序：
        # run → node_run → node_attempt → project），调用方须已按序持有前三级；
        # 若调用方已持锁，则本操作为同事务内空操作。
        # 再计算 max(revision_no)+1，避免并发 max()+1 竞争。
        projects.lock(conn, project_id, tenant_id)
        rno = next_revision_no(conn, project_id, tenant_id, kind)
    else:
        rno = revision_no
    if supersedes_id is UNSET:
        previous = latest_for_kind(conn, project_id, tenant_id, kind)
        supersedes_id = previous["artifact_revision_id"] if previous else None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cybercut.artifact_revisions (artifact_revision_id, project_id,"
                " run_id, node_run_id, attempt_id, tenant_id, kind, revision_no,"
                " relative_path, sha256, size_bytes, metadata_json, supersedes_id, status)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_COLS}",
                (
                    arid, project_id, run_id, node_run_id, attempt_id, tenant_id, kind,
                    int(rno), relative_path, sha256, int(size_bytes),
                    Json(metadata or {}), supersedes_id, str(ArtifactStatus.ACTIVE),
                ),
            )
            return dict(cur.fetchone())
    except Exception as exc:  # noqa: BLE001
        # 唯一键冲突统一映射为「不可覆盖」
        if exc.__class__.__name__ == "UniqueViolation":
            raise ArtifactImmutableError(
                f"产物版本已存在且不可覆盖：project={project_id} kind={kind}"
                f" revision_no={rno}",
                details={"project_id": project_id, "kind": kind, "revision_no": int(rno)},
            ) from exc
        raise


def require(conn: Any, artifact_revision_id: str, tenant_id: str) -> dict:
    return require_row(get(conn, artifact_revision_id, tenant_id), what="artifact_revision")


def referenced_paths(
    conn: Any, project_id: str, tenant_id: str, relative_paths: Any
) -> set[str]:
    """返回给定 `relative_paths` 中**已被 ArtifactRevision 引用**的子集。

    WB-01R2 §一.2/§一.5：删除前与提交前都必须查询该集合，
    以保证历史正式产物不被覆盖或删除。
    """
    rels = sorted({str(p) for p in relative_paths if p})
    if not rels:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT relative_path FROM cybercut.artifact_revisions"
            " WHERE project_id = %s AND tenant_id = %s AND relative_path = ANY(%s)",
            (project_id, tenant_id, rels),
        )
        return {str(r["relative_path"]) for r in cur.fetchall()}


def refs_for_node_runs(
    conn: Any, project_id: str, tenant_id: str, node_run_ids: list[str]
) -> list[dict]:
    """每个 (node_run_id, kind) 的**最新** ArtifactRevision 引用（WB-02 §三.5）。

    返回按 kind 稳定排序的列表，用于冻结进下游 `node_runs.input_revision_refs`
    与 `node_attempts.input_json.input_artifacts`；重试时直接复用冻结结果，
    **不会漂移到新版本**。
    """
    if not node_run_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (node_run_id, kind)"
            " node_run_id, kind, artifact_revision_id, relative_path, sha256,"
            " size_bytes, revision_no"
            "  FROM cybercut.artifact_revisions"
            " WHERE project_id = %s AND tenant_id = %s AND node_run_id = ANY(%s)"
            " ORDER BY node_run_id, kind, revision_no DESC",
            (project_id, tenant_id, list(node_run_ids)),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return [
        {
            "artifact_revision_id": r["artifact_revision_id"],
            "kind": r["kind"],
            "relative_path": r["relative_path"],
            "sha256": r["sha256"],
            "size_bytes": r["size_bytes"],
            "node_run_id": r["node_run_id"],
        }
        for r in sorted(rows, key=lambda r: (r["kind"], r["node_run_id"]))
    ]


__all__ = [
    "get",
    "latest_for_kind",
    "list_for_node_run",
    "next_revision_no",
    "referenced_paths",
    "refs_for_node_runs",
    "register",
    "require",
]
