"""WB-05 渲染节点（内部 NodeHandler，**不注册 FlowMind `@skill`**）。

节点：
    render.narration   TTS Provider 接口（本版无 TTS → not_available 产物）
    render.composite   FFmpeg 合成（前置校验 accepted / unresolved_shots）
    render.qc          QC 检查 + 封面导出

前置门槛（ADR-0006 §7）：
- `TemplateBinding.accepted == False` → `RENDER_BINDING_NOT_ACCEPTED`
- `unresolved_shots` 非空 → `RENDER_UNRESOLVED_SHOTS`
- 冻结引用交叉验证
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from cybercut.contracts.creative import ScriptRevision
from cybercut.contracts.editing import TemplateBinding, TimelineSpec
from cybercut.contracts.errors import NodeExecutionError, ValidationError
from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.creative.policy import AUTO_POLICY, ApprovalPolicy
from cybercut.media.toolchain import FFmpegToolchain
from cybercut.rendering.ffmpeg_renderer import (
    COMPOSITE_MODEL_ID,
    COMPOSITE_PROMPT_VERSION,
    DEFAULT_FONT_PATH,
    render_spec,
)
from cybercut.rendering.qc import extract_cover, run_qc
from cybercut.storage import paths
from cybercut.storage.paths import resolve_staged_path

NARRATION_KIND = "render.narration"
COMPOSITE_KIND = "render.composite"
QC_KIND = "render.qc"
INTERMEDIATE_MP4_KIND = "render.intermediate.mp4"
FINAL_MP4_KIND = "render.mp4"
COVER_KIND = "render.cover.jpg"

INPUT_MISSING = "INPUT_MISSING"
INPUT_INVALID = "INPUT_INVALID"
RENDER_BINDING_NOT_ACCEPTED = "RENDER_BINDING_NOT_ACCEPTED"
RENDER_UNRESOLVED_SHOTS = "RENDER_UNRESOLVED_SHOTS"
QC_FAILED = "QC_FAILED"
CANCELLED = "CANCELLED"
FFMPEG_PATH_ENV = "CYBERCUT_FFMPEG_PATH"
FFPROBE_PATH_ENV = "CYBERCUT_FFPROBE_PATH"
FONT_PATH_ENV = "CYBERCUT_FONT_PATH"


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
            "渲染节点被取消", error_code=CANCELLED,
            details={"node_key": context.node_key},
        )


def _identity(context: NodeContext, kind: str) -> dict[str, Any]:
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


def _toolchain() -> FFmpegToolchain:
    ffmpeg = os.environ.get(FFMPEG_PATH_ENV, "ffmpeg")
    ffprobe = os.environ.get(FFPROBE_PATH_ENV, "ffprobe")
    return FFmpegToolchain(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)


def _font_path(input_json: dict[str, Any] | None) -> str:
    data = input_json or {}
    ci = data.get("creative_input") or {}
    ei = ci.get("edit_input") or {}
    font = ei.get("font_path") or os.environ.get(FONT_PATH_ENV) or DEFAULT_FONT_PATH
    return str(font)


def _read_binding(context: NodeContext, input_json: dict[str, Any]) -> TemplateBinding:
    refs = _refs_by_kind(input_json)
    ref = refs.get("template.bind")
    _require(ref is not None, INPUT_MISSING,
             "缺少上游产物：template.bind", {"kind": "template.bind"})
    payload = _read_json(_official(context, ref))
    try:
        return TemplateBinding.model_validate(payload)
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            "上游产物不符合 schema：template.bind", error_code=INPUT_INVALID,
            details={"reason": str(exc)[:300]},
        ) from exc


def _read_spec(context: NodeContext, input_json: dict[str, Any]) -> TimelineSpec:
    refs = _refs_by_kind(input_json)
    ref = refs.get("edit.plan")
    _require(ref is not None, INPUT_MISSING,
             "缺少上游产物：edit.plan", {"kind": "edit.plan"})
    payload = _read_json(_official(context, ref))
    try:
        return TimelineSpec.model_validate(payload)
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            "上游产物不符合 schema：edit.plan", error_code=INPUT_INVALID,
            details={"reason": str(exc)[:300]},
        ) from exc


def _read_script(context: NodeContext, input_json: dict[str, Any]) -> ScriptRevision:
    refs = _refs_by_kind(input_json)
    ref = refs.get("creative.script")
    _require(ref is not None, INPUT_MISSING,
             "缺少上游产物：creative.script", {"kind": "creative.script"})
    payload = _read_json(_official(context, ref))
    try:
        return ScriptRevision.model_validate(payload)
    except (ValidationError, PydanticValidationError) as exc:
        raise NodeExecutionError(
            "上游产物不符合 schema：creative.script", error_code=INPUT_INVALID,
            details={"reason": str(exc)[:300]},
        ) from exc


def _check_gates(binding: TemplateBinding, spec: TimelineSpec) -> None:
    """前置门槛：accepted + unresolved_shots。"""
    if not binding.accepted:
        unmet_summary = [
            f"{u.code}:{u.subject}" for u in binding.unmet
        ]
        raise NodeExecutionError(
            "TemplateBinding.accepted 为 False，禁止渲染",
            error_code=RENDER_BINDING_NOT_ACCEPTED,
            details={"unmet": unmet_summary},
        )
    if spec.unresolved_shots:
        shot_ids = [u.shot_id for u in spec.unresolved_shots]
        raise NodeExecutionError(
            f"TimelineSpec 存在 {len(shot_ids)} 个未匹配镜头，阻止交付成片",
            error_code=RENDER_UNRESOLVED_SHOTS,
            details={"unresolved_shot_ids": shot_ids},
        )


def _source_paths(spec: TimelineSpec, context: NodeContext) -> dict[str, str]:
    """从冻结 assets 读取源素材绝对路径。"""
    data = context.data_root / "projects" / context.project_id
    result: dict[str, str] = {}
    for clip in spec.clips:
        asset_id = clip.asset_id
        if asset_id in result:
            continue
        # 源素材标准化路径由 WB-02 生成
        asset_dir = data / "assets" / asset_id
        for sub in ("normalized", "original"):
            candidate = asset_dir / sub
            if candidate.is_dir():
                for f in sorted(candidate.iterdir()):
                    if f.suffix in (".mp4", ".png", ".jpg"):
                        result[asset_id] = str(f)
                        break
            if asset_id in result:
                break
    return result


class _RenderHandlerBase:
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
# render.narration
# ══════════════════════════════════════════════════════════════════


class RenderNarrationHandler(_RenderHandlerBase):
    """TTS Provider 接口（本版无 TTS 凭证 → not_available 产物 + 告警）。"""

    node_key = "render.narration"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        binding = _read_binding(context, input_json)
        _check_gates(binding, _read_spec(context, input_json))

        identity = _identity(context, "narration")
        narration_payload = {
            "narration_revision_id": identity["narration_revision_id"],
            "revision_no": identity["revision_no"],
            "status": "not_available",
            "reason": "TTS 凭证未配置（D3=B），本版不生成旁白音频",
            "audio_paths": [],
            "config_snapshot": {},
            "warnings": ["NARRATION_NOT_AVAILABLE"],
        }
        artifact = _write_json(context, "render/narration.json",
                               narration_payload, NARRATION_KIND)
        return NodeResult(
            output={"kind": NARRATION_KIND, "status": "not_available"},
            artifacts=[artifact],
            warnings=["NARRATION_NOT_AVAILABLE"],
            awaiting_approval=self._awaiting(input_json),
        )


# ══════════════════════════════════════════════════════════════════
# render.composite
# ══════════════════════════════════════════════════════════════════


class RenderCompositeHandler(_RenderHandlerBase):
    """FFmpeg 合成（画面 + 裁切/画幅适配 + 转场 + 字幕烧录）。"""

    node_key = "render.composite"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        binding = _read_binding(context, input_json)
        spec = _read_spec(context, input_json)
        _check_gates(binding, spec)

        identity = _identity(context, "composite")
        refs = _refs_by_kind(input_json)
        font = _font_path(input_json)
        chain = _toolchain()
        source_paths = _source_paths(spec, context)
        output_dir = context.attempt_dir / "render" / "composite"
        _check_cancel(context)

        render_result = render_spec(
            spec=spec,
            source_paths=source_paths,
            output_dir=output_dir,
            toolchain=chain,
            font_path=font,
            cancel_check=context.cancel_check,
        )
        _check_cancel(context)

        composite_payload = {
            "render_composite_revision_id":
                identity["composite_revision_id"],
            "revision_no": identity["revision_no"],
            "project_id": context.project_id,
            "run_id": context.run_id,
            "timeline_artifact_revision_id":
                refs.get("edit.plan", {}).get("artifact_revision_id", ""),
            "template_bind_artifact_revision_id":
                refs.get("template.bind", {}).get("artifact_revision_id", ""),
            "template_id": binding.template_id,
            "template_version": binding.template_version,
            "renderer_id": "ffmpeg",
            "renderer_version": "9.0.2",
            "output_artifact_kind": INTERMEDIATE_MP4_KIND,
            "output_format": "mp4",
            "width": 1080, "height": 1920,
            "frame_rate_num": spec.frame_rate.num,
            "frame_rate_den": spec.frame_rate.den,
            "total_frames": spec.total_frames,
            "duration_us": spec.playback_duration_us,
            "narration": {
                "status": "not_available",
                "reason": "TTS 凭证未配置（D3=B）",
                "audio_paths": [], "config_snapshot": {}, "warnings": [],
            },
            "bgm_status": "not_provided",
            "clip_count": len(spec.clips),
            "unresolved_shot_count": len(spec.unresolved_shots),
            "prompt_version": COMPOSITE_PROMPT_VERSION,
            "model_id": COMPOSITE_MODEL_ID,
            "revision_note": _revision_note(input_json),
            "warnings": render_result["warnings"],
            "decision_trace": [{
                "step": "render.composite",
                "decision": "ffmpeg_composite",
                "rationale_code": "deterministic_template",
                "evidence_count": len(spec.clips),
                "notes": [f"clips={len(spec.clips)}",
                          f"output_kind={INTERMEDIATE_MP4_KIND}"],
            }],
        }
        artifact = _write_json(context, "render/composite.json",
                               composite_payload, COMPOSITE_KIND)
        # 合成文件仅作待 QC 的内部中间产物；正式交付由 render.qc 发布。
        mp4_artifact = ArtifactSpec(
            kind=INTERMEDIATE_MP4_KIND,
            staged_relative_path=str(
                Path(render_result["output_path"]).relative_to(
                    context.attempt_dir)),
        )
        return NodeResult(
            output={"kind": COMPOSITE_KIND,
                    "output_artifact_kind": INTERMEDIATE_MP4_KIND},
            artifacts=[artifact, mp4_artifact],
            decision_trace=composite_payload["decision_trace"],
            warnings=list(composite_payload["warnings"]),
            awaiting_approval=self._awaiting(input_json),
        )


# ══════════════════════════════════════════════════════════════════
# render.qc
# ══════════════════════════════════════════════════════════════════


class RenderQcHandler(_RenderHandlerBase):
    """QC 检查 + 封面导出。QC 失败 ⇒ 不交付（节点失败）。"""

    node_key = "render.qc"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        _check_cancel(context)
        refs = _refs_by_kind(input_json)
        composite_ref = refs.get(COMPOSITE_KIND)
        _require(composite_ref is not None, INPUT_MISSING,
                 "缺少上游产物：render.composite", {"kind": COMPOSITE_KIND})
        composite_payload = _read_json(_official(context, composite_ref))
        intermediate_ref = refs.get(INTERMEDIATE_MP4_KIND)
        _require(intermediate_ref is not None, INPUT_MISSING,
                 "缺少待质检的视频中间产物",
                 {"kind": INTERMEDIATE_MP4_KIND})
        _require(
            composite_payload.get("output_artifact_kind") == INTERMEDIATE_MP4_KIND,
            INPUT_INVALID,
            "合成产物未声明正确的待质检视频类型",
            {"expected_kind": INTERMEDIATE_MP4_KIND},
        )
        spec = _read_spec(context, input_json)
        _check_cancel(context)

        chain = _toolchain()
        output_path = str(_official(context, intermediate_ref))
        _require(Path(output_path).exists(), INPUT_MISSING,
                 f"渲染产物不存在：{output_path}", {"path": output_path})

        qc_result = run_qc(toolchain=chain, output_path=output_path, spec=spec)
        _check_cancel(context)

        identity = _identity(context, "qc")
        final_video = context.attempt_dir / "render" / "output.mp4"
        final_video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output_path, final_video)
        cover_path = str(context.attempt_dir / "render" / "cover.jpg")
        extract_cover(toolchain=chain, video_path=output_path,
                      output_path=cover_path, cancel_check=context.cancel_check)

        qc_payload = {
            "qc_revision_id": identity["qc_revision_id"],
            "revision_no": identity["revision_no"],
            "project_id": context.project_id,
            "run_id": context.run_id,
            "composite_artifact_revision_id":
                str(composite_ref["artifact_revision_id"]),
            "output_artifact_kind": FINAL_MP4_KIND,
            **{k: qc_result[k] for k in (
                "width", "height", "frame_rate_num", "frame_rate_den",
                "duration_us", "has_video", "has_audio",
                "video_codec", "audio_codec", "checks", "all_passed")},
            "cover_artifact_kind": COVER_KIND,
            "prompt_version": "render-qc.v1",
            "model_id": "deterministic:render-qc-v1",
            "warnings": [c["detail"] for c in qc_result["checks"]
                         if not c["passed"]],
            "decision_trace": [{
                "step": "render.qc",
                "decision": "run_qc",
                "rationale_code": "deterministic_template",
                "evidence_count": len(qc_result["checks"]),
                "notes": [f"all_passed={qc_result['all_passed']}",
                          f"checks={len(qc_result['checks'])}"],
            }],
        }
        if not qc_result["all_passed"]:
            failed = [c["check_id"] for c in qc_result["checks"] if not c["passed"]]
            raise NodeExecutionError(
                f"QC 检查未通过：{failed}",
                error_code=QC_FAILED,
                details={"failed_checks": failed, "qc": qc_payload},
            )
        artifact = _write_json(context, "render/qc.json",
                               qc_payload, QC_KIND)
        final_artifact = ArtifactSpec(
            kind=FINAL_MP4_KIND,
            staged_relative_path=str(final_video.relative_to(context.attempt_dir)),
        )
        cover_artifact = ArtifactSpec(
            kind=COVER_KIND,
            staged_relative_path=str(Path(cover_path).relative_to(context.attempt_dir)),
        )
        return NodeResult(
            output={"kind": QC_KIND, "all_passed": True,
                    "output_artifact_kind": FINAL_MP4_KIND},
            artifacts=[artifact, final_artifact, cover_artifact],
            decision_trace=qc_payload["decision_trace"],
            awaiting_approval=self._awaiting(input_json),
        )


__all__ = [
    "CANCELLED",
    "COMPOSITE_KIND",
    "NARRATION_KIND",
    "QC_FAILED",
    "QC_KIND",
    "RENDER_BINDING_NOT_ACCEPTED",
    "RENDER_UNRESOLVED_SHOTS",
    "RenderCompositeHandler",
    "RenderNarrationHandler",
    "RenderQcHandler",
]
