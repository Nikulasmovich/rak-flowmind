"""`cybercut.idempotency_keys` 仓储（幂等键）。

语义：
- 主键 `(tenant_id, operation, idempotency_key)`；
- 同键**同** `request_hash` -> 重放：返回既有 `response_json` + `resource_id`；
- 同键**不同** `request_hash` -> `IdempotencyConflictError`（同键不可用于不同请求）。

两段式用法::

    with db.transaction() as conn:
        replay = idem.check_or_reserve(conn, tenant_id, op, key, request_hash)
        if replay is not None:
            return replay["response_json"], replay["resource_id"]
        ... 执行副作用 ...
        idem.complete(conn, tenant_id, op, key, response_json=..., resource_id=...)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.errors import ConflictError, IdempotencyConflictError

_COLS = (
    "tenant_id, operation, idempotency_key, request_hash, response_json,"
    " resource_id, created_at, expires_at"
)


def request_hash(payload: Any) -> str:
    """稳定计算请求指纹（排序键，UTF-8，SHA-256）。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(conn: Any, tenant_id: str, operation: str, key: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.idempotency_keys"
            " WHERE tenant_id = %s AND operation = %s AND idempotency_key = %s",
            (tenant_id, operation, key),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def check_or_reserve(
    conn: Any, tenant_id: str, operation: str, key: str, req_hash: str
) -> dict | None:
    """**原子**占位：返回既有记录（重放）或 None（本次新占位）。

    实现（WB-01R 修正：消除「先 SELECT 再普通 INSERT」的竞争窗口）：
    1. `INSERT ... ON CONFLICT (PK) DO NOTHING RETURNING ...`
       —— 并发同键请求由唯一索引在数据库层串行化；若对方事务尚未提交，
       本语句会**阻塞等待**其提交/回滚，因此不存在「双方都读到不存在」的窗口；
    2. 写入成功 -> 本次为新占位，返回 None；
    3. 未写入（冲突）-> 读取既有记录：
       - 同 `request_hash` -> 返回该记录（规范重放）；
       - 不同 `request_hash` -> `IdempotencyConflictError`。

    异常语义：任何底层驱动异常（含 `UniqueViolation`）一律转换为领域异常，
    **不向调用方泄漏 psycopg2 异常类型**。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cybercut.idempotency_keys (tenant_id, operation,"
                " idempotency_key, request_hash) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, operation, idempotency_key) DO NOTHING"
                f" RETURNING {_COLS}",
                (tenant_id, operation, key, req_hash),
            )
            inserted = cur.fetchone()
    except IdempotencyConflictError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一包装，绝不外泄驱动异常
        raise ConflictError(
            f"幂等键占位失败（{type(exc).__name__}）",
            details={"operation": operation, "idempotency_key": key},
        ) from exc

    if inserted is not None:
        return None  # 本次占位成功

    existing = get(conn, tenant_id, operation, key)
    if existing is None:
        # 极端情况：并发方在占位后立即回滚，且本语句未阻塞到（理论上不可达）
        raise ConflictError(
            "幂等键并发竞争后未读到记录，请重试",
            details={"operation": operation, "idempotency_key": key},
        )
    if existing["request_hash"] != req_hash:
        raise IdempotencyConflictError(
            "幂等键已被不同请求占用",
            details={
                "operation": operation,
                "idempotency_key": key,
                "existing_request_hash": existing["request_hash"],
                "incoming_request_hash": req_hash,
            },
        )
    return existing


def complete(
    conn: Any,
    tenant_id: str,
    operation: str,
    key: str,
    *,
    response_json: dict | None = None,
    resource_id: str | None = None,
) -> None:
    """回填响应（供后续重放）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.idempotency_keys SET response_json = %s, resource_id = %s"
            " WHERE tenant_id = %s AND operation = %s AND idempotency_key = %s",
            (Json(response_json) if response_json is not None else None, resource_id,
             tenant_id, operation, key),
        )


__all__ = ["check_or_reserve", "complete", "get", "request_hash"]
