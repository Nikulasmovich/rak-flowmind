"""WB-02 素材分析工作流节点（asset-analysis-v1）。

节点：
  asset_probe        读原始素材 -> ffprobe 结构化报告
  asset_normalize    视频 → 代理 MP4（H.264/yuv420p/CFR30/AAC 48k）；图片 → PNG
  scene_detect       PySceneDetect 镜头切分 + 每镜头中点关键帧
  speech_transcribe  ASR（无音轨 → 空 transcript + NO_AUDIO_STREAM 警告）
  asset_analysis     仅合并确定性媒体事实（不调用 LLM）

输入冻结（WB-02 §三）：
  root 节点：`input_json.asset_ids` / `input_json.assets`
  下游节点：`input_json.input_artifacts`（上游 ArtifactRevision 固化引用）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cybercut.contracts.errors import NodeExecutionError
from cybercut.contracts.media import AssetSegment, MediaProbeReport
from cybercut.contracts.node_handler import ArtifactSpec, NodeContext, NodeResult
from cybercut.media import probe as probe_mod
from cybercut.media.asr import AsrDependencyMissing
from cybercut.media.scenes import SceneDetectDependencyMissing
from cybercut.media.toolchain import FFmpegToolchain, MediaToolError
from cybercut.storage import paths
from cybercut.storage.paths import resolve_staged_path

PROXY_SUFFIX = ".proxy.mp4"
IMAGE_PROXY_SUFFIX = ".proxy.png"
KEYFRAME_SUFFIX = ".keyframe.jpg"
TRANSCRIPT_SUFFIX = ".transcript.json"
SCENES_SUFFIX = ".scenes.json"
PROBE_SUFFIX = ".probe.json"
ANALYSIS_SUFFIX = ".analysis.json"


def _assets(input_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = input_json or {}
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        raise NodeExecutionError("input_json.assets 非法", error_code="INPUT_INVALID")
    return list(assets)


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


def _write_json(context: NodeContext, staged_name: str, payload: Any,
                kind: str) -> ArtifactSpec:
    from cybercut.storage.paths import resolve_staged_path

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


# ══════════════════════════════════════════════════════════════════
# asset_probe
# ══════════════════════════════════════════════════════════════════


class AssetProbeHandler:
    """对 run 中冻结的全部素材逐个 ffprobe，发布结构化探测报告。"""

    node_key = "asset_probe"

    def __init__(self, *, toolchain: FFmpegToolchain | None = None) -> None:
        self._toolchain = toolchain

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        chain = self._toolchain or FFmpegToolchain()
        specs: list[ArtifactSpec] = []
        for asset in _assets(input_json):
            asset_id = asset["asset_id"]
            source = paths.resolve_official_asset_path(
                context.project_root, asset["source_relative_path"]
            )
            try:
                data = chain.ffprobe_json(source, cancel_check=context.cancel_check)
            except MediaToolError as exc:
                raise NodeExecutionError(
                    f"ffprobe 失败：{exc.message}", error_code=exc.code,
                    details={"asset_id": asset_id, "stderr": exc.stderr},
                ) from exc
            report = probe_mod.build_probe_report(
                data, media_kind=asset["media_kind"],
                source_size_bytes=int(asset["size_bytes"]),
            )
            specs.append(_write_json(
                context, f"probe/{asset_id}{PROBE_SUFFIX}",
                report.model_dump(mode="json"), kind=f"asset.probe.{asset_id}",
            ))
        return NodeResult(
            output={"probed": len(specs)}, artifacts=specs,
            decision_trace=[{"step": "asset_probe", "assets": len(specs)}],
        )


# ══════════════════════════════════════════════════════════════════
# asset_normalize
# ══════════════════════════════════════════════════════════════════

VIDEO_SCALE = "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"
IMAGE_SCALE = "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=1,setsar=1"


class AssetNormalizeHandler:
    """视频 → 代理 MP4（H.264/yuv420p/CFR30/AAC 48k 双声道）；图片 → PNG。"""

    node_key = "asset_normalize"

    def __init__(self, *, toolchain: FFmpegToolchain | None = None) -> None:
        self._toolchain = toolchain

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        chain = self._toolchain or FFmpegToolchain()
        specs: list[ArtifactSpec] = []
        for asset in _assets(input_json):
            asset_id = asset["asset_id"]
            # WB-02R §一.3/§一.4：从素材的 source_relative_path 读取**原素材**；
            # probe JSON 仅用于媒体事实，绝不作为 FFmpeg 输入。
            source = paths.resolve_official_asset_path(
                context.project_root, asset["source_relative_path"]
            )
            _require(source.is_file(), "ASSET_FILE_MISSING",
                     f"素材原始文件缺失：{asset['source_relative_path']}",
                     {"asset_id": asset_id})
            is_video = asset["media_kind"] == "video"
            staged_rel = (f"proxy/{asset_id}{PROXY_SUFFIX}"
                          if is_video else
                          f"proxy/{asset_id}{IMAGE_PROXY_SUFFIX}")
            dest = resolve_staged_path(context.attempt_dir, staged_rel)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if asset["media_kind"] == "video":
                args = [
                    "-i", source,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-r", "30", "-fps_mode", "cfr",
                    "-vf", VIDEO_SCALE,
                    "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart",
                    str(dest),
                ]
            else:
                args = [
                    "-i", source, "-map", "0:v:0",
                    "-vf", IMAGE_SCALE, "-frames:v", "1", str(dest),
                ]
            try:
                chain.run_ffmpeg(args, cancel_check=context.cancel_check)
            except MediaToolError as exc:
                raise NodeExecutionError(
                    f"标准化失败：{exc.message}", error_code=exc.code,
                    details={"asset_id": asset_id, "stderr": exc.stderr},
                ) from exc
            specs.append(
                ArtifactSpec(kind=f"asset.proxy.{asset_id}",
                             staged_relative_path=staged_rel,
                             metadata={"media_kind": asset["media_kind"]})
            )
        return NodeResult(
            output={"normalized": len(specs)}, artifacts=specs,
            decision_trace=[{"step": "asset_normalize", "assets": len(specs)}],
        )


# ══════════════════════════════════════════════════════════════════
# scene_detect
# ══════════════════════════════════════════════════════════════════


class SceneDetectHandler:
    """镜头切分（PySceneDetect）+ 每视频镜头提取一张中点关键帧。"""

    node_key = "scene_detect"

    def __init__(self, *, detector: Any = None,
                 toolchain: FFmpegToolchain | None = None) -> None:
        if detector is None:
            from cybercut.media.scenes import PySceneDetectAdapter

            detector = PySceneDetectAdapter()
        self._detector = detector
        self._toolchain = toolchain

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        chain = self._toolchain or FFmpegToolchain()
        refs = _refs_by_kind(input_json)
        assets = {a["asset_id"]: a for a in _assets(input_json)}
        specs: list[ArtifactSpec] = []

        for asset_id, asset in sorted(assets.items()):
            proxy_ref = refs.get(f"asset.proxy.{asset_id}")
            _require(proxy_ref is not None, "INPUT_MISSING",
                     f"缺少上游代理产物：asset.proxy.{asset_id}", {"asset_id": asset_id})
            proxy = _official(context, proxy_ref)

            try:
                # WB-02R5 §二：代理探测同样必须接入协作式取消
                probe_data = chain.ffprobe_json(
                    proxy, cancel_check=context.cancel_check
                )
                duration_us = probe_mod.build_probe_report(
                    probe_data, media_kind=asset["media_kind"]
                ).duration_us
            except MediaToolError as exc:
                raise NodeExecutionError(
                    f"代理文件探测失败：{exc.message}", error_code=exc.code,
                    details={"asset_id": asset_id},
                ) from exc

            if asset["media_kind"] == "video":
                try:
                    raw = self._detector.detect_segments(
                        proxy, duration_us=duration_us
                    )
                except SceneDetectDependencyMissing as exc:
                    raise NodeExecutionError(
                        f"镜头切分依赖不可用：{exc}",
                        error_code=exc.code,
                        details={"asset_id": asset_id},
                    ) from exc
                segments = [
                    AssetSegment(segment_index=i, start_us=s, end_us=e,
                                 is_time_based=True,
                                 source="scene_detect")
                    for i, (s, e) in enumerate(raw)
                ]
                for i, seg in enumerate(segments):
                    mid_us = (seg.start_us + seg.end_us) // 2
                    staged_rel = f"keyframes/{asset_id}/scene-{i:04d}{KEYFRAME_SUFFIX}"
                    dest = resolve_staged_path(context.attempt_dir, staged_rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        chain.run_ffmpeg([
                            "-ss", f"{mid_us / 1_000_000:.6f}", "-i", proxy,
                            "-frames:v", "1", "-vf", "setsar=1", str(dest),
                        ], cancel_check=context.cancel_check)
                    except MediaToolError as exc:
                        raise NodeExecutionError(
                            f"关键帧提取失败：{exc.message}", error_code=exc.code,
                            details={"asset_id": asset_id, "segment": i},
                        ) from exc
                    specs.append(ArtifactSpec(
                        kind=f"asset.keyframe.{asset_id}.{i:04d}",
                        staged_relative_path=staged_rel,
                        metadata={"asset_id": asset_id, "segment_index": i},
                    ))
            else:
                segments = [AssetSegment(segment_index=0, start_us=0, end_us=0,
                                         is_time_based=False,
                                         source="single_frame")]

            payload = {
                "asset_id": asset_id,
                "media_kind": asset["media_kind"],
                "duration_us": duration_us,
                "segments": [s.model_dump(mode="json") for s in segments],
                "detector": getattr(self._detector, "backend", "unknown"),
            }
            specs.append(_write_json(
                context, f"scenes/{asset_id}{SCENES_SUFFIX}", payload,
                kind=f"asset.scenes.{asset_id}",
            ))

        return NodeResult(
            output={"scene_assets": len(assets)}, artifacts=specs,
            decision_trace=[{"step": "scene_detect", "assets": len(assets)}],
        )


# ══════════════════════════════════════════════════════════════════
# speech_transcribe
# ══════════════════════════════════════════════════════════════════


class SpeechTranscribeHandler:
    """ASR 转写：无音轨 → 空 transcript + NO_AUDIO_STREAM 警告。"""

    node_key = "speech_transcribe"

    def __init__(self, *, asr_adapter: Any = None,
                 toolchain: FFmpegToolchain | None = None,
                 require_asr: bool = True) -> None:
        self._asr = asr_adapter
        self._toolchain = toolchain
        self._require_asr = require_asr

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        chain = self._toolchain or FFmpegToolchain()
        refs = _refs_by_kind(input_json)
        assets = {a["asset_id"]: a for a in _assets(input_json)}
        specs: list[ArtifactSpec] = []
        warnings: list[str] = []

        for asset_id in sorted(assets):
            proxy_ref = refs.get(f"asset.proxy.{asset_id}")
            _require(proxy_ref is not None, "INPUT_MISSING",
                     f"缺少上游代理产物：asset.proxy.{asset_id}", {"asset_id": asset_id})
            proxy = _official(context, proxy_ref)

            try:
                probe_data = chain.ffprobe_json(proxy, cancel_check=context.cancel_check)
            except MediaToolError as exc:
                raise NodeExecutionError(
                    f"代理文件探测失败：{exc.message}", error_code=exc.code,
                    details={"asset_id": asset_id},
                ) from exc
            if not probe_mod.has_audio_stream(probe_data):
                warnings.append(f"NO_AUDIO_STREAM:{asset_id}")
                specs.append(_write_json(
                    context, f"asr/{asset_id}{TRANSCRIPT_SUFFIX}",
                    {"asset_id": asset_id, "backend": "none", "model": None,
                     "language": None, "segments": [],
                     "warnings": ["NO_AUDIO_STREAM"]},
                    kind=f"asset.asr.{asset_id}",
                ))
                continue

            if self._asr is None:
                raise NodeExecutionError(
                    "素材需要 ASR 但未配置 ASR 适配器", error_code="ASR_NOT_CONFIGURED",
                    details={"asset_id": asset_id},
                )
            try:
                segments = self._asr.transcribe(proxy, duration_us=None)
            except AsrDependencyMissing as exc:
                # WB-02R §四.6：稳定错误码，不得降级为 HANDLER_EXCEPTION
                raise NodeExecutionError(
                    f"ASR 依赖不可用：{exc}", error_code=exc.code,
                    details={"asset_id": asset_id},
                ) from exc

            payload = {
                "asset_id": asset_id,
                "backend": getattr(self._asr, "backend", "unknown"),
                "model": getattr(self._asr, "model", None),
                "language": getattr(self._asr, "language", None),
                "segments": [s.model_dump(mode="json") for s in segments],
                "warnings": [],
            }
            specs.append(_write_json(
                context, f"asr/{asset_id}{TRANSCRIPT_SUFFIX}", payload,
                kind=f"asset.asr.{asset_id}",
            ))

        return NodeResult(
            output={"transcribed": len(assets), "warnings": warnings},
            artifacts=specs, warnings=warnings,
            decision_trace=[{"step": "speech_transcribe", "assets": len(assets)}],
        )


# ══════════════════════════════════════════════════════════════════
# asset_analysis
# ══════════════════════════════════════════════════════════════════


class AssetAnalysisHandler:
    """仅合并确定性媒体事实（probe/镜头/关键帧/ASR），不调用 LLM。"""

    node_key = "asset_analysis"

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        refs = _refs_by_kind(input_json)
        assets = {a["asset_id"]: a for a in _assets(input_json)}
        specs: list[ArtifactSpec] = []

        for asset_id in sorted(assets):
            probe_ref = refs.get(f"asset.probe.{asset_id}")
            scenes_ref = refs.get(f"asset.scenes.{asset_id}")
            asr_ref = refs.get(f"asset.asr.{asset_id}")
            _require(probe_ref is not None and scenes_ref is not None
                     and asr_ref is not None, "INPUT_MISSING",
                     f"缺少上游产物（probe/scenes/asr）：{asset_id}",
                     {"asset_id": asset_id})

            probe = _read_json(_official(context, probe_ref), MediaProbeReport)
            scenes_payload = _read_json(_official(context, scenes_ref), dict)
            asr_payload = _read_json(_official(context, asr_ref), dict)

            keyframe_ids = {
                ref["kind"].rsplit(".", 1)[-1]: ref["artifact_revision_id"]
                for ref in _input_artifacts(input_json)
                if ref["kind"].startswith(f"asset.keyframe.{asset_id}.")
            }
            segments = []
            for raw in scenes_payload.get("segments", []):
                seg = AssetSegment.model_validate(raw)
                if seg.is_time_based:
                    seg = seg.model_copy(update={
                        "keyframe_artifact_revision_id": keyframe_ids.get(
                            f"{seg.segment_index:04d}"
                        )
                    })
                segments.append(seg)

            transcript = [
                _require_transcript(item) for item in asr_payload.get("segments", [])
            ]
            report = {
                "schema_version": "asset-analysis.v1",
                "asset_id": asset_id,
                "media_kind": assets[asset_id]["media_kind"],
                "duration_us": scenes_payload.get("duration_us")
                or probe.duration_us,
                "probe": probe.model_dump(mode="json"),
                "segments": [s.model_dump(mode="json") for s in segments],
                "transcript": [t.model_dump(mode="json") for t in transcript],
                "keyframe_artifact_revision_ids": [
                    ref["artifact_revision_id"]
                    for ref in _input_artifacts(input_json)
                    if ref["kind"].startswith(f"asset.keyframe.{asset_id}.")
                ],
                "warnings": list(asr_payload.get("warnings", [])),
            }
            specs.append(_write_json(
                context, f"analysis/{asset_id}{ANALYSIS_SUFFIX}", report,
                kind=f"asset.analysis.{asset_id}",
            ))

        return NodeResult(
            output={"analysed": len(assets)}, artifacts=specs,
            decision_trace=[{"step": "asset_analysis", "assets": len(assets)}],
        )


def _read_json(path: Path, model: type | None) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NodeExecutionError(
            f"上游产物读取失败：{exc}", error_code="INPUT_INVALID"
        ) from exc
    if model is not None and hasattr(model, "model_validate"):
        return model.model_validate(data)
    return data


def _require_transcript(item: dict[str, Any]):
    from cybercut.contracts.media import TranscriptSegment

    return TranscriptSegment.model_validate(item)


__all__ = [
    "AssetAnalysisHandler",
    "AssetNormalizeHandler",
    "AssetProbeHandler",
    "SceneDetectHandler",
    "SpeechTranscribeHandler",
]
