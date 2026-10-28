"""edit_status 技能：剪辑项目状态查询（含审批等待信息与产物清单）。

批量查询；租户过滤与任务引擎一致（启用鉴权而跨租户 → 与不存在同形，
不给枚举探针留口子）。产物清单扫描项目目录，成片未产出时 outputs 为空。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from flowmind import auth
from flowmind.contracts import Evidence, ReasoningChain, SkillOutput
from flowmind.editing.models import PSTATE_AWAITING_REVIEW
from flowmind.editing.pipeline import project_dir
from flowmind.editing.store import get_project_store
from flowmind.skill import skill

_VERSION = "0.1.0"

_ARTIFACT_NAMES = ("understand.json", "copy.json", "script.json",
                   "storyboard.json", "timeline.json", "output.srt")
_OUTPUT_NAMES = ("output.mp4", "output.srt", "voiceover.mp3")


class StatusInput(BaseModel):
    project_ids: list[str] = Field(..., min_length=1,
                                   description="项目 id 列表")


class ProjectStatus(BaseModel):
    project_id: str
    found: bool = True
    state: str = ""
    stage: str = ""
    stages: dict[str, str] = Field(default_factory=dict)
    task_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    storage: str = "file"
    # 审批等待详情（state=awaiting_review 时非空）
    review: dict | None = None
    artifacts: list[str] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    preview: list[str] = Field(default_factory=list)   # v1.1 成片自检帧


class StatusReport(BaseModel):
    projects: list[ProjectStatus]


def _visible(project) -> bool:
    """跨租户同形不可见（与 TaskManager._visible 语义一致）。"""
    required, tenant = auth.tenant_scope()
    if not required:
        return True
    if project is None or tenant is None:
        return False
    return project.tenant_id == tenant


def _summarize_artifact(pdir: Path, name: str) -> str | None:
    """中间产物的一句话摘要（前端审批卡片直接展示用）。"""
    path = pdir / name
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 摘要失败不影响查询主体
        return f"{name}（存在但不可解析）"
    if name == "copy.json":
        return f"《{data.get('title', '')}》 hook={data.get('hook', '')}"
    if name == "script.json":
        secs = data.get("sections") or []
        return f"{len(secs)} 段，总时长 {data.get('total_duration_s', '?')}s"
    if name == "storyboard.json":
        shots = data.get("shots") or []
        notes = "；".join((s.get("note") or "")[:40] for s in shots[:3])
        return f"{len(shots)} 镜头。选材理由：{notes}"
    if name == "understand.json":
        assets = data.get("assets") or []
        return f"{len(assets)} 素材：{'、'.join(a.get('id', '?') + ':' + a.get('kind', '?') for a in assets)}"
    return path.name


@skill(id="edit_status", name="剪辑项目状态查询", version=_VERSION)
def edit_status(inp: StatusInput) -> SkillOutput[StatusReport]:
    """查询项目状态：阶段进度 / 审批等待详情 / 产物与成片清单。"""
    store = get_project_store()
    results: list[ProjectStatus] = []
    for pid in inp.project_ids:
        project = store.get_project(pid)
        if project is None or not _visible(project):
            results.append(ProjectStatus(project_id=pid, found=False,
                                         state="not_found", stage="not_found"))
            continue
        pdir = project_dir(pid)
        review = None
        if project.state == PSTATE_AWAITING_REVIEW:
            review = {
                "gate": project.stage,
                "feedback_history": project.review_feedback.get(
                    project.stage, []),
                "hint": (f"经 edit_review 对闸门 {project.stage} 执行 "
                         "approve / revise（带 feedback）/ reject"),
                "summary": next(iter(filter(None, (
                    _summarize_artifact(pdir, "storyboard.json"),
                    _summarize_artifact(pdir, "script.json"),
                    _summarize_artifact(pdir, "copy.json"),
                ))), None),
            }
        artifacts = [n for n in _ARTIFACT_NAMES if (pdir / n).exists()]
        outputs = {
            n: str(pdir / n) for n in _OUTPUT_NAMES if (pdir / n).exists()}
        preview_dir = pdir / "preview"
        preview = ([str(p) for p in sorted(preview_dir.glob("preview_*.jpg"))]
                   if preview_dir.exists() else [])
        results.append(ProjectStatus(
            project_id=pid, state=project.state, stage=project.stage,
            stages=project.stages, task_ids=project.task_ids,
            error=project.error, storage=project.storage,
            review=review, artifacts=artifacts, outputs=outputs,
            preview=preview))

    found = [r for r in results if r.found]
    awaiting = [r for r in found if r.state == PSTATE_AWAITING_REVIEW]
    done = [r for r in found if r.state == "succeeded"]
    return SkillOutput(
        data=StatusReport(projects=results),
        reasoning=[ReasoningChain(
            conclusion=(f"查询 {len(results)} 项：命中 {len(found)}，"
                        f"待审批 {len(awaiting)}，完成 {len(done)}"),
            evidence=[Evidence(metric="awaiting_review",
                               value=str(len(awaiting)), comparison="=="),
                      Evidence(metric="succeeded",
                               value=str(len(done)), comparison="==")],
            causal_analysis="项目状态机实时读自 ProjectStore（PG/文件），"
                            "产物清单按项目目录实际文件枚举",
            risk_note="not_found 与跨租户同形（防 id 枚举探测）",
            confidence=1.0)],
        confidence=1.0, sample_size=len(results),
    )
