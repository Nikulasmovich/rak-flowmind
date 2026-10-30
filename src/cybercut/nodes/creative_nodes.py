"""WB-03 创作层节点（内部 NodeHandler，**不注册 FlowMind `@skill`**）。

节点：
    creative.brief       素材分析 -> CreativeBrief（创作简报）
    creative.script      Brief     -> ScriptRevision（脚本版本）
    creative.storyboard  Script    -> StoryboardRevision（智能分镜版本）
    creative.match       Storyboard+AssetSegment -> AssetMatchResult（确定性匹配）

统一约束（ADR-0004）：
- 读取**冻结**的 `input_json.input_artifacts`（重试不漂移）；
- 只写 `NodeContext.attempt_dir`，正式路径由执行器分配；
- 经 `ArtifactSpec` 发布，产物为**确定性序列化**的 JSON（`sort_keys=True`）；
- 支持 `cancel_check`；失败抛稳定错误码；
- 只保存 `DecisionTrace` **摘要**，不保存原始思维链；
- 所有模型输出先经 Pydantic 校验，再经事实/证据校验（`creative.validators`）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from cybercut.contracts.creative import (
    MATCH_MODEL_ID,
    MATCH_PROMPT_VERSION,
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.errors import NodeExecutionError, ValidationError
from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.creative import validators as creative_validators
from cybercut.creative.matching import (
    DEFAULT_SELECTION_THRESHOLD,
    build_match_result,
)
from cybercut.creative.policy import AUTO_POLICY, ApprovalPolicy
from cybercut.creative.prompts import prompt_version
from cybercut.creative.provider import (
    CreativeProvider,
    GenerationRequest,
    ProviderError,
)
from cybercut.creative.traces import step, summarize
from cybercut.storage import creative_revisions, paths
from cybercut.storage.paths import resolve_staged_path

BRIEF_ARTIFACT_KIND = "creative.brief"
SCRIPT_ARTIFACT_KIND = "creative.script"
STORYBOARD_ARTIFACT_KIND = "creative.storyboard"
MATCH_ARTIFACT_KIND = "creative.match"

CANCELLED = "CANCELLED"
INPUT_MISSING = "INPUT_MISSING"
INPUT_INVALID = "INPUT_INVALID"
SCHEMA_INVALID = "CREATIVE_SCHEMA_INVALID"
FACT_UNSUPPORTED = "CREATIVE_FACT_UNSUPPORTED"


# ══════════════════════════════════════════════════════════════════
# 公共工具
# ══════════════════════════════════════════════════════════════════


def _assets(input_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = input_json or {}
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise NodeExecutionError("input_json.assets 非法", error_code=INPUT_INVALID)
    return [a for a in assets if isinstance(a, dict)]


def _input_artifacts(input_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = input_json or {}
    refs = data.get("input_artifacts") or []
    return [r for r in refs if isinstance(r, dict)]


def _refs_by_kind(input_json: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {r["kind"]: r for r in _input_artifacts(input_json) if r.get("kind")}


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
            "创作节点被取消", error_code=CANCELLED,
            details={"node_key": context.node_key},
        )


def _policy(input_json: dict[str, Any] | None) -> ApprovalPolicy:
    data = input_json or {}
    return ApprovalPolicy.from_json(data.get("approval_policy"))


def _creative_input(input_json: dict[str, Any] | None) -> dict[str, Any]:
    data = input_json or {}
    value = data.get("creative_input") or {}
    if not isinstance(value, dict):
        raise NodeExecutionError(
            "input_json.creative_input 非法", error_code=INPUT_INVALID
        )
    return dict(value)


def _frozen_kinds(input_json: dict[str, Any] | None) -> set[str]:
    """当前 Run **冻结输入**中的产物 kind 集合（证据必须来自其中）。"""
    return {str(r["kind"]) for r in _input_artifacts(input_json) if r.get("kind")}


def _revision_request(input_json: dict[str, Any] | None) -> dict[str, Any]:
    """定向修订请求（由控制面随 Run 冻结并下发到新 attempt）。"""
    data = input_json or {}
    value = data.get("revision_request") or {}
    if not isinstance(value, dict):
        raise NodeExecutionError(
            "input_json.revision_request 非法", error_code=INPUT_INVALID
        )
    return dict(value)


def _revision_note(input_json: dict[str, Any] | None) -> str | None:
    """新提示词优先；否则用人工反馈。"""
    request = _revision_request(input_json)
    note = request.get("new_prompt") or request.get("feedback")
    return str(note) if note else None


def _identity(context: NodeContext, kind: str) -> dict[str, Any]:
    """节点侧写出的**临时**身份（执行器发布前用数据库权威值覆盖）。"""
    return creative_revisions.provisional_identity(
        kind, run_id=context.run_id, attempt_no=context.attempt_no
    )


def _analyses(context: NodeContext, input_json: dict[str, Any]
              ) -> list[dict[str, Any]]:
    """按冻结素材顺序读取每个素材的 `AssetAnalysisReport`。"""
    refs = _refs_by_kind(input_json)
    result: list[dict[str, Any]] = []
    for asset in _assets(input_json):
        asset_id = str(asset.get("asset_id", ""))
        ref = refs.get(f"asset.analysis.{asset_id}")
        _require(ref is not None, INPUT_MISSING,
                 f"缺少素材分析产物：asset.analysis.{asset_id}",
                 {"asset_id": asset_id})
        result.append(_read_json(_official(context, ref)))
    _require(bool(result), INPUT_MISSING, "当前 Run 没有可用的素材分析报告")
    return result


def _read_artifact(context: NodeContext, input_json: dict[str, Any],
                   kind: str, model: type) -> Any:
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


def _generate(provider: CreativeProvider, request: GenerationRequest):
    """调用 provider；**任何**失败都在此边界转换为稳定错误码。

    WB-03R §7：`ProviderError` 及其他异常一律转换为
    `NodeExecutionError(error_code="CREATIVE_PROVIDER_FAILED")`，
    不得让 Runner 退化为 `HANDLER_EXCEPTION`。
    """
    from cybercut.creative.provider import provider_model_id

    provider_model_id(provider)
    try:
        return provider.generate(request)
    except NodeExecutionError:
        raise
    except ProviderError as exc:
        raise NodeExecutionError(
            f"创作生成失败：{exc.message}",
            error_code="CREATIVE_PROVIDER_FAILED",
            details={"kind": request.kind, "provider": provider.model_id,
                     "provider_code": exc.code,
                     **({"provider_details": exc.details}
                        if getattr(exc, "details", None) else {})},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 统一映射为稳定错误码
        raise NodeExecutionError(
            f"创作生成失败：{exc}", error_code="CREATIVE_PROVIDER_FAILED",
            details={"kind": request.kind, "provider": provider.model_id},
        ) from exc


def _validate_model(model: type, payload: dict[str, Any], kind: str) -> Any:
    try:
        return model.model_validate(payload)
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            f"模型输出未通过 schema 校验：{kind}", error_code=SCHEMA_INVALID,
            details={"kind": kind, "reason": str(exc)[:300]},
        ) from exc


def _run_fact_checks(check) -> None:
    try:
        check()
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            f"模型输出未通过事实证据校验：{exc}",
            error_code=FACT_UNSUPPORTED,
            details=getattr(exc, "details", None),
        ) from exc


class _CreativeHandlerBase:
    """创作节点公共基类（provider + 审批策略注入）。"""

    node_key = ""

    def __init__(self, *, provider: CreativeProvider | None = None,
                 policy: ApprovalPolicy | None = None) -> None:
        if provider is None:
            from cybercut.creative.fake_provider import FakeCreativeProvider

            provider = FakeCreativeProvider()
        self._provider: CreativeProvider = provider
        self._policy = policy or AUTO_POLICY

    def _effective_policy(self, input_json: dict[str, Any]) -> ApprovalPolicy:
        """Run 冻结的策略优先；缺省用构造时注入的策略。"""
        data = input_json or {}
        if "approval_policy" in data:
            return _policy(data)
        return self._policy

    def _awaiting(self, input_json: dict[str, Any]) -> bool:
        return self._effective_policy(input_json).requires_approval(self.node_key)


# ══════════════════════════════════════════════════════════════════
# creative.brief
# ══════════════════════════════════════════════════════════════════


class CreativeBriefHandler(_CreativeHandlerBase):
    """素材分析 + 用户目标 → CreativeBrief。"""

    node_key = "creative.brief"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        assets = _assets(input_json)
        _require(bool(assets), INPUT_MISSING, "creative.brief 需要一个以上素材")
        analyses = _analyses(context, input_json)
        brief_input = _creative_input(input_json)

        identity = _identity(context, "brief")
        revision_note = _revision_note(input_json)
        request = GenerationRequest(
            kind="brief",
            prompt_version=prompt_version("brief"),
            context={
                "project_id": context.project_id,
                "run_id": context.run_id,
                "revision_id": identity["brief_revision_id"],
                "revision_no": identity["revision_no"],
                "revision_note": revision_note,
                "assets": assets,
                "analyses": analyses,
                "brief_input": brief_input,
            },
            revision_hint=revision_note,
        )
        result = _generate(self._provider, request)
        _check_cancel(context)

        brief: CreativeBrief = _validate_model(
            CreativeBrief, result.payload, BRIEF_ARTIFACT_KIND)
        _run_fact_checks(lambda: creative_validators.validate_brief(
            brief,
            frozen_asset_ids={str(a["asset_id"]) for a in assets},
            analyses=analyses,
            brief_input=brief_input,
            frozen_kinds=_frozen_kinds(input_json),
            expected_project_id=context.project_id,
            expected_run_id=context.run_id,
        ))

        rationale = ("single_source_asset" if len(assets) == 1
                     else "multi_source_assets")
        trace = summarize([step(
            "creative.brief",
            decision="generate_brief",
            rationale_code=rationale,
            inputs={"asset_ids": sorted(a["asset_id"] for a in assets),
                    "objective": brief.objective},
            outputs={"selling_points": len(brief.selling_points),
                     "target_duration_us": brief.target_duration_us},
            evidence_count=len(brief.evidence_refs),
        )])
        brief = brief.model_copy(update={
            "decision_trace": trace,
            "warnings": sorted(set(brief.warnings) | set(result.warnings)),
        })
        spec = _write_json(context, "creative/brief.json",
                           brief.model_dump(mode="json"), BRIEF_ARTIFACT_KIND)
        return NodeResult(
            output={"kind": BRIEF_ARTIFACT_KIND,
                    "selling_points": len(brief.selling_points),
                    "source_asset_ids": brief.source_asset_ids},
            artifacts=[spec], decision_trace=trace, warnings=brief.warnings,
            awaiting_approval=self._awaiting(input_json),
        )


# ══════════════════════════════════════════════════════════════════
# creative.script
# ══════════════════════════════════════════════════════════════════


class CreativeScriptHandler(_CreativeHandlerBase):
    """Brief（+ 修订提示词）→ ScriptRevision。"""

    node_key = "creative.script"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        brief: CreativeBrief = _read_artifact(
            context, input_json, BRIEF_ARTIFACT_KIND, CreativeBrief)
        analyses = _analyses(context, input_json)
        creative_input = _creative_input(input_json)
        script_input = dict(creative_input.get("script_input") or {})
        revision_hint = script_input.get("revision_hint") or \
            creative_input.get("revision_hint")

        identity = _identity(context, "script")
        revision_note = _revision_note(input_json) or revision_hint
        request = GenerationRequest(
            kind="script",
            prompt_version=prompt_version("script"),
            context={
                "project_id": context.project_id,
                "run_id": context.run_id,
                "revision_no": identity["revision_no"],
                # 临时身份：执行器发布前用数据库权威身份覆盖
                "revision_id": identity["script_revision_id"],
                "revision_note": revision_note,
                "brief": brief.model_dump(mode="json"),
                "analyses": analyses,
                "script_input": script_input,
            },
            revision_hint=revision_note,
        )
        result = _generate(self._provider, request)
        _check_cancel(context)

        script: ScriptRevision = _validate_model(
            ScriptRevision, result.payload, SCRIPT_ARTIFACT_KIND)
        _run_fact_checks(lambda: creative_validators.validate_script(
            script, brief=brief, analyses=analyses,
            frozen_kinds=_frozen_kinds(input_json),
            expected_project_id=context.project_id,
            expected_run_id=context.run_id))

        trace = summarize([step(
            "creative.script",
            decision="generate_script",
            rationale_code=("revision_hint_applied" if revision_hint
                            else "deterministic_template"),
            inputs={"brief_schema": brief.schema_version,
                    "core_message_digest": brief.core_message[:32]},
            outputs={"sections": len(script.sections),
                     "total_target_duration_us": script.total_target_duration_us},
            evidence_count=len(script.evidence_refs),
        )])
        script = script.model_copy(update={
            "decision_trace": trace,
            "warnings": sorted(set(script.warnings) | set(result.warnings)),
        })
        spec = _write_json(context, "creative/script.json",
                           script.model_dump(mode="json"), SCRIPT_ARTIFACT_KIND)
        return NodeResult(
            output={"kind": SCRIPT_ARTIFACT_KIND,
                    "sections": len(script.sections),
                    "total_target_duration_us": script.total_target_duration_us},
            artifacts=[spec], decision_trace=trace, warnings=script.warnings,
            awaiting_approval=self._awaiting(input_json),
        )


# ══════════════════════════════════════════════════════════════════
# creative.storyboard
# ══════════════════════════════════════════════════════════════════


class CreativeStoryboardHandler(_CreativeHandlerBase):
    """Script + 素材分析 → StoryboardRevision（只描述意图与素材约束）。"""

    node_key = "creative.storyboard"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        script: ScriptRevision = _read_artifact(
            context, input_json, SCRIPT_ARTIFACT_KIND, ScriptRevision)
        analyses = _analyses(context, input_json)
        brief: CreativeBrief = _read_artifact(
            context, input_json, BRIEF_ARTIFACT_KIND, CreativeBrief)

        request = GenerationRequest(
            kind="storyboard",
            prompt_version=prompt_version("storyboard"),
            context={
                "project_id": context.project_id,
                "run_id": context.run_id,
                "revision_no": _identity(context, "storyboard")["revision_no"],
                "revision_id": _identity(
                    context, "storyboard")["storyboard_revision_id"],
                "revision_note": _revision_note(input_json),
                "brief": brief.model_dump(mode="json"),
                "script": script.model_dump(mode="json"),
                "analyses": analyses,
            },
        )
        result = _generate(self._provider, request)
        _check_cancel(context)

        storyboard: StoryboardRevision = _validate_model(
            StoryboardRevision, result.payload, STORYBOARD_ARTIFACT_KIND)
        _run_fact_checks(lambda: creative_validators.validate_storyboard(
            storyboard, script=script, analyses=analyses,
            frozen_kinds=_frozen_kinds(input_json),
            expected_project_id=context.project_id,
            expected_run_id=context.run_id))

        trace = summarize([step(
            "creative.storyboard",
            decision="generate_storyboard",
            rationale_code="duration_allocated",
            inputs={"sections": len(script.sections),
                    "script_total_us": script.total_target_duration_us},
            outputs={"shots": len(storyboard.shots),
                     "total_target_duration_us":
                         storyboard.total_target_duration_us},
            evidence_count=len(storyboard.evidence_refs),
        )])
        storyboard = storyboard.model_copy(update={
            "decision_trace": trace,
            "warnings": sorted(set(storyboard.warnings) | set(result.warnings)),
        })
        spec = _write_json(context, "creative/storyboard.json",
                           storyboard.model_dump(mode="json"),
                           STORYBOARD_ARTIFACT_KIND)
        return NodeResult(
            output={"kind": STORYBOARD_ARTIFACT_KIND,
                    "shots": len(storyboard.shots),
                    "total_target_duration_us":
                        storyboard.total_target_duration_us},
            artifacts=[spec], decision_trace=trace, warnings=storyboard.warnings,
            awaiting_approval=self._awaiting(input_json),
        )


# ══════════════════════════════════════════════════════════════════
# creative.match
# ══════════════════════════════════════════════════════════════════


class CreativeMatchHandler(_CreativeHandlerBase):
    """Storyboard + AssetSegment → AssetMatchResult（**确定性算法**，不走 provider）。"""

    node_key = "creative.match"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        storyboard: StoryboardRevision = _read_artifact(
            context, input_json, STORYBOARD_ARTIFACT_KIND, StoryboardRevision)
        analyses = _analyses(context, input_json)
        creative_input = _creative_input(input_json)
        threshold = creative_input.get("match_threshold")
        threshold = (DEFAULT_SELECTION_THRESHOLD if threshold is None
                     else float(threshold))
        try:
            identity = _identity(context, "match")
            match: AssetMatchResult = build_match_result(
                revision_id=identity["match_revision_id"],
                project_id=context.project_id,
                run_id=context.run_id,
                revision_no=identity["revision_no"],
                revision_note=_revision_note(input_json),
                storyboard=storyboard.model_dump(mode="json"),
                analyses=analyses,
                assets=_assets(input_json),
                threshold=threshold,
            )
        except (ValidationError, PydanticValidationError, ValueError) as exc:
            raise NodeExecutionError(
                f"素材匹配失败：{exc}", error_code=SCHEMA_INVALID
            ) from exc
        _check_cancel(context)
        _run_fact_checks(lambda: creative_validators.validate_match(
            match, storyboard=storyboard, analyses=analyses,
            expected_project_id=context.project_id,
            expected_run_id=context.run_id))

        unmatched = match.unselected_shot_ids
        trace = summarize([step(
            "creative.match",
            decision="select_segments",
            rationale_code=("threshold_met" if not unmatched
                            else "threshold_missed"),
            inputs={"shots": len(match.shots),
                    "threshold": match.selection_threshold},
            outputs={"selected": len(match.shots) - len(unmatched),
                     "unmatched": unmatched,
                     "algorithm_version": match.algorithm_version},
            evidence_count=len(match.shots),
        )])
        match = match.model_copy(update={
            "decision_trace": trace,
            "prompt_version": MATCH_PROMPT_VERSION,
            "model_id": MATCH_MODEL_ID,
        })
        spec = _write_json(context, "creative/match.json",
                           match.model_dump(mode="json"), MATCH_ARTIFACT_KIND)
        return NodeResult(
            output={"kind": MATCH_ARTIFACT_KIND,
                    "selected": len(match.shots) - len(unmatched),
                    "unmatched_shot_ids": unmatched,
                    "algorithm_version": match.algorithm_version},
            artifacts=[spec], decision_trace=trace, warnings=match.warnings,
            awaiting_approval=self._awaiting(input_json),
        )


__all__ = [
    "BRIEF_ARTIFACT_KIND",
    "CANCELLED",
    "FACT_UNSUPPORTED",
    "INPUT_INVALID",
    "INPUT_MISSING",
    "MATCH_ARTIFACT_KIND",
    "SCHEMA_INVALID",
    "SCRIPT_ARTIFACT_KIND",
    "STORYBOARD_ARTIFACT_KIND",
    "CreativeBriefHandler",
    "CreativeMatchHandler",
    "CreativeScriptHandler",
    "CreativeStoryboardHandler",
]
