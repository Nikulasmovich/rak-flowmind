"""WB-04 编辑规划与模板绑定节点（内部 NodeHandler，**不注册 FlowMind `@skill`**）。

节点：
    edit.plan      冻结的 StoryboardRevision + AssetMatchResult + ScriptRevision
                   + CreativeBrief + asset.analysis.* → TimelineSpec
    template.bind  冻结的 TimelineSpec（edit.plan）+ ScriptRevision → TemplateBinding

统一约束（ADR-0005）：
- 只读 attempt 冻结的 `input_artifacts`，禁止查询“最新版本”替代冻结引用；
- 每个已选中素材的镜头必须可溯源：shot → match 选中 → AssetSegment → asset；
- 未匹配镜头进入 `unresolved_shots`（显式保留），绝不偷换素材凑数；
- 源区间必须落在 segment 与媒体时长范围内；图片区分源时间与目标时间；
- 帧换算为纯整数运算，偏差显式记录；
- 确定性：相同冻结输入与配置 ⇒ 字节级一致输出；
- 接入取消检查，稳定错误码，模型/数据错误不泄漏为 `HANDLER_EXCEPTION`。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.editing import TemplateBinding, TimelineSpec
from cybercut.contracts.errors import NodeExecutionError, ValidationError
from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.creative.policy import AUTO_POLICY, ApprovalPolicy
from cybercut.editing.binder import bind_template
from cybercut.editing.planner import plan_timeline
from cybercut.editing.templates import get_manifest, select_default_manifest
from cybercut.storage import paths
from cybercut.storage.paths import resolve_staged_path

EDIT_PLAN_KIND = "edit.plan"
TEMPLATE_BIND_KIND = "template.bind"

INPUT_MISSING = "INPUT_MISSING"
INPUT_INVALID = "INPUT_INVALID"
SCHEMA_INVALID = "EDIT_SCHEMA_INVALID"
FACT_INVALID = "EDIT_FACT_INVALID"
TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
CANCELLED = "CANCELLED"


# ══════════════════════════════════════════════════════════════════
# 公共工具
# ══════════════════════════════════════════════════════════════════


def _refs_by_kind(input_json: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    data = input_json or {}
    refs = data.get("input_artifacts") or []
    return {r["kind"]: r for r in refs if isinstance(r, dict) and r.get("kind")}


def _official(context: NodeContext, ref: dict[str, Any]) -> Path:
    return paths.resolve_official_artifact_path(
        context.project_root, ref["relative_path"]
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeExecutionError(
            f"上游产物读取失败：{exc}", error_code=INPUT_INVALID,
            details={"path": str(path)},
        ) from exc
    if not isinstance(data, dict):
        raise NodeExecutionError(
            "上游产物必须是 JSON 对象", error_code=INPUT_INVALID,
            details={"path": str(path)},
        )
    return data


def _write_json(context: NodeContext, staged_name: str, payload: Any,
                kind: str) -> ArtifactSpec:
    """确定性写入暂存 JSON（`sort_keys=True` ⇒ 相同内容 ⇒ 相同哈希）。"""
    target = resolve_staged_path(context.attempt_dir, staged_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return ArtifactSpec(kind=kind, staged_relative_path=staged_name)


def _require(condition: bool, code: str, message: str,
             details: dict | None = None) -> None:
    if not condition:
        raise NodeExecutionError(message, error_code=code, details=details)


def _check_cancel(context: NodeContext) -> None:
    if context.cancel_check():
        raise NodeExecutionError(
            "编辑规划节点被取消", error_code=CANCELLED,
            details={"node_key": context.node_key},
        )


def _read_model(context: NodeContext, input_json: dict[str, Any], kind: str,
                model: type) -> Any:
    ref = _refs_by_kind(input_json).get(kind)
    _require(ref is not None, INPUT_MISSING, f"缺少上游产物：{kind}", {"kind": kind})
    payload = _read_json(_official(context, ref))
    try:
        return model.model_validate(payload)
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            f"上游产物不符合 schema：{kind}", error_code=INPUT_INVALID,
            details={"kind": kind, "reason": str(exc)[:300]},
        ) from exc


def _identity(context: NodeContext, kind: str) -> dict[str, Any]:
    """节点侧写出的**临时**身份（执行器发布前用数据库权威值覆盖）。"""
    from cybercut.storage import creative_revisions

    return creative_revisions.provisional_identity(
        kind, run_id=context.run_id, attempt_no=context.attempt_no
    )


def _policy(input_json: dict[str, Any] | None,
            default: ApprovalPolicy) -> ApprovalPolicy:
    data = input_json or {}
    if "approval_policy" in data:
        return ApprovalPolicy.from_json(data.get("approval_policy"))
    return default


def _revision_note(input_json: dict[str, Any] | None) -> str | None:
    data = input_json or {}
    request = data.get("revision_request") or {}
    if not isinstance(request, dict):
        return None
    note = request.get("new_prompt") or request.get("feedback")
    return str(note) if note else None


def _check_identity(entity: Any, context: NodeContext, where: str) -> None:
    if getattr(entity, "project_id", None) != context.project_id:
        raise NodeExecutionError(
            f"{where} 的 project_id 与当前 Run 不一致",
            error_code=FACT_INVALID,
            details={"where": where, "project_id": getattr(entity, "project_id", None)},
        )
    if getattr(entity, "run_id", None) != context.run_id:
        raise NodeExecutionError(
            f"{where} 的 run_id 与当前 Run 不一致",
            error_code=FACT_INVALID,
            details={"where": where, "run_id": getattr(entity, "run_id", None)},
        )


def _analyses(context: NodeContext, input_json: dict[str, Any]) -> list[dict[str, Any]]:
    assets = data_assets(input_json)
    refs = _refs_by_kind(input_json)
    result: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset.get("asset_id", ""))
        ref = refs.get(f"asset.analysis.{asset_id}")
        _require(ref is not None, INPUT_MISSING,
                 f"缺少素材分析产物：asset.analysis.{asset_id}",
                 {"asset_id": asset_id})
        result.append(_read_json(_official(context, ref)))
    _require(bool(result), INPUT_MISSING, "当前 Run 没有可用的素材分析报告")
    return result


def data_assets(input_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = input_json or {}
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise NodeExecutionError("input_json.assets 非法", error_code=INPUT_INVALID)
    return [a for a in assets if isinstance(a, dict)]


def _run_fact_checks(check) -> None:
    try:
        check()
    except ValidationError as exc:
        raise NodeExecutionError(
            f"编辑规划产物未通过事实校验：{exc}", error_code=FACT_INVALID,
            details=getattr(exc, "details", None),
        ) from exc


class _EditHandlerBase:
    """编辑节点公共基类（审批策略注入）。"""

    node_key = ""

    def __init__(self, *, policy: ApprovalPolicy | None = None) -> None:
        self._policy = policy or AUTO_POLICY

    def _effective_policy(self, input_json: dict[str, Any]) -> ApprovalPolicy:
        data = input_json or {}
        if "approval_policy" in data:
            return _policy(data, self._policy)
        return self._policy

    def _awaiting(self, input_json: dict[str, Any]) -> bool:
        return self._effective_policy(input_json).requires_approval(self.node_key)


# ══════════════════════════════════════════════════════════════════
# edit.plan
# ══════════════════════════════════════════════════════════════════


class EditPlanHandler(_EditHandlerBase):
    """分镜 + 匹配 + 脚本 + 简报 + 素材分析 → TimelineSpec（确定性）。"""

    node_key = "edit.plan"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        refs = _refs_by_kind(input_json)
        storyboard: StoryboardRevision = _read_model(
            context, input_json, "creative.storyboard", StoryboardRevision)
        match: AssetMatchResult = _read_model(
            context, input_json, "creative.match", AssetMatchResult)
        script: ScriptRevision = _read_model(
            context, input_json, "creative.script", ScriptRevision)
        brief: CreativeBrief = _read_model(
            context, input_json, "creative.brief", CreativeBrief)
        analyses = _analyses(context, input_json)

        for entity, where in ((storyboard, "storyboard"), (match, "match"),
                              (script, "script"), (brief, "brief")):
            _check_identity(entity, context, where)

        frozen_refs = {kind: str(ref["artifact_revision_id"])
                       for kind, ref in refs.items()}
        missing = [k for k in ("creative.storyboard", "creative.match",
                               "creative.script", "creative.brief")
                   if k not in frozen_refs]
        _require(not missing, INPUT_MISSING,
                 f"缺少冻结引用：{missing}", {"missing": missing})
        for asset in data_assets(input_json):
            key = f"asset.analysis.{asset['asset_id']}"
            _require(key in frozen_refs, INPUT_MISSING,
                     f"缺少冻结引用：{key}", {"kind": key})

        identity = _identity(context, "edit_plan")
        _check_cancel(context)

        spec: TimelineSpec = plan_timeline(
            project_id=context.project_id,
            run_id=context.run_id,
            revision_no=identity["revision_no"],
            revision_note=_revision_note(input_json),
            storyboard=storyboard,
            match=match,
            script=script,
            brief=brief,
            analyses=analyses,
            frozen_refs=frozen_refs,
        )
        _check_cancel(context)
        _run_fact_checks(lambda: _validate_plan(spec, context, frozen_refs))

        spec = spec.model_copy(update={"decision_trace": [{
            "step": "edit.plan",
            "decision": "build_timeline",
            "rationale_code": "deterministic_template",
            "evidence_count": len(spec.clips),
            "notes": [f"clips={len(spec.clips)}",
                      f"unresolved={len(spec.unresolved_shots)}",
                      f"total_frames={spec.total_frames}"],
        }]})
        artifact = _write_json(context, "editing/plan.json",
                               spec.model_dump(mode="json"), EDIT_PLAN_KIND)
        return NodeResult(
            output={"kind": EDIT_PLAN_KIND, "clips": len(spec.clips),
                    "unresolved": len(spec.unresolved_shots),
                    "total_frames": spec.total_frames,
                    "frame_rate": f"{spec.frame_rate.num}/{spec.frame_rate.den}"},
            artifacts=[artifact],
            decision_trace=spec.decision_trace,
            warnings=list(spec.warnings),
            awaiting_approval=self._awaiting(input_json),
        )


def _validate_plan(spec: TimelineSpec, context: NodeContext,
                   frozen_refs: dict[str, str]) -> None:
    """编辑规划事实校验：身份一致 + 溯源指向本 Run 冻结产物。"""
    if spec.project_id != context.project_id or spec.run_id != context.run_id:
        raise ValidationError(
            "TimelineSpec 身份与当前 Run 不一致",
            details={"project_id": spec.project_id, "run_id": spec.run_id})
    required = ("storyboard_artifact_revision_id", "match_artifact_revision_id",
                "script_artifact_revision_id", "brief_artifact_revision_id")
    for field in required:
        value = getattr(spec, field)
        if value not in set(frozen_refs.values()):
            raise ValidationError(
                f"{field} 不在本 Run 冻结产物中：{value!r}",
                details={"field": field})
    for clip in spec.clips:
        for field in ("storyboard_artifact_revision_id",
                      "match_artifact_revision_id",
                      "analysis_artifact_revision_id"):
            if getattr(clip, field) not in set(frozen_refs.values()):
                raise ValidationError(
                    f"{clip.clip_id}.{field} 不在本 Run 冻结产物中",
                    details={"clip": clip.clip_id, "field": field})


# ══════════════════════════════════════════════════════════════════
# template.bind
# ══════════════════════════════════════════════════════════════════


class TemplateBindHandler(_EditHandlerBase):
    """TimelineSpec + ScriptRevision → TemplateBinding（确定性）。"""

    node_key = "template.bind"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        refs = _refs_by_kind(input_json)
        plan_ref = refs.get(EDIT_PLAN_KIND)
        _require(plan_ref is not None, INPUT_MISSING,
                 f"缺少上游产物：{EDIT_PLAN_KIND}", {"kind": EDIT_PLAN_KIND})
        plan_payload = _read_json(_official(context, plan_ref))
        try:
            timeline = TimelineSpec.model_validate(plan_payload)
        except (ValidationError, PydanticValidationError) as exc:
            raise NodeExecutionError(
                f"上游产物不符合 schema：{EDIT_PLAN_KIND}",
                error_code=INPUT_INVALID,
                details={"kind": EDIT_PLAN_KIND, "reason": str(exc)[:300]},
            ) from exc
        script: ScriptRevision = _read_model(
            context, input_json, "creative.script", ScriptRevision)
        _check_identity(timeline, context, "timeline")
        _check_identity(script, context, "script")

        creative_input = (input_json or {}).get("creative_input") or {}
        edit_input = (creative_input.get("edit_input") or {})
        if not isinstance(edit_input, dict):
            raise NodeExecutionError(
                "creative_input.edit_input 非法", error_code=INPUT_INVALID)
        requested = edit_input.get("template_id")

        if requested:
            try:
                manifest = get_manifest(str(requested))
                selection_reason = "explicit:frozen_run_input"
            except ValidationError as exc:
                raise NodeExecutionError(
                    f"模板不存在：{requested!r}", error_code=TEMPLATE_NOT_FOUND,
                    details={"template_id": str(requested),
                             "available": sorted(
                                 __import__("cybercut.editing.templates",
                                            fromlist=["MANIFESTS"]).MANIFESTS)},
                ) from exc
        else:
            manifest, selection_reason = select_default_manifest(
                timeline.aspect_ratio, timeline.frame_rate)

        identity = _identity(context, "template_bind")
        _check_cancel(context)

        binding: TemplateBinding = bind_template(
            project_id=context.project_id,
            run_id=context.run_id,
            revision_no=identity["revision_no"],
            manifest=manifest,
            selection_reason=selection_reason,
            timeline=timeline,
            timeline_artifact_revision_id=str(
                refs[EDIT_PLAN_KIND]["artifact_revision_id"]),
            script=script,
            script_artifact_revision_id=str(
                refs["creative.script"]["artifact_revision_id"]),
            revision_note=_revision_note(input_json),
        )
        _check_cancel(context)
        _run_fact_checks(lambda: _validate_binding(binding, context, refs))

        binding = binding.model_copy(update={"decision_trace": [{
            "step": "template.bind",
            "decision": "bind_slots",
            "rationale_code": "deterministic_template",
            "evidence_count": len(binding.bindings),
            "notes": [f"template={binding.template_id}@{binding.template_version}",
                      f"selection_reason={selection_reason}",
                      f"unmet={len(binding.unmet)}"],
        }]})
        artifact = _write_json(context, "editing/bound.json",
                               binding.model_dump(mode="json"), TEMPLATE_BIND_KIND)
        return NodeResult(
            output={"kind": TEMPLATE_BIND_KIND, "template_id": binding.template_id,
                    "template_version": binding.template_version,
                    "accepted": binding.accepted,
                    "unmet": len(binding.unmet),
                    "bindings": len(binding.bindings)},
            artifacts=[artifact],
            decision_trace=binding.decision_trace,
            warnings=list(binding.warnings),
            awaiting_approval=self._awaiting(input_json),
        )


def _validate_binding(binding: TemplateBinding, context: NodeContext,
                      refs: dict[str, dict[str, Any]]) -> None:
    if binding.project_id != context.project_id or binding.run_id != context.run_id:
        raise ValidationError(
            "TemplateBinding 身份与当前 Run 不一致",
            details={"project_id": binding.project_id, "run_id": binding.run_id})
    if binding.timeline_artifact_revision_id != \
            str(refs[EDIT_PLAN_KIND]["artifact_revision_id"]):
        raise ValidationError(
            "TemplateBinding 引用的 edit.plan 产物不在本 Run 冻结输入中",
            details={"expected": str(refs[EDIT_PLAN_KIND]["artifact_revision_id"]),
                     "actual": binding.timeline_artifact_revision_id})


__all__ = [
    "CANCELLED",
    "EDIT_PLAN_KIND",
    "FACT_INVALID",
    "INPUT_INVALID",
    "INPUT_MISSING",
    "SCHEMA_INVALID",
    "TEMPLATE_BIND_KIND",
    "TEMPLATE_NOT_FOUND",
    "EditPlanHandler",
    "TemplateBindHandler",
]
