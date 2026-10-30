"""版本化提示词（WB-03）。

本模块只负责**构造确定性提示词文本**与登记版本号；不联网、不调用任何模型。
提示词中硬性声明输出契约（必须为符合 schema 的 JSON），并禁止模型自行执行
文件/数据库操作（ADR-0004 §决策 1）。
"""
from __future__ import annotations

import json
from typing import Any

#: kind -> 提示词版本（变更提示词必须升版本，以保证产物可追溯）
PROMPT_VERSIONS: dict[str, str] = {
    "brief": "creative-brief-prompt.v1",
    "script": "creative-script-prompt.v1",
    "storyboard": "creative-storyboard-prompt.v1",
}

#: 所有提示词共享的硬性约束
SYSTEM_INSTRUCTIONS = (
    "你是结构化创作决策组件。必须严格遵守：\n"
    "1) 只输出符合给定 JSON schema 的单个 JSON 对象，不得输出解释性文字；\n"
    "2) 不得虚构素材中不存在的产品能力、数字或事实；\n"
    "3) 任何文案性表述都必须能追溯到提供的素材分析证据；\n"
    "4) 不得尝试执行文件操作、命令或修改数据库；\n"
    "5) 时间字段一律使用**整数微秒**，且必须大于 0。\n"
)

#: 每种产物需要产出的顶层字段（用于提示词自描述与校验对齐）
_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "brief": (
        "target_platform", "aspect_ratio", "target_duration_us", "language",
        "audience", "objective", "core_message", "selling_points",
        "call_to_action", "tone", "visual_style", "forbidden_claims",
        "source_asset_ids", "evidence_refs", "warnings",
    ),
    "script": (
        "language", "hook", "sections", "total_target_duration_us", "cta",
        "evidence_refs", "warnings",
    ),
    "storyboard": (
        "shots", "total_target_duration_us", "rhythm", "evidence_refs", "warnings",
    ),
}


def prompt_version(kind: str) -> str:
    try:
        return PROMPT_VERSIONS[kind]
    except KeyError as exc:  # pragma: no cover - 编程错误
        raise ValueError(f"未登记的提示词 kind：{kind!r}") from exc


def build_prompt(kind: str, context: dict[str, Any]) -> str:
    """构造确定性提示词文本（相同 context ⇒ 相同文本）。"""
    fields = _OUTPUT_FIELDS.get(kind)
    if fields is None:  # pragma: no cover - 编程错误
        raise ValueError(f"未登记的提示词 kind：{kind!r}")
    body = json.dumps(context, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return (
        f"[{prompt_version(kind)}]\n"
        f"{SYSTEM_INSTRUCTIONS}"
        f"需要的顶层字段：{list(fields)}\n"
        f"输入上下文（JSON）：{body}\n"
    )


__all__ = [
    "PROMPT_VERSIONS",
    "SYSTEM_INSTRUCTIONS",
    "build_prompt",
    "prompt_version",
]
