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


__all__ = ["NodeSpec", "PipelineSpec", "TEST_PIPELINE"]
