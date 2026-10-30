# WB-03 · 测试报告（创作层）

> 本报告记录 **WB-03（创作层）+ WB-03R + WB-03R2（定向整改）** 的实际测试结果。
> 所有数字来自本机实测输出。承接 WB-02R6 基线：`275 passed, 1 skipped`。

| 项 | 值 |
|---|---|
| 阶段 | WB-03（创作层：简报 / 脚本 / 分镜 / 素材匹配）+ WB-03R / WB-03R2 定向整改 |
| 日期 | 2026-10-08 |
| 基线 commit | `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（未 commit、未 push） |
| 数据库 | PostgreSQL 18.6 @55432（数据目录 `D:\cybercut-pgdata-n2`，库 `cybercut_test`） |
| 测试 DSN | `postgresql://postgres@127.0.0.1:55432/cybercut_test` |
| 关联文档 | `ADR-0004-creative-layer.md`、`WB-03-creative-layer.md` |

## 1. 验收命令与结果（串行执行，实测输出）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q -W error::pytest.PytestUnhandledThreadExceptionWarning
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
git diff --check
```

| 命令 | 实测结果 | 退出码 |
|---|---|---|
| `pytest tests/cybercut -q -W error::…ThreadExceptionWarning` | **479 passed, 1 skipped**，`0 failed`，`0 warnings` | 0 |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | 0 |
| `ruff check src` | `All checks passed!` | 0 |
| `git diff --check` | 无 diff 冲突 | 0 |

未处理线程异常以 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 提升为错误执行，
结果 **0 error**；全套件共收集 **480** 项（= 479 passed + 1 skipped）。

唯一 SKIP：`test_media_ingest.py::test_ingest_rejects_symlink`
（Windows 无 `SeCreateSymbolicLinkPrivilege`，WB-02 既有用例，未新增 SKIP）。

## 2. WB-03 / WB-03R / WB-03R2 新增测试分项（7 个文件，共 204 项，全部通过）

| 测试文件 | 用例数 | 通过 | 失败 | 跳过 |
|---|---|---|---|---|
| `test_creative_contracts.py` | 84 | 84 | 0 | 0 |
| `test_creative_nodes.py` | 25 | 25 | 0 | 0 |
| `test_creative_migration.py` | 23 | 23 | 0 | 0 |
| `test_creative_revision.py`（WB-03R） | 23 | 23 | 0 | 0 |
| `test_creative_pipeline_e2e.py` | 19 | 19 | 0 | 0 |
| `test_creative_locking.py`（WB-03R2） | 19 | 19 | 0 | 0 |
| `test_project_aggregation.py`（WB-03R2-复审） | 11 | 11 | 0 | 0 |
| **合计** | **204** | **204** | **0** | **0** |

（WB-02R6 基线 276 项 → WB-03 411 → WB-03R 450 → WB-03R2 469 项 →
复审 **480** 项，累计净增 **204** 项。）

### 2.1 迁移与版本化（`test_creative_migration.py`，23 项）

覆盖：0004 首次应用与幂等重复执行、四张表存在与必需列、`(project, tenant,
revision_no)` 唯一性、`revision_no >= 1` CHECK、**租户不一致被数据库拒绝**
（复合外键）、`status` CHECK、`supersedes_id` 外键、仓储往返与
**旧版本不可覆盖**（`ConflictError`）、`set_status`。

### 2.2 契约、匹配与校验（`test_creative_contracts.py`，71 项）

- **契约**：`target_duration_us > 0`、`aspect_ratio` 形状、`total == Σ section`、
  `total == Σ shot`、`sequence_no` 确定性 `0..n-1`、`candidates` 降序、
  选中项必须在候选内且需给出评分、同一 segment 不得被两个 shot 同时选中、
  卖点必须带证据、`role` / `transition_hint` 枚举、NaN/Infinity 拒绝；
- **匹配确定性**：同输入两次调用输出**逐字节一致**；候选排序稳定；
  高度对齐的夹具下全部 shot 均可选出且不重复；被占用 segment 不被复用
  （`MATCH_ALL_CANDIDATES_USED`）；无效 segment 引用被淘汰并告警；
  阈值过高时**不伪造**（`MATCH_BELOW_THRESHOLD` + `selected=None`）；
  未知算法版本与非法阈值抛错；权重正和 = 1.0；
- **校验器（反虚构）**：编造数字 → `UNSUPPORTED_FACT`；禁用声明 →
  `FORBIDDEN_CLAIM`；素材越出 Run → `ASSET_OUT_OF_RUN`；证据指向不存在产物 →
  `EVIDENCE_UNREACHABLE`；分镜未覆盖 section / 未知 section / 候选越界；
- **决策摘要**：理由码登记校验、稳定哈希、禁止思维链字段、`summarize` 确定性；
- **提示词**：版本登记与确定性文本；
- **策略**：`auto` 无门、`hybrid` 默认仅门 `creative.script`、非法门/非法模式拒绝、
  `to_json`/`from_json` 往返。

### 2.3 节点与持久化（`test_creative_nodes.py`，25 项）

覆盖：四节点产物 kind 与暂存路径、链路产出全部产物、**节点确定性**
（重复执行暂存 JSON 逐字节一致）、匹配节点两次重跑一致、
缺上游产物 → `INPUT_MISSING`、上游负载不符 schema → `INPUT_INVALID`、
`cancel_check` → `CANCELLED`、禁用声明 → `CREATIVE_FACT_UNSUPPORTED`、
非法 `creative_input` → `INPUT_INVALID`、非法匹配阈值 → `CREATIVE_SCHEMA_INVALID`、
`auto` 全程无审批、`hybrid` 仅门 `creative.script`、
**Run 冻结策略优先于注入默认**、自定义闸门、
Provider 错误透传与未预期异常映射为稳定错误码、
`persist_for_artifact` 对媒体 kind 空操作 / 非法负载拒绝 / 合法负载落库。

### 2.4 创作管线端到端（`test_creative_pipeline_e2e.py`，16 项）

经真实 PostgreSQL + `Orchestrator` + `Worker` 跑完整九节点管线：

| 用例 | 断言要点 |
|---|---|
| `test_auto_pipeline_completes_all_nine_nodes` | 9 个 node_run 全部 `succeeded` |
| `test_auto_pipeline_publishes_four_creative_artifacts` | 每个创作节点恰好发布 1 个对应 kind 的 ArtifactRevision |
| `test_creative_tables_are_populated_once` | 四张版本表各恰好 1 行，`revision_no=1`、`status=active`、`prompt_version` 非空 |
| `test_creative_payloads_pass_pydantic` | 四个负载均可被权威模型校验；时长链条一致；`sequence_no` 为 `0..n-1` |
| `test_match_selects_unique_segments` | 选中不重复；每个镜头有候选与打分明细；未选中必带 warning |
| `test_match_reports_threshold_shortfall_never_fabricates` | 阈值 0.999 时全部 `selected=None` 且显式告警 |
| `test_creative_brief_is_reproducible_from_frozen_inputs` | 用**同一冻结输入**重算简报，逐字段等于已发布产物 |
| `test_downstream_inputs_are_frozen_and_do_not_drift` | `input_revision_refs` 含 `creative.script`/`creative.brief`/`asset.analysis.*`；**反复推进 3 次不漂移**；引用指向真实产物 |
| `test_decision_trace_is_summary_only` | 摘要字段集合严格受限（无思维链字段） |
| `test_hybrid_pipeline_pauses_at_script` | `creative.brief` succeeded、`creative.script` waiting_approval、下游 blocked、Run 为 waiting_approval |
| `test_hybrid_approval_resumes_pipeline` | 审批通过后 9 节点全部 succeeded 且匹配产物落库 |
| `test_hybrid_rejection_blocks_downstream` | 审批落 `rejected`（含 feedback）；下游 `creative.match` 未 succeeded 且无产物 |
| `test_hybrid_policy_matches_pipeline_definition` | 管线 `approval_required` 与策略 `gates` 一致 |
| `test_frozen_policy_is_delivered_to_every_attempt` | `runs.input_json` 冻结创作输入；**每个 attempt** 都携带 |
| `test_creative_payload_json_is_published_as_artifact` | 正式产物文件内容与版本表 `payload_json` 完全一致 |
| `test_registry_contains_creative_and_media_nodes` | `build_creative_registry` 恰含四节点；full registry 含媒体五节点 |

**说明**：媒体层使用可注入的 fake toolchain / detector / ASR（本机无 ffmpeg），
创作层使用确定性 `FakeCreativeProvider`；**本报告不宣称真实媒体或真实 LLM 链路已通过**。

### 2.5 WB-03R 定向整改：整改项 → 文件 → 测试 对照表

| # | 整改要求 | 涉及文件 / 函数 | 对应测试 |
|---|---|---|---|
| 1 | 驳回只重跑自身、只失效创作层下游、媒体节点不受影响；feedback 冻结；新 attempt 产新版本且保留旧版本与 rejected 记录 | `orchestrator/engine.py::_decide`（闸门分派）、`_invalidate_creative_stage`、`_freeze_revision_request`；`creative/policy.py::creative_descendants` | `test_creative_revision.py::test_reject_script_only_reruns_script_and_keeps_media`、`::test_reject_storyboard_invalidates_match_only`、`test_creative_contracts.py::test_creative_descendants_only_covers_creative_chain` |
| 2 | 新增 `revise_artifact` / `revise_node`（expected_revision + feedback + 新提示词）；按阶段失效；清除被失效节点旧 `input_revision_refs`；未失效节点不重跑 | `orchestrator/engine.py::{revise_node, revise_artifact, _freeze_revision_request, _reopen_for_revision, _invalidate_creative_stage}`；`storage/runs.py::set_input_json` | `test_creative_revision.py::test_revise_brief_invalidates_script_storyboard_match`、`::test_revise_script_invalidates_storyboard_and_match_only`、`::test_revise_storyboard_invalidates_match_only`、`::test_revise_match_invalidates_nothing_upstream`、`::test_revise_artifact_resolves_node_and_revision`、`::test_revise_rejects_stale_expected_revision`、`::test_revise_rejects_non_creative_node`、`::test_revise_requires_feedback_or_prompt` |
| 3 | 版本身份统一（DB 分配 id/revision_no；payload 同值；project/run 与 ArtifactRevision 一致；禁止 attempt_no 充当 revision_no；四者一一追溯） | `executor/runner.py::_finalize_creative_payloads`；`storage/creative_revisions.py::{finalize_payload, assert_payload_identity, provisional_identity, id_field}`；`contracts/creative.py` | `test_creative_revision.py::test_identity_is_consistent_across_four_artifacts`、`::test_payload_revision_no_is_not_attempt_no`、`::test_two_runs_in_same_project_share_revision_sequence`、`test_creative_nodes.py::test_persist_for_artifact_accepts_valid_brief` |
| 4 | 0005 迁移：runs 可引用唯一键 + 四表 `(run_id, project_id, tenant_id)` 复合外键；跨 Project 插入被拒 | `migrations/cybercut/0005_run_project_composite.sql` | `test_creative_revision.py::test_cross_project_run_combination_is_rejected`（4 参数）、`::test_runs_unique_key_exists_for_composite_fk`、`::test_weak_run_tenant_fk_was_replaced`；`test_creative_migration.py::test_0004_applied_and_idempotent`（版本清单含 0005）；`test_migrations.py` |
| 5 | 证据全覆盖：Script 顶层与每个 section、Storyboard 顶层与每个 shot 校验 asset / artifact kind / segment_index；segment_index 必须真实存在；证据必须来自冻结输入；project/run 与上下文一致 | `creative/validators.py::{check_evidence_refs, _check_identity, _segment_index_map, validate_script, validate_storyboard, validate_match, validate_brief}`；`nodes/creative_nodes.py::_frozen_kinds` | `test_creative_contracts.py::test_evidence_segment_index_must_exist`、`::test_evidence_segment_index_exists_is_accepted`、`::test_evidence_must_come_from_frozen_inputs`、`::test_storyboard_top_level_evidence_is_checked`、`::test_storyboard_shot_evidence_is_checked`、`::test_script_evidence_out_of_run_is_rejected`、`::test_storyboard_identity_must_match_context`、`::test_script_identity_must_match_context`、`::test_match_identity_must_match_context` |
| 6 | manual 模式（四节点全审批）+ auto/hybrid；策略冻结与 E2E | `creative/policy.py::{ApprovalMode, MANUAL_POLICY, policy_for}`；`orchestrator/pipeline.py::CREATIVE_PIPELINE_MANUAL` | `test_creative_pipeline_e2e.py::test_manual_pipeline_gates_every_creative_node`、`::test_manual_policy_gates_match_pipeline_definition`、`test_creative_contracts.py::test_manual_policy_gates_all_creative_nodes`、`::test_non_auto_policy_requires_explicit_gates`、`::test_manual_policy_is_frozen_and_roundtrips` |
| 7 | `ProviderError` 在 NodeHandler 边界转 `NodeExecutionError("CREATIVE_PROVIDER_FAILED")`，不退化 `HANDLER_EXCEPTION` | `nodes/creative_nodes.py::_generate` | `test_creative_nodes.py::test_provider_error_maps_to_stable_node_error`、`::test_unexpected_provider_failure_maps_to_stable_code`、`test_creative_pipeline_e2e.py::test_provider_error_becomes_stable_failure_code`（断言 `node_attempts.error_code` 与 `events.payload_json.error_code`） |
| 8 | 强化测试（驳回后媒体 attempt 数不变 / brief 不重跑 / script 1→2 且反馈入 payload / 只失效 storyboard+match / 新 refs 指向 script rev 2 / 跨 Project 拒绝 / 非本 Run 素材·错误 kind·越界 segment 拒绝 / row 与 payload 身份一致 / ProviderError 失败码） | 见上 | 全部落在 `test_creative_revision.py`、`test_creative_contracts.py`、`test_creative_pipeline_e2e.py` |

**状态机受控出边（承载修订）**：仅放开两处 —— `run.succeeded → running`、
`project.completed → active`；其余终态仍无出边。
`test_state_machine.py` 同步更新为「非法转换表移除这两项 + 终态无出边仅保留两处受控例外」。

**非创作层闸门语义不变**：`TEST_PIPELINE` 的 `approval_gate` 走既有
`_invalidate_on_reject`（重开上游并级联失效），
`test_approvals.py` / `test_invalidation.py` 的既有保证全部保留。

### 2.6 WB-03R2 复审整改：整改项 → 文件 → 测试 对照表

| # | 整改要求 | 涉及文件 / 函数 | 对应测试 |
|---|---|---|---|
| 1 | 修订锁序统一为 Run → NodeRun → NodeAttempt → Project；先锁 Run 再锁 NodeRun；锁内重读并校验节点状态；修订与成功提交并发不死锁、无部分更新 | `orchestrator/engine.py::revise_node`（非加锁探测 run_id → `runs.get_for_update` → `node_runs.get_for_update` → 持锁重读） | `test_creative_locking.py::test_revise_node_acquires_locks_in_global_order`（白盒记录加锁顺序）、`::test_revise_node_rereads_node_status_under_lock`（判别性：锁内重读才拒绝）、`::test_revise_accepts_waiting_approval_under_lock`、`::test_revise_rejects_cancelled_node`、`::test_revise_concurrent_with_worker_commit_no_deadlock` |
| 2 | `expected_revision` 以**目标 Run** 当前产物为基准；保留项目级 `revision_no` 分配；同项目两 Run 测试 | `orchestrator/engine.py::revise_node`（改用 `latest_for_run`，错误详情回显 `project_latest`）；`storage/creative_revisions.py::latest_for_run` | `test_creative_locking.py::test_expected_revision_is_run_scoped`、`::test_expected_revision_rejects_project_level_confusion`、`::test_expected_revision_advances_within_same_run` |
| 3 | 版本-审批状态联动：待审不得 active；批准 → active + 旧 active superseded；驳回 → rejected；自动模式 active 并正确 supersede | `storage/creative_revisions.py::{STATUS_*, supersede_others, activate, mark_rejected, persist_for_artifact(status=...)}`；`executor/runner.py::_register_artifacts`；`orchestrator/engine.py::{_activate_creative_version, _reject_creative_version}` | `test_creative_locking.py::test_pending_version_is_draft_not_active`、`::test_approval_activates_and_supersedes_previous`、`::test_rejection_marks_version_rejected`、`::test_auto_mode_versions_are_active_and_supersede`、`::test_auto_pipeline_all_versions_active`、`::test_status_transitions_recorded_in_events`、`::test_rejection_event_records_rejected_version` |
| 4 | 不通过全局状态表开放终态出边；受控修订路径重开；清理终态元数据 | `contracts/state_machine.py`（**恢复** `succeeded`/`completed` 无出边）；`storage/runs.py::reopen_for_revision`、`storage/projects.py::reopen_for_revision`；`orchestrator/engine.py::_reopen_for_revision` | `test_creative_locking.py::test_generic_transition_still_rejects_terminal_reopen`、`::test_reopen_for_revision_resets_terminal_metadata`、`::test_reopen_helper_only_accepts_terminal_succeeded`、`::test_repaired_doc_claim_only_two_controlled_edges`；`test_state_machine.py::test_terminal_states_have_no_outgoing`、`::test_illegal_transitions_raise[project-completed-active]`、`::test_illegal_transitions_raise[run-succeeded-running]` |
| 5 | 更新 ADR / WB-03 文档 / 测试报告，明确状态、锁序、版本守卫语义 | `docs/cybercut/ADR-0004-creative-layer.md`（决策 31/32/33，决策 26 标注被取代）、`WB-03-creative-layer.md`（§6.1 与锁序/守卫/重开小节）、本报告 §2.6 | 文档自查 + 上述行为测试 |

**状态语义（WB-03R2）**：`draft`（待审，**不得** active）/ `active`（`(project, tenant, kind)` 内唯一）
/ `superseded` / `rejected`。

**锁序（WB-03R2）**：唯一全序 `Run → NodeRun → NodeAttempt → Project`；
`revise_node` 先非加锁读 `run_id` 定位目标，再按序加锁并在锁内重读节点状态。

**版本守卫（WB-03R2）**：`expected_revision` 按 **Run 作用域** 比较
（`latest_for_run`），项目级 `revision_no` 仅用于分配（跨 Run 连续递增）。

**受控重开（WB-03R2）**：状态机表终态仍无出边；重开只能经
`runs.reopen_for_revision` / `projects.reopen_for_revision`，并清空
`finished_at` / `failure_code` / `failure_message` / `archived_at`。

### 2.7 WB-03R2-复审：多 Run Project 状态聚合

**语义澄清（已同步文档）**：Project 状态由 **`cybercut.runs` 中该 Project
全部 Run 的状态聚合**得出；`projects.current_run_id` 只是「最近一次启动的
Run」的指针，**不**决定 Project 状态。

| # | 整改要求 | 涉及文件 / 函数 | 对应测试 |
|---|---|---|---|
| 1 | 澄清并文档化：Project 状态由全部 Run 聚合，`current_run_id` 仅为指针 | `docs/cybercut/ADR-0004-creative-layer.md`（决策 34）、`WB-03-creative-layer.md` §8 | `test_project_aggregation.py::test_current_run_id_is_a_pointer_not_the_status_source` |
| 2 | 修改聚合逻辑：存在任一非终态 Run 时项目不得进入终态（避免非当前 Run 误标 completed/failed/cancelled） | `orchestrator/engine.py::_aggregate_project_status`（取代 `_finish_project`，3 处调用点）、`storage/runs.py::statuses_for_project` | `::test_older_run_completes_project_stays_active_when_new_run_queued`、`::test_older_run_completes_project_stays_active_when_new_run_running`、`::test_cancelled_older_run_does_not_terminalize_project` |
| 3 | 当前 Run 失败时不得保持错误的 `completed` | `storage/projects.py::apply_aggregate_status`（受控聚合修正：终态间允许修正） | `::test_stale_completed_is_corrected_when_current_run_fails`、`::test_aggregate_status_is_idempotent_and_validated` |
| 4 | 新 Run 成功 / 失败两种终态下 Project 状态正确 | 同上 | `::test_new_run_success_marks_project_completed`、`::test_new_run_failure_marks_project_failed`、`::test_mixed_terminal_states_prefer_failed` |
| 5 | **在 `start_run` 一致的项目锁下重读 Run 状态**并计算聚合，串行化「新 Run 创建」与「项目终态写入」 | `orchestrator/engine.py::_aggregate_project_status`（两段式：未加锁快照仅做安全短路 → `projects.get_for_update` 后**重读**并权威判定） | `::test_new_run_commit_between_snapshot_and_terminal_write_keeps_project_active`（稳定复现「快照 → 新 Run 提交 → 落终态」顺序并断言 Project 保持 `active`）、`::test_aggregate_takes_project_lock_before_authoritative_read` |

共 **11** 项新增测试，全部通过（见 `tests/cybercut/test_project_aggregation.py`）。

## 3. 真实 ffmpeg / FunASR / LLM 执行情况

| 组件 | 是否真实执行 | 说明 |
|---|---|---|
| ffmpeg / ffprobe | **否** | 本机缺失；E2E 使用 fake toolchain |
| PySceneDetect / OpenCV | **否** | 本机缺失；E2E 使用 fake detector |
| FunASR / torch | **否** | 本机缺失；E2E 使用 fake ASR |
| 真实 LLM API | **否（本阶段禁止）** | 仅使用确定性 `FakeCreativeProvider`，无任何网络调用 |

## 4. 依赖与许可证变化

**无新增依赖。** WB-03 只使用标准库与既有依赖（pydantic、psycopg2、pytest、ruff）；
未引入 Redis / Celery / Kafka / Milvus / 工作流框架；未新增网络或模型依赖。

## 5. 已知风险与未完成事项

1. **未接入真实 LLM**：`FakeCreativeProvider` 为确定性模板实现，真实 provider 需另行接入
   （接口已固定，接入时不得绕过 `validators` 的事实层校验）；
2. **未实现** TimelineSpec、FFmpeg 渲染、字幕烧录、TTS、BGM、音频混音、
   REST / SSE / MCP、前端；
3. **`semantic_score` 为轻量分词重合**，不是语义向量检索；素材语义匹配精度受限；
4. **分镜镜头数由脚本时长决定**：素材不足时如实报告未匹配镜头（不降级、不伪造），
   实际生产需配合更多素材或降低阈值策略；
5. **版本回滚/合并未实现**：仅有 `supersedes_id` 链与 `status` 迁移；
6. `payload_json` 随分镜规模增长，大对象存储需求留待后续阶段；
7. **Run/Project 的重开是受控例外**：状态机表保持「终态无出边」，
   重开只能经 `runs.reopen_for_revision` / `projects.reopen_for_revision`
   （仅接受 `succeeded` / `completed`，并清空终态元数据）；
   `failed` / `cancelled` 始终为硬终态；
8. 定向修订会**重新分配版本号**并在同一 project 内跨 Run 连续递增；
   版本表不提供回滚/删除（仅 `supersedes_id` 链与 `status`）。

## 6. 声明

- **未进入 WB-04 及之后阶段**；
- 未实现 TimelineSpec / 渲染 / 字幕 / TTS / BGM / 混音 / REST / SSE / MCP / 前端；
- **未修改** `src/flowmind/`、`migrations/cybercut/0001_init.sql`、
  `0002_tenant_consistency.sql`、`0003_media_assets.sql`、`0004_creative_revisions.sql`
  （WB-03R 的修复以新增 `0005_run_project_composite.sql` 表达）；
- 未引入 Redis / Celery / Kafka / Milvus / 工作流框架；未进行真实 LLM 网络调用；
- **未 commit、未 push**；`git diff --check` 退出码 0；
- 所有新增产物均经 `ArtifactRevision` 登记，且落库前经 Pydantic + 事实层双重校验。
