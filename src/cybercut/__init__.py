"""CyberCut —— 无 GUI 的 Agent 视频剪辑后端（控制面与可靠任务执行器）。

本包为 CyberCut 自有业务代码，与基座 `flowmind`（rak-flowmind）严格分离：
- 不 import、不修改基座核心文件；不注册任何 FlowMind `@skill`；
- 所有业务对象位于 PostgreSQL `cybercut` schema，不改动 `flowmind` schema / `tasks` 表；
- 参见 docs/cybercut/ADR-0001-flowmind-integration-boundary.md（基座边界）
  与 docs/cybercut/ADR-0002-node-execution-and-claim.md（执行器与 claim 协议）。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
