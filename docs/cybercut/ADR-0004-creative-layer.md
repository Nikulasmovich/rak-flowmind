# ADR-0004 · 创作层：结构化创作决策、版本化产物与确定性素材匹配

- 状态：已接受（WB-03）
- 基线：`913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（WB-02R6 之后）
- 关联：`contracts/creative.py`、`creative/`、`storage/creative_revisions.py`、`nodes/creative_nodes.py`、`migrations/cybercut/0004_creative_revisions.sql`
- 承接：ADR-0002（节点执行与领取）、ADR-0003（素材资产与分析）

## 背景

WB-02 已交付「素材接入 → 探测 → 标准化 → 镜头切分 → ASR → 结构化分析」的可靠后端。
WB-03 需要在其之上实现**创作层**：从素材事实出发产出创作简报、脚本、分镜与素材匹配，
且必须满足三条硬约束：

1. **可追溯**：任何文案性表述都要能追溯到素材分析证据，不允许凭空编造；
2. **可版本化、可审批、可局部重跑**：中间产物有版本、可挂人工审批、可单独重算而不污染历史；
3. **确定性**：素材选择不得依赖模型自由发挥，同一输入必须得到同一结果。

本阶段**不**实现 TimelineSpec、渲染、字幕烧录、TTS/BGM、REST/SSE/MCP、前端，
且不引入真实 LLM 网络调用。

## 决策

### 1. LLM 只能产出结构化创作决策
新增 `creative/provider.py` 定义统一接口：`generate(GenerationRequest) -> GenerationResult`，
`GenerationResult` 固定为 `{payload, model_id, prompt_version, warnings}`。
Provider **只**接收结构化上下文、**只**返回结构化 payload，**不得**执行文件操作、命令或
修改数据库；`model_id` 必填，缺失即 `ProviderError`。
默认且当前唯一启用的是 `FakeCreativeProvider`（确定性、不联网、不下载模型）。

### 2. 所有模型输出双重复核：schema + 事实证据
`creative/validators.py` 在 Pydantic 之外再做**事实层**校验：

- **素材归属**：`source_asset_ids` / 证据必须来自**当前 Run 冻结**的素材；
- **证据可达**：`evidence_refs.artifact_kind` 必须指向真实存在的分析产物
  （`asset.analysis.{id}` / `asset.scenes.{id}` / `asset.probe.{id}`）；
- **反虚构**：文案中的**数字**必须能在「用户输入 + 素材分析事实」中找到依据
  （`unsupported_numbers`），否则 `UNSUPPORTED_FACT`；
- **禁用声明**：`forbidden_claims` 不得出现在任何对外文案中，否则 `FORBIDDEN_CLAIM`。

同一校验在执行器落库前**再次**执行（`creative_revisions.validate_payload`），
保证任何路径都无法把非法负载写进版本表。

### 3. 创作产物一律经过 ArtifactRevision，并旁落版本表
节点只写 `NodeContext.attempt_dir`，正式路径由执行器分配（沿用 R3 模型）。
执行器在**同一事务**内，对 `kind ∈ {creative.brief, creative.script,
creative.storyboard, creative.match}` 的产物，读取已发布的正式文件并登记到
`creative_brief_revisions` / `script_revisions` / `storyboard_revisions` /
`asset_match_revisions`。任一步失败即整体回滚，**绝不发布非法产物**。

### 4. 版本不可覆盖，`revision_no` 在 Project 锁下分配
四张版本表均有 `UNIQUE (project_id, tenant_id, revision_no)`；`revision_no` 由
`creative_revisions.insert` 在 `projects.lock()` 下取 `max+1`（锁序中 Project 为最内层），
并自动与上一版本建立 `supersedes_id` 版本链。旧版本永久保留，可审计、可对比。

`payload_json` 只保存**结构化产物**；决策过程以 `decision_trace` 摘要随负载保存。

### 5. DecisionTrace 只保存摘要，不保存思维链
`creative/traces.py` 规定摘要结构：`step / decision / rationale_code /
inputs_digest / outputs_digest / evidence_count / notes`。
- `rationale_code` 为**登记枚举**（机器可读），未知理由码直接拒绝；
- `inputs_digest` / `outputs_digest` 为稳定哈希（可追溯、不可逆）；
- `assert_no_chain_of_thought` 守卫禁止 `chain_of_thought` / `reasoning` /
  `messages` 等字段进入摘要。

### 6. 素材匹配必须是确定性代码
`creative/matching.py` 不依赖任何 provider：

- Segment 身份：`AssetSegment` 无独立主键，故以 `f"{asset_id}#{index:04d}"` 作为
  Run 内唯一标识；
- 打分分解为六项（`semantic / quality / duration / continuity /
  repetition_penalty / evidence`），权重为模块常量，正权之和 = 1.0；
- 选择规则：加权分降序，**同分按 `segment_id` 升序**决胜（完全可复现）；
- **禁止复用**：已被选中的 segment 对后续 shot 加满惩罚（1.0）并淘汰，
  若某 shot 的全部候选都已被占用，输出 `MATCH_ALL_CANDIDATES_USED`；
- **低于阈值不伪造**：输出 `MATCH_BELOW_THRESHOLD` 并令
  `selected_segment_id=None`；引用不存在的 segment 记 `invalid_reference` 并告警；
- 算法版本 `MATCH_ALGORITHM_VERSION` 参与契约，传入未知版本直接拒绝。

### 7. 审批策略：全自动 / 混合
`creative/policy.py` 提供 `ApprovalPolicy(mode, gates)`：

- `auto`：无审批门（`gates` 必须为空）；
- `hybrid`：在指定创作节点挂审批门（默认 `creative.script`）。

策略随 Run **冻结**进 `runs.input_json.approval_policy` 并下发到**每个** attempt；
节点据此返回 `awaiting_approval`，执行器据此创建 pending 审批
（复用 WB-01 的 `approvals` 模型与乐观并发守卫）。
管线定义（`CREATIVE_PIPELINE` / `CREATIVE_PIPELINE_HYBRID`）与策略保持一致，
并由测试断言二者对齐。

### 8. Run 级创作输入随 Run 冻结
`StartRunInput` / `freeze_run_input` 扩展承载 `creative_input`（目标平台、画幅、
时长、受众、目标、语调、风格、禁用声明、修订提示词、匹配阈值等）与
`approval_policy`；`_enqueue_attempt` 将其下发到**每个** attempt，
因此 root 与下游节点的输入不随时间漂移。

### 9. 依赖关系（比「顺序链」多一条必要边）
创作节点除需要上游**创作产物**外，还需要**素材分析产物**（事实约束与候选素材），
故 `creative.script` / `creative.storyboard` / `creative.match` 均显式依赖
`asset_analysis`。这是任务书 DAG 的**超集**（不缺少任何要求的边）。

## 备选方案与取舍

| 备选 | 未采用的原因 |
|---|---|
| 让 Provider 直接写库/写文件 | 违反「LLM 不得执行文件操作或修改数据库状态」；无法审计与回滚 |
| 只在 Pydantic 层校验 | 结构合法但事实虚构的负载仍会被接受（如编造数字/禁用声明） |
| 由 LLM 自由挑选素材 | 不可复现、不可解释、无法保证不复用；改为确定性打分 + 可解释明细 |
| 把版本号交给模型或调用方 | 并发下会覆盖旧版本；改在 Project 锁下分配并加唯一约束 |
| 每个 shot 一个 ArtifactRevision | 产物数量爆炸且难以整体审批；改为一次匹配一个版本化 JSON |
| 用 `None` 表示「无候选」并静默跳过 | 会掩盖素材不足；改为显式 warning（或进入人工审批） |

### 10. 版本身份唯一：数据库分配，四处一致（WB-03R 决策 23）
此前节点自造的 `script-{run_id}-{attempt_no}` 与版本表分配的 id / `revision_no`
相互独立，且 `attempt_no` 被当作业务 `revision_no` 使用，导致
「产物文件 / ArtifactRevision / 创作版本表 / payload」四者可追溯性不闭合。改为：

- 节点只写**临时**身份（`pending:{kind}-{run_id}-{attempt_no}` + `revision_no=1`）；
- 执行器在**发布前、同一事务内**（`projects.lock` 之后）分配权威身份：
  `artifact_revision_id`（UUID4）与该产物 kind 的 `revision_no`（`max+1`），
  用 `creative_revisions.finalize_payload` 改写 payload，**确定性重写 staged 文件**
  并重算 SHA-256/大小，再发布并登记；
- `creative_revisions.persist_for_artifact` 落库前用 `assert_payload_identity`
  **强制** payload 身份与 ArtifactRevision 完全一致，不一致即整体回滚；
- 因此四处恒等：`payload[id_field] == 版本表[id_field] == artifact_revision_id`、
  `payload.revision_no == 版本表.revision_no == artifact_revisions.revision_no`、
  `payload.project_id/run_id == ArtifactRevision.project_id/run_id`，
  且正式产物文件内容与版本表 `payload_json` 逐字节一致。

### 11. 驳回 / 修订只作用于创作层（WB-03R 决策 24）
原驳回逻辑会失效**上游依赖**并级联重跑，把 `asset_analysis` 等媒体节点
（乃至 `asset_probe`）一并拖入重跑。改为：

- 驳回与定向修订**只重跑被作用节点自身**；
- 只失效其**创作层下游**（`creative.brief → script → storyboard → match` 链），
  由 `creative.policy.creative_descendants` 精确给出；
- 媒体层五节点（`asset_probe` / `asset_normalize` / `scene_detect` /
  `speech_transcribe` / `asset_analysis`）**永不**被失效或重跑；
- 被失效节点同时清空 `input_revision_refs`，使其首次重新入队时冻结**新的**
  上游版本；未失效节点不重跑。

### 12. 定向修订请求随 Run 冻结（WB-03R 决策 25）
新增 `Orchestrator.revise_node` / `revise_artifact`：

- `expected_revision`（乐观守卫）必须等于该节点当前创作产物的 `revision_no`；
- `feedback` / `new_prompt` 写入 `runs.input_json.revision_requests[node_key]`
  （已冻结的 `asset_ids` / `assets` / `creative_input` / `approval_policy` 原样保留），
  并由 `_enqueue_attempt` 复制进**新 attempt** 的 `input_json.revision_request`；
- 修订后的新 attempt 产出**新版本**（`revision_no` 递增），
  旧版本与历史（含 `rejected`）审批记录全部保留。

### 13. 修订需要重开 Run / Project（WB-03R 决策 26；**已被决策 33 取代**）
> **WB-03R2 更正**：原实现通过放开状态机表的终态出边来实现重开，属于
> 「全局开口」——任何调用方都能把终态 Run/Project 迁回活动态。
> 现已改为**受控专用路径**，状态机表恢复「终态无出边」。见决策 33。

### 14. 证据校验全覆盖（WB-03R 决策 27）
`creative/validators.py` 对 `CreativeBrief` / `ScriptRevision` /
`StoryboardRevision`（**顶层与每个 section / shot**）与 `AssetMatchResult` 统一执行：

1. **素材归属**：证据 `asset_id` 必须属于当前 Run 的分析产物；
2. **产物可达**：`artifact_kind` 必须是真实存在的分析产物 kind；
3. **冻结输入**：`artifact_kind` 必须属于当前 Run 冻结的 `input_artifacts`；
4. **分段存在**：`segment_index` 非空时必须真实存在于对应
   `AssetAnalysisReport.segments`；
5. **身份一致**：`project_id` / `run_id` 必须与执行上下文一致。

新增稳定错误码：`EVIDENCE_NOT_FROZEN`、`EVIDENCE_SEGMENT_MISSING`、
`PROJECT_MISMATCH`、`RUN_MISMATCH`、`BRIEF_EVIDENCE_UNREACHABLE`。

### 15. 审批三档：auto / hybrid / manual（WB-03R 决策 28）
新增 `ApprovalMode.MANUAL`、`MANUAL_POLICY`（默认对四个创作节点**全部**审批）
与 `CREATIVE_PIPELINE_MANUAL`：

| 模式 | 语义 |
|---|---|
| `auto` | 无审批门（`gates` 必须为空） |
| `hybrid` | 显式 `gates`（默认 `creative.script`） |
| `manual` | 默认四个创作节点全部审批，可显式覆盖 `gates` |

非 `auto` 模式**必须**显式配置至少一个门；策略随 Run 冻结，
管线定义的 `approval_required` 与策略 `gates` 由测试断言一致。

### 16. ProviderError 在 handler 边界归一（WB-03R 决策 29）
`nodes/creative_nodes.py::_generate` 捕获 `ProviderError` 及其他异常，
统一转换为 `NodeExecutionError(error_code="CREATIVE_PROVIDER_FAILED")`，
使 `attempt.failure_code` 稳定可观测，**不得**在 Runner 中退化成
`HANDLER_EXCEPTION`。

### 17. Project/Run 组合约束（WB-03R 决策 30）
创作层四表原先只有 `(run_id, tenant_id)` 弱外键：同租户下把 Project B 的
`run_id` 写进 Project A 的行仍会通过。`0005_run_project_composite.sql`：

- 为 `runs` 建 `uq_runs_id_project_tenant (run_id, project_id, tenant_id)` 唯一键；
- 四表改用 `(run_id, project_id, tenant_id)` 复合外键，并移除原弱外键；
- 跨 Project 的 run 组合直接 SQL 插入也必须 `ForeignKeyViolation`。

### 18. 修订路径的锁序与版本守卫作用域（WB-03R2 决策 31）
两处缺陷被更正：

1. **锁序**：`revise_node` 原先先锁 `node_run` 再锁 `run`（并在其中调用
   `projects.get_for_update`），违反唯一全序
   `run → node_run → node_attempt → project`，与执行器的成功提交路径
   （`_succeed` 先锁 run/node_run/node_attempt，最后由登记步骤锁 project）
   构成反向加锁，存在死锁风险。
   现改为：先用**非加锁**读取取得 `run_id`（仅用于确定加锁目标），
   随后严格按全序加锁 —— **先锁 Run，再锁 NodeRun**，Project 由后续调用
   最内层加锁；节点的**状态在持锁之后重新读取并校验**，不复用锁前快照。
2. **版本守卫作用域**：`expected_revision` 原先与
   `creative_revisions.latest(project, kind)`（**项目级最新版本**）比较。
   同一 project 存在多个 Run 时，较新 Run 会抬高项目级版本号，
   导致较旧 Run 永远无法用自己的当前版本通过守卫。
   现改为与 `latest_for_run(run_id=...)`（**目标 Run 内**该 kind 的最新版本）比较；
   **项目级** `revision_no` 分配规则保持不变（跨 Run 连续递增），
   仅在冲突详情中一并回显 `project_latest` 便于排查。

`creative_revisions.latest_for_run` 为本次新增的显式接口。

### 19. 创作版本与审批状态联动（WB-03R2 决策 32）
版本状态语义（`creative_*_revisions.status`）：

| 状态 | 含义 |
|---|---|
| `draft` | **待审批**（人工模式下新产出的版本，**不得**标记为 `active`） |
| `active` | 当前生效版本；在 `(project, tenant, kind)` 内**唯一** |
| `superseded` | 被更新的 `active` 版本取代 |
| `rejected` | 人工驳回 |

转移规则：

- **自动模式**（该节点无审批门）：执行器登记时直接写 `active`，
  并在**同一事务**内把同 `(project, tenant, kind)` 的旧 `active` 置 `superseded`
  （`creative_revisions.supersede_others`）；
- **人工模式**：新版本登记为 `draft`，既有 `active` 保持不变；
  - 审批通过 ⇒ `creative_revisions.activate`：该版本 `active`，旧 `active` 变 `superseded`；
  - 审批驳回 ⇒ `creative_revisions.mark_rejected`：该版本 `rejected`，
    既有 `active` 不受影响；
- 审批事件（`approval.approved` / `approval.rejected`）的 `payload_json`
  记录 `activated` / `rejected_version`（含 `kind` / `revision_id` / `revision_no` /
  `status`），保证状态迁移可审计。

### 20. 终态重开：受控专用路径，不开放全局出边（WB-03R2 决策 33）
状态机表**恢复**为「`run.succeeded` / `project.completed` 无出边」，
通用 `update_status` 对终态一律抛 `InvalidTransitionError`。

定向修订所需的重开由 **storage 层受控函数**实现：

- `runs.reopen_for_revision(conn, run_id, tenant_id)`：
  仅接受 `succeeded`，置 `running` 并**清空终态元数据**
  （`finished_at` / `failure_code` / `failure_message`）；
- `projects.reopen_for_revision(conn, project_id, tenant_id)`：
  仅接受 `completed`，置 `active` 并清空 `archived_at`。

两者**只**由 `Orchestrator._reopen_for_revision`
（即 `revise_node` / `revise_artifact` 的受控修订路径）调用，
并在 `events` 中写入 `run.reopened`（`payload.path = "revision"`）。
非终态或非 `succeeded`/`completed` 的输入一律 `ConflictError`。

### 21. Project 状态由「项目下全部 Run」聚合（WB-03R2-复审 决策 34）
复审发现：`_aggregate(run_id)` 只看**触发聚合的那一个 Run** 就把 Project 置为
`completed` / `failed` / `cancelled`。同一 Project 存在多个 Run 时，
较旧 Run 先完成会把仍有活动 Run（新 Run 仍 `queued` / `running`）的项目
误标为终态；随后当前 Run 失败时，项目又会**保持**错误的 `completed`。

**语义澄清**：

- Project 状态由 **`cybercut.runs` 中该 Project 全部 Run 的状态聚合**得出；
- `projects.current_run_id` **只是**「最近一次启动的 Run」的指针，
  **不**决定 Project 状态（`projects.set_current_run` 也不触发状态迁移）。

实现：

- 新增 `runs.statuses_for_project(conn, project_id, tenant_id)`（无 limit）；
- 新增 `projects.apply_aggregate_status(conn, project_id, tenant_id,
  to_status, reason)` —— **受控聚合路径**：
  - 目标必须是终态（`completed` / `failed` / `cancelled`）；
  - 与当前状态一致 → 幂等返回；
  - 当前为 `active` → 走通用状态机迁移；
  - 当前为**另一个终态** → 允许**聚合修正**（终态间修正），使
    「先前被误标为 `completed`，随后有 Run 失败」能被纠正为 `failed`；
- `Orchestrator._aggregate_project_status` 取代 `_finish_project`：
  只要存在任一非终态 Run（`queued` / `running` / `waiting_approval` /
  `cancelling`）就**不**进入终态；全部终态时按
  `failed > cancelled > completed` 取优先级。

`_finish_project`（只看单个 Run）已删除；三处调用点全部改走聚合。

**两段式判定（WB-03R2-复审 决策 35）**：聚合若在**不加锁**的情况下
读取 Run 状态快照并直接落终态，会与 `start_run` 产生竞态
——「快照（无新 Run）→ 新 Run 提交 → 落终态」会让新创建的 Run
出现在一个已终结的 Project 里。改为：

1. **① 快照**（未加项目锁）：仅用于提前短路 —— 快照中已有活动 Run 则直接返回；
   该路径只会**避免**写终态，绝不会造成误标；
2. **② 权威判定**：在与 `start_run` 一致的 **Project 锁**
   （`projects.get_for_update`，即 `start_run` 创建 Run 前持有的同一把锁）
   保护下**重新读取**全部 Run 状态并计算聚合结果。

由此「新 Run 的创建」与「项目终态写入」被同一把项目锁串行化：
新 Run 先提交 ⇒ 锁内重读发现活动 Run ⇒ 项目保持 `active`；
终态写入先提交 ⇒ `start_run` 因项目已终态被拒绝。

## 后果

- 创作产物与素材产物共享同一套不可变发布模型 ⇒ 崩溃孤儿目录、GC 需求同 ADR-0003；
- `payload_json` 体积随分镜规模增长 ⇒ 后续如需大对象存储需另行设计；
- 分镜镜头数由脚本时长决定，素材不足时**如实报告**未匹配镜头，不降级也不伪造；
- `revision_no` 单调递增且不可覆盖 ⇒ 局部重跑产生新版本，旧版本可对比审计；
- 混合 / 全人工模式下 Run 会停在 `waiting_approval`，需外部审批推进（与 WB-01 一致）；
- Run/Project 可在**受控修订路径**重开（专用函数 + 终态元数据清理）⇒
  终态语义仍由状态机表保持严格，重开是显式、可审计的例外；
  `failed` / `cancelled` 始终为硬终态；
- 版本状态在 `(project, tenant, kind)` 内保证至多一个 `active`
  ⇒ 下游「当前版本」读取无歧义；
- Project 状态由全部 Run 聚合 ⇒ 多 Run 项目的终态只在**全部 Run 终结**后产生，
  且终态间允许受控修正（消除「早期完成误标」的后遗症）；
  修订重开单个 Run 不会让 Project 保持在错误的终态；
- 版本号在 project 内跨 Run 连续 ⇒ 同一 project 的多次 Run 与多次修订共享同一版本序列。

## 已知限制

1. 未接入真实 LLM：`FakeCreativeProvider` 只做确定性的模板化决策；
2. 未实现 TimelineSpec / 渲染 / 字幕 / TTS / BGM / 混音；
3. 素材匹配的 `semantic_score` 采用轻量分词（ASCII 词 + 中文二元组），
   不是语义向量检索；
4. 未实现版本回滚/合并（只有 `supersedes_id` 链与 `status` 迁移）。
