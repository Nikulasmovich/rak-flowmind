"""审批策略（WB-03 §五）。

两种模式：
- `auto`：**全自动**——所有创作节点产物直接生效，无人工审批门；
- `hybrid`：**混合**——在指定节点（默认 `creative.script`）挂人工审批门，
  审批通过后才继续下游；其余节点自动。

策略是**纯数据 + 纯函数**：同一策略对同一 `node_key` 的判定恒定，
便于在 `NodeSpec.approval_required`（管线定义）与节点运行时（`NodeResult.
awaiting_approval`）两处保持一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cybercut.contracts.errors import ValidationError

#: 创作层节点键（WB-03）
CREATIVE_NODE_KEYS: tuple[str, ...] = (
    "creative.brief",
    "creative.script",
    "creative.storyboard",
    "creative.match",
)

#: 编辑规划节点键（WB-04）
EDIT_NODE_KEYS: tuple[str, ...] = (
    "edit.plan",
    "template.bind",
)

#: 渲染层节点键（WB-05）
RENDER_NODE_KEYS: tuple[str, ...] = (
    "render.narration",
    "render.composite",
    "render.qc",
)

#: 完整规划链（拓扑序）：创作层 + 编辑层 + 渲染层。
#: 用于「只失效规划层下游」的精确判定与审批门校验。
PLAN_NODE_KEYS: tuple[str, ...] = CREATIVE_NODE_KEYS + EDIT_NODE_KEYS + RENDER_NODE_KEYS

#: 完整规划链（别名，向后兼容命名）
CREATIVE_STAGES: tuple[str, ...] = PLAN_NODE_KEYS


def creative_descendants(node_key: str) -> tuple[str, ...]:
    """规划层**下游**节点（不含自身）。

    只沿规划链传递，**不会**波及媒体层节点
    （`asset_probe` / `asset_normalize` / `scene_detect` /
    `speech_transcribe` / `asset_analysis`）。
    """
    if node_key not in CREATIVE_STAGES:
        return ()
    index = CREATIVE_STAGES.index(node_key)
    return CREATIVE_STAGES[index + 1:]


class ApprovalMode(StrEnum):
    AUTO = "auto"
    HYBRID = "hybrid"
    MANUAL = "manual"


@dataclass(frozen=True)
class ApprovalPolicy:
    """审批策略（可序列化）；`gates` 仅在 `hybrid` 模式下生效。"""

    mode: ApprovalMode = ApprovalMode.AUTO
    gates: tuple[str, ...] = ()
    decided_by: str = "system:policy"

    def __post_init__(self) -> None:
        unknown = [g for g in self.gates if g not in PLAN_NODE_KEYS]
        if unknown:
            raise ValidationError(
                f"审批门只能配置在规划层节点上：{unknown}",
                details={"allowed": list(PLAN_NODE_KEYS)},
            )
        if self.mode == ApprovalMode.AUTO and self.gates:
            raise ValidationError("auto 模式不得配置审批门（gates 必须为空）")
        if self.mode != ApprovalMode.AUTO and not self.gates:
            raise ValidationError(
                f"{self.mode} 模式必须显式配置至少一个审批门（gates 不能为空）"
            )

    def requires_approval(self, node_key: str) -> bool:
        return self.mode != ApprovalMode.AUTO and node_key in self.gates

    def to_json(self) -> dict[str, Any]:
        return {"mode": str(self.mode), "gates": list(self.gates),
                "decided_by": self.decided_by}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ApprovalPolicy:
        if not data:
            return AUTO_POLICY
        try:
            return cls(
                mode=ApprovalMode(str(data.get("mode", "auto"))),
                gates=tuple(data.get("gates") or ()),
                decided_by=str(data.get("decided_by", "system:policy")),
            )
        except ValueError as exc:
            raise ValidationError(f"非法审批策略：{data!r}") from exc


AUTO_POLICY = ApprovalPolicy(mode=ApprovalMode.AUTO)
HYBRID_POLICY = ApprovalPolicy(
    mode=ApprovalMode.HYBRID, gates=("creative.script",)
)
#: 全人工：全部规划节点（创作四节点 + 编辑两节点）都需要审批
MANUAL_POLICY = ApprovalPolicy(
    mode=ApprovalMode.MANUAL, gates=PLAN_NODE_KEYS
)


@dataclass(frozen=True)
class PolicyPresets:
    """常用预设（便于按名称取用）。"""

    presets: dict[str, ApprovalPolicy] = field(default_factory=lambda: {
        "auto": AUTO_POLICY,
        "hybrid": HYBRID_POLICY,
        "manual": MANUAL_POLICY,
    })

    def get(self, name: str) -> ApprovalPolicy:
        try:
            return self.presets[name]
        except KeyError as exc:
            raise ValidationError(
                f"未知的审批预设：{name!r}",
                details={"presets": sorted(self.presets)},
            ) from exc


def policy_for(mode: str | ApprovalMode, *,
               gates: tuple[str, ...] | None = None) -> ApprovalPolicy:
    """按模式名构造策略。

    - `auto`：无审批门；
    - `hybrid`：默认门 `creative.script`，可用 `gates` 显式覆盖；
    - `manual`：默认**四个创作节点全部**需要审批，可用 `gates` 覆盖。
    """
    normalized = ApprovalMode(str(mode))
    if normalized == ApprovalMode.AUTO:
        return AUTO_POLICY
    if normalized == ApprovalMode.HYBRID:
        return ApprovalPolicy(
            mode=ApprovalMode.HYBRID,
            gates=gates if gates is not None else HYBRID_POLICY.gates,
        )
    return ApprovalPolicy(
        mode=ApprovalMode.MANUAL,
        gates=gates if gates is not None else PLAN_NODE_KEYS,
    )


__all__ = [
    "AUTO_POLICY",
    "CREATIVE_NODE_KEYS",
    "CREATIVE_STAGES",
    "EDIT_NODE_KEYS",
    "HYBRID_POLICY",
    "MANUAL_POLICY",
    "PLAN_NODE_KEYS",
    "ApprovalMode",
    "ApprovalPolicy",
    "PolicyPresets",
    "creative_descendants",
    "policy_for",
]
