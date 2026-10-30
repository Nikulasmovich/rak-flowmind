"""WB-03 创作层契约（Pydantic v2，extra=forbid，时间统一整数微秒）。

设计原则（ADR-0004）：
- **结构化创作决策**：LLM 只能产出符合本模块 schema 的 JSON，不得直接操作文件或数据库；
- **时间统一整数微秒**，`target_duration_us` 必须 > 0；
- **事实可追溯**：所有文案性字段必须通过 `EvidenceRef` 关联到真实的
  `AssetAnalysisReport` / `AssetSegment`；
- **确定性**：匹配等决策由确定性算法给出，同一输入 + 同一算法版本必须得到同一结果；
- 只保存 `DecisionTrace` 摘要，**不保存原始思维链**；
- 所有 JSON 输出稳定、可复现，并携带 `schema_version`。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CREATIVE_SCHEMA_VERSION = "creative.v1"
BRIEF_SCHEMA_VERSION = "creative-brief.v1"
SCRIPT_SCHEMA_VERSION = "script-revision.v1"
STORYBOARD_SCHEMA_VERSION = "storyboard-revision.v1"
MATCH_SCHEMA_VERSION = "asset-match.v1"

#: 素材匹配算法版本（参与匹配结果的确定性契约；变更即失效旧结果的可比性）
MATCH_ALGORITHM_VERSION = "asset-match-v1"
#: 匹配由确定性代码完成，此为其「模型」标识
MATCH_MODEL_ID = f"deterministic:{MATCH_ALGORITHM_VERSION}"
MATCH_PROMPT_VERSION = "deterministic-match.v1"

_ASPECT_RATIO_RE = re.compile(r"^[1-9]\d{0,3}:[1-9]\d{0,3}$")

SectionRole = Literal["hook", "problem", "solution", "proof", "benefit", "cta"]
TransitionHint = Literal["cut", "fade", "dissolve", "match_cut", "whip_pan"]
RejectReason = Literal[
    "score_below_threshold",
    "duration_insufficient",
    "already_used",
    "evidence_missing",
    "invalid_reference",
]


def _no_nan_inf(value: Any) -> Any:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("禁止 NaN / Infinity")
    return value


class _Strict(BaseModel):
    """统一严格基类：禁止额外字段 + 禁止 NaN/Infinity。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="before")
    @classmethod
    def _reject_nan_inf(cls, value: Any) -> Any:
        return _no_nan_inf(value)


# ══════════════════════════════════════════════════════════════════
# 证据引用
# ══════════════════════════════════════════════════════════════════


class EvidenceRef(_Strict):
    """事实依据引用。

    `artifact_kind` 必须是**当前 Run 冻结的**素材产物 kind
    （如 `asset.analysis.{asset_id}` / `asset.scenes.{asset_id}`），
    且必须包含对应的 `asset_id`；`segment_index` 指向该素材内的具体分段。
    """

    asset_id: str = Field(min_length=1)
    artifact_kind: str = Field(min_length=1, max_length=128)
    segment_index: int | None = Field(default=None, ge=0)
    quote: str | None = None

    @field_validator("artifact_kind")
    @classmethod
    def _kind_mentions_asset(cls, value: str, info) -> str:
        asset_id = info.data.get("asset_id")
        if asset_id and asset_id not in value:
            raise ValueError(
                f"artifact_kind 必须指向该素材的产物（需包含 asset_id={asset_id!r}）"
            )
        return value


# ══════════════════════════════════════════════════════════════════
# 1) CreativeBrief
# ══════════════════════════════════════════════════════════════════


class SellingPoint(_Strict):
    """核心卖点：**必须**关联至少一条素材分析证据（禁止虚构产品能力）。"""

    text: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class CreativeBrief(_Strict):
    """创作简报（Run 级、可版本化）。

    `brief_revision_id` / `revision_no` 为**权威身份**：由数据库分配后写入正式
    payload（执行器在发布前统一改写），四者（产物文件 / ArtifactRevision /
    创作版本表 / payload）必须完全一致。
    """

    schema_version: str = BRIEF_SCHEMA_VERSION
    brief_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    target_platform: str = Field(min_length=1)
    aspect_ratio: str
    target_duration_us: int = Field(gt=0)
    language: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    core_message: str = Field(min_length=1)
    selling_points: list[SellingPoint] = Field(min_length=1)
    call_to_action: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    visual_style: str = Field(min_length=1)
    forbidden_claims: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    #: 溯源：生成该版本所用的提示词版本与「模型」标识
    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    #: 本次修订的人工反馈 / 新提示词（仅在有定向修订时非空）
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("aspect_ratio")
    @classmethod
    def _ratio_shape(cls, value: str) -> str:
        if not _ASPECT_RATIO_RE.match(value):
            raise ValueError(f"aspect_ratio 必须形如 '16:9'：{value!r}")
        return value

    @field_validator("source_asset_ids")
    @classmethod
    def _unique_assets(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source_asset_ids 不得重复")
        if any(not item for item in value):
            raise ValueError("source_asset_ids 不得包含空字符串")
        return value

    @model_validator(mode="after")
    def _evidence_within_source_assets(self) -> CreativeBrief:
        allowed = set(self.source_asset_ids)
        for ref in [*self.evidence_refs,
                    *(r for sp in self.selling_points for r in sp.evidence_refs)]:
            if ref.asset_id not in allowed:
                raise ValueError(
                    f"证据引用了非本次 Run 的素材：{ref.asset_id!r}"
                )
        return self


# ══════════════════════════════════════════════════════════════════
# 2) ScriptRevision
# ══════════════════════════════════════════════════════════════════


class ScriptSection(_Strict):
    """脚本分段（每个分段都必须标记事实依据）。"""

    section_id: str = Field(min_length=1)
    role: SectionRole
    narration: str = Field(min_length=1)
    on_screen_text: str = Field(min_length=1)
    target_duration_us: int = Field(gt=0)
    required_visual_intent: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class ScriptRevision(_Strict):
    """脚本版本（Run 级、可版本化）。"""

    schema_version: str = SCRIPT_SCHEMA_VERSION
    script_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    language: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    sections: list[ScriptSection] = Field(min_length=1)
    total_target_duration_us: int = Field(gt=0)
    cta: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    #: 本次修订的人工反馈 / 新提示词（仅在有定向修订时非空）
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    #: 仅保存决策摘要（**不保存原始思维链**）
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("sections")
    @classmethod
    def _unique_section_ids(cls, value: list[ScriptSection]) -> list[ScriptSection]:
        ids = [s.section_id for s in value]
        if len(set(ids)) != len(ids):
            raise ValueError("section_id 不得重复")
        return value

    @model_validator(mode="after")
    def _total_matches_sections(self) -> ScriptRevision:
        expected = sum(s.target_duration_us for s in self.sections)
        if self.total_target_duration_us != expected:
            raise ValueError(
                "total_target_duration_us 必须等于各 section 时长之和："
                f"{self.total_target_duration_us} != {expected}"
            )
        return self


# ══════════════════════════════════════════════════════════════════
# 3) StoryboardRevision
# ══════════════════════════════════════════════════════════════════


class StoryboardShot(_Strict):
    """分镜镜头：**只描述镜头意图与素材约束**，不决定最终剪辑入出点。"""

    shot_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    sequence_no: int = Field(ge=0)
    intent: str = Field(min_length=1)
    visual_requirement: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    composition: str = Field(min_length=1)
    camera_motion: str = Field(min_length=1)
    emotion: str = Field(min_length=1)
    target_duration_us: int = Field(gt=0)
    source_constraints: list[str] = Field(default_factory=list)
    transition_hint: TransitionHint
    #: 候选素材**只能**来自当前 Run 的 AssetSegment（由节点对照冻结分析结果校验）
    candidate_segment_ids: list[str] = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    @field_validator("candidate_segment_ids")
    @classmethod
    def _unique_candidates(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_segment_ids 不得重复")
        if any(not item for item in value):
            raise ValueError("candidate_segment_ids 不得包含空字符串")
        return value


class StoryboardRevision(_Strict):
    """智能分镜版本（Run 级、可版本化）。"""

    schema_version: str = STORYBOARD_SCHEMA_VERSION
    storyboard_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    shots: list[StoryboardShot] = Field(min_length=1)
    total_target_duration_us: int = Field(gt=0)
    rhythm: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    #: 本次修订的人工反馈 / 新提示词（仅在有定向修订时非空）
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("shots")
    @classmethod
    def _deterministic_sequence(
        cls, value: list[StoryboardShot]
    ) -> list[StoryboardShot]:
        ids = [s.shot_id for s in value]
        if len(set(ids)) != len(ids):
            raise ValueError("shot_id 不得重复")
        expected = list(range(len(value)))
        actual = [s.sequence_no for s in value]
        if actual != expected:
            raise ValueError(
                f"sequence_no 必须是确定性的 0..n-1 递增序列，实际 {actual}"
            )
        return value

    @model_validator(mode="after")
    def _total_matches_shots(self) -> StoryboardRevision:
        expected = sum(s.target_duration_us for s in self.shots)
        if self.total_target_duration_us != expected:
            raise ValueError(
                "total_target_duration_us 必须等于各 shot 时长之和："
                f"{self.total_target_duration_us} != {expected}"
            )
        return self


# ══════════════════════════════════════════════════════════════════
# 4) AssetMatchResult
# ══════════════════════════════════════════════════════════════════


class ScoreBreakdown(_Strict):
    """匹配打分明细（确定性算法的可审计分解，各项归一化到 [0, 1]）。"""

    semantic_score: float = Field(ge=0.0, le=1.0)
    quality_score: float = Field(ge=0.0, le=1.0)
    duration_score: float = Field(ge=0.0, le=1.0)
    continuity_score: float = Field(ge=0.0, le=1.0)
    repetition_penalty: float = Field(ge=0.0, le=1.0)
    evidence_score: float = Field(ge=0.0, le=1.0)
    algorithm_version: str = MATCH_ALGORITHM_VERSION

    @property
    def total(self) -> float:
        """加权总分（权重为算法常量，见 `creative.matching.WEIGHTS`）。"""
        from cybercut.creative.matching import total_score

        return total_score(self)


class MatchCandidate(_Strict):
    """参与评分的候选素材分段。"""

    segment_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    breakdown: ScoreBreakdown


class RejectedCandidate(_Strict):
    """被淘汰的候选（必须给出可解释原因，不得静默丢弃）。"""

    segment_id: str = Field(min_length=1)
    reason: RejectReason
    score: float | None = Field(default=None, ge=0.0, le=1.0)


class ShotMatch(_Strict):
    """单个 shot 的匹配结果。"""

    shot_id: str = Field(min_length=1)
    candidates: list[MatchCandidate] = Field(default_factory=list)
    selected_segment_id: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    score_breakdown: ScoreBreakdown | None = None
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _candidates_ordered(self) -> ShotMatch:
        scores = [c.score for c in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates 必须按 score 降序排列（确定性输出）")
        if self.selected_segment_id is not None:
            ids = {c.segment_id for c in self.candidates}
            if self.selected_segment_id not in ids:
                raise ValueError("selected_segment_id 必须在 candidates 中")
            if self.score is None or self.score_breakdown is None:
                raise ValueError("选中素材时必须同时给出 score 与 score_breakdown")
        return self


class AssetMatchResult(_Strict):
    """素材匹配结果（Run 级、可版本化）。

    以 `shots` 承载每个镜头的 `shot_id / candidates / selected_segment_id /
    score / score_breakdown / rejected_candidates / warnings`。
    """

    schema_version: str = MATCH_SCHEMA_VERSION
    match_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)
    algorithm_version: str = MATCH_ALGORITHM_VERSION
    selection_threshold: float = Field(ge=0.0, le=1.0)
    shots: list[ShotMatch] = Field(min_length=1)
    unused_segment_ids: list[str] = Field(default_factory=list)
    #: 溯源：匹配由确定性代码完成（无 LLM 参与）
    prompt_version: str = MATCH_PROMPT_VERSION
    model_id: str = MATCH_MODEL_ID
    #: 本次修订的人工反馈 / 新提示词（仅在有定向修订时非空）
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_conflicting_reuse(self) -> AssetMatchResult:
        """同一 segment 不得被两个 shot 同时**选中**（重复使用冲突）。"""
        selected = [s.selected_segment_id for s in self.shots
                    if s.selected_segment_id is not None]
        if len(set(selected)) != len(selected):
            raise ValueError("同一 segment 不得被多个 shot 同时选中")
        shot_ids = [s.shot_id for s in self.shots]
        if len(set(shot_ids)) != len(shot_ids):
            raise ValueError("shot_id 不得重复")
        return self

    @property
    def unselected_shot_ids(self) -> list[str]:
        return [s.shot_id for s in self.shots if s.selected_segment_id is None]


__all__ = [
    "BRIEF_SCHEMA_VERSION",
    "CREATIVE_SCHEMA_VERSION",
    "MATCH_ALGORITHM_VERSION",
    "MATCH_MODEL_ID",
    "MATCH_PROMPT_VERSION",
    "MATCH_SCHEMA_VERSION",
    "SCRIPT_SCHEMA_VERSION",
    "STORYBOARD_SCHEMA_VERSION",
    "AssetMatchResult",
    "CreativeBrief",
    "EvidenceRef",
    "MatchCandidate",
    "RejectedCandidate",
    "ScoreBreakdown",
    "ScriptRevision",
    "ScriptSection",
    "SellingPoint",
    "ShotMatch",
    "StoryboardRevision",
    "StoryboardShot",
]
