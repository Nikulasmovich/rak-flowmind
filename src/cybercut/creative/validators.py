"""Schema 与事实证据校验（WB-03 §一 / §四 validators；WB-03R §5 强化）。

校验分两层：
1. **Schema 层**：由 Pydantic 模型本身保证（`extra=forbid`、时长 > 0、
   时长求和一致、`sequence_no` 确定性等）；
2. **事实层**（本模块）：模型虽然在结构上合法，但仍必须满足
   - **素材归属**：`source_asset_ids` / 证据必须来自**当前 Run 冻结的素材**；
   - **证据可达**：`evidence_refs.artifact_kind` 必须（a）指向真实存在的分析产物，
     且（b）属于**当前 Run 的冻结输入**（`input_artifacts`）；
   - **分段存在**：`segment_index` 非空时，必须真实存在于对应
     `AssetAnalysisReport.segments`；
   - **身份一致**：`project_id` / `run_id` 必须与当前 context（及上游产物）一致；
   - **反虚构**：文案中的**数字**必须能在「用户输入 + 素材分析事实」中找到依据；
   - **禁用声明**：`forbidden_claims` 不得出现在任何对外文案中；
   - **匹配约束**：选中素材必须来自候选、不得重复、不得引用不存在的 segment。
"""
from __future__ import annotations

import re
from typing import Any

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.errors import ValidationError
from cybercut.creative.matching import iter_run_segments

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def numeric_tokens(text: str) -> list[str]:
    """提取文本中的数字 token（整数或小数）。"""
    return _NUMBER_RE.findall(text or "")


def _add_number(pool: set[str], value: float) -> None:
    pool.add(str(value))
    if float(value).is_integer():
        pool.add(str(int(value)))


def allowed_numbers_from_analyses(analyses: list[dict]) -> set[str]:
    """素材分析事实中**可被引用**的数字集合。"""
    pool: set[str] = set()
    durations: list[int] = []
    for analysis in analyses or []:
        segments = list(analysis.get("segments") or [])
        pool.add(str(len(segments)))
        duration = int(analysis.get("duration_us") or 0)
        if duration:
            durations.append(duration)
            _add_number(pool, round(duration / 1_000_000, 3))
        probe = analysis.get("probe") or {}
        video = probe.get("video") or {}
        for key in ("width", "height"):
            value = video.get(key)
            if value:
                pool.add(str(int(value)))
        pool.add(str(len(analysis.get("transcript") or [])))
    if durations:
        _add_number(pool, round(sum(durations) / 1_000_000, 3))
    pool.add(str(len(analyses or [])))
    return pool


def allowed_numbers_from_user_input(*values: Any) -> set[str]:
    """用户输入（目标、受众、提示词等）中的数字——属于事实，允许引用。"""
    pool: set[str] = set()
    stack = list(values)
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            pool.update(numeric_tokens(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
    return pool


def unsupported_numbers(text: str, allowed: set[str]) -> list[str]:
    """返回文本中**无事实依据**的数字。"""
    return sorted({token for token in numeric_tokens(text) if token not in allowed})


def _fail(code: str, message: str, **details: Any) -> None:
    raise ValidationError(message, details={"code": code, **details})


def _check_text_numbers(text: str, allowed: set[str], where: str) -> None:
    found = unsupported_numbers(text, allowed)
    if found:
        _fail("UNSUPPORTED_FACT", f"{where} 出现无依据的数字：{found}",
              where=where, numbers=found)


def _check_forbidden(text: str, forbidden: list[str], where: str) -> None:
    for claim in forbidden or []:
        if claim and claim in (text or ""):
            _fail("FORBIDDEN_CLAIM", f"{where} 含禁用声明：{claim!r}",
                  where=where, claim=claim)


def _analysis_kinds(analyses: list[dict]) -> set[str]:
    kinds: set[str] = set()
    for analysis in analyses or []:
        asset_id = str(analysis.get("asset_id") or "")
        if not asset_id:
            continue
        kinds |= {
            f"asset.analysis.{asset_id}",
            f"asset.scenes.{asset_id}",
            f"asset.probe.{asset_id}",
        }
    return kinds


def _segment_index_map(analyses: list[dict]) -> dict[str, set[int]]:
    """`{asset_id: {segment_index, ...}}`（用于校验 `segment_index` 真实性）。"""
    result: dict[str, set[int]] = {}
    for analysis in analyses or []:
        asset_id = str(analysis.get("asset_id") or "")
        if not asset_id:
            continue
        result[asset_id] = {
            int(seg.get("segment_index", 0))
            for seg in (analysis.get("segments") or [])
        }
    return result


def check_evidence_refs(
    refs: list,
    *,
    allowed_assets: set[str],
    allowed_kinds: set[str],
    segment_indexes: dict[str, set[int]],
    frozen_kinds: set[str] | None = None,
    where: str,
) -> None:
    """逐条校验证据引用：素材归属 → 产物可达 → Run 冻结输入 → 分段真实存在。"""
    for index, ref in enumerate(refs or []):
        if isinstance(ref, dict):
            asset_id = ref.get("asset_id")
            kind = ref.get("artifact_kind")
            segment_index = ref.get("segment_index")
        else:
            asset_id = getattr(ref, "asset_id", None)
            kind = getattr(ref, "artifact_kind", None)
            segment_index = getattr(ref, "segment_index", None)
        at = f"{where}[{index}]"
        if asset_id not in allowed_assets:
            _fail("EVIDENCE_OUT_OF_RUN",
                  f"{at} 引用了非本次 Run 的素材：{asset_id!r}",
                  where=at, asset_id=asset_id)
        if kind not in allowed_kinds:
            _fail("EVIDENCE_UNREACHABLE",
                  f"{at} 引用了不存在的分析产物：{kind!r}",
                  where=at, artifact_kind=kind)
        if frozen_kinds is not None and kind not in frozen_kinds:
            _fail("EVIDENCE_NOT_FROZEN",
                  f"{at} 的证据产物不在当前 Run 的冻结输入中：{kind!r}",
                  where=at, artifact_kind=kind)
        if segment_index is not None:
            indexes = segment_indexes.get(str(asset_id), set())
            if int(segment_index) not in indexes:
                _fail("EVIDENCE_SEGMENT_MISSING",
                      f"{at} 的 segment_index={segment_index} 在素材 "
                      f"{asset_id!r} 中不存在",
                      where=at, asset_id=asset_id,
                      segment_index=int(segment_index))


def _check_identity(
    entity: Any, *, expected_project_id: str | None,
    expected_run_id: str | None, where: str,
) -> None:
    project_id = getattr(entity, "project_id", None)
    run_id = getattr(entity, "run_id", None)
    if expected_project_id and project_id != expected_project_id:
        _fail("PROJECT_MISMATCH",
              f"{where} 的 project_id 与当前 context 不一致：{project_id!r}",
              where=where, project_id=project_id)
    if expected_run_id and run_id != expected_run_id:
        _fail("RUN_MISMATCH",
              f"{where} 的 run_id 与当前 context 不一致：{run_id!r}",
              where=where, run_id=run_id)


# ══════════════════════════════════════════════════════════════════
# 1) CreativeBrief
# ══════════════════════════════════════════════════════════════════


def validate_brief(
    brief: CreativeBrief,
    *,
    frozen_asset_ids: set[str],
    analyses: list[dict],
    brief_input: dict | None = None,
    frozen_kinds: set[str] | None = None,
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
) -> None:
    """校验简报：身份 → 素材归属 → 证据可达/冻结/分段 → 反虚构 → 禁用声明。"""
    _check_identity(brief, expected_project_id=expected_project_id,
                    expected_run_id=expected_run_id, where="brief")
    if not frozen_asset_ids:
        _fail("NO_FROZEN_ASSET", "当前 Run 没有冻结素材")
    if not set(brief.source_asset_ids) <= frozen_asset_ids:
        _fail("ASSET_OUT_OF_RUN",
              "source_asset_ids 必须来自当前 Run 冻结的 asset_ids",
              extra=sorted(set(brief.source_asset_ids) - frozen_asset_ids))

    allowed_assets = {str(a.get("asset_id")) for a in analyses or []}
    allowed_kinds = _analysis_kinds(analyses)
    segment_indexes = _segment_index_map(analyses)
    check_evidence_refs(brief.evidence_refs, allowed_assets=allowed_assets,
                        allowed_kinds=allowed_kinds,
                        segment_indexes=segment_indexes,
                        frozen_kinds=frozen_kinds,
                        where="brief.evidence_refs")

    allowed_numbers = allowed_numbers_from_analyses(analyses)
    allowed_numbers |= allowed_numbers_from_user_input(brief_input)

    texts = [("core_message", brief.core_message),
             ("call_to_action", brief.call_to_action)]
    for index, point in enumerate(brief.selling_points):
        where = f"brief.selling_points[{index}]"
        check_evidence_refs(point.evidence_refs, allowed_assets=allowed_assets,
                            allowed_kinds=allowed_kinds,
                            segment_indexes=segment_indexes,
                            frozen_kinds=frozen_kinds, where=where)
        _check_text_numbers(point.text, allowed_numbers, where)
        _check_forbidden(point.text, brief.forbidden_claims, where)
        texts.append((where, point.text))
    for where, text in texts:
        _check_text_numbers(text, allowed_numbers, f"brief.{where}")
        _check_forbidden(text, brief.forbidden_claims, f"brief.{where}")


# ══════════════════════════════════════════════════════════════════
# 2) ScriptRevision
# ══════════════════════════════════════════════════════════════════


def validate_script(
    script: ScriptRevision,
    *,
    brief: CreativeBrief,
    analyses: list[dict] | None = None,
    frozen_kinds: set[str] | None = None,
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
) -> None:
    """校验脚本：身份 → 证据（顶层 + 每个 section）→ 文案事实 → 禁用声明。"""
    _check_identity(script, expected_project_id=expected_project_id,
                    expected_run_id=expected_run_id, where="script")

    allowed_numbers = allowed_numbers_from_user_input(
        brief.core_message, brief.objective, brief.call_to_action,
        brief.audience, brief.tone, brief.visual_style,
        [sp.text for sp in brief.selling_points],
    )
    allowed_numbers |= allowed_numbers_from_analyses(analyses or [])

    allowed_assets = {str(a.get("asset_id")) for a in analyses or []}
    allowed_kinds = _analysis_kinds(analyses or [])
    segment_indexes = _segment_index_map(analyses or [])
    # 证据只能引用 Brief 已确认的素材（脚本消费的是 Brief 的证据集）
    brief_assets = {ref.asset_id for ref in brief.evidence_refs}
    check_evidence_refs(script.evidence_refs, allowed_assets=allowed_assets,
                        allowed_kinds=allowed_kinds,
                        segment_indexes=segment_indexes,
                        frozen_kinds=frozen_kinds,
                        where="script.evidence_refs")
    if not brief_assets <= allowed_assets:
        _fail("BRIEF_EVIDENCE_UNREACHABLE",
              "Brief 的证据素材不在当前 Run 的分析产物中",
              extra=sorted(brief_assets - allowed_assets))

    forbidden = list(brief.forbidden_claims)
    for where, text in (("hook", script.hook), ("cta", script.cta)):
        _check_text_numbers(text, allowed_numbers, f"script.{where}")
        _check_forbidden(text, forbidden, f"script.{where}")

    for index, section in enumerate(script.sections):
        where = f"script.sections[{index}]({section.section_id})"
        check_evidence_refs(section.evidence_refs, allowed_assets=allowed_assets,
                            allowed_kinds=allowed_kinds,
                            segment_indexes=segment_indexes,
                            frozen_kinds=frozen_kinds,
                            where=f"{where}.evidence_refs")
        _check_text_numbers(section.narration, allowed_numbers, f"{where}.narration")
        _check_text_numbers(section.on_screen_text, allowed_numbers,
                            f"{where}.on_screen_text")
        _check_forbidden(section.narration, forbidden, f"{where}.narration")
        _check_forbidden(section.on_screen_text, forbidden, f"{where}.on_screen_text")
        if not section.evidence_refs:
            _fail("SECTION_WITHOUT_EVIDENCE", f"{where} 缺少事实依据")

    if script.language != brief.language:
        _fail("LANGUAGE_MISMATCH",
              f"script.language={script.language!r} 与 brief 不一致")


# ══════════════════════════════════════════════════════════════════
# 3) StoryboardRevision
# ══════════════════════════════════════════════════════════════════


def validate_storyboard(
    storyboard: StoryboardRevision,
    *,
    script: ScriptRevision,
    analyses: list[dict],
    frozen_kinds: set[str] | None = None,
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
) -> None:
    """校验分镜：身份 → 证据（顶层 + 每个 shot）→ 覆盖脚本 → 候选来自当前 Run。"""
    _check_identity(storyboard, expected_project_id=expected_project_id,
                    expected_run_id=expected_run_id, where="storyboard")

    segments = iter_run_segments(analyses)
    allowed_assets = {str(a.get("asset_id")) for a in analyses or []}
    allowed_kinds = _analysis_kinds(analyses or [])
    segment_indexes = _segment_index_map(analyses or [])

    check_evidence_refs(storyboard.evidence_refs, allowed_assets=allowed_assets,
                        allowed_kinds=allowed_kinds,
                        segment_indexes=segment_indexes,
                        frozen_kinds=frozen_kinds,
                        where="storyboard.evidence_refs")

    section_durations = {s.section_id: s.target_duration_us
                         for s in script.sections}
    per_section: dict[str, int] = {}
    for index, shot in enumerate(storyboard.shots):
        where = f"storyboard.shots[{index}]({shot.shot_id})"
        check_evidence_refs(shot.evidence_refs, allowed_assets=allowed_assets,
                            allowed_kinds=allowed_kinds,
                            segment_indexes=segment_indexes,
                            frozen_kinds=frozen_kinds,
                            where=f"{where}.evidence_refs")
        if shot.section_id not in section_durations:
            _fail("UNKNOWN_SECTION", f"{where} 指向不存在的 section：{shot.section_id!r}",
                  where=where, section_id=shot.section_id)
        per_section[shot.section_id] = (
            per_section.get(shot.section_id, 0) + shot.target_duration_us
        )
        missing = [sid for sid in shot.candidate_segment_ids
                   if sid not in segments]
        if missing:
            _fail("CANDIDATE_OUT_OF_RUN",
                  f"{where} 引用了不存在的 AssetSegment：{missing}",
                  where=where, segment_ids=missing)

    for section in script.sections:
        covered = per_section.get(section.section_id, 0)
        if covered != section.target_duration_us:
            _fail("SECTION_NOT_COVERED",
                  f"section {section.section_id!r} 时长未被镜头完整覆盖："
                  f"{covered} != {section.target_duration_us}",
                  section_id=section.section_id, covered=covered,
                  expected=section.target_duration_us)

    if storyboard.total_target_duration_us != script.total_target_duration_us:
        _fail("TOTAL_DURATION_MISMATCH",
              "分镜总时长必须等于脚本总时长",
              storyboard=storyboard.total_target_duration_us,
              script=script.total_target_duration_us)


# ══════════════════════════════════════════════════════════════════
# 4) AssetMatchResult
# ══════════════════════════════════════════════════════════════════


def validate_match(
    match: AssetMatchResult,
    *,
    storyboard: StoryboardRevision,
    analyses: list[dict],
    expected_project_id: str | None = None,
    expected_run_id: str | None = None,
) -> None:
    """校验匹配：身份 → 候选合法 → 选中来自候选 → 不得重复 → 不得越界。"""
    _check_identity(match, expected_project_id=expected_project_id,
                    expected_run_id=expected_run_id, where="match")
    segments = iter_run_segments(analyses)
    shot_ids = {s.shot_id for s in storyboard.shots}
    for index, shot_match in enumerate(match.shots):
        where = f"match.shots[{index}]({shot_match.shot_id})"
        if shot_match.shot_id not in shot_ids:
            _fail("UNKNOWN_SHOT", f"{where} 指向不存在的 shot", where=where)
        for candidate in shot_match.candidates:
            if candidate.segment_id not in segments:
                _fail("CANDIDATE_OUT_OF_RUN",
                      f"{where} 引用了不存在的 segment：{candidate.segment_id!r}",
                      where=where, segment_id=candidate.segment_id)
        if shot_match.selected_segment_id is not None:
            if shot_match.selected_segment_id not in {
                c.segment_id for c in shot_match.candidates
            }:
                _fail("SELECTED_NOT_CANDIDATE",
                      f"{where} 选中素材不在候选列表中", where=where)
            if shot_match.score is None:
                _fail("SELECTED_WITHOUT_SCORE",
                      f"{where} 选中素材但缺少评分", where=where)
            if (shot_match.score is not None
                    and shot_match.score < match.selection_threshold):
                _fail("SELECTED_BELOW_THRESHOLD",
                      f"{where} 选中素材低于阈值", where=where,
                      score=shot_match.score, threshold=match.selection_threshold)


__all__ = [
    "allowed_numbers_from_analyses",
    "allowed_numbers_from_user_input",
    "check_evidence_refs",
    "numeric_tokens",
    "unsupported_numbers",
    "validate_brief",
    "validate_match",
    "validate_script",
    "validate_storyboard",
]
