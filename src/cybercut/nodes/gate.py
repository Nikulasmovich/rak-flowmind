"""测试用审批闸门 handler：产出待审产物并声明 `awaiting_approval=True`。

executor 收到该声明后会把 node_run 置为 `waiting_approval` 并创建待审记录。
"""
from __future__ import annotations

import json
from typing import Any

from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.storage.paths import resolve_staged_path


class ApprovalGateHandler:
    """产出「待审批载荷」的节点（暂存到 attempt_dir）。"""

    def __init__(self, node_key: str, *, artifact_kind: str | None = None,
                 staged_filename: str | None = None) -> None:
        self.node_key = node_key
        self.artifact_kind = artifact_kind or f"{node_key}_payload"
        self.staged_filename = staged_filename or f"{node_key}.json"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        context.progress_cb(50.0, "await_approval", f"{self.node_key}: 等待人工审批")
        payload = {
            "node_key": self.node_key,
            "attempt_no": context.attempt_no,
            "attempt_id": context.attempt_id,
            "upstream": input_json,
            "requires": "human_approval",
        }
        # WB-01R3：只写 attempt 独占暂存目录；正式路径由执行器分配
        staged_rel = self.staged_filename
        target = resolve_staged_path(context.attempt_dir, staged_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return NodeResult(
            output=payload,
            artifacts=[
                ArtifactSpec(
                    kind=self.artifact_kind,
                    staged_relative_path=staged_rel,
                    metadata={"producer": self.node_key, "approval_required": True},
                )
            ],
            decision_trace=[{"step": "gate", "node_key": self.node_key}],
            awaiting_approval=True,
        )


__all__ = ["ApprovalGateHandler"]
