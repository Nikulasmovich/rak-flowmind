"""WB-03 创作层（结构化创作决策，不含真实 LLM 网络调用）。

模块划分（ADR-0004）：
    provider.py      统一结构化生成接口（只收/只吐结构化数据）
    fake_provider.py 确定性实现（默认启用；不联网、不下载模型）
    prompts.py       版本化提示词
    validators.py    schema + 事实证据校验（反虚构）
    traces.py        DecisionTrace 摘要（不含原始思维链）
    matching.py      确定性素材匹配
    policy.py        审批策略（auto / hybrid）
"""
from __future__ import annotations

from cybercut.creative.fake_provider import FAKE_MODEL_ID, FakeCreativeProvider
from cybercut.creative.matching import (
    DEFAULT_SELECTION_THRESHOLD,
    WEIGHTS,
    build_match_result,
    iter_run_segments,
    match_storyboard,
    segment_id,
    total_score,
)
from cybercut.creative.policy import (
    AUTO_POLICY,
    CREATIVE_NODE_KEYS,
    CREATIVE_STAGES,
    HYBRID_POLICY,
    MANUAL_POLICY,
    ApprovalMode,
    ApprovalPolicy,
    creative_descendants,
    policy_for,
)
from cybercut.creative.prompts import PROMPT_VERSIONS, build_prompt, prompt_version
from cybercut.creative.provider import (
    CreativeProvider,
    GenerationRequest,
    GenerationResult,
    ProviderError,
    provider_model_id,
)
from cybercut.creative.traces import (
    RATIONALE_CODES,
    assert_no_chain_of_thought,
    digest,
    step,
    summarize,
)
from cybercut.creative.validators import (
    allowed_numbers_from_analyses,
    allowed_numbers_from_user_input,
    unsupported_numbers,
    validate_brief,
    validate_match,
    validate_script,
    validate_storyboard,
)

__all__ = [
    "AUTO_POLICY",
    "CREATIVE_NODE_KEYS",
    "DEFAULT_SELECTION_THRESHOLD",
    "FAKE_MODEL_ID",
    "HYBRID_POLICY",
    "MANUAL_POLICY",
    "CREATIVE_STAGES",
    "PROMPT_VERSIONS",
    "RATIONALE_CODES",
    "WEIGHTS",
    "ApprovalMode",
    "ApprovalPolicy",
    "CreativeProvider",
    "FakeCreativeProvider",
    "GenerationRequest",
    "GenerationResult",
    "ProviderError",
    "allowed_numbers_from_analyses",
    "allowed_numbers_from_user_input",
    "assert_no_chain_of_thought",
    "build_match_result",
    "build_prompt",
    "creative_descendants",
    "digest",
    "iter_run_segments",
    "match_storyboard",
    "policy_for",
    "prompt_version",
    "provider_model_id",
    "segment_id",
    "step",
    "summarize",
    "total_score",
    "unsupported_numbers",
    "validate_brief",
    "validate_match",
    "validate_script",
    "validate_storyboard",
]
