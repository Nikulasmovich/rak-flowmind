"""editing 包：Agent 智能视频剪辑（剪映式一站式后端）。

设计文档：docs/agent-video-editing.md。模块边界（import 无环）：
models（纯模型）→ media / llm（工具层）→ store（持久层）→ pipeline（编排）。
技能层在 skills/edit_*.py，经 @skill 注册后自动进 manifest。
"""
from flowmind.editing.models import (  # noqa: F401
    PSTATE_AWAITING_REVIEW,
    PSTATE_CANCELLED,
    PSTATE_FAILED,
    PSTATE_RUNNING,
    PSTATE_SUCCEEDED,
    STAGE_ORDER,
    ProjectRecord,
)
