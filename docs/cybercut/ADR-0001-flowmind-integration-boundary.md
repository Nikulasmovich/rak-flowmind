# ADR-0001 · FlowMind 集成边界（CyberCut ↔ rak-flowmind）

| 项 | 值 |
|---|---|
| 状态 | **Proposed**（待 WB-01 评审确认；**执行器选型等未决项已在 WB-00R 拍板消除**） |
| 日期 | 2026-10-06（WB-00R） |
| 阶段 | WB-00 + **WB-00R 定向整改**（仅审计与边界定义，不实现业务） |
| 决策依据 | `WB-00-base-audit.md`、`WB-00-capability-matrix.md`（均据 commit `c8ea1c6` 实测） |
| 基座状态 | 维护冻结（`AGENTS.md:3-6`、`CLAUDE.md:3-5` 明示「不做非必要改动」） |
| 影响范围 | CyberCut 全部后续阶段（WB-01+） |

---

## 背景（Context）

CyberCut 目标是一个「无 GUI 的 Agent 视频剪辑后端」（素材分析→文案→脚本→分镜→素材匹配→时间线→模板→渲染→质检→审批→局部重跑→批量）。基座 `rak-flowmind`（包名 `mcp-base-gpu`）自称提供「Agent 技能底座 + MCP 服务框架 + 异步任务引擎」。

WB-00 审计的**决定性事实**：

1. 基座真正的骨架是：`@skill` 技能注册 → 自动 JSON schema / MCP 工具 / discover / manifest；`invoke()` 统一信封（trace / metrics / 四段式推理链 / 结构化错误）；`TaskManager` 单技能异步执行器（PG+CAS+取消+重试+TTL+背压+MQTT）；鉴权与 `tenant_id`。**这些实测可用**。
2. 基座**不是**工作流引擎：`tasks` 表只有 `{skill_id, args_json}` 一层，**无 Project/Run/NodeRun、无 DAG、无依赖、无步骤间产物传递、无审批态、无局部重跑**。
3. 基座**不具备** CyberCut 的业务能力：注册表仅 7 个 `localize_*`，其中唯一处理视频的 `localize_video` **当前不可执行**（`LocalizerConfig` 缺字段 → `AttributeError`，F-01）。**但 F-01 不阻塞 CyberCut 首版**（首版不依赖该流水线）。
4. 基座处于**维护冻结**；无 CI、无测试；`contracts.py` 变更即破坏对外契约。
5. **MCP 暴露是全量无差别的**：`register_all()` 遍历整个 `_REGISTRY`（`server.py:44-54`），**不存在「公开白名单 / 内外技能分离」机制**——「注册为 `@skill`」＝「对外公开为 MCP 工具」。
6. **`TaskManager` 不能作多进程执行器**：其构造函数无条件执行 `recover_running()`（`manager.py:103`），会把该库**所有** `queued`/`running` 打成 `interrupted`；且**无多进程可靠任务领取（claim）机制**（`workers=1` + 进程内锁；PgBouncer 禁 advisory lock）。产物侧亦**只识别单个** `data.output_path` 且受 **TTL** 删除。

因此必须显式划定「谁负责什么」，否则 CyberCut 要么重复造基座已有的轮子，要么把工作流语义硬塞进单技能执行器而撕裂架构。

---

## 决策（Decision）

### 0. 拍板摘要（WB-00R 消除未决项）

- **CyberCut 默认执行器 = 自建**（基于 PostgreSQL 的 NodeAttempt 队列 + worker claim），**不使用** FlowMind `TaskManager` 作为默认 NodeRun 执行器。
- **FlowMind 只负责**：公开 MCP 技能契约、trace 信封、既有 `localize_*` 兼容能力。
- **CyberCut REST、SSE、控制面、内部节点执行全部归 CyberCut**。
- **不修改 FlowMind 核心契约**；首版**不修改** `server.py`。
- **F-01 不阻塞 CyberCut**：首版不依赖 `localize_video` 流水线。

### 1. FlowMind 在 CyberCut 中的唯一职责是什么？

**唯一职责（WB-00R 拍板）：为 CyberCut 提供「公开 MCP 技能契约 + trace 信封 + 既有 `localize_*` 兼容能力」。**

FlowMind **不是** CyberCut 的节点执行器，**不是**工作流引擎，**不是**编排/领域模型承载者。基座负责且仅负责：

| 职责 | 载体 | 说明 |
|---|---|---|
| **(a) 公开 MCP 技能的契约与暴露** | `skill.py` + `contracts.py` + `manifest.py` + `discover.py` + `server.py` | 只有**明确要公开的项目级 MCP 能力**才写成 `@skill` 并登记（登记即对外暴露，见 §3）；提供统一 JSON schema / discover / manifest |
| **(b) trace 信封与可观测** | `contracts.py`（`SkillResult` / `TraceContext` / `ReliabilityMetrics` / `ReasoningChain`）+ `invoke()` | 为公开技能提供 `trace_id` 贯穿、四段式推理链、失败分类——**这是 CyberCut 复用的核心价值** |
| **(c) 既有 `localize_*` 兼容能力** | 7 个 `localize_*` 技能 + `/api/v1/tasks` 任务 REST | 保持外部既有本地化任务通道可用；**仅作兼容**，不作为 CyberCut 项目级执行路径 |
| **(d) 可复用的基础设施面** | `auth.py`（鉴权+租户）、`tasks/gpu.py`（GPU 双道闸）、`tasks/vectors.py`/`_bge_embed.py` | 与执行器解耦、可原样复用 |

**明确排除（基座不做，CyberCut 全权负责）**：流水线/节点编排、DAG、Project/Run/NodeRun/NodeAttempt 建模、中间产物模型（Script/Storyboard/Timeline）、**节点执行器与多进程任务领取**、人工审批、局部重跑、批量批次语义、多产物登记、渲染与质检业务逻辑、素材上传接收端、CyberCut 自己的 REST/SSE/控制面。

> 判定理由：T-01/T-05/T-06/T-09..T-18、V-10/V-13 等项在能力矩阵中已就是 `ADAPTER_REQUIRED`/`REPLACE`/`NOT_PRESENT`；且基座 `TaskManager` 的「重启即中断 + 无多进程 claim」是硬工程障碍（见 §2/§3）。

### 2. Project / Run / NodeRun 是否必须独立建模？

**是，必须独立建模，且必须建在 CyberCut 自己的 schema 中，不得复用 `flowmind.tasks` 表当作 NodeRun 或 NodeAttempt。**

理由（源码证据）：

- `tasks` 表结构为 `{task_id, skill_id, args_json, status, stage, progress, error, created_at, started_at, finished_at, tenant_id, output_paths}`（`store.py:37-58`）——**单技能、扁平、无层级、无依赖**。
- 批量提交 = 循环 `submit` N 个平级 task（`localize_submit.py:269-281`）——**无父子/批次聚合**。
- 无任何编排表/图结构（全仓无 `depends_on`/`parent`/`dag`/`step` 概念），且**无多进程可靠领取**、**只登记单产物**。

**采用的领域模型（CyberCut 侧，基座不感知）**：

```
Project            1 ──< N  Run               1 ──< N  NodeRun
NodeRun            1 ──< N  NodeAttempt        （每次「实际执行」= 一条）
NodeRun            1 ──< N  ArtifactRevision   （每次「产物版本」= 一条）
NodeRun            0 ──< N  Approval           （0..N 次审批记录）

基数关系（逐条）：
  Project 1:N Run
  Run     1:N NodeRun
  NodeRun 1:N NodeAttempt
  NodeRun 1:N ArtifactRevision
  NodeRun 0:N Approval
```

**关键约定（逐条）**：

1. **`NodeRun` = 逻辑节点 + 依赖状态**：承载节点类型、依赖关系（`depends_on`）、输入/输出契约、聚合状态、审批状态。它**不代表某一次执行**。
2. **`NodeAttempt` = 一次实际执行**：承载 `attempt_no`、`status`、`worker` 归属、起止时间、错误、以及执行器引用。
3. **重试必须新建 `NodeAttempt`，绝不覆盖旧尝试**：每次重跑 `attempt_no + 1` 生成新行，旧尝试**保留为历史**（可审计、可对比）。`NodeRun` 的状态由其**最新一次** `NodeAttempt` 投影而来。
4. **`executor_kind ∈ {cybercut_worker, flowmind_task}`**：
   - `cybercut_worker`（**默认**）：由 CyberCut 自建 worker 执行 `NodeHandler`；
   - `flowmind_task`（**可选适配**）：仅当该节点确实要执行一个基座技能时使用。
5. **`executor_ref` 仅在 `executor_kind = flowmind_task` 时填写**：指向基座 `flowmind.tasks.task_id`；`cybercut_worker` 时为空。
6. **不建立跨 schema 外键**：CyberCut 表与基座 `flowmind.tasks` **不做数据库级 FK**，`executor_ref` 仅作**软引用**（避免耦合基座 schema 演化）。
7. **`ArtifactRevision` = 产物版本**：CyberCut 的多产物在此独立登记（类型 / 路径 / 版本 / 关联 NodeAttempt），是**项目正式产物的唯一权威登记处**（见 §4 与能力矩阵 T-17）。
8. **`Approval` = 审批记录**：`NodeRun 0:N Approval` 支持「待审 → 通过/打回 → 再走新 NodeAttempt」的人在环闭环。
9. 所有 CyberCut 业务表带 `tenant_id`，与基座租户模型对齐（`auth.py:94-110`）。

> 与 WB-00 首版的差异（R 修正）：首版把「一次尝试」直接等同 `flowmind.tasks.task_id`（1:1）；R 版改为 **NodeAttempt 独立建模**，`flowmind_task` 只是**可选**执行器之一，`executor_ref` 亦仅在该模式下填写。

### 3. 哪些能力作为公开 MCP 工具？

**原则（WB-00R 拍板）：只有「明确要公开的项目级 MCP 能力」才使用 FlowMind `@skill`。**

由于基座 `register_all()` **无差别**暴露整个 `_REGISTRY`（`server.py:44-54`，见审计 §2.1.4），**不存在「MCP 公开白名单 / 内外技能分离」机制**。因此隔离**必须**通过「**不注册**」实现：

| 作为公开 MCP 工具（用 FlowMind `@skill` 并登记 `_REGISTRY`） | 依据 |
|---|---|
| 基座既有只读/轻技能（`localize_status`、`localize_search`、`localize_download`、`localize_cancel`、`localize_retry`） | 基座已如此设计（`README.md:57-61`：轻技能走 MCP，长任务走 REST）；**兼容**保留 |
| CyberCut 明确要公开的**只读查询**（如「查 Project/Run 状态」「查 NodeRun 产物清单」「列模板」） | 幂等、低延迟、无副作用 |
| CyberCut 明确要公开的**人工审批提交**（如「通过/打回某 NodeRun」） | 幂等状态迁移，轻量 |

| **不作为**公开 MCP 工具（用 CyberCut 内部 `NodeHandler`，**不进 `_REGISTRY`**） | 依据 |
|---|---|
| **素材分析、ASR、脚本、分镜、素材匹配、Timeline、渲染、QC** | 重/长/需 GPU 或外部服务；且**内部能力不应对外暴露** |
| 一切 GPU/媒体长任务、批量生产、分钟级操作 | 走 CyberCut 自建执行器 + CyberCut REST/事件面，避免阻塞 `/mcp` 同步通道 |

**隔离实现方式（首版）**：CyberCut 把内部节点实现为**自有的 `NodeHandler`**（见 §4），**不写入** `flowmind/skills/`、**不追加** `skills/__init__.py`，因而**天然不会被 `register_all()` 暴露**。首版**不修改** FlowMind `server.py`、**不新增**任何可见性机制。

> 落地注意：基座 MCP 工具入参 schema 顶层多一层 `inp`（实测），CyberCut 的 MCP 客户端与契约测试须按 `{"inp": {...}}` 对齐。

### 4. 哪些能力仅作为内部 worker？

**所有「重、长、有副作用、需要 GPU/外部服务」的处理，仅作为 CyberCut 内部 `NodeHandler`，由 CyberCut 自建 worker 执行——既不暴露为 MCP 工具，也不进入 FlowMind `_REGISTRY`。**

| 仅内部 `NodeHandler` | 说明 |
|---|---|
| 素材分析 / ASR / OCR / 擦除 / TTS / 混音 / 渲染合成 | GPU/CPU 密集，分钟级 |
| 分镜生成 / 文案 / 脚本 / 素材匹配 / 时间线编排 | LLM 或多步计算，长且需重试 |
| 质检 | 需读取产物，长任务 |
| 批量生产调度 | 编排层行为，非原子能力 |
| 审批后的续跑（局部重跑） | 由 CyberCut 编排器触发**新的 `NodeAttempt`** |

**实现形态（WB-00R 修正）**：

- 这些能力实现为 **CyberCut 自有的 `NodeHandler`**（位于 `src/cybercut/nodes/`，CyberCut 自己的接口约定期望输入/输出/取消/进度），**不写入** `flowmind/skills/`、**不追加** `skills/__init__.py`；
- 因**未注册进 `_REGISTRY`**，它们**不会**被基座 `register_all()` 暴露为 MCP 工具（隔离即「不注册」，见 §3）；
- 执行由 **CyberCut 自建 worker** 承担：从 PG `NodeAttempt` 队列 **claim** 一条记录 → 调用对应 `NodeHandler` → 落终态并登记 `ArtifactRevision`；
- 若某 `NodeHandler` 需要复用基座媒体 helper（`_media`/`_local_asr`/`_inpaint` 等），**必须经 `cybercut/adapters/`** 调用（见 §7），**不得**让该能力变成基座 `@skill`。

> 与 WB-00 首版的差异（R 修正）：首版错误地描述为「以基座 `@skill` 实现并注册 `_REGISTRY`，仅口头承诺不对外」——该设计**在基座中无法成立**（注册即公开）。R 版改为 **CyberCut `NodeHandler` + 不注册**。

### 5. 是否允许修改 FlowMind 核心契约？

**不允许修改基座核心契约。** 边界为（红线）：

**禁止修改**（改之即破坏可升级性与对外 API）：`contracts.py`、`skill.py`、`manifest.py`、`discover.py`、`rules.py`、`server.py`、`server_http.py`、`server_rest.py`、`server_tasks.py`、`auth.py`、`tasks/*`（`__init__` 状态机常量、`store.py` 表结构、`manager.py` 语义、`events.py` 主题契约、`gpu.py`）。

理由：`CLAUDE.md:15-17` 与 `AGENTS.md:83-85` 明确禁止改框架层；基座维护冻结；且 `contracts.py` 变更会波及所有消费方。

**允许的接入方式**（仅填预留扩展点 + 新增，且**仅用于公开 MCP 技能**）：

1. `src/flowmind/skills/<name>.py` 新增 `@skill` 函数（不碰框架层）——**仅限明确要公开的项目级 MCP 能力**；
2. 在 `src/flowmind/skills/__init__.py` 白名单追加 import（**登记 = 对外公开**）；
3. `config.py` **新增** Config 类并纳入 `FlowmindConfig`（**新增**字段，不改既有字段语义）；
4. `examples/<name>_demo.py` 新增 demo。

**首版硬约束（WB-00R）**：**不修改 `server.py`**，也不新增任何「可见性/白名单」机制——内外隔离一律以「内部节点不注册」实现（见 §3/§4）。**CyberCut 内部 `NodeHandler` 不进入 `flowmind/skills/`**。

**例外与前置**：若基座存在**阻断性缺陷**（如 F-01），修复须作为**独立上游提交**、单独评审、可选择不改（由 CyberCut 侧经 `adapters/` 规避）。**WB-00/WB-00R 不做任何源码修改**。

### 6. CyberCut 代码应放在哪个包和 schema？

**包**：

- CyberCut **不复用** `flowmind` 包命名空间去放业务编排代码（避免与基座维护冻结的文件混居）。建议新建独立包 **`cybercut`**（源码根 `src/cybercut/`），分层：

```
src/cybercut/
├── contracts/     # Project / Run / NodeRun / NodeAttempt / ArtifactRevision / Approval / Script / Storyboard / Timeline 模型
├── orchestrator/  # 编排状态机、DAG、依赖、审批、重跑
├── nodes/         # CyberCut 内部 NodeHandler（媒体/LLM/渲染/质检）——不注册进基座 _REGISTRY
├── executor/      # CyberCut 自建执行器：PG NodeAttempt 队列 + worker claim（默认执行器）
├── adapters/      # 对基座私有 helper 与（可选）flowmind_task 的隔离适配层（见 §7）
├── api/           # CyberCut 自己的 REST / SSE / 控制面
└── storage/       # CyberCut 自己的 PG 仓储（cybercut schema）
```

- **默认执行路径**：`executor/` 的 CyberCut worker 从 `NodeAttempt` 队列 claim → 调用 `nodes/` 的 `NodeHandler` → 登记产物。
- 对基座的调用**只经**：`invoke()`（仅公开 MCP 技能）/（可选）任务 REST / `adapters/`。**不直接 import** `flowmind.tasks.store`、`flowmind.skills._*` 等内部实现（除经 `adapters/` 封装）。

**Schema（PostgreSQL）**：

- 基座硬依赖 PG，且任务表在统一库 `rak` 的 **`flowmind`** schema（`store.py:31-34`，ECO-ADR-0013）。
- CyberCut **同库不同 schema**：使用 **`rak.cybercut`**（业务表：`project` / `run` / `node_run` / `node_attempt` / `artifact_revision` / `approval` 等）。**不在 `flowmind` schema 建任何表**，也不改 `flowmind.tasks`。
- **不建立跨 schema 外键**：`node_attempt.executor_ref`（仅 `executor_kind=flowmind_task` 时填）对 `flowmind.tasks.task_id` 仅作**软引用**。
- 连接复用基座既有规格解析顺序（env → config.toml → 默认），避免引入第二套连接配置口径。
- 所有业务表必须带 `tenant_id`，与基座租户模型对齐（`auth.py:94-110`）。

**产物存储目录（WB-00R 新增）**：

- CyberCut 正式产物统一落 **`data/projects/{project_id}/artifacts/{artifact_id}/`**；
- 该目录**与基座 TTL 目录解耦**，不随 `flowmind.tasks` 的 TTL GC 清理（`manager.py:363-393` 只扫 `data_dir/tasks/`、`data_dir/outputs/`）；
- 若某 `NodeAttempt` 经 `flowmind_task` 适配器执行，`adapters/` 须在基座任务成功后**立即**把 `data.output_path` 文件复制进上述目录（基座 `output_paths` 只作**临时执行产物索引**）。

> 若 WB-01 决定「CyberCut 不依赖基座 PG」而用独立库，本 ADR 第 6 条需修订；但在基座已硬依赖 PG 的前提下，同库异 schema 是最低成本的方案。

### 7. 如何避免上游升级破坏 CyberCut？

采用**「固化 + 隔离 + 契约快照」三段式**：

| 手段 | 具体做法 |
|---|---|
| **(a) 版本固化** | CyberCut 锁定基座 **commit SHA**（当前 `c8ea1c6`）或打 tag；以 git submodule / vendored 快照 / 固定 wheel 形式引入；**不跟随 `main` 漂移**。依赖清单须补齐未声明项（如 `tomli_w`）并为无上界依赖（如 `starlette`）**设定上界**。 |
| **(b) 适配层隔离** | 所有对基座私有实现的调用收敛到 `cybercut/adapters/`（薄封装）。基座内部改动只影响适配层，业务层不受波及。**禁止**在业务层直接 import `flowmind.skills._*` / `flowmind.tasks.store`。 |
| **(c) 契约快照测试** | 在 CyberCut 侧建立「基座契约快照」测试（基座本身无 CI/测试）：断言 `SkillResult` 字段集、`ReasoningChain` 结构、`/api/v1/manifest` 形状、MCP `tools/list` 数量与 `inputSchema`（含 `inp` 包裹）、`tasks` 表列集与状态机常量、MQTT 主题/QoS 契约。任一变更即红灯。 |
| **(d) 不碰撞共享文件** | 基座 `skills/__init__.py`、`config.py` 是上游冲突热点。CyberCut 新增技能时**只追加**、按字母序，并记录到独立清单，降低 merge 冲突概率。 |
| **(e) 缺陷隔离** | 对 F-01 这类上游阻断缺陷：优先**CyberCut 侧适配规避**（如自建媒体节点），把「修复上游」与「CyberCut 交付」解耦，避免 CyberCut 进度被维护冻结的基座阻塞。 |
| **(f) 升级准入** | 基座升级须走独立评审：跑通契约快照测试 + 端到端冒烟（真实 Linux + GPU + PG + MQTT）方可升级；升级不得伴随任何 CyberCut 业务代码改动。 |

---

## 后果（Consequences）

**正面**

- 职责清晰：基座 = **公开 MCP 技能契约 + trace 信封 + `localize_*` 兼容 + 基础设施复用面**；CyberCut = 编排 + 领域模型 + 执行 + 对外面。避免重复造轮子（复用信封/推理链/trace/发现面/鉴权/租户/GPU lane）。
- 上游冻结 + 适配层 + 契约快照 → CyberCut 可长期稳定演进，不被基座缺失能力（DAG/HITL/局部重跑/多产物/多进程领取）卡死。
- 复用基座既有 PG/租户体系，无第二套鉴权；执行器自建后**摆脱**基座 `recover_running()` 的全局副作用。

**负面 / 代价**

- CyberCut 需自建：**默认执行器（PG `NodeAttempt` 队列 + worker claim）**、Project/Run/NodeRun/NodeRun 状态机、DAG 编排、审批、局部重跑、批量聚合、素材上传接收端、REST/SSE/事件面（基座无 SSE）、**多产物登记（`ArtifactRevision`）**、以及全部业务节点（素材分析→质检）。
- `flowmind_task` 适配路径必须在基座任务终态与 TTL（默认 3600s）之间**立即搬运产物**到 `data/projects/.../artifacts/`，否则正式产物丢失。
- 若复用基座媒体 helper，需维护适配层以对抗其对 `LocalizerConfig`（当前字段不全，F-01）及 GPU/ffmpeg 的强耦合。
- 「不修改核心契约 + 首版不改 `server.py`」意味着若干期望的便利（如给 `tasks` 加 `node_id` 列、给技能加 `public` 标记）不可行，须以「软引用 + 不注册」替代。

**未决项（WB-01 需拍板）**

1. F-01 的处置：等待上游修复 vs CyberCut 侧经 `adapters/` 规避（建议后者，并同步上报上游）；**不阻塞首版**。
2. CyberCut 是否**部分**复用基座媒体 helper，还是**全部自建**媒体节点（决定 `adapters/` 的厚度）。
3. 引入基座的方式：submodule / vendored / 固定 wheel（影响构建与发布）。
4. CyberCut 自身 REST/SSE 与控制面契约的具体形态（本 ADR 已定归属，形态待 WB-01 设计）。

> 已消除的未决项（WB-00R）：**CyberCut 执行器选型**——拍板为「默认自建」，FlowMind `TaskManager` 仅作 `localize_*` 兼容/可选适配器。
