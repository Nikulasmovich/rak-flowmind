"""测试用 echo handler：产出一个 JSON 产物，验证执行器闭环。"""
from __future__ import annotations

import json
from typing import Any

from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.storage.paths import resolve_staged_path


class EchoHandler:
    """把输入原样回显，并在 **attempt_dir** 内落一个 staged 产物（正式路径由执行器分配）。"""

    def __init__(self, node_key: str, *, artifact_kind: str | None = None,
                 staged_filename: str | None = None) -> None:
        self.node_key = node_key
        self.artifact_kind = artifact_kind or f"{node_key}_output"
        #: staged 文件名（相对 attempt_dir）；同名来自不同 attempt 仍会发布到不同正式路径
        self.staged_filename = staged_filename or f"{node_key}.json"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        context.progress_cb(10.0, "prepare", f"{self.node_key}: 准备")
        payload = {
            "node_key": self.node_key,
            "attempt_no": context.attempt_no,
            "attempt_id": context.attempt_id,
            "echo": input_json,
        }
        # WB-01R3：只写 attempt 独占暂存目录；正式路径由执行器分配
        staged_rel = self.staged_filename
        target = resolve_staged_path(context.attempt_dir, staged_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        context.progress_cb(100.0, "done", f"{self.node_key}: 完成")
        return NodeResult(
            output=payload,
            artifacts=[
                ArtifactSpec(
                    kind=self.artifact_kind,
                    staged_relative_path=staged_rel,
                    metadata={"producer": self.node_key},
                )
            ],
            decision_trace=[
                {"step": "echo", "node_key": self.node_key,
                 "attempt_no": context.attempt_no}
            ],
        )


__all__ = ["EchoHandler"]
