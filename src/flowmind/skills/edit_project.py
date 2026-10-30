"""edit_project 技能：创建剪辑项目并提交全链路任务（批量入口）。

剪映式一站式后端的主入口：投喂素材（视频/图片/音频）+ 创作要求 →
自动跑 素材理解→文案→脚本→分镜→剪辑渲染；逐阶段审批可配（默认只审
分镜）。返回 project_id + task_id：任务走既有引擎（进度事件/取消/下载
全复用），项目状态走 editing ProjectStore。

素材沙箱：本地路径必须位于 data_dir/uploads/ 内（复用
localize_submit._split_paths 收口）；URL v1 不支持（下载能力留 v2，
显式拒绝不静默）。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from flowmind.config import load_config
from flowmind.contracts import Evidence, ReasoningChain, SkillOutput
from flowmind.editing.models import ProjectRecord
from flowmind.editing.store import ProjectStoreError, get_project_store
from flowmind.skill import skill
from flowmind.skills.localize_submit import _split_paths
from flowmind.tasks import TaskQueueFull
from flowmind.tasks.manager import get_task_manager

_VERSION = "0.1.0"

PIPELINE_SKILL_ID = "edit_pipeline"   # 项目任务的执行技能（内部）


class EditProjectInput(BaseModel):
    """创建剪辑项目入参。"""

    assets: list[str] = Field(
        ..., min_length=1,
        description="素材路径列表（视频/图片/音频；本地路径须位于上传沙箱内）")
    theme: str = Field(min_length=1, description="创作主题/要求")
    style: str | None = Field(default=None, description="风格关键词")
    target_duration_s: float | None = Field(
        default=None, gt=0, description="目标成片时长（秒）；缺省=按素材自定")
    aspect_ratio: str | None = Field(
        default=None, description="输出画幅 16:9 / 9:16 / 1:1；缺省=16:9")
    watermark_bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="固定视频水印区域的归一化框 (x0,y0,x1,y1)，各坐标 0-1；仅视频素材")
    voiceover: bool = Field(
        default=False, description="是否 TTS 口播（需 AI_SPEECH_API_KEY）")
    subtitle: bool = Field(default=True, description="是否烧录字幕")
    review_stages: list[str] | None = Field(
        default=None,
        description="需人工审批的闸门阶段（copy/script/storyboard/pre_render）；"
                    "缺省=默认闸 storyboard；空数组=全自动")
    extra_brief: str | None = Field(
        default=None, description="追加提示词（注入所有 LLM 阶段）")

    @field_validator("assets")
    @classmethod
    def _no_empty(cls, v: list[str]) -> list[str]:
        cleaned = [p for p in v if isinstance(p, str) and p.strip()]
        if not cleaned:
            raise ValueError("assets 不能全为空字符串")
        return cleaned

    @field_validator("watermark_bbox")
    @classmethod
    def _valid_watermark_bbox(cls, value):
        if value is not None and (
            any(coord < 0 or coord > 1 for coord in value)
            or value[2] <= value[0]
            or value[3] <= value[1]
        ):
            raise ValueError("watermark_bbox 必须是 0-1 范围内的非空区域")
        return value

    @model_validator(mode="after")
    def _precheck(self) -> "EditProjectInput":
        """URL/沙箱/扩展名预检 + 审批闸/画幅合法性（不占队列槽位）。"""
        cfg = load_config().editing
        urls = [p for p in self.assets
                if p.startswith(("http://", "https://"))]
        if urls:
            raise ValueError(
                f"v1 暂不支持 URL 素材（下载能力留 v2），请先上传到服务器"
                f"（data_dir/uploads/）：{urls}")
        accepted, rejected = _split_paths(self.assets, cfg.allowed_asset_exts)
        if not accepted:
            raise ValueError(
                f"全部素材被拒（允许扩展名：{cfg.allowed_asset_exts}；本地路径"
                f"必须位于 data_dir/uploads/ 内；FLOWMIND_ALLOW_ANY_PATH=1 "
                f"仅限本地测试放行）；被拒：{rejected}")
        if cfg.backend == "cybercut" and any(
            Path(path).suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}
            for path in accepted
        ):
            raise ValueError("CyberCut 后端暂不支持独立音频素材，请仅提交视频或图片")
        if cfg.backend == "cybercut" and self.voiceover:
            raise ValueError("CyberCut 当前版本没有可用的 TTS 服务，voiceover 请设为 false")
        if cfg.backend == "cybercut" and not self.subtitle:
            raise ValueError("CyberCut 当前版本会渲染字幕，暂不支持关闭 subtitle")
        if cfg.backend == "cybercut" and self.watermark_bbox is not None and any(
            Path(path).suffix.lower() not in {
                ".mp4", ".mov", ".mkv", ".webm",
            }
            for path in accepted
        ):
            raise ValueError("watermark_bbox 仅支持视频素材；请移除该项目中的图片素材")
        if cfg.backend == "flowmind" and self.watermark_bbox is not None:
            raise ValueError("watermark_bbox 仅在 CyberCut 后端支持")
        if len(accepted) > cfg.max_assets:
            raise ValueError(
                f"素材数 {len(accepted)} 超过上限 {cfg.max_assets}")
        if self.aspect_ratio is not None and \
                self.aspect_ratio not in cfg.allowed_aspect_ratios:
            raise ValueError(
                f"aspect_ratio 必须是 {cfg.allowed_aspect_ratios} 之一")
        if self.target_duration_s is not None and \
                self.target_duration_s > cfg.max_duration_s:
            raise ValueError(
                f"target_duration_s 超过上限 {cfg.max_duration_s}s")
        if self.review_stages is not None:
            bad = [s for s in self.review_stages
                   if s not in cfg.reviewable_stages]
            if bad:
                raise ValueError(
                    f"review_stages 含未知闸门 {bad}；可选："
                    f"{cfg.reviewable_stages}")
        return self


class EditProjectReport(BaseModel):
    """创建项目业务载荷。"""
    project_id: str
    task_id: str
    skill_id: str = PIPELINE_SKILL_ID
    assets_accepted: int
    assets_rejected: list[str] = Field(default_factory=list)
    review_stages: list[str] = Field(default_factory=list)
    state: str = "running"
    next: str = ("轮询 edit_status 获取进度；审批闸打开时经 edit_review "
                 "approve/revise/reject 驱动；进度事件走任务事件通道")


@skill(id="edit_project", name="智能剪辑项目创建（全链路）",
       version=_VERSION)
def edit_project(inp: EditProjectInput) -> SkillOutput[EditProjectReport]:
    """创建剪辑项目：落项目记录 + 提交 edit_pipeline 异步任务。"""
    cfg = load_config().editing
    accepted, rejected = _split_paths(inp.assets, cfg.allowed_asset_exts)
    review_stages = (list(inp.review_stages)
                     if inp.review_stages is not None
                     else list(cfg.default_review_stages))

    from flowmind.editing.pipeline import new_project_id
    project_id = new_project_id()
    input_dump = inp.model_dump(exclude_none=True)
    input_dump["asset_paths"] = [
        str(Path(p).resolve()) if not p.startswith(("http://", "https://"))
        else p for p in accepted]

    from flowmind import auth
    required, tenant = auth.tenant_scope()
    project = ProjectRecord(
        project_id=project_id, review_stages=review_stages,
        input=input_dump, tenant_id=tenant,
    )
    store = get_project_store()
    try:
        store.save_project(project)
    except ProjectStoreError as exc:
        return SkillOutput(
            data=EditProjectReport(
                project_id=project_id, task_id="",
                assets_accepted=len(accepted), assets_rejected=rejected,
                review_stages=review_stages, state="failed"),
            reasoning=[ReasoningChain(
                conclusion="项目存储不可用，未受理",
                causal_analysis=str(exc)[:500],
                risk_note="检查 PG 配置或文件回落开关后重试", confidence=1.0)],
            confidence=1.0, degraded=True, degradation_reason="environment")

    manager = get_task_manager()
    try:
        task_id = manager.submit(PIPELINE_SKILL_ID, {"project_id": project_id})
    except TaskQueueFull:
        project.state = "failed"
        project.error = "队列满未受理"
        store.save_project(project)
        raise   # invoke 兜底 429 语义
    except Exception as exc:  # noqa: BLE001 提交失败：项目置 failed 再抛
        project.state = "failed"
        project.error = f"submit 失败：{exc}"
        store.save_project(project)
        raise
    # 回存前重载：worker 可能已推进项目状态（内联/极速任务），不能用
    # 创建时的内存副本覆盖；task_ids 合并由 store.save_project 保底
    fresh = store.get_project(project_id) or project
    fresh.task_ids.append(task_id)
    store.save_project(fresh)

    return SkillOutput(
        data=EditProjectReport(
            project_id=project_id, task_id=task_id,
            assets_accepted=len(accepted), assets_rejected=rejected,
            review_stages=review_stages),
        reasoning=[ReasoningChain(
            conclusion=f"项目已受理：{project_id}",
            evidence=[Evidence(metric="assets_accepted",
                               value=str(len(accepted)), comparison="=="),
                      Evidence(metric="review_gates",
                               value=",".join(review_stages) or "全自动",
                               comparison="==")],
            causal_analysis=(
                "素材经沙箱与扩展名校验 → 项目记录落库 → edit_pipeline "
                "任务提交（理解→文案→脚本→分镜→渲染逐阶段推进）"),
            risk_note=("分镜阶段将暂停等待人工审批（approve/revise/reject）"
                       if review_stages else
                       "全自动模式：全程无人工介入，产物见 edit_status"),
            confidence=1.0)],
        confidence=1.0,
        sample_size=len(accepted),
    )
