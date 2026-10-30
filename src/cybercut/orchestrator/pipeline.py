"""测试管线定义（WB-01 §七）。

    node_a -> node_b -> approval_gate -> node_c

- `node_a` / `node_b` / `node_c` 为 echo 节点；
- `approval_gate` 需要人工审批（`approval_required=True`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NodeSpec:
    """单个节点定义。"""

    node_key: str
    depends_on: tuple[str, ...] = ()
    approval_required: bool = False


@dataclass(frozen=True)
class PipelineSpec:
    """管线定义（节点集合 + 依赖关系）。"""

    name: str
    version: str
    nodes: tuple[NodeSpec, ...] = field(default_factory=tuple)
    #: True 表示该管线必须携带至少一个素材（WB-02R §二.2）
    requires_assets: bool = False

    def get(self, node_key: str) -> NodeSpec:
        for spec in self.nodes:
            if spec.node_key == node_key:
                return spec
        raise KeyError(f"管线 {self.name!r} 中不存在节点 {node_key!r}")

    def keys(self) -> list[str]:
        return [spec.node_key for spec in self.nodes]

    def roots(self) -> list[str]:
        return [spec.node_key for spec in self.nodes if not spec.depends_on]

    def dependents_of(self, node_key: str) -> list[str]:
        return [
            spec.node_key for spec in self.nodes if node_key in spec.depends_on
        ]

    def descendants_of(self, node_key: str) -> list[str]:
        """所有（传递）依赖 node_key 的下游节点键（不含自身）。"""
        found: list[str] = []
        frontier = [node_key]
        seen: set[str] = {node_key}
        while frontier:
            current = frontier.pop()
            for child in self.dependents_of(current):
                if child in seen:
                    continue
                seen.add(child)
                found.append(child)
                frontier.append(child)
        return found

    def topo_order(self) -> list[str]:
        """拓扑序（依赖在前）。"""
        ordered: list[str] = []
        pending = {spec.node_key: set(spec.depends_on) for spec in self.nodes}
        while pending:
            ready = [k for k, deps in pending.items() if not (deps - set(ordered))]
            if not ready:  # pragma: no cover - 定义错误时才会发生
                raise ValueError(f"管线 {self.name!r} 存在循环依赖：{sorted(pending)}")
            for key in sorted(ready):
                ordered.append(key)
                pending.pop(key)
        return ordered


TEST_PIPELINE = PipelineSpec(
    name="test-pipeline",
    version="test-pipeline-v1",
    nodes=(
        NodeSpec("node_a"),
        NodeSpec("node_b", depends_on=("node_a",)),
        NodeSpec("approval_gate", depends_on=("node_b",), approval_required=True),
        NodeSpec("node_c", depends_on=("approval_gate",)),
    ),
)

#: WB-02 素材分析工作流（asset-analysis-v1）
#:
#: .. code-block:: text
#:
#:     asset_probe -> asset_normalize -> scene_detect ─┐
#:                        └──────────> speech_transcribe ┴-> asset_analysis
#:
#: 说明：`asset_analysis` 额外依赖 `asset_probe`（报告需要确定性媒体事实），
#: 是任务书 DAG 的**超集**（不缺少任何要求的边），见 ADR-0003。
ASSET_ANALYSIS_PIPELINE = PipelineSpec(
    name="asset-analysis-v1",
    version="asset-analysis-v1",
    requires_assets=True,
    nodes=(
        NodeSpec("asset_probe"),
        NodeSpec("asset_normalize", depends_on=("asset_probe",)),
        NodeSpec("scene_detect", depends_on=("asset_normalize",)),
        NodeSpec("speech_transcribe", depends_on=("asset_normalize",)),
        NodeSpec("asset_analysis",
                 depends_on=("asset_probe", "scene_detect", "speech_transcribe")),
    ),
)


def _creative_nodes(*, hybrid_gates: tuple[str, ...] = ()) -> tuple[NodeSpec, ...]:
    """创作管线节点：媒体分析五节点 + 创作四节点（WB-03 §三）。

    依赖说明：创作节点除需要**上游创作产物**外，还需要**素材分析产物**
    （`asset.analysis.{asset_id}`，用于事实约束与候选素材），
    故每个创作节点都显式依赖 `asset_analysis`。
    """
    return (
        NodeSpec("asset_probe"),
        NodeSpec("asset_normalize", depends_on=("asset_probe",)),
        NodeSpec("scene_detect", depends_on=("asset_normalize",)),
        NodeSpec("speech_transcribe", depends_on=("asset_normalize",)),
        NodeSpec("asset_analysis",
                 depends_on=("asset_probe", "scene_detect", "speech_transcribe")),
        NodeSpec("creative.brief", depends_on=("asset_analysis",),
                 approval_required="creative.brief" in hybrid_gates),
        NodeSpec("creative.script",
                 depends_on=("creative.brief", "asset_analysis"),
                 approval_required="creative.script" in hybrid_gates),
        NodeSpec("creative.storyboard",
                 depends_on=("creative.script", "creative.brief", "asset_analysis"),
                 approval_required="creative.storyboard" in hybrid_gates),
        NodeSpec("creative.match",
                 depends_on=("creative.storyboard", "asset_analysis"),
                 approval_required="creative.match" in hybrid_gates),
        # ── WB-04：编辑规划与模板绑定 ──
        NodeSpec("edit.plan",
                 depends_on=("creative.match", "creative.storyboard",
                             "creative.script", "creative.brief",
                             "asset_analysis"),
                 approval_required="edit.plan" in hybrid_gates),
        NodeSpec("template.bind",
                 depends_on=("edit.plan", "creative.script"),
                 approval_required="template.bind" in hybrid_gates),
    )

def _full_nodes(*, hybrid_gates: tuple[str, ...] = ()) -> tuple["NodeSpec", ...]:
    """完整管线（WB-02–WB-05）：规划 11 节点 + 渲染 3 节点。"""
    return _creative_nodes(hybrid_gates=hybrid_gates) + (
        NodeSpec("render.narration",
                 depends_on=("template.bind",),
                 approval_required="render.narration" in hybrid_gates),
        NodeSpec("render.composite",
                 depends_on=("render.narration", "template.bind"),
                 approval_required="render.composite" in hybrid_gates),
        NodeSpec("render.qc",
                 depends_on=("render.composite",),
                 approval_required="render.qc" in hybrid_gates),
    )


def build_creative_pipeline(policy) -> "PipelineSpec":
    """按审批策略构造创作管线（`NodeSpec.approval_required` ⇔ 策略门一致）。"""
    gates = tuple(policy.gates)
    name_suffix = "auto" if not gates else "-".join(
        g.replace(".", "") for g in gates)
    return PipelineSpec(
        name=f"creative-v1-{name_suffix}",
        version=f"creative-v1-{name_suffix}",
        requires_assets=True,
        nodes=_creative_nodes(hybrid_gates=gates),
    )


#: WB-03 创作管线（**全自动**审批策略）：
#:
#: .. code-block:: text
#:
#:     asset_probe -> asset_normalize -> scene_detect ─┐
#:                        └────────> speech_transcribe ┴-> asset_analysis
#:     asset_analysis -> creative.brief -> creative.script
#:                    -> creative.storyboard -> creative.match
CREATIVE_PIPELINE = PipelineSpec(
    name="creative-v1",
    version="creative-v1",
    requires_assets=True,
    nodes=_creative_nodes(),
)

#: WB-03 创作管线（**混合**审批策略：`creative.script` 处挂人工审批门）
CREATIVE_PIPELINE_HYBRID = PipelineSpec(
    name="creative-v1-hybrid",
    version="creative-v1-hybrid",
    requires_assets=True,
    nodes=_creative_nodes(hybrid_gates=("creative.script",)),
)

#: WB-03 创作管线（**全人工**审批策略：四个创作节点全部挂审批门，WB-03R §6）
CREATIVE_PIPELINE_MANUAL = PipelineSpec(
    name="creative-v1-manual",
    version="creative-v1-manual",
    requires_assets=True,
    nodes=_creative_nodes(hybrid_gates=(
        "creative.brief", "creative.script", "creative.storyboard",
        "creative.match", "edit.plan", "template.bind",
    )),
)


#: WB-05 完整管线（规划 11 节点 + 渲染 3 节点）
RENDER_PIPELINE = PipelineSpec(
    name="render-v1",
    version="render-v1",
    requires_assets=True,
    nodes=_full_nodes(),
)


__all__ = [
    "ASSET_ANALYSIS_PIPELINE",
    "CREATIVE_PIPELINE",
    "CREATIVE_PIPELINE_HYBRID",
    "CREATIVE_PIPELINE_MANUAL",
    "TEST_PIPELINE",
    "NodeSpec",
    "PipelineSpec",
    "build_creative_pipeline",
    "RENDER_PIPELINE",
]
