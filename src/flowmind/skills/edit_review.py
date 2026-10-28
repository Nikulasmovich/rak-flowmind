"""edit_review 技能：剪辑项目审批动作（人工复查回路）。

动作语义（设计文档 §3.3 / §4）：
- approve  放行：该闸记入 approved → 提交续跑任务进入下一阶段；
- revise   重跑：feedback 必填 → 打回该阶段（storyboard 闸）或带注续跑
            （pre_render 闸：渲染尚未开跑，反馈记档供后续参考）→ 提交续跑；
- reject   终止：项目落 failed（error=review_rejected），不再续跑。

仅 awaiting_review 状态且 stage 与当前闸一致的项目可动作（否则显式
报错）；跨租户与不存在同形。续跑 = 新的 edit_pipeline 任务（引擎语义
不变：进度/取消/事件全复用）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from flowmind import auth
from flowmind.config import load_config
from flowmind.contracts import Evidence, ReasoningChain, SkillOutput
from flowmind.editing.models import (
    PSTATE_AWAITING_REVIEW,
    PSTATE_RUNNING,
    STAGE_RENDER,
)
from flowmind.editing.store import get_project_store
from flowmind.skill import skill
from flowmind.tasks import TaskQueueFull
from flowmind.tasks.manager import get_task_manager

_VERSION = "0.1.0"


class ReviewInput(BaseModel):
    project_id: str = Field(min_length=8, description="项目 id")
    stage: str = Field(description="待审闸门：copy/script/storyboard/pre_render")
    action: Literal["approve", "revise", "reject"] = Field(
        description="approve=放行；revise=带反馈重跑；reject=终止项目")
    feedback: str | None = Field(
        default=None,
        description="revise 必填（进该阶段 LLM 提示词）；reject 建议填（记档）")


class ReviewReport(BaseModel):
    project_id: str
    action: str
    state: str
    task_id: str | None = None       # approve/revise 续跑任务；reject 无
    message: str = ""


@skill(id="edit_review", name="剪辑项目审批（approve/revise/reject）",
       version=_VERSION)
def edit_review(inp: ReviewInput) -> SkillOutput[ReviewReport]:
    """审批闸动作：放行 / 带新提示词重跑本阶段 / 终止项目。"""
    cfg = load_config().editing
    if inp.stage not in cfg.reviewable_stages:
        raise ValueError(
            f"stage 必须是 {cfg.reviewable_stages} 之一，收到：{inp.stage}")
    if inp.action == "revise" and not (inp.feedback or "").strip():
        raise ValueError("revise 必须带 feedback（重跑提示词）")
    if inp.action == "reject" and not (inp.feedback or "").strip():
        raise ValueError("reject 需要 feedback 说明拒绝原因（记档）")

    store = get_project_store()
    project = store.get_project(inp.project_id)
    required, tenant = auth.tenant_scope()
    if required and (project is None or tenant is None
                     or project.tenant_id != tenant):
        raise ValueError(f"项目不存在：{inp.project_id}")   # 跨租户同形
    if project is None:
        raise ValueError(f"项目不存在：{inp.project_id}")
    if project.state != PSTATE_AWAITING_REVIEW:
        raise ValueError(
            f"项目当前状态 {project.state} 不在审批等待，无法对闸门 "
            f"{inp.stage} 执行动作（当前闸门：{project.stage}）")
    if project.stage != inp.stage:
        raise ValueError(
            f"闸门不匹配：当前等待 {project.stage}，收到 {inp.stage}")

    feedback = (inp.feedback or "").strip()

    if inp.action == "reject":
        project.state = "failed"
        project.error = f"review_rejected[{inp.stage}]: {feedback}"
        store.save_project(project)
        return SkillOutput(
            data=ReviewReport(
                project_id=inp.project_id, action="reject",
                state=project.state, message="项目已按人工审批终止"),
            reasoning=[ReasoningChain(
                conclusion=f"闸门 {inp.stage} 被拒绝，项目终止",
                causal_analysis=f"人工审批反馈：{feedback[:200]}",
                risk_note="可基于原素材重新发起新项目", confidence=1.0)],
            confidence=1.0)

    # ── approve / revise：驱动续跑 ──
    if inp.action == "approve":
        if inp.stage not in project.approved:
            project.approved.append(inp.stage)
    else:   # revise
        project.review_feedback.setdefault(inp.stage, []).append(feedback)
        if inp.stage != "pre_render":
            # storyboard 闸：打回分镜重跑；copy/script 闸同理（阶段名=闸名）
            if inp.stage in project.stages:
                project.stages[inp.stage] = "pending"
    project.state = PSTATE_RUNNING
    project.stage = STAGE_RENDER if inp.action == "revise" and \
        inp.stage == "pre_render" else project.stage
    store.save_project(project)

    manager = get_task_manager()
    try:
        task_id = manager.submit(
            "edit_pipeline", {"project_id": inp.project_id})
    except TaskQueueFull:
        project.state = PSTATE_AWAITING_REVIEW   # 提交失败：退回等待，可重试
        store.save_project(project)
        raise
    # 回存前重载：续跑任务可能已推进项目状态，不能用本副本覆盖
    fresh = store.get_project(inp.project_id) or project
    fresh.task_ids.append(task_id)
    store.save_project(fresh)

    message = {
        "approve": f"闸门 {inp.stage} 已放行，续跑下一阶段",
        "revise": (f"闸门 {inp.stage} 已带反馈重跑"
                   if inp.stage != "pre_render"
                   else f"闸门 {inp.stage} 已带反馈放行（渲染续跑，反馈记档）"),
    }[inp.action]
    return SkillOutput(
        data=ReviewReport(
            project_id=inp.project_id, action=inp.action,
            state=project.state, task_id=task_id, message=message),
        reasoning=[ReasoningChain(
            conclusion=message,
            evidence=[Evidence(metric="feedback_len",
                               value=str(len(feedback)), comparison=">=",
                               window="本次动作")],
            causal_analysis=(f"人工审批 {inp.action} → 项目状态更新 → "
                             "提交 edit_pipeline 续跑任务"),
            risk_note="续跑任务复用引擎进度事件；再次到闸仍会暂停等待审批",
            confidence=1.0)],
        confidence=1.0, sample_size=1,
    )
