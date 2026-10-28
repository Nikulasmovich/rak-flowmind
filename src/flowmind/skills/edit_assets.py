"""edit_assets 技能：素材库（v1 轻量：登记 + 过滤列出）。

- register：本地素材登记（类型自动判定 + 存在性校验；沙箱语义与提交
  通道一致——须位于 data_dir/uploads/ 内或 FLOWMIND_ALLOW_ANY_PATH=1）；
- list：按 kind/tags 过滤。剪辑项目跑 understand 阶段会自动登记其素材。

语义向量检索（Milvus + BGE）预留 v2：与 localize_segments 分 collection。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from flowmind import auth
from flowmind.contracts import Evidence, ReasoningChain, SkillOutput
from flowmind.editing.models import AssetRecord
from flowmind.editing.store import get_project_store
from flowmind.skill import skill

_VERSION = "0.1.0"


class AssetsInput(BaseModel):
    action: Literal["list", "register"] = "list"
    kind: str | None = Field(
        default=None, description="按类型过滤 video/image/audio（list）")
    tags: list[str] | None = Field(
        default=None, description="按标签过滤（list，全部命中）")
    path: str | None = Field(
        default=None, description="素材本地路径（register 必填）")
    tags_to_add: list[str] | None = Field(
        default=None, description="登记时附加的标签（register）")
    limit: int = Field(default=100, ge=1, le=500)


class AssetItem(BaseModel):
    id: str
    kind: str
    path: str
    tags: list[str]
    project_id: str | None = None
    created_at: str = ""


class AssetsReport(BaseModel):
    action: str
    assets: list[AssetItem] = Field(default_factory=list)
    registered_id: str | None = None


def _sandbox_reject(path: str) -> bool:
    """复用提交通道沙箱判定（uploads 目录内才放行）。"""
    from flowmind.skills.localize_submit import _sandbox_reject as reject
    return reject(path)


@skill(id="edit_assets", name="剪辑素材库（登记/列出）", version=_VERSION)
def edit_assets(inp: AssetsInput) -> SkillOutput[AssetsReport]:
    """素材库操作：register 登记本地素材 / list 按条件列素材。"""
    store = get_project_store()
    required, tenant = auth.tenant_scope()
    if required and tenant is None:
        # fail-closed：启用鉴权而无凭证，不可见也不可写
        return SkillOutput(
            data=AssetsReport(action=inp.action),
            reasoning=[ReasoningChain(
                conclusion="无调用方凭证，素材库不可见",
                causal_analysis="auth.tenant_scope() 要求凭证但上下文为空",
                risk_note="携带合法 Bearer 后重试", confidence=1.0)],
            confidence=1.0, degraded=True, degradation_reason="environment")

    if inp.action == "register":
        if not inp.path or not inp.path.strip():
            raise ValueError("register 动作必须提供 path")
        path = inp.path.strip()
        if path.startswith(("http://", "https://")):
            raise ValueError("v1 素材库仅支持本地路径（URL 下载留 v2）")
        if _sandbox_reject(path):
            raise ValueError(
                f"素材路径越界（必须位于 data_dir/uploads/ 内；"
                f"FLOWMIND_ALLOW_ANY_PATH=1 仅限本地测试）：{path}")
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise ValueError(f"素材不存在：{resolved}")
        from flowmind.editing.media import classify_asset
        kind = classify_asset(str(resolved))   # 未知扩展名显式报错
        import uuid
        asset_id = f"asset_{uuid.uuid4().hex[:12]}"
        store.save_asset(AssetRecord(
            id=asset_id, kind=kind, path=str(resolved),
            tags=(inp.tags_to_add or []) + [kind],
            tenant_id=tenant))
        return SkillOutput(
            data=AssetsReport(action="register", registered_id=asset_id),
            reasoning=[ReasoningChain(
                conclusion=f"素材已登记：{asset_id}（{kind}）",
                evidence=[Evidence(metric="path", value=str(resolved),
                                   comparison="存在")],
                causal_analysis="扩展名判定类型 → 沙箱校验 → 素材库落库",
                risk_note="登记不复制文件；源文件被删后剪辑任务会显式失败",
                confidence=1.0)],
            confidence=1.0, sample_size=1)

    # list
    rows = store.list_assets(tenant_id=tenant, kind=inp.kind, limit=inp.limit)
    if inp.tags:
        want = set(inp.tags)
        rows = [r for r in rows if want.issubset(set(r.tags))]
    items = [AssetItem(id=r.id, kind=r.kind, path=r.path, tags=r.tags,
                       project_id=r.project_id, created_at=r.created_at)
             for r in rows]
    return SkillOutput(
        data=AssetsReport(action="list", assets=items),
        reasoning=[ReasoningChain(
            conclusion=f"素材库命中 {len(items)} 条",
            evidence=[Evidence(metric="count", value=str(len(items)),
                               comparison="==", window="当前租户")],
            causal_analysis="kind/tags 过滤后返回；项目 understand 阶段的素材"
                            "也会自动进入此库",
            risk_note="语义检索（向量）v2 提供",
            confidence=1.0)],
        confidence=1.0, sample_size=len(items),
    )
