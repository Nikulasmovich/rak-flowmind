"""测试与默认使用的**确定性**创作决策 provider（WB-03 §四）。

特性：
- 完全确定性：不联网、不下载模型、不读时钟、不使用随机数；
  相同 `GenerationRequest` ⇒ 完全相同的 `payload`；
- 只陈述**可度量事实**（片段数、时长），不虚构产品能力；
- 所有文案性字段都绑定到 `asset.analysis.{asset_id}` 证据；
- 支持 `revision_hint`（局部重跑）——确定性改写并在 warnings 中标记。
"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.creative import (  # noqa: I001 - 与上方分组保持一致
    SCRIPT_SCHEMA_VERSION,
    STORYBOARD_SCHEMA_VERSION,
)
from cybercut.creative.matching import segment_id
from cybercut.creative.prompts import prompt_version
from cybercut.creative.provider import (
    GenerationRequest,
    GenerationResult,
    ProviderError,
)

FAKE_MODEL_ID = "fake-deterministic-v1"

#: 脚本分段计划（role, 时长权重）——固定，保证确定性
SECTION_PLAN: tuple[tuple[str, float], ...] = (
    ("hook", 0.15),
    ("solution", 0.40),
    ("proof", 0.30),
    ("cta", 0.15),
)

#: 分镜：单个镜头的目标时长（微秒）
SHOT_TARGET_US = 2_500_000
_TRANSITIONS = ("cut", "dissolve", "fade")


def _stem(filename: str) -> str:
    name = str(filename or "asset")
    return name.rsplit(".", 1)[0] or "asset"


def _allocate(total_us: int, weights: list[float]) -> list[int]:
    """按权重做**确定性整数分配**：和恰等于 total，且每段 ≥ 1 微秒。"""
    count = len(weights)
    if total_us < count:
        raise ProviderError(
            "DURATION_TOO_SMALL",
            f"目标时长 {total_us}us 不足以分配 {count} 个分段",
        )
    shares = [int(total_us * w) for w in weights]
    remainder = total_us - sum(shares)
    # 余数固定给权重最大者（索引由最大权重决定，同权重取最小索引）——确定性
    pivot = max(range(count), key=lambda i: (weights[i], -i))
    shares[pivot] += remainder
    for index in range(count):
        if shares[index] < 1:
            deficit = 1 - shares[index]
            shares[pivot] -= deficit
            shares[index] += deficit
    if sum(shares) != total_us:  # pragma: no cover - 数学上不可能
        raise ProviderError("ALLOCATION_FAILED", "时长分配失败")
    return shares


def _asset_fact(asset: dict, analysis: dict) -> dict[str, Any]:
    segments = list(analysis.get("segments") or [])
    duration_us = int(analysis.get("duration_us") or 0)
    return {
        "asset_id": asset["asset_id"],
        "stem": _stem(asset.get("original_filename", "")),
        "segment_count": len(segments),
        "duration_us": duration_us,
        # 图片没有自然时长：不伪造时长，只陈述画面片段数
        "is_timed": duration_us > 0,
        "seconds": round(duration_us / 1_000_000, 3),
        "media_kind": asset.get("media_kind", analysis.get("media_kind", "")),
    }


def _selling_point_text(fact: dict[str, Any]) -> str:
    """只陈述**可度量事实**，不虚构产品能力。"""
    if fact["is_timed"]:
        return (
            f"{fact['stem']} 素材共 {fact['segment_count']} 个可切分片段，"
            f"时长 {fact['seconds']} 秒"
        )
    return (
        f"{fact['stem']} 素材为静态画面，含 {fact['segment_count']} 个画面片段"
    )


class FakeCreativeProvider:
    """确定性 provider（默认启用）。"""

    model_id = FAKE_MODEL_ID

    # ── 入口 ──────────────────────────────────────────────────────

    def generate(self, request: GenerationRequest) -> GenerationResult:
        handlers = {
            "brief": self._brief,
            "script": self._script,
            "storyboard": self._storyboard,
        }
        handler = handlers.get(request.kind)
        if handler is None:
            raise ProviderError(
                "UNSUPPORTED_KIND", f"不支持的生成类型：{request.kind!r}"
            )
        payload, warnings = handler(request.context)
        assert isinstance(payload, dict)
        return GenerationResult(
            payload=payload,
            model_id=self.model_id,
            prompt_version=request.prompt_version or prompt_version(request.kind),
            warnings=warnings,
        )

    # ── brief ────────────────────────────────────────────────────

    def _brief(self, context: dict[str, Any]) -> tuple[dict, list[str]]:
        assets = list(context.get("assets") or [])
        analyses = {a.get("asset_id"): a for a in (context.get("analyses") or [])}
        spec = dict(context.get("brief_input") or {})
        if not assets:
            raise ProviderError("NO_ASSETS", "brief 生成至少需要一个素材")

        warnings: list[str] = []
        facts: list[dict[str, Any]] = []
        for asset in assets:
            analysis = analyses.get(asset["asset_id"]) or {}
            fact = _asset_fact(asset, analysis)
            if fact["segment_count"] == 0:
                warnings.append(f"NO_SEGMENT:{asset['asset_id']}")
            facts.append(fact)

        selling_points = [
            {
                "text": _selling_point_text(fact),
                "evidence_refs": [
                    {"asset_id": fact["asset_id"],
                     "artifact_kind": f"asset.analysis.{fact['asset_id']}",
                     "segment_index": None,
                     "quote": f"segments={fact['segment_count']}"}
                ],
            }
            for fact in facts
        ]
        total_seconds = round(
            sum(fact["duration_us"] for fact in facts) / 1_000_000, 3
        )
        objective = str(spec.get("objective") or "推广素材内容")
        core_message = (
            f"{objective}；已结构化分析 {len(facts)} 个素材，"
            f"合计可用时长 {total_seconds} 秒"
        )
        revision_note = context.get("revision_note")
        if revision_note:
            core_message = f"{core_message}（修订：{revision_note}）"
            warnings.append("REVISION_HINT_APPLIED")

        payload = {
            "brief_revision_id": str(context.get("revision_id") or ""),
            "project_id": context.get("project_id", ""),
            "run_id": context.get("run_id", ""),
            "revision_no": int(context.get("revision_no") or 1),
            "target_platform": str(spec.get("target_platform") or "generic"),
            "aspect_ratio": str(spec.get("aspect_ratio") or "9:16"),
            "target_duration_us": int(spec.get("target_duration_us") or 30_000_000),
            "language": str(spec.get("language") or "zh"),
            "audience": str(spec.get("audience") or "通用受众"),
            "objective": objective,
            "core_message": core_message,
            "selling_points": selling_points,
            "call_to_action": str(spec.get("call_to_action") or f"了解{objective}"),
            "tone": str(spec.get("tone") or "清晰"),
            "visual_style": str(spec.get("visual_style") or "实拍为主"),
            "forbidden_claims": list(spec.get("forbidden_claims") or []),
            "source_asset_ids": [fact["asset_id"] for fact in facts],
            "evidence_refs": [
                {"asset_id": fact["asset_id"],
                 "artifact_kind": f"asset.analysis.{fact['asset_id']}",
                 "segment_index": None}
                for fact in facts
            ],
            "prompt_version": prompt_version("brief"),
            "model_id": self.model_id,
            "revision_note": context.get("revision_note"),
            "warnings": warnings,
            "decision_trace": [],
        }
        return payload, warnings

    # ── script ───────────────────────────────────────────────────

    def _script(self, context: dict[str, Any]) -> tuple[dict, list[str]]:
        brief = dict(context.get("brief") or {})
        if not brief:
            raise ProviderError("BRIEF_REQUIRED", "script 生成需要 CreativeBrief")
        spec = dict(context.get("script_input") or {})
        warnings: list[str] = []

        total = int(brief["target_duration_us"])
        weights = [weight for _, weight in SECTION_PLAN]
        durations = _allocate(total, weights)
        selling_points = list(brief.get("selling_points") or [])
        evidence_refs = list(brief.get("evidence_refs") or [])

        narrations = {
            "hook": brief["core_message"],
            "solution": (selling_points[0]["text"] if selling_points
                         else brief["core_message"]),
            # 与 solution 相同的**可度量事实**；不拼接全部卖点
            # （避免超出模板字幕长度限制，见 WB-04 审计修正）
            "proof": (selling_points[0]["text"] if selling_points
                      else brief["core_message"]),
            "cta": brief["call_to_action"],
        }
        on_screen = {
            "hook": "核心信息",
            "solution": "方案要点",
            "proof": "事实与数据",
            "cta": "行动号召",
        }

        sections = []
        for index, (role, _weight) in enumerate(SECTION_PLAN):
            refs = _refs_for_section(evidence_refs, index, len(SECTION_PLAN))
            sections.append({
                "section_id": f"s{index:02d}-{role}",
                "role": role,
                "narration": narrations[role],
                "on_screen_text": on_screen[role],
                "target_duration_us": durations[index],
                "required_visual_intent": f"{role} 段落的画面意图",
                "evidence_refs": refs or evidence_refs[:1],
            })

        hook = brief["core_message"]
        revision_note = context.get("revision_note")
        if revision_note:
            hook = f"{hook}（修订：{revision_note}）"
            warnings.append("REVISION_HINT_APPLIED")
        if not selling_points:
            warnings.append("NO_SELLING_POINT")

        payload = {
            "schema_version": SCRIPT_SCHEMA_VERSION,
            "script_revision_id": str(context.get("revision_id") or ""),
            "project_id": context.get("project_id", ""),
            "run_id": context.get("run_id", ""),
            "revision_no": int(context.get("revision_no") or 1),
            "language": str(brief.get("language") or spec.get("language") or "zh"),
            "hook": hook,
            "sections": sections,
            "total_target_duration_us": total,
            "cta": brief["call_to_action"],
            "evidence_refs": evidence_refs,
            "prompt_version": prompt_version("script"),
            "model_id": self.model_id,
            "revision_note": context.get("revision_note"),
            "warnings": warnings,
            "decision_trace": [],
        }
        return payload, warnings

    # ── storyboard ───────────────────────────────────────────────

    def _storyboard(self, context: dict[str, Any]) -> tuple[dict, list[str]]:
        script = dict(context.get("script") or {})
        analyses = list(context.get("analyses") or [])
        if not script:
            raise ProviderError("SCRIPT_REQUIRED", "storyboard 生成需要 ScriptRevision")

        warnings: list[str] = []
        candidates = sorted(
            segment_id(str(analysis.get("asset_id")), int(seg.get("segment_index", 0)))
            for analysis in analyses
            for seg in (analysis.get("segments") or [])
        )
        if not candidates:
            raise ProviderError(
                "NO_CANDIDATE_SEGMENT", "当前 Run 没有任何可用于分镜的 AssetSegment"
            )

        shots: list[dict] = []
        sequence = 0
        for section in script.get("sections") or []:
            section_id = str(section["section_id"])
            section_duration = int(section["target_duration_us"])
            count = max(1, -(-section_duration // SHOT_TARGET_US))  # 向上取整
            durations = _allocate(section_duration, [1.0 / count] * count)
            for index in range(count):
                shots.append({
                    "shot_id": f"shot-{sequence:04d}",
                    "section_id": section_id,
                    "sequence_no": sequence,
                    # 意图基于**事实文案**（与素材关键词池有可解释的重叠）
                    "intent": str(section["narration"]),
                    "visual_requirement": str(section["required_visual_intent"]),
                    "subject": str(section["on_screen_text"]),
                    "composition": "中心构图",
                    "camera_motion": "固定",
                    "emotion": "中性",
                    "target_duration_us": durations[index],
                    "source_constraints": ["需来自本次 Run 的素材"],
                    "transition_hint": _TRANSITIONS[sequence % len(_TRANSITIONS)],
                    "candidate_segment_ids": candidates,
                    "evidence_refs": list(section.get("evidence_refs") or []),
                })
                sequence += 1

        payload = {
            "schema_version": STORYBOARD_SCHEMA_VERSION,
            "storyboard_revision_id": str(context.get("revision_id") or ""),
            "project_id": context.get("project_id", ""),
            "run_id": context.get("run_id", ""),
            "revision_no": int(context.get("revision_no") or 1),
            "shots": shots,
            "total_target_duration_us": sum(
                int(s["target_duration_us"]) for s in shots
            ),
            "rhythm": f"{len(shots)} 镜头 / {len(script.get('sections') or [])} 段落",
            "evidence_refs": list(script.get("evidence_refs") or []),
            "prompt_version": prompt_version("storyboard"),
            "model_id": self.model_id,
            "revision_note": context.get("revision_note"),
            "warnings": warnings,
            "decision_trace": [],
        }
        return payload, warnings


def _refs_for_section(refs: list[dict], index: int, count: int) -> list[dict]:
    """把证据引用确定性地分配到各段落（排序后按索引轮转取 1 条）。"""
    if not refs:
        return []
    ordered = sorted(
        refs, key=lambda r: (str(r.get("asset_id")), str(r.get("artifact_kind")))
    )
    return [ordered[index % len(ordered)]]


__all__ = [
    "FAKE_MODEL_ID",
    "SECTION_PLAN",
    "SHOT_TARGET_US",
    "FakeCreativeProvider",
]
