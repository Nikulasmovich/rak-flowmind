"""storage 内部小工具。"""
from __future__ import annotations

import uuid
from typing import Any

#: 显式“未提供”哨兵：区分「不修改某列」与「把某列置为 NULL」
UNSET: Any = object()


def new_id() -> str:
    """UUID4 字符串主键。"""
    return str(uuid.uuid4())


def require_row(row: dict | None, *, what: str) -> dict:
    """行不存在 -> NotFoundError（跨租户与真不存在同形，不泄漏存在性）。"""
    from cybercut.contracts.errors import NotFoundError

    if row is None:
        raise NotFoundError(f"{what} 不存在")
    return row


__all__ = ["UNSET", "new_id", "require_row"]
