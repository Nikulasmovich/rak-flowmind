# ADR-0005 · 编辑规划与模板绑定层（WB-04）

- 状态：已接受（WB-04）
- 基线：`913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（WB-03R2 及两次复审之后）
- 关联：`contracts/editing.py`、`editing/`、`nodes/edit_nodes.py`、
  `migrations/cybercut/0006_edit_planning.sql`、ADR-0004
- 承接：ADR-0004（创作层）、ADR-0003（素材资产与分析）

## 背景

WB-03 已交付 `creative.brief → creative.script → creative.storyboard → creative.match`
的可版本化创作链。WB-04 在其之后增加**编辑规划**能力：把已匹配的分镜转换为
确定性、整数帧的时间线，并绑定到可版本化模板清单。本阶段**只产结构化 JSON
规划产物**，不做任何媒体渲染（渲染/字幕/TTS/BGM/混音留给 WB-05+）。

## 决策

### 1. TimelineSpec 的权威身份与版本来源
与创作层完全一致（ADR-0004 决策 23）：

- 节点只写 `pending:*` 临时身份；
- 执行器在**发布前、同一事务内**（`_finalize_creative_payloads`）分配权威身份
  `artifact_revision_id` + 该 artifact kind 的 `revision_no`，改写 payload 并
  确定性重写 staged 文件后重算哈希；
- `creative_revisions.persist_for_artifact` 用 `assert_payload_identity`
  强制「产物文件 / ArtifactRevision / 版本表 / payload」四者一致。

**版本表**：新增 `0006_edit_planning.sql`，建 `edit_plan_revisions` 与
`template_bind_revisions`（与 0004 同构：复合外键 projects / runs
`(run_id, project_id, tenant_id)`（按 0005）、`UNIQUE (project_id, tenant_id,
revision_no)`、status CHECK、supersedes 自引用）。选择新增迁移而非复用
`artifact_revisions`，是为了复用创作层的 draft/active/superseded/rejected
版本状态机与审批联动（`activate` / `mark_rejected` / `supersede_others`），
避免另造一套版本框架。

### 2. 两个新节点的输入输出 artifact kind

| 节点 | 冻结输入（`input_artifacts`） | 产物 kind | 版本表 |
|---|---|---|---|
| `edit.plan` | `creative.storyboard` + `creative.match` + `creative.script` + `creative.brief` + `asset.analysis.{asset_id}` | `edit.plan` | `edit_plan_revisions` |
| `template.bind` | `edit.plan` + `creative.script` | `template.bind` | `template_bind_revisions` |

节点只读 attempt 冻结的 `input_artifacts`，**禁止**查询“最新版本”替代冻结引用
（沿用 WB-02 §三.4-9 与 WB-03R 的冻结语义）。

管线依赖（`CREATIVE_PIPELINE` 扩展为 11 节点）：

```
creative.match → edit.plan → template.bind
   └──────────────┘┘（edit.plan 另依赖 creative.storyboard / creative.script /
                      creative.brief / asset_analysis）
                    （template.bind 另依赖 creative.script）
```

### 3. 模板选择来源及默认规则
模板是**纯数据清单**（`TemplateManifest`），不执行代码或表达式。三个首版内置模板
`product_highlight` / `event_promo` / `brand_story`，版本固定 `1.0.0`，经
`TemplateManifest` schema 校验后注册，可枚举、可查询。

选择来源（按优先级）：

1. **显式指定**：`runs.input_json.creative_input.edit_input.template_id`
   （随 Run 冻结并下发到每个 attempt）。指定的 `template_id` 不存在 ⇒ 节点以
   稳定错误码 `TEMPLATE_NOT_FOUND` 失败（**fail-closed**，不做静默回退）；
2. **默认规则**（未指定时，**确定性**）：在支持当前时间线画幅
   （`aspect_ratio ∈ applicable_aspect_ratios`）的清单中，按 `template_id`
   升序取第一个；记录 `selection_reason="default:first_by_template_id"`。

旧 `CREATIVE_PIPELINE` 输入（无 `creative_input.edit_input`）⇒ 走默认规则，
**无需修改任何既有输入**（向后兼容）。

### 4. timeline 审批 gate 如何接入现有 `ApprovalPolicy`
`template.bind` 纳入 `PLAN_NODE_KEYS`（= 创作四节点 + 两个新节点），
因此自然接入现有机制：

- `ApprovalPolicy.gates` 校验集合扩展为 `PLAN_NODE_KEYS`；
- **`auto`**：无审批门，`edit.plan` / `template.bind` 直接生效；
- **`hybrid`**：默认门保持 WB-03R2 已验收的 `("creative.script",)` **不变**；
  需要对最终绑定时间线人工把关时，可用显式 `gates` 配置
  （如 `("creative.script", "template.bind")`）——管线由
  `build_creative_pipeline(policy)` 按 policy 的 gates 生成
  `NodeSpec.approval_required`，保证「管线定义 ⇔ 冻结策略」一致，
  不存在隐式绕过；
- **`manual`**：默认门 = 全部六个规划节点（含 `template.bind`）。

审批/驳回/修订复用既有路径：`draft → active/superseded/rejected` 联动、
Run 级 `expected_revision` 守卫、受控终态重开、`creative_descendants`
失效链（`match → edit.plan → template.bind`）。

### 5. 对旧 `CREATIVE_PIPELINE` 输入兼容的策略
- 旧输入不含 `creative_input.edit_input` ⇒ `edit.plan` / `template.bind`
  使用 ADR §3 的默认模板规则，其余行为不变；
- 旧输入不携带任何时间线专属字段 ⇒ 时间线的画幅/帧率均**派生自已冻结产物**
  （画幅取 `CreativeBrief.aspect_ratio`，帧率取素材分析 probe 的
  `avg_frame_rate`，无可用值时回退 `30/1`），不要求用户新增输入；
- `TemplateManifest` 为纯数据，新增模板不需要迁移；
- 既有创作节点、媒体节点语义**零改动**（WB-03 已验收行为保持）。

## 备选方案与取舍

| 备选 | 未采用的原因 |
|---|---|
| 只用 `artifact_revisions`，不建版本表 | 无法复用 draft/active/superseded/rejected 联动与审批状态机 |
| 用浮点秒累计时间轴 | 非确定性累计与舍入；改用整数帧 + 显式偏差记录 |
| 素材不足时用其它素材凑满 | 违反「禁止挑选未被 match 选中的素材」；改为显式 unresolved 项 |
| 图片 clip 伪装自然源时长 | 违反「不伪装」；契约区分 source 时间（图片为空区间）与目标时间 |
| 模板缺槽时让节点失败 | 规划阶段失败会阻塞审阅；改为 `accepted=False` + 结构化 unmet，交由审批决策 |
| 在绑定节点改写文案以适配字数限制 | 违反「不得改写事实性文案」；只输出超限未满足项 |

## 补充：`TemplateBinding.accepted` 语义（WB-04 审计修正）

`accepted` 的语义为**整体可接受**：当且仅当 `unmet`（阻断性未满足约束）为空。
WB-05 渲染前必须依据 `unmet` 阻止不兼容渲染。

| 约束 | 是否阻断（进入 `unmet`） |
|---|---|
| 画幅不支持（`ASPECT_UNSUPPORTED`） | **是** |
| 帧率不支持（`FRAME_RATE_UNSUPPORTED`） | **是** |
| 文案超限（`TEXT_LIMIT_EXCEEDED`） | **是** |
| 转场不支持（`TRANSITION_NOT_ALLOWED`） | **是** |
| **必需**槽位为空且策略 `fail`（`SLOT_UNFILLED`） | **是** |
| **必需**槽位为空且策略 `warn` | 否（仅 `SLOT_UNFILLED` 告警） |
| **可选**槽位为空（`OPTIONAL_SLOT_EMPTY`） | 否（任何策略下） |

契约层以模型校验强制 `accepted ⇔ unmet 为空`（`_accepted_consistency`），
使两者不可能不一致。`missing_slot_strategy` 仍记录在绑定产物中供审计。
FakeCreativeProvider 的 proof 段旁白同步改为单条卖点事实
（原拼接文本会超出全部模板的字幕长度上限，属测试夹具问题）。

## 已知限制

1. 不渲染：无 FFmpeg/ffprobe 执行器、无导出、无封面、无技术 QC；
2. 不生成/烧录字幕、无 TTS/BGM、无混音（仅结构化意图）；
3. 速度本阶段固定 `1x`（不重定时）；素材短于目标时长时如实缩短并告警；
4. 轨道模型为最小可用（`v1` / `a1` / `t1` 三轨）；
5. 模板槽位按时间线顺序贪心绑定，不做全局最优。
