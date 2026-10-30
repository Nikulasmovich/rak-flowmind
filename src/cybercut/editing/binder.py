"""确定性模板绑定：TimelineSpec + ScriptRevision → TemplateBinding。

规则（ADR-0005；WB-04 审计修正后语义）：
- 绑定**不得**改变 match 的素材选择、捏造缺失镜头、改写事实性文案；
- 槽位按 `order` 从时间线片段中**依序**取一个媒体类型匹配的片段（贪心）；
- **`accepted` 语义 = 整体可接受**：当且仅当 `unmet`（阻断性未满足约束）为空。
  WB-05 渲染前必须依据 `unmet` 阻止不兼容渲染；
- **阻断性**未满足约束（进入 `unmet`）：
  画幅不支持、帧率不支持、文案超限、转场不支持、
  以及「必需槽位为空且缺槽策略为 fail」；
- **非阻断**问题（仅 `warnings`，不进入 `unmet`）：
  可选槽位为空（`OPTIONAL_SLOT_EMPTY`）、
  必需槽位为空且策略为 warn（`SLOT_UNFILLED` 告警）；
- 全程确定性：相同输入 ⇒ 字节级一致输出。
"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.creative import ScriptRevision
from cybercut.contracts.editing import (
    TemplateBinding,
    TemplateManifest,
    TimelineSpec,
    UnmetConstraint,
)

BIND_PROMPT_VERSION = "template-bind.v1"
BIND_MODEL_ID = "deterministic:template-bind-v1"


def bind_template(
    *,
    project_id: str,
    run_id: str,
    revision_no: int,
    manifest: TemplateManifest,
    selection_reason: str,
    timeline: TimelineSpec,
    timeline_artifact_revision_id: str,
    script: ScriptRevision,
    script_artifact_revision_id: str,
    revision_note: str | None,
) -> TemplateBinding:
    """构造并校验 `TemplateBinding`（纯确定性）。

    `accepted` 当且仅当 `unmet` 为空（整体可接受、可交由 WB-05 渲染）。
    """
    unmet: list[UnmetConstraint] = []
    warnings: list[str] = []

    # ── 画幅支持（阻断性）──
    if timeline.aspect_ratio not in manifest.applicable_aspect_ratios:
        unmet.append(UnmetConstraint(
            code="ASPECT_UNSUPPORTED", subject=manifest.template_id,
            detail=f"时间线画幅 {timeline.aspect_ratio!r} 不在模板支持的画幅中："
                   f"{manifest.applicable_aspect_ratios}"))
        warnings.append(
            f"ASPECT_UNSUPPORTED:{timeline.aspect_ratio}:{manifest.template_id}")

    # ── 帧率支持（阻断性）──
    supported_fps = {(f.num, f.den) for f in manifest.applicable_frame_rates}
    if (timeline.frame_rate.num, timeline.frame_rate.den) not in supported_fps:
        unmet.append(UnmetConstraint(
            code="FRAME_RATE_UNSUPPORTED", subject=manifest.template_id,
            detail=f"时间线帧率 {timeline.frame_rate.num}/"
                   f"{timeline.frame_rate.den} 不在模板支持的帧率中"))
        warnings.append(
            f"FRAME_RATE_UNSUPPORTED:{timeline.frame_rate.num}/"
            f"{timeline.frame_rate.den}")

    # ── 槽位绑定（按 order 依序贪心）──
    remaining = list(timeline.clips)
    bindings: list[dict] = []
    required_slot_empty = False
    for slot in manifest.slots:
        chosen: tuple[int, Any] | None = None
        for index, clip in enumerate(remaining):
            if slot.media_kind != "any" and clip.media_kind != slot.media_kind:
                continue
            if clip.duration_frames < slot.min_duration_frames:
                continue
            if (slot.max_duration_frames is not None
                    and clip.duration_frames > slot.max_duration_frames):
                continue
            chosen = (index, clip)
            break

        if chosen is None:
            if not slot.required:
                # 可选槽位为空：**永不阻断**（任何缺槽策略下）
                warnings.append(f"OPTIONAL_SLOT_EMPTY:{slot.slot_id}")
                bindings.append({
                    "slot_id": slot.slot_id, "sequence": slot.order,
                    "clip_id": None, "satisfied": False,
                    "reason": "optional_slot_empty",
                })
                continue
            # 必需槽位为空：按模板缺槽策略处理
            if manifest.missing_slot_strategy == "fail":
                unmet.append(UnmetConstraint(
                    code="SLOT_UNFILLED", subject=slot.slot_id,
                    detail=f"必需槽位 {slot.slot_id!r}（{slot.media_kind}）"
                           f"没有可用片段（未匹配镜头 "
                           f"{len(timeline.unresolved_shots)} 个）"))
                warnings.append(f"SLOT_UNFILLED:{slot.slot_id}")
                reason = "required_slot_empty"
            else:
                warnings.append(f"SLOT_UNFILLED:{slot.slot_id}")
                reason = "required_slot_empty_warn"
            required_slot_empty = True
            bindings.append({
                "slot_id": slot.slot_id, "sequence": slot.order,
                "clip_id": None, "satisfied": False, "reason": reason,
            })
            continue

        index, clip = chosen
        remaining.pop(index)
        bindings.append({
            "slot_id": slot.slot_id, "sequence": slot.order,
            "clip_id": clip.clip_id, "satisfied": True, "reason": None,
        })

    # ── 文案限制（阻断性；只校验，绝不改写）──
    counts: dict[str, list[str]] = {}
    for intent in timeline.text_intents:
        counts.setdefault(intent.kind, []).append(intent.text)
    for section in script.sections:
        counts.setdefault("narration", []).append(section.narration)

    for limit in manifest.text_limits:
        items = counts.get(limit.kind, [])
        if len(items) > limit.max_items:
            unmet.append(UnmetConstraint(
                code="TEXT_LIMIT_EXCEEDED", subject=limit.kind,
                detail=f"{limit.kind} 数量 {len(items)} 超过上限 {limit.max_items}"))
            warnings.append(
                f"TEXT_LIMIT_EXCEEDED:{limit.kind}:count:{len(items)}")
        for text in items:
            if len(text) > limit.max_chars_per_item:
                unmet.append(UnmetConstraint(
                    code="TEXT_LIMIT_EXCEEDED", subject=limit.kind,
                    detail=f"{limit.kind} 单条 {len(text)} 字超过上限 "
                           f"{limit.max_chars_per_item}"))
                warnings.append(
                    f"TEXT_LIMIT_EXCEEDED:{limit.kind}:chars:{len(text)}")
                break

    # ── 转场允许性（阻断性）──
    for transition in timeline.transitions:
        if transition.kind not in manifest.allowed_transitions:
            unmet.append(UnmetConstraint(
                code="TRANSITION_NOT_ALLOWED", subject=transition.transition_id,
                detail=f"转场 {transition.kind!r} 不在模板允许的转场中"))
            warnings.append(
                f"TRANSITION_NOT_ALLOWED:{transition.transition_id}")

    # ── accepted = 整体可接受 = 无阻断性未满足约束 ──
    accepted = not unmet
    if manifest.missing_slot_strategy == "fail" and required_slot_empty:
        warnings.append("TEMPLATE_SLOT_MISSING_FAIL")

    return TemplateBinding(
        template_bind_revision_id=f"pending:template.bind-{run_id}",
        project_id=project_id,
        run_id=run_id,
        revision_no=revision_no,
        template_id=manifest.template_id,
        template_version=manifest.version,
        selection_reason=selection_reason,
        missing_slot_strategy=manifest.missing_slot_strategy,
        timeline_artifact_revision_id=timeline_artifact_revision_id,
        script_artifact_revision_id=script_artifact_revision_id,
        bindings=bindings,
        unmet=unmet,
        accepted=accepted,
        warnings=sorted(set(warnings)),
        decision_trace=[{
            "step": "template.bind",
            "decision": "bind_slots",
            "rationale_code": "deterministic_template",
            "evidence_count": len(bindings),
            "notes": [f"template={manifest.template_id}@{manifest.version}",
                      f"selection_reason={selection_reason}",
                      f"missing_slot_strategy={manifest.missing_slot_strategy}",
                      f"blocking_unmet={len(unmet)}",
                      f"accepted={accepted}"],
        }],
    )


__all__ = [
    "BIND_MODEL_ID",
    "BIND_PROMPT_VERSION",
    "bind_template",
]
