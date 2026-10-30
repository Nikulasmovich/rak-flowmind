# WB-03 · 创作层：创作简报、脚本、智能分镜与素材匹配

> 承接 WB-02（素材接入与分析）。本阶段只做**结构化创作决策与版本化产物**，
> 不实现 TimelineSpec、渲染、字幕、TTS/BGM、REST/SSE/MCP、前端与真实 LLM 调用。
> **WB-03R / WB-03R2 定向整改已合入本文档**（版本身份统一、创作层定向修订、
> 证据全覆盖、manual 审批、Provider 错误码边界、Project/Run 组合约束；
> WB-03R2 追加：修订锁序、按 Run 的版本守卫、版本-审批状态联动、受控重开）。

## 1. 数据模型

### 1.1 新增迁移 `0004_creative_revisions.sql`

| 表 | 主键 | 说明 |
|---|---|---|
| `creative_brief_revisions` | `brief_revision_id` | 创作简报版本 |
| `script_revisions` | `script_revision_id` | 脚本版本 |
| `storyboard_revisions` | `storyboard_revision_id` | 分镜版本 |
| `asset_match_revisions` | `match_revision_id` | 素材匹配版本 |

四表结构一致（`project_id / run_id / tenant_id / revision_no / status /
payload_json / prompt_version / model_id / created_by / supersedes_id /
created_at / updated_at`），并遵循与 WB-01R/01R2 相同的约束策略：

- **复合外键**（不建跨 schema 外键）：
  `(project_id, tenant_id) → projects`、`(run_id, tenant_id) → runs`、
  `(supersedes_id, tenant_id) → 同表主键`（NULL 放行）；
- **唯一约束**：`UNIQUE (project_id, tenant_id, revision_no)`
  ⇒ 旧版本不可被覆盖；`UNIQUE (id, tenant_id)` 作为复合外键引用目标；
- **CHECK**：`revision_no >= 1`、`status IN ('draft','active','superseded','rejected')`；
- **索引**：`(project_id, run_id, revision_no DESC)`；
- `updated_at` 触发器复用 0001 的 `touch_updated_at`（幂等重建）；
- 文件可重复执行；不修改 0001 / 0002 / 0003。

### 1.1b 迁移 `0005_run_project_composite.sql`（WB-03R）

- 为 `runs` 建 `uq_runs_id_project_tenant (run_id, project_id, tenant_id)` 唯一键；
- 四张创作表原 `(run_id, tenant_id)` 弱外键替换为
  `(run_id, project_id, tenant_id)` 复合外键（ON DELETE CASCADE）；
- 同租户下「Project A + Project B 的 run」组合被数据库拒绝；
- 不修改 0001–0004。

### 1.2 Run 输入扩展

`runs.input_json` 新增冻结字段：

```json
{
  "asset_ids": ["..."],
  "assets": [{ "asset_id": "...", "media_kind": "video", "...": "..." }],
  "creative_input": {
    "target_platform": "douyin", "aspect_ratio": "9:16",
    "target_duration_us": 10000000, "language": "zh", "audience": "...",
    "objective": "...", "tone": "...", "visual_style": "...",
    "call_to_action": "...", "forbidden_claims": ["..."],
    "match_threshold": 0.55, "revision_hint": null
  },
  "approval_policy": { "mode": "hybrid", "gates": ["creative.script"] }
}
```

另外 `revision_requests`（WB-03R）由 `revise_node` / `revise_artifact` 写入，
键为节点键，值为 `{feedback, new_prompt, expected_revision, revision_index}`；
`_enqueue_attempt` 会把该节点的请求复制为 attempt 的 `revision_request`。
以上内容由 `freeze_run_input` 与 `_enqueue_attempt` 下发到**每个** attempt。

## 2. 领域契约（`contracts/creative.py`）

统一 `extra="forbid"`、整数微秒、拒绝 NaN/Infinity。`schema_version`：
`creative-brief.v1` / `script-revision.v1` / `storyboard-revision.v1` /
`asset-match.v1`。

| 模型 | 关键约束 |
|---|---|
| `CreativeBrief` | `target_duration_us > 0`；`aspect_ratio` 形如 `16:9`；`selling_points` 每项 **≥1** 条证据；证据素材必须 ∈ `source_asset_ids`；`source_asset_ids` 不得重复；必须携带 `prompt_version` / `model_id` |
| `ScriptRevision` | 每个 `section.target_duration_us > 0`；`total == Σ section`；`section_id` 唯一；`role` 取自枚举（hook/problem/solution/proof/benefit/cta）；每段 ≥1 条证据 |
| `StoryboardRevision` | `sequence_no` 必须为确定性 `0..n-1`；`total == Σ shot`；`candidate_segment_ids` 非空且不重复；`transition_hint` 取自枚举 |
| `AssetMatchResult` | `candidates` 必须按分数降序；选中项必须在候选内且同时给出 `score` + `score_breakdown`；同一 segment 不得被两个 shot 同时选中；`score_breakdown` 含六项且各自 ∈ [0,1] |

`EvidenceRef` 要求 `artifact_kind` 必须包含对应 `asset_id`。

## 3. 创作节点（`nodes/creative_nodes.py`）

| 节点 | 输入（冻结 `input_artifacts`） | 产物 kind |
|---|---|---|
| `creative.brief` | `asset.analysis.{asset_id}` × n + Run 冻结 `creative_input` | `creative.brief` |
| `creative.script` | `creative.brief` + `asset.analysis.{id}` | `creative.script` |
| `creative.storyboard` | `creative.script` + `creative.brief` + `asset.analysis.{id}` | `creative.storyboard` |
| `creative.match` | `creative.storyboard` + `asset.analysis.{id}` | `creative.match` |

统一行为：

- 只读冻结的 `input_artifacts`（重试不漂移）；
- 只写 `NodeContext.attempt_dir`，经 `ArtifactSpec` 发布；
- JSON 以 `sort_keys=True` 确定性序列化（内容相同 ⇒ 哈希相同）；
- 全部接入 `context.cancel_check`，取消返回稳定错误码 `CANCELLED`；
- 稳定错误码：`INPUT_MISSING` / `INPUT_INVALID` / `CREATIVE_SCHEMA_INVALID` /
  `CREATIVE_FACT_UNSUPPORTED` / `CREATIVE_PROVIDER_FAILED`；
- `awaiting_approval` 由 Run 冻结的审批策略决定。

**版本状态（WB-03R2）**：`status` 语义与转移规则见 §6.1。

**版本身份（WB-03R）**：节点只写**临时**身份
（`pending:{kind}-{run_id}-{attempt_no}`，`revision_no=1`，明显非权威值）。
执行器在**发布前、同一事务内**（`projects.lock` 之后）分配权威身份：

| 身份 | 来源 |
|---|---|
| `*_revision_id` | 该产物 `artifact_revision_id`（UUID4） |
| `revision_no` | 该产物 kind 在 project 内的 `max+1` |
| `project_id` / `run_id` | 当前 Run |

随后用 `finalize_payload` 改写 payload、确定性重写 staged 文件并重算哈希，
再发布与登记。`persist_for_artifact` 落库前用 `assert_payload_identity`
强制一致（不一致即回滚）。因此
**产物文件 / ArtifactRevision / 创作版本表 / payload 四者一一对应**，
且 `attempt_no` **绝不**充当业务 `revision_no`。

## 4. 适配层（`creative/`）

| 模块 | 职责 |
|---|---|
| `provider.py` | `CreativeProvider` 协议 + `GenerationRequest` / `GenerationResult`（`{payload, model_id, prompt_version, warnings}`）+ `ProviderError` |
| `fake_provider.py` | 确定性实现（默认启用）：无网络、无模型下载、无随机数、不读时钟；只陈述**可度量事实** |
| `prompts.py` | 版本化提示词（`creative-brief-prompt.v1` 等）+ `build_prompt` 确定性文本 |
| `validators.py` | schema 之外的**事实层**校验：素材归属、证据可达、**冻结输入**、**segment_index 真实性**、**project/run 身份一致**、反虚构数字、禁用声明 |
| `traces.py` | DecisionTrace 摘要（登记理由码 + 稳定哈希 + 反思维链守卫） |
| `matching.py` | 确定性素材匹配（六项打分明细 + 确定性决胜 + 反复用） |
| `policy.py` | 审批策略（`auto` / `hybrid` / `manual`）、创作层阶段映射 `creative_descendants`，与管线定义对齐 |

### 4.1 语料与事实来源

`FakeCreativeProvider` 的文案只由以下事实构成：

- 素材片段数、时长（图片无自然时长 ⇒ 只陈述画面片段数，不伪造「0 秒」）；
- 用户输入的目标/受众/语调/风格/CTA/禁用声明；
- 素材分析证据引用（`asset.analysis.{asset_id}`）。

## 5. 确定性素材匹配

Segment 身份：`f"{asset_id}#{segment_index:04d}"`。

打分明细（权重为模块常量，正权之和 = 1.0，`repetition` 为扣减项）：

| 项 | 计算方式 |
|---|---|
| `semantic_score` | shot 关键词（intent/subject/visual_requirement/emotion）与候选关键词池（素材文件名 + 该段转写文本）的重合比 |
| `quality_score` | 分辨率归一 + 关键帧存在加成 |
| `duration_score` | 时间型 segment：`min(1, seg_duration / shot_duration)`；非时间型固定 0.5 |
| `continuity_score` | 与前一 shot 同 section 同素材 1.0 / 同 section 换素材 0.7 / 跨 section 0.5 |
| `repetition_penalty` | 已被选中 = 1.0；已被列为候选 = 0.3；否则 0 |
| `evidence_score` | shot 证据中与该候选同素材的比例（无证据时 0.5 中性） |

选择与告警：

- 分数降序，**同分按 `segment_id` 升序**决胜；
- 已被占用的 segment 加满惩罚并淘汰（**禁止重复使用**）；
- 某 shot 的全部候选都被占用 ⇒ `MATCH_ALL_CANDIDATES_USED:{shot_id}`；
- 最高分 < 阈值 ⇒ `selected_segment_id = None` 且 `MATCH_BELOW_THRESHOLD:{shot_id}`；
- 引用不存在的 segment ⇒ `invalid_reference` 淘汰 + `INVALID_SEGMENT_REFERENCE` 告警；
- 未知 `algorithm_version` 或非法 `threshold` 直接抛错。

## 6. 审批策略

| 模式 | 管线 | 行为 |
|---|---|---|
| `auto` | `CREATIVE_PIPELINE` | 无审批门，全自动跑完九个节点 |
| `hybrid` | `CREATIVE_PIPELINE_HYBRID` | 在 `creative.script` 挂人工审批门；Run 停在 `waiting_approval`，审批通过后继续 |
| `manual` | `CREATIVE_PIPELINE_MANUAL` | **四个创作节点全部**挂审批门，依次人工放行（WB-03R） |

**驳回语义（WB-03R）**：只重跑被驳节点自身，且**只失效其创作层下游**
（`creative.brief → script → storyboard → match`）；媒体层五节点永不失效或重跑；
人工 `feedback` 冻结进新 attempt，新 attempt 产出**新版本**，
旧版本与 `rejected` 审批记录原样保留。

**定向修订（WB-03R）**：`Orchestrator.revise_node(node_run_id, ...)` /
`revise_artifact(artifact_revision_id, ...)`，签名含
`expected_revision`（乐观守卫）、`feedback`、`new_prompt`：

| 被修订节点 | 失效范围 |
|---|---|
| `creative.brief` | script / storyboard / match |
| `creative.script` | storyboard / match |
| `creative.storyboard` | match |
| `creative.match` | 无（不失效上游） |

被失效节点清空 `input_revision_refs`，首次重新入队时冻结**新的**上游版本；
未失效节点不重跑。

**锁序（WB-03R2）**：`revise_node` 严格遵循唯一全序
`Run → NodeRun → NodeAttempt → Project` —— 先**非加锁**读取 `run_id` 以确定
加锁目标，随后先锁 Run、再锁 NodeRun，并在**持锁后重读**节点状态再校验；
Project 由后续调用在最内层加锁。

**版本守卫（WB-03R2）**：`expected_revision` 与
`creative_revisions.latest_for_run(...)`（**目标 Run 内**该 kind 的最新版本）比较，
而**不是**项目级最新版本；项目级 `revision_no` 分配规则不变（跨 Run 连续递增）。

**受控重开（WB-03R2）**：已完成（`succeeded` / `completed`）的 Run 与 Project
**不通过**通用状态迁移重开（状态机表终态仍无出边），而是走受控专用函数
`runs.reopen_for_revision` / `projects.reopen_for_revision`（仅接受
`succeeded` / `completed`，并清空 `finished_at` / `failure_code` /
`failure_message` / `archived_at`），仅由 `_reopen_for_revision` 调用。

### 6.1 版本状态与审批联动（WB-03R2）

| 状态 | 含义 |
|---|---|
| `draft` | 待审批（人工模式新版本，**不得**为 `active`） |
| `active` | 当前生效版本，`(project, tenant, kind)` 内**唯一** |
| `superseded` | 被更新的 `active` 取代 |
| `rejected` | 人工驳回 |

- **自动模式**：登记即 `active`，同事务把旧 `active` 置 `superseded`；
- **人工模式**：登记为 `draft`；批准 → 该版本 `active` 且旧 `active` → `superseded`；
  驳回 → 该版本 `rejected`（既有 `active` 不变）；
- 审批事件记录 `activated` / `rejected_version`（kind / revision_id /
  revision_no / status）。

策略以 `ApprovalPolicy(mode, gates)` 表达，随 Run 冻结；管线定义的
`NodeSpec.approval_required` 与策略 `gates` 由测试断言保持一致。

## 7. 管线（DAG）

```
asset_probe ──> asset_normalize ──┬──> scene_detect ────────┐
                                  └──> speech_transcribe ───┴──> asset_analysis
                                                                      │
                                        ┌─────────────────────────────┤
                                        ▼                             │
                                 creative.brief                       │
                                        │                             │
                                        ▼                             │
                                 creative.script ◄────────────────────┤
                                        │                             │
                                        ▼                             │
                                 creative.storyboard ◄────────────────┤
                                        │                             │
                                        ▼                             │
                                 creative.match ◄─────────────────────┘
```

`creative.script` / `creative.storyboard` / `creative.match` 额外依赖
`asset_analysis`（事实约束与候选素材），是任务书 DAG 的**超集**。

## 8. Project 状态聚合（WB-03R2-复审）

**语义澄清**：Project 状态由 **`cybercut.runs` 中该 Project 全部 Run 的状态聚合**
得出；`projects.current_run_id` 只是「最近一次启动的 Run」的**指针**，
**不**决定 Project 状态。

聚合规则（`Orchestrator._aggregate_project_status`）：

| 条件 | Project 状态 |
|---|---|
| 存在任一**非终态** Run（`queued` / `running` / `waiting_approval` / `cancelling`） | **保持 `active`**（不得进入终态） |
| 全部 Run 终态，且任一 `failed` | `failed` |
| 全部 Run 终态，且任一 `cancelled`（无 `failed`） | `cancelled` |
| 全部 Run 终态且全部 `succeeded` | `completed` |

- 终态优先级：`failed > cancelled > completed`；
- 聚合经**受控路径** `projects.apply_aggregate_status(...)` 写入：
  目标必须是终态；与当前一致则幂等；当前为 `active` 走通用状态机迁移；
  当前为**另一个终态**时允许**聚合修正**（例如先前被较早 Run 完成误标为
  `completed`，随后当前 Run 失败 ⇒ 修正为 `failed`，不得保持错误状态）；
- 原 `_finish_project`（只看触发聚合的那一个 Run）已删除。

**两段式判定（WB-03R2-复审）**：聚合分两步，消除
「Run 状态快照 → 新 Run 提交 → 落终态」竞态：

1. **① 快照**（未加项目锁）：仅用于提前短路 —— 已有活动 Run 则直接返回；
   该路径只会**避免**写终态，绝不会造成误标；
2. **② 权威判定**：在与 `start_run` 一致的 **Project 锁**
   （`projects.get_for_update`）保护下**重新读取**全部 Run 状态并计算聚合。

由此「新 Run 的创建」与「项目终态写入」被同一把项目锁串行化：
新 Run 先提交 ⇒ 锁内重读发现活动 Run ⇒ 项目保持 `active`；
终态写入先提交 ⇒ `start_run` 因项目已终态被拒绝。
回归测试：`test_project_aggregation.py::test_new_run_commit_between_snapshot_and_terminal_write_keeps_project_active`、
`::test_aggregate_takes_project_lock_before_authoritative_read`。

覆盖测试：`tests/cybercut/test_project_aggregation.py`
（旧 Run 完成而新 Run `queued` / `running` ⇒ `active`；
旧 Run `cancelled` ⇒ `active`；新 Run 成功 ⇒ `completed`；
新 Run 失败 ⇒ `failed`；混合终态失败优先；
错误 `completed` 被修正；`current_run_id` 仅为指针）。

## 9. 已知限制

1. 未接入真实 LLM（只有确定性 `FakeCreativeProvider`）；
2. 未实现 TimelineSpec / 渲染 / 字幕 / TTS / BGM / 混音；
3. `semantic_score` 为轻量分词重合，不是语义向量检索；
4. 无说话人分离、无素材语义 embedding；
5. 未实现版本回滚/合并（仅 `supersedes_id` 链与 `status` 迁移）；
6. 分镜镜头数由脚本时长决定：素材不足时如实报告未匹配镜头，不降级、不伪造；
7. 版本状态在 `(project, tenant, kind)` 内保证至多一个 `active`；
   跨 Run 修订会让旧 Run 的 `active` 版本变为 `superseded`（单一「当前版本」模型）；
8. 终态重开是**受控例外**（专用函数），不是通用状态迁移能力。
