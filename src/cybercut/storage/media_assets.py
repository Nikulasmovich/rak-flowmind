"""media_assets 仓储（WB-02 §四）。

状态机：`ingesting -> ready | failed`（ingesting 超时视为崩溃残留，可被接管）。
"""
from __future__ import annotations

import json
from typing import Any

from cybercut.contracts.errors import ConflictError, NotFoundError
from cybercut.storage._util import new_id, require_row

_COLS = """
    asset_id, project_id, tenant_id, media_kind, original_filename,
    source_relative_path, sha256, size_bytes, status, error_code, error_message,
    created_at, updated_at
"""

READY = "ready"
INGESTING = "ingesting"
FAILED = "failed"


def get(conn: Any, asset_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.media_assets"
            " WHERE asset_id = %s AND tenant_id = %s",
            (asset_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def require(conn: Any, asset_id: str, tenant_id: str) -> dict:
    return require_row(get(conn, asset_id, tenant_id), what="media_asset")


def get_by_sha(
    conn: Any, project_id: str, tenant_id: str, sha256: str
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.media_assets"
            " WHERE project_id = %s AND tenant_id = %s AND sha256 = %s",
            (project_id, tenant_id, sha256),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_by_ids(
    conn: Any, project_id: str, tenant_id: str, asset_ids: list[str]
) -> list[dict]:
    """按显式 id 列表读取素材；任何一个不存在/不属于该项目都抛 NotFound。"""
    if not asset_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.media_assets"
            " WHERE project_id = %s AND tenant_id = %s AND asset_id = ANY(%s)"
            " ORDER BY created_at",
            (project_id, tenant_id, list(asset_ids)),
        )
        found = {r["asset_id"]: dict(r) for r in cur.fetchall()}
    missing = [a for a in asset_ids if a not in found]
    if missing:
        raise NotFoundError(
            "素材不存在或不属于该项目",
            details={"missing_asset_ids": missing},
        )
    return [found[a] for a in asset_ids]


def insert_ingesting(
    conn: Any,
    *,
    project_id: str,
    tenant_id: str,
    media_kind: str,
    original_filename: str,
    source_relative_path: str,
    sha256: str,
    size_bytes: int,
    asset_id: str | None = None,
) -> dict | None:
    """登记 `ingesting` 行；同项目同 sha256 冲突时返回 None（去重锚点）。

    并发安全：`ON CONFLICT (project_id, sha256) DO NOTHING`。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.media_assets (asset_id, project_id, tenant_id,"
            " media_kind, original_filename, source_relative_path, sha256,"
            " size_bytes, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ingesting')"
            " ON CONFLICT (project_id, sha256) DO NOTHING"
            f" RETURNING {_COLS}",
            (asset_id or new_id(), project_id, tenant_id, media_kind,
             original_filename, source_relative_path, sha256, size_bytes),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def lock_row(conn: Any, asset_id: str, tenant_id: str) -> dict | None:
    """`FOR UPDATE` 读取该行（**不限制状态**），用于发布失败后的归属确认。

    WB-02R6：清理路径必须能区分「行已 ready（他人已成功发布）」与
    「行仍为 ingesting/failed（本次失败事务残留）」。带状态过滤的
    `lock_for_takeover` 无法表达 ready 分支，故单独提供本函数。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.media_assets"
            " WHERE asset_id = %s AND tenant_id = %s"
            " FOR UPDATE",
            (asset_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def lock_for_takeover(
    conn: Any, asset_id: str, tenant_id: str, *,
    allowed_statuses: tuple[str, ...], stale_after_s: float | None = None,
) -> dict | None:
    """WB-02R §五.1：接管所有权 —— `FOR UPDATE` 行锁 + 状态 CAS。

    `stale_after_s` 仅对 `ingesting` 生效（要求 updated_at 早于该阈值）。
    """
    stale_clause = ""
    params: list = [asset_id, tenant_id]
    if stale_after_s is not None:
        stale_clause = " AND updated_at <= now() - make_interval(secs => %s)"
        params.append(float(stale_after_s))
    statuses = ",".join(f"'{s}'" for s in allowed_statuses)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.media_assets"
            f" WHERE asset_id = %s AND tenant_id = %s"
            f"   AND status IN ({statuses}){stale_clause}"
            " FOR UPDATE",
            tuple(params),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# 注意（WB-02R5 §一）：此处**刻意不提供** `lock_ready()`。
# 「正式文件已存在」的恢复路径必须通过 `lock_for_takeover()` 锁定
# ingesting/failed 行，只匹配 ready 的行锁会让接管者绕过所有权校验。


def mark_ready(
    conn: Any, asset_id: str, tenant_id: str, *, source_relative_path: str
) -> dict:
    """发布原始文件成功后置 `ready`（乐观锁语义：仅从 ingesting/failed 转移）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.media_assets"
            " SET status = 'ready', source_relative_path = %s, error_code = NULL,"
            " error_message = NULL, updated_at = now()"
            " WHERE asset_id = %s AND tenant_id = %s"
            "   AND status IN ('ingesting','failed')"
            f" RETURNING {_COLS}",
            (source_relative_path, asset_id, tenant_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ConflictError(
            f"素材 {asset_id} 无法置为 ready（状态已变化）",
            details={"asset_id": asset_id},
        )
    return dict(row)


def mark_failed(
    conn: Any, asset_id: str, tenant_id: str, *, error_code: str, message: str
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.media_assets"
            " SET status = 'failed', error_code = %s, error_message = %s,"
            " updated_at = now()"
            " WHERE asset_id = %s AND tenant_id = %s AND status IN ('ingesting','ready')"
            f" RETURNING {_COLS}",
            (error_code, message[:1000], asset_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def set_source_path(
    conn: Any, asset_id: str, tenant_id: str, *, source_relative_path: str
) -> None:
    """接管/重试时更新正式路径（仍要求行处于 ingesting/failed）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.media_assets SET source_relative_path = %s,"
            " updated_at = now()"
            " WHERE asset_id = %s AND tenant_id = %s"
            "   AND status IN ('ingesting','failed')",
            (source_relative_path, asset_id, tenant_id),
        )


def export_json(asset: dict) -> dict[str, Any]:
    """素材的稳定 JSON 表示（用于 input_json / 报告引用）。"""
    return {
        "asset_id": asset["asset_id"],
        "media_kind": asset["media_kind"],
        "original_filename": asset["original_filename"],
        "source_relative_path": asset["source_relative_path"],
        "sha256": asset["sha256"],
        "size_bytes": asset["size_bytes"],
        "status": asset["status"],
    }


def to_jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = [
    "FAILED",
    "INGESTING",
    "READY",
    "export_json",
    "get",
    "get_by_sha",
    "insert_ingesting",
    "list_by_ids",
    "lock_for_takeover",
    "lock_row",
    "mark_failed",
    "mark_ready",
    "require",
    "set_source_path",
]
