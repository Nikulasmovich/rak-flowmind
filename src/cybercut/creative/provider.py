"""统一结构化生成接口（WB-03 §四）。

边界（ADR-0004 §决策 1）：
- Provider **只**接收结构化上下文、**只**返回结构化 `payload`；
- Provider 不得执行文件操作、命令或修改数据库状态；
- 默认只启用 `FakeCreativeProvider`（见 `fake_provider.py`），不联网、不下载模型、
  不依赖任何外部 API；真实 Provider 需由调用方显式注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cybercut.contracts.errors import CyberCutError


class ProviderError(CyberCutError):
    """Provider 生成失败（稳定错误码）。"""

    code = "CREATIVE_PROVIDER_FAILED"

    def __init__(self, code: str, message: str, *,
                 details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.code = code


@dataclass(frozen=True)
class GenerationRequest:
    """结构化生成请求（**不包含**任何可执行指令）。"""

    kind: str
    prompt_version: str
    #: 确定性上下文（由节点构造，仅含素材事实与用户输入）
    context: dict[str, Any]
    #: 修订提示词（局部重跑时使用；None 表示首次生成）
    revision_hint: str | None = None

    def cache_key(self) -> str:
        """确定性请求标识（同 kind + prompt 版本 + 上下文 ⇒ 同 key）。"""
        from cybercut.creative.traces import digest

        return digest({
            "kind": self.kind,
            "prompt_version": self.prompt_version,
            "context": self.context,
            "revision_hint": self.revision_hint,
        })


@dataclass(frozen=True)
class GenerationResult:
    """结构化生成结果（契约固定为四个字段）。"""

    payload: dict[str, Any]
    model_id: str
    prompt_version: str
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class CreativeProvider(Protocol):
    """结构化创作决策 provider 协议。"""

    model_id: str

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """执行一次结构化生成；失败时抛 `ProviderError`。"""
        ...


def provider_model_id(provider: CreativeProvider) -> str:
    model_id = getattr(provider, "model_id", None)
    if not isinstance(model_id, str) or not model_id:
        raise ProviderError(
            "PROVIDER_INVALID", "Provider 必须定义非空 model_id"
        )
    return model_id


__all__ = [
    "CreativeProvider",
    "GenerationRequest",
    "GenerationResult",
    "ProviderError",
    "provider_model_id",
]
