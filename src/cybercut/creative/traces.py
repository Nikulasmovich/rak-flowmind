"""决策摘要（DecisionTrace）。

**只保存摘要**，不保存原始思维链（ADR-0004 §决策 5）：
- 每个步骤记录 `step` / `decision` / `rationale_code` / 输入摘要 / 输出摘要；
- `inputs_digest` 为结构化输入的稳定哈希（可追溯、可对比，但不可逆）；
- `rationale_code` 是**机器可读的理由码**而非自由文本，避免思维链泄漏；
- `notes` 仅允许简短的结构化备注（不承载推理过程）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

TRACE_SCHEMA_VERSION = "decision-trace.v1"

#: 允许的理由码（确定性、可枚举、可测试）
RATIONALE_CODES = frozenset({
    "single_source_asset",
    "multi_source_assets",
    "deterministic_template",
    "duration_allocated",
    "duration_proportional",
    "evidence_bound",
    "threshold_met",
    "threshold_missed",
    "repetition_avoided",
    "no_candidate_available",
    "revision_hint_applied",
    "revision_hint_ignored",
    "user_input_applied",
    "default_applied",
    "forbidden_claim_detected",
})


def digest(value: Any) -> str:
    """稳定摘要：`sha256(canonical json)[:16]`。"""
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def step(
    name: str,
    *,
    decision: str,
    rationale_code: str,
    inputs: Any = None,
    outputs: Any = None,
    evidence_count: int | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """构造一个决策摘要步骤（禁用原始思维链字段）。"""
    if rationale_code not in RATIONALE_CODES:
        raise ValueError(f"未登记的理由码：{rationale_code!r}")
    record: dict[str, Any] = {
        "step": name,
        "decision": decision,
        "rationale_code": rationale_code,
    }
    if inputs is not None:
        record["inputs_digest"] = digest(inputs)
    if outputs is not None:
        record["outputs_digest"] = digest(outputs)
    if evidence_count is not None:
        record["evidence_count"] = int(evidence_count)
    if notes:
        record["notes"] = list(notes)
    return record


def summarize(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化摘要列表：按 `step` 排序，保证确定性输出。"""
    cleaned = [{k: v for k, v in dict(s).items() if not k.startswith("_")}
               for s in steps]
    return sorted(cleaned, key=lambda s: (str(s.get("step")), str(s.get("decision"))))


def assert_no_chain_of_thought(trace: list[dict[str, Any]]) -> None:
    """守卫：摘要中不得出现思维链风格的自由文本字段。"""
    banned = {"chain_of_thought", "cot", "reasoning", "thought", "raw_output",
              "prompt_text", "messages"}
    for item in trace:
        leaked = banned & set(item)
        if leaked:
            raise ValueError(f"DecisionTrace 不得包含思维链字段：{sorted(leaked)}")


__all__ = [
    "RATIONALE_CODES",
    "TRACE_SCHEMA_VERSION",
    "assert_no_chain_of_thought",
    "digest",
    "step",
    "summarize",
]
