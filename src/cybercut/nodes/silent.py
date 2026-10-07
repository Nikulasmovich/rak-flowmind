"""测试用 silent handler：执行期间**完全不调用** `progress_cb`。

用途：验证「独立周期心跳」——即使 handler 长时间不报进度，
租约也不会陈旧、attempt 不会被巡检误判为 `WORKER_LOST`。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.storage.paths import resolve_staged_path


class SilentHandler:
    """静默 handler：sleep（或等待 release 事件）后在 attempt_dir 产出 staged 产物，不报进度。"""

    def __init__(
        self,
        node_key: str,
        *,
        sleep_seconds: float = 0.0,
        release_event: threading.Event | None = None,
        artifact_kind: str | None = None,
        entered_event: threading.Event | None = None,
        staged_filename: str | None = None,
    ) -> None:
        self.node_key = node_key
        self.sleep_seconds = float(sleep_seconds)
        self.release_event = release_event
        self.artifact_kind = artifact_kind or f"{node_key}_output"
        self.entered_event = entered_event
        self.staged_filename = staged_filename or f"{node_key}.json"
        self.calls = 0

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        self.calls += 1
        if self.entered_event is not None:
            self.entered_event.set()
        if self.release_event is not None:
            self.release_event.wait(timeout=self.sleep_seconds or None)
        elif self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        # 注意：此处**刻意不调用** context.progress_cb —— 续租职责在独立心跳

        payload = {
            "node_key": self.node_key,
            "attempt_no": context.attempt_no,
            "attempt_id": context.attempt_id,
            "silent": True,
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
                    metadata={"silent": True},
                )
            ],
            decision_trace=[{"step": "silent", "node_key": self.node_key}],
        )


__all__ = ["SilentHandler"]
