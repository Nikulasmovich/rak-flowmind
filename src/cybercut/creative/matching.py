"""确定性素材匹配（WB-03 §一.4 / §四）。

契约：
- **纯确定性**：同一输入 + 同一 `algorithm_version` 必须得到**同一结果**
  （候选排序、打分明细、淘汰原因全部可复现）；
- 不允许 LLM 自由选择：本模块不依赖任何 provider；
- 禁止越界、重复使用冲突素材、引用不存在的 segment；
- 低于阈值时返回 warning（`MATCH_BELOW_THRESHOLD`）并置 `selected_segment_id=None`，
  **不得静默伪造**选中结果。

Segment 身份：`AssetSegment` 无独立主键，故以
`f"{asset_id}#{segment_index:04d}"` 作为 Run 内**唯一**的 segment 标识。
"""
from __future__ import annotations

import re
from typing import Any

from cybercut.contracts.creative import (
    MATCH_ALGORITHM_VERSION,
    AssetMatchResult,
    MatchCandidate,
    RejectedCandidate,
    ScoreBreakdown,
    ShotMatch,
)

SEGMENT_ID_SEP = "#"
_SEGMENT_ID_RE = re.compile(r"^(?P<asset>[^#]+)#(?P<idx>\d{4,})$")

#: 打分为加权和；正权之和为 1.0，`repetition` 为**扣减项**（最后裁剪到 [0, 1]）
WEIGHTS: dict[str, float] = {
    "semantic": 0.35,
    "quality": 0.15,
    "duration": 0.20,
    "continuity": 0.10,
    "evidence": 0.20,
    "repetition": 0.50,
}

DEFAULT_SELECTION_THRESHOLD = 0.55

#: 连续性：同一 section 且同一素材 / 同一 section 换素材 / 跨 section
_CONTINUITY_SAME_ASSET = 1.0
_CONTINUITY_SAME_SECTION = 0.7
_CONTINUITY_CROSS_SECTION = 0.5
#: 非时间型 segment（单帧图片）没有自然时长，给中性分并记录 warning
_IMAGE_DURATION_SCORE = 0.5
#: shot 无证据引用时的中性证据分
_NEUTRAL_EVIDENCE_SCORE = 0.5

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def segment_id(asset_id: str, segment_index: int) -> str:
    """Run 内唯一的 segment 标识。"""
    if not asset_id:
        raise ValueError("asset_id 不能为空")
    return f"{asset_id}{SEGMENT_ID_SEP}{int(segment_index):04d}"


def parse_segment_id(value: str) -> tuple[str, int]:
    match = _SEGMENT_ID_RE.match(value or "")
    if match is None:
        raise ValueError(f"非法 segment_id：{value!r}")
    return match.group("asset"), int(match.group("idx"))


def tokenize(text: str) -> list[str]:
    """确定性分词：ASCII 词（≥2 字符）+ 中文二元组。"""
    lowered = (text or "").lower()
    tokens = _ASCII_TOKEN_RE.findall(lowered)
    chars = _CJK_RE.findall(lowered)
    tokens.extend("".join(pair) for pair in zip(chars, chars[1:]))
    return tokens


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def round6(value: float) -> float:
    """统一舍入（保证跨进程可复现的十进制表示）。"""
    return round(clamp01(value), 6)


def total_score(breakdown: ScoreBreakdown) -> float:
    """加权总分（`ScoreBreakdown.total` 的实现）。"""
    positive = (
        WEIGHTS["semantic"] * breakdown.semantic_score
        + WEIGHTS["quality"] * breakdown.quality_score
        + WEIGHTS["duration"] * breakdown.duration_score
        + WEIGHTS["continuity"] * breakdown.continuity_score
        + WEIGHTS["evidence"] * breakdown.evidence_score
    )
    penalty = WEIGHTS["repetition"] * breakdown.repetition_penalty
    return round6(positive - penalty)


class RunSegment:
    """Run 内可用于匹配的素材分段（由 `iter_run_segments` 构造）。"""

    __slots__ = ("segment_id", "asset_id", "segment_index", "start_us", "end_us",
                 "is_time_based", "keyframe_artifact_revision_id", "source",
                 "width", "height", "pix_fmt", "has_audio", "transcript_text",
                 "duration_us", "extra_keywords", "_pool")

    def __init__(self, asset_id: str, segment: dict, probe: dict,
                 transcript_text: str, extra_keywords: set[str] | None = None) -> None:
        self.asset_id = asset_id
        self.segment_index = int(segment.get("segment_index", 0))
        self.segment_id = segment_id(asset_id, self.segment_index)
        self.start_us = int(segment.get("start_us", 0))
        self.end_us = int(segment.get("end_us", 0))
        self.is_time_based = bool(segment.get("is_time_based", False))
        self.keyframe_artifact_revision_id = segment.get(
            "keyframe_artifact_revision_id")
        self.source = str(segment.get("source", ""))
        self.duration_us = max(0, self.end_us - self.start_us)
        video = (probe or {}).get("video") or {}
        self.width = int(video.get("width") or 0)
        self.height = int(video.get("height") or 0)
        self.pix_fmt = video.get("pix_fmt")
        self.has_audio = bool((probe or {}).get("has_audio"))
        self.transcript_text = transcript_text or ""
        self.extra_keywords = set(extra_keywords or ())
        self._pool: set[str] | None = None

    @property
    def keyword_pool(self) -> set[str]:
        """候选素材的关键词池（素材文件名 + 该段时间范围内的转写文本）。"""
        if self._pool is None:
            self._pool = set(tokenize(self.transcript_text)) | self.extra_keywords
        return self._pool


def _stem_of(filename: str) -> str:
    name = str(filename or "")
    return name.rsplit(".", 1)[0] if "." in name else name


def iter_run_segments(
    analyses: list[dict], assets: list[dict] | None = None
) -> dict[str, RunSegment]:
    """把 Run 冻结的 `AssetAnalysisReport` 展开为 `{segment_id: RunSegment}`。

    `assets` 可选：传入冻结素材后，素材**文件名**也会进入候选关键词池
    （文件名是真实事实，可提升语义匹配的可解释性）。
    """
    stems: dict[str, set[str]] = {}
    for asset in assets or []:
        asset_id = str(asset.get("asset_id") or "")
        if asset_id:
            stems[asset_id] = set(tokenize(_stem_of(asset.get("original_filename", ""))))

    result: dict[str, RunSegment] = {}
    for analysis in analyses or []:
        asset_id = str(analysis.get("asset_id") or "")
        if not asset_id:
            continue
        probe = analysis.get("probe") or {}
        transcript = analysis.get("transcript") or []
        for segment in analysis.get("segments") or []:
            start = int(segment.get("start_us", 0))
            end = int(segment.get("end_us", 0))
            text = " ".join(
                str(item.get("text", "")) for item in transcript
                if start <= int(item.get("start_us", 0)) < max(end, start + 1)
            )
            item = RunSegment(asset_id, segment, probe, text,
                              extra_keywords=stems.get(asset_id))
            result[item.segment_id] = item
    return result


# ── 打分 ──────────────────────────────────────────────────────────


def _semantic_score(shot: dict, segment: RunSegment) -> float:
    shot_tokens = set()
    for field in ("intent", "subject", "visual_requirement", "emotion"):
        shot_tokens |= set(tokenize(str(shot.get(field, ""))))
    if not shot_tokens:
        return 0.0
    overlap = shot_tokens & segment.keyword_pool
    return round6(len(overlap) / len(shot_tokens))


def _quality_score(segment: RunSegment) -> float:
    pixels = segment.width * segment.height
    base = min(1.0, pixels / (1280 * 720)) if pixels else 0.0
    keyframe_bonus = 0.2 if segment.keyframe_artifact_revision_id else 0.0
    return round6(base * 0.8 + keyframe_bonus)


def _duration_score(segment: RunSegment, shot_duration_us: int) -> float:
    if not segment.is_time_based:
        return _IMAGE_DURATION_SCORE
    if shot_duration_us <= 0:
        return 0.0
    return round6(min(1.0, segment.duration_us / shot_duration_us))


def _continuity_score(shot: dict, segment: RunSegment,
                      previous: tuple[str | None, str | None]) -> float:
    """`previous` = (上一个 shot 的 section_id, 选中的 asset_id)。"""
    prev_section, prev_asset = previous
    if prev_section is None:
        return _CONTINUITY_CROSS_SECTION
    if prev_section != str(shot.get("section_id", "")):
        return _CONTINUITY_CROSS_SECTION
    return (_CONTINUITY_SAME_ASSET if prev_asset == segment.asset_id
            else _CONTINUITY_SAME_SECTION)


def _repetition_penalty(segment: RunSegment, *, selected: set[str],
                        proposed: set[str]) -> float:
    if segment.segment_id in selected:
        return 1.0
    if segment.segment_id in proposed:
        return 0.3
    return 0.0


def _evidence_score(shot: dict, segment: RunSegment) -> float:
    refs = [r for r in (shot.get("evidence_refs") or []) if isinstance(r, dict)]
    if not refs:
        return _NEUTRAL_EVIDENCE_SCORE
    matched = sum(1 for r in refs if str(r.get("asset_id")) == segment.asset_id)
    return round6(matched / len(refs))


def score(
    shot: dict,
    segment: RunSegment,
    *,
    previous: tuple[str | None, str | None],
    selected: set[str],
    proposed: set[str],
) -> ScoreBreakdown:
    """对单个候选打分（纯函数）。"""
    shot_duration = int(shot.get("target_duration_us", 0))
    return ScoreBreakdown(
        semantic_score=_semantic_score(shot, segment),
        quality_score=_quality_score(segment),
        duration_score=_duration_score(segment, shot_duration),
        continuity_score=_continuity_score(shot, segment, previous),
        repetition_penalty=_repetition_penalty(
            segment, selected=selected, proposed=proposed),
        evidence_score=_evidence_score(shot, segment),
        algorithm_version=MATCH_ALGORITHM_VERSION,
    )


def _reject_reason(breakdown: ScoreBreakdown, total: float,
                   threshold: float) -> str:
    if breakdown.repetition_penalty >= 1.0:
        return "already_used"
    if breakdown.evidence_score == 0.0:
        return "evidence_missing"
    if breakdown.duration_score < 1.0 and breakdown.duration_score > 0.0:
        return "duration_insufficient"
    if total < threshold:
        return "score_below_threshold"
    return "score_below_threshold"


def match_storyboard(
    storyboard: dict,
    analyses: list[dict],
    *,
    assets: list[dict] | None = None,
    threshold: float = DEFAULT_SELECTION_THRESHOLD,
    algorithm_version: str = MATCH_ALGORITHM_VERSION,
) -> dict[str, Any]:
    """对整张分镜做确定性匹配，返回 `AssetMatchResult` 的 JSON dict。

    处理顺序为 `sequence_no` 升序（确定性）；每个 shot 只在其
    `candidate_segment_ids` 内选择，且同一 segment 不得被两个 shot 同时选中。
    """
    if algorithm_version != MATCH_ALGORITHM_VERSION:
        raise ValueError(
            f"未知的匹配算法版本：{algorithm_version!r}"
            f"（当前仅支持 {MATCH_ALGORITHM_VERSION!r}）"
        )
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须在 [0, 1] 内")

    segments = iter_run_segments(analyses, assets)
    shots = sorted(storyboard.get("shots") or [],
                   key=lambda s: int(s.get("sequence_no", 0)))

    shot_matches: list[ShotMatch] = []
    selected_ids: set[str] = set()
    proposed_ids: set[str] = set()
    previous: tuple[str | None, str | None] = (None, None)
    warnings: list[str] = []

    for shot in shots:
        shot_id = str(shot.get("shot_id", ""))
        candidate_ids = [str(c) for c in (shot.get("candidate_segment_ids") or [])]
        scored: list[tuple[RunSegment, ScoreBreakdown, float]] = []
        rejected: list[RejectedCandidate] = []

        for candidate in sorted(set(candidate_ids)):
            segment = segments.get(candidate)
            if segment is None:
                # 引用不存在的 segment：显式淘汰（禁止越界引用）
                rejected.append(RejectedCandidate(
                    segment_id=candidate, reason="invalid_reference", score=None))
                warnings.append(f"INVALID_SEGMENT_REFERENCE:{shot_id}:{candidate}")
                continue
            breakdown = score(shot, segment, previous=previous,
                              selected=selected_ids, proposed=proposed_ids)
            scored.append((segment, breakdown, total_score(breakdown)))

        # 确定性排序：分数降序，同分按 segment_id 升序
        scored.sort(key=lambda item: (-item[2], item[0].segment_id))

        winner: tuple[RunSegment, ScoreBreakdown, float] | None = None
        if scored and all(b.repetition_penalty >= 1.0 for _, b, _ in scored):
            # 所有候选都已被前面的 shot 占用 —— 明确报告，绝不重复使用
            warnings.append(f"MATCH_ALL_CANDIDATES_USED:{shot_id}")
        elif scored:
            winner = scored[0]
            if winner[2] >= threshold:
                selected_ids.add(winner[0].segment_id)
                previous = (str(shot.get("section_id", "")), winner[0].asset_id)
            else:
                warnings.append(f"MATCH_BELOW_THRESHOLD:{shot_id}")
                winner = None
        else:
            warnings.append(f"MATCH_NO_CANDIDATE:{shot_id}")

        for segment, breakdown, total in scored:
            if winner is not None and segment.segment_id == winner[0].segment_id:
                continue
            rejected.append(RejectedCandidate(
                segment_id=segment.segment_id,
                reason=_reject_reason(breakdown, total, threshold),
                score=total,
            ))
        rejected.sort(key=lambda r: r.segment_id)

        candidates = [MatchCandidate(
            segment_id=segment.segment_id,
            asset_id=segment.asset_id,
            score=total,
            breakdown=breakdown,
        ) for segment, breakdown, total in scored]

        if winner is None:
            proposed_ids.update(c.segment_id for c in candidates)
            shot_matches.append(ShotMatch(
                shot_id=shot_id, candidates=candidates,
                selected_segment_id=None, score=None, score_breakdown=None,
                rejected_candidates=rejected,
                warnings=[f"NO_SELECTION:{shot_id}"],
            ))
        else:
            shot_matches.append(ShotMatch(
                shot_id=shot_id, candidates=candidates,
                selected_segment_id=winner[0].segment_id,
                score=winner[2], score_breakdown=winner[1],
                rejected_candidates=rejected, warnings=[],
            ))

    unused = sorted(set(segments) - selected_ids)
    return {
        "algorithm_version": algorithm_version,
        "selection_threshold": threshold,
        "shots": [m.model_dump(mode="json") for m in shot_matches],
        "unused_segment_ids": unused,
        "warnings": sorted(set(warnings)),
    }


def build_match_result(
    *,
    revision_id: str,
    project_id: str,
    run_id: str,
    revision_no: int,
    storyboard: dict,
    analyses: list[dict],
    assets: list[dict] | None = None,
    threshold: float = DEFAULT_SELECTION_THRESHOLD,
    revision_note: str | None = None,
    decision_trace: list[dict] | None = None,
) -> AssetMatchResult:
    """构造并**校验** `AssetMatchResult`（模型校验失败即抛）。

    `revision_id` / `revision_no` 由节点传入**临时**身份，执行器发布前会用
    数据库分配的权威身份覆盖（见 `storage.creative_revisions.finalize_payload`）。
    """
    payload = match_storyboard(storyboard, analyses, assets=assets,
                               threshold=threshold)
    payload.update({
        "match_revision_id": str(revision_id),
        "project_id": project_id,
        "run_id": run_id,
        "revision_no": int(revision_no),
        "revision_note": revision_note,
        "decision_trace": list(decision_trace or []),
    })
    return AssetMatchResult.model_validate(payload)


__all__ = [
    "DEFAULT_SELECTION_THRESHOLD",
    "SEGMENT_ID_SEP",
    "WEIGHTS",
    "build_match_result",
    "clamp01",
    "iter_run_segments",
    "match_storyboard",
    "parse_segment_id",
    "round6",
    "score",
    "segment_id",
    "tokenize",
    "total_score",
]
