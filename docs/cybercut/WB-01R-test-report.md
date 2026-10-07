# WB-01R · 测试报告（控制面可靠性定向整改）

| 项 | 值 |
|---|---|
| 阶段 | WB-01R（只整改 WB-01 已确认的问题，不进入 WB-02） |
| 日期 | 2026-10-06 |
| 基座锁定 | `c8ea1c6e025cbc697a87d5626c2a300ef78504ce`（FlowMind，未修改任何文件） |
| 数据库 | PostgreSQL **18.6**（`D:/cybercut-pgdata-wb01`，端口 55432，库 `cybercut_test`） |
| Python / pytest | 3.13.12（受管隔离 venv） / pytest 9.1.1 |
| 关联文档 | `WB-01-control-plane.md`、`ADR-0002-node-execution-and-claim.md`（决策 8–13） |

---

## 1. 验收命令与结果（串行执行，单一 pytest 进程独占 `cybercut_test`）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
```

| 命令 | 输出 | 退出码 |
|---|---|---|
| `pytest tests/cybercut -q` | `161 passed in 559.35s (0:09:19)` | **0** |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | **0** |
| `ruff check src`（原项目门禁） | `All checks passed!` | **0** |

### 测试数量

| 模式 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---|---|---|
| **PostgreSQL 可用** | **161** | **0** | 0 | 21 个测试文件 |
| **无数据库**（DSN 不可达） | **73** | 0 | **88** | 纯单元测试可独立运行；集成测试**明确 skip** 并给出原因 |

WB-01 基线为 126 项；WB-01R 新增 **35** 项（心跳 7 + 原子提交 7 + 租户一致性 11 + 幂等并发 4 + 版本号并发 4 + 迁移 2），
并改写 1 个既有文件（`test_artifacts.py`）。

---

## 2. 逐项「缺陷 → 修改文件 → 测试用例」对照表

| # | 缺陷 / 整改项 | 修改文件 | 测试用例 |
|---|---|---|---|
| **D1** | **续租仅依赖 `progress_cb`**：silent handler 不报进度 → `heartbeat_at` 陈旧 → 被巡检误判 `WORKER_LOST` | `src/cybercut/executor/heartbeat.py`（新增 `HeartbeatMonitor`/`compute_interval`）<br>`src/cybercut/executor/runner.py`（执行期启动/停止心跳；`finally` 中 join；租约丢失拒绝提交）<br>`src/cybercut/executor/worker.py`（透传间隔/探针）<br>`src/cybercut/executor/__init__.py`（导出）<br>`src/cybercut/nodes/silent.py`（新增 silent handler）<br>`src/cybercut/nodes/__init__.py` | `test_heartbeat.py::test_silent_handler_survives_multiple_lease_periods`（silent + 并发巡检，不得判 WORKER_LOST）<br>`::test_heartbeat_renews_lease_even_without_progress`<br>`::test_heartbeat_false_rejects_final_commit`<br>`::test_compute_interval_bounds`<br>`::test_heartbeat_probe_can_be_injected`<br>`::test_scanner_does_not_kill_fresh_claim` |
| **D2** | **成功提交非原子**：先登记产物（事务 1）再写终态（事务 2）→ 可能留下「已登记但 attempt 未成功」的中间态 | `src/cybercut/executor/runner.py`（`_succeed` 改为**单事务**：project 锁 → running 守卫 → 登记产物 → attempt 终态 → node_run 终态 → Approval → Event；失败整体回滚 + 尽力清理未登记文件）<br>`src/cybercut/storage/node_attempts.py`（新增 `require_running_owned`）<br>`src/cybercut/storage/projects.py`（新增 `lock`） | `test_atomic_commit.py::test_cancel_between_register_and_finish_rolls_back`<br>`::test_second_artifact_failure_rolls_back`<br>`::test_attempt_killed_by_worker_lost_rolls_back`<br>`::test_approval_creation_failure_rolls_back`<br>`::test_success_event_failure_rolls_back`<br>`::test_happy_path_is_atomic_and_succeeds` |
| **D3** | **租户一致性仅靠应用层约定**：跨租户引用在数据库层不被拒绝 | `migrations/cybercut/0002_tenant_consistency.sql`（新增：父表 `(主键, tenant_id)` 复合唯一索引 + 12 个复合外键；移除 0001 的单列外键）<br>`docs/cybercut/ADR-0002-*.md`（决策 8：`events` 不设外键的理由与应用层保证） | `test_tenant_consistency.py`（11 项：run/node_run/attempt/artifact/approval/supersedes 跨租户均被拒；同租户链路通过；可空引用放行；约束目录断言；`events` 无外键断言）<br>`test_migrations.py::test_upgrade_from_0001_only_succeeds`<br>`::test_migration_order_on_fresh_schema`<br>`::test_0002_is_repeatable_after_ledger_loss` |
| **D4** | **幂等「先 SELECT 再普通 INSERT」竞争** → 并发同键可能创建重复 Project | `src/cybercut/storage/idempotency.py`（`INSERT ... ON CONFLICT DO NOTHING` 原子占位；驱动异常统一包装为领域异常）<br>`src/cybercut/orchestrator/engine.py`（重放分支收紧：命中记录但资源不可用则拒绝新建） | `test_idempotency_concurrency.py::test_concurrent_same_key_same_payload_creates_one_project`（8 并发）<br>`::test_concurrent_same_key_different_payload_one_winner`（8 并发，7 冲突）<br>`::test_replay_does_not_raise_driver_exception`<br>`::test_concurrent_replay_after_commit_is_stable`<br>（既有）`test_idempotency.py`（4 项） |
| **D5** | **`revision_no = max()+1` 并发竞争** → 版本号重复或合法请求失败 | `src/cybercut/storage/artifacts.py`（缺省 `revision_no` 时先 `projects.lock` 再取下一版本）<br>`src/cybercut/storage/projects.py`（`lock`） | `test_artifact_concurrency.py::test_concurrent_revision_allocation_is_unique_and_contiguous`（6 并发 → 1..6 + 版本链）<br>`::test_allocated_revisions_match_database_state`<br>`::test_sequential_registrations_also_chain`<br>`::test_different_kinds_have_independent_sequences` |
| **D6** | **既有产物测试使用占位父 ID**：新复合外键下不合法（测试需反映真实约束） | `tests/cybercut/test_artifacts.py`（改为播种真实同租户父链 `project→run→node_run→attempt`） | `test_artifacts.py`（6 项全部通过） |
| **D7** | **心跳审计事件缺 `run_id`**（整改中自发现）：`attempt.lease_lost` 事件无法按 run 过滤 | `src/cybercut/executor/runner.py`（`_start_heartbeat` 接收 `node_run` 并写入 `run_id`） | `test_heartbeat.py::test_heartbeat_false_rejects_final_commit`（断言 `attempt.lease_lost` 事件存在于 run 作用域） |

---

## 3. 关键证据（原始输出）

> 由 `wb01r_evidence.py` 直接运行产出（未写入仓库）。

### 3.1 独立心跳（跨越多个租约周期 + 并发巡检）

```
① 独立心跳：silent handler 1.5s（lease=1s）+ 并发 recover/fail_lost 巡检
outcome        : succeeded
beats          : 4 | lease_lost: False | thread_alive: False
attempts       : [(1, 'succeeded', None)]
```
> `lease_seconds=1`、心跳间隔 0.15s、handler 执行 1.5s 且**零 `progress_cb`**；
> 巡检线程全程执行 `recover_expired_claims` + `fail_lost_running_attempts`（lease=1s），
> attempt **未被判 WORKER_LOST**，未新建第二个 attempt，心跳线程已 join（`thread_alive=False`）。

### 3.2 心跳失败 → 拒绝最终提交

```
② heartbeat 返回 False -> 拒绝最终提交（无正式产物）
outcome        : failed | error_code: LEASE_LOST
attempts       : [('failed', 'LEASE_LOST')]
artifacts      : 0
```

### 3.3 原子提交回滚（第二个产物登记失败）

```
③ 原子提交回滚：第二个产物登记失败 -> 无 ArtifactRevision
outcome        : failed | error_code: COMMIT_FAILED
artifacts      : 0
attempts       : ['failed']
```
> 服务端日志同时给出回滚原因：`成功提交事务失败（已回滚）attempt=...` + `RuntimeError: 模拟第二个产物登记失败`。

### 3.4 租户一致性由数据库强制

```
④ 跨租户引用被数据库拒绝（复合外键）
rejected       : ForeignKeyViolation -> insert or update on table "runs" violates
                 foreign key constraint "fk_runs_project_tenant"
```

### 3.5 并发幂等（8 并发同键同 payload）

```
⑤ 8 并发同 key 同 payload -> 只创建 1 个 Project
created        : 1
replayed       : 7
distinct ids   : 1
projects in db : 1
errors         : []
```

### 3.6 版本号并发分配（6 并发同 project+kind）

```
⑥ 6 并发登记同一 project+kind -> revision_no 1..6 连续且版本链正确
revision_nos   : [1, 2, 3, 4, 5, 6]
chain_ok       : True
errors         : []
```

---

## 4. 迁移验证

| 场景 | 结果 |
|---|---|
| 全新数据库应用全部迁移 | 应用顺序 `['0001_init', '0002_tenant_consistency']` ✅ |
| 只应用 0001 的库升级到最新 | `apply_migrations(db, only_0001_dir)` → `['0001_init']`；再 `apply_migrations(db)` → `['0002_tenant_consistency']`，**既有数据不丢**、复合外键到位 ✅ |
| 迁移可重复执行 | 二次 `apply_migrations` 返回 `[]` ✅ |
| 账本丢失后重放 0002 | 删除账本记录后重新应用成功（DDL 幂等）✅ |
| `schema_migrations` 顺序与记录 | 与 `_EXPECTED_VERSIONS` 完全一致 ✅ |
| 0001 未被修改 | `migrations/cybercut/0001_init.sql` 无改动（仅新增 0002）✅ |

---

## 5. 文档同步修正

| 文档 | 修正内容 |
|---|---|
| `WB-01-control-plane.md` | 头部新增 WB-01R 整改要点；§5 新增心跳/提交守卫规则与 **§5.1 关键事务边界**（含时序图）；§6 handler 契约改为「单事务原子 + 独立心跳」；§8 新增租户一致性/锁顺序/`events` 例外；§9 新增 0002 与升级路径；§11 已知限制更新 |
| `ADR-0002-node-execution-and-claim.md` | 状态改 Accepted（WB-01R 修订）；新增**决策 8**（租户一致性 + `events` 不设外键理由）、**决策 9**（锁顺序）、**决策 10**（独立心跳）、**决策 11**（单事务原子提交）、**决策 12**（ON CONFLICT 幂等）、**决策 13**（Project 行锁分配版本号）；备选方案表与已知限制同步扩充 |

> 修正点：WB-01 文档曾把「登记产物」与「写终态」描述为同一事务，但实现为两个事务 —— 本次已让**实现与文档一致**（决策 11）。

---

## 6. 已知限制（WB-01R 范围内）

1. **心跳间隔是下界**：实际周期 = `interval` + 单次 beat 的连接开销；取 `lease/3` 预留 ≥3 倍余量，
   但在极高负载下仍应监控 `beats/执行时长` 的实际比率。
2. **提交事务较重**：锁 `projects → node_attempts → node_runs` 并在事务内登记全部产物；
   产物数量大时事务变长（本阶段可接受）。
3. **`events` 无外键**（刻意）：由应用层写入约定保证租户一致性，见 ADR-0002 决策 8。
4. **未测多 OS 进程**：并发用线程 + 独立数据库连接（等价并发路径）；
   claim 互斥由数据库行锁保证，与是否同进程无关。
5. **`pytest` 未加入项目依赖**：未修改 `pyproject.toml`（避免触碰基座元数据）。
6. **无 CI**：验收命令需手工串行执行。

---

## 7. 结论

- WB-01R 五项整改（独立心跳 / 原子提交 / 租户强约束 / 并发幂等 / 版本号串行化）**全部落地并有真实 PostgreSQL 测试证据**；
- **161 项测试全绿**（失败 0、跳过 0），无数据库环境下 73 项纯单元测试仍全绿、88 项集成测试明确 skip；
- `ruff` 对新增代码与原项目门禁**双绿**；
- **未修改任何 FlowMind 文件**、未修改 `0001_init.sql`、未引入被禁止组件、未 commit / 未 push；
- 未开始 WB-02。

---

# WB-01R2 · 可靠性最终收口（本报告新增章节）

| 项 | 值 |
|---|---|
| 阶段 | WB-01R2（只修复复审发现的问题；不进入 WB-02，不改 FlowMind，不重构无关代码） |
| 日期 | 2026-10-06 |
| 环境 | 同上（PostgreSQL 18.6 @55432、Python 3.13.12、pytest 9.1.1） |

## R2-1. 验收命令与结果（串行执行，单一 pytest 进程独占 `cybercut_test`）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
```

| 命令 | 输出 | 退出码 |
|---|---|---|
| `pytest tests/cybercut -q` | `178 passed in 842.89s (0:14:02)` | **0** |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | **0** |
| `ruff check src`（原项目门禁） | `All checks passed!` | **0** |

### 测试数量

| 模式 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---|---|---|
| **PostgreSQL 可用** | **178** | **0** | 0 | 24 个测试文件 |
| **无数据库** | **75** | 0 | **103** | 纯单元测试可独立运行；集成测试明确 skip |

WB-01R 基线 161 项 → R2 新增 **17** 项：
`test_artifact_ownership.py`(7) · `test_heartbeat_window.py`(2) · `test_lock_order.py`(4) ·
`test_heartbeat.py`(+3) · `test_migrations.py`(+1)。

## R2-2. 逐项「缺陷 → 修改文件 → 测试用例」对照表

| # | 缺陷 / 整改项 | 修改文件 | 测试用例 |
|---|---|---|---|
| **E1** | 清理会删除**未证明属于本 attempt** 的文件（仅按 handler 声明路径删除） | `src/cybercut/storage/paths.py`（新增 `ATTEMPTS_DIRNAME` / `attempt_dir` / `ensure_attempt_dir` / `is_within_attempt_dir` / `snapshot_project_files`）<br>`src/cybercut/contracts/node_context`（`NodeContext.attempt_dir`）<br>`src/cybercut/executor/runner.py`（独占目录 + 执行前快照 + 归属判定）<br>`src/cybercut/nodes/{echo,silent,gate}.py`（产物路径改 attempt 唯一） | `test_artifact_ownership.py::test_cleanup_does_not_delete_other_attempt_files`<br>`::test_own_unregistered_file_is_cleaned`<br>`::test_attempt_workdir_is_cleaned_after_attempt` |
| **E2** | 历史正式产物可能被覆盖/删除（无数据库引用校验） | `src/cybercut/storage/artifacts.py`（新增 `referenced_paths`）<br>`src/cybercut/contracts/errors.py`（`ArtifactOwnershipError`/`ArtifactHistoricalError`）<br>`src/cybercut/executor/runner.py`（提交事务内引用检查 + 清理双重校验） | `test_artifact_ownership.py::test_historical_path_declared_while_file_present_is_rejected`<br>`::test_historical_path_with_missing_file_is_rejected_by_database_check`<br>`::test_lease_lost_cleanup_never_deletes_referenced_file`<br>`::test_declaring_inside_attempt_dir_is_rejected` |
| **E3** | 心跳只覆盖 handler，未覆盖路径校验/哈希计算；进入提交事务前未收敛心跳 | `src/cybercut/executor/runner.py`（心跳窗口延长至 `_prepare_artifacts`；`finally` 中 `stop()` 并校验 join；提交事务首部重新校验） | `test_heartbeat_window.py::test_heartbeat_covers_hashing_window`<br>`::test_hashed_file_registered_with_original_digest` |
| **E4** | 提交事务锁序与 `advance_run` 相反（`project→attempt→node_run` vs `run→node_run→node_attempt→project`）→ 潜在死锁 | `src/cybercut/executor/runner.py`（`_start`/`_succeed`/`_fail` 全部按 `run→node_run→node_attempt→project`）<br>`src/cybercut/storage/node_runs.py`（新增 `get_for_update`）<br>`src/cybercut/orchestrator/engine.py`（`_apply_cancellation` 先锁 run） | `test_lock_order.py::test_concurrent_commit_and_cancel_locks_overlap_without_deadlock`<br>`::test_cancel_before_commit_leaves_no_partial_state`<br>`::test_repeated_commit_cancel_race_is_stable` |
| **E5** | `stop()` 在 join 超时时清空线程引用 → `running` 失真、无法重试 | `src/cybercut/executor/heartbeat.py`（`stop()` 返回 bool；超时保留引用；仅确已退出才清空） | `test_heartbeat.py::test_stop_timeout_keeps_thread_reference_and_allows_retry`<br>`::test_stop_on_never_started_monitor_is_clean`<br>`::test_normal_path_leaves_no_thread` |
| **E6** | `0002` 仅按 `conname` 判重：其他 schema 同名约束会导致跳过创建 | `migrations/cybercut/0002_tenant_consistency.sql`（新增临时函数 `ensure_fk_if_absent(conname, table regclass, def)`，判重限定 `conname + conrelid`，用后 DROP） | `test_migrations.py::test_0002_creates_constraint_despite_same_name_in_other_schema` |
| **E7** | （整改中自发现）心跳每拍新建连接，实际周期 = interval + 建连耗时，挤压租约余量 | `src/cybercut/executor/runner.py`（新增 `_DedicatedBeat` 专用持久连接）<br>`src/cybercut/executor/heartbeat.py`（按节拍起点计算等待，使实际周期 ≈ interval） | `test_heartbeat.py::test_silent_handler_survives_multiple_lease_periods`<br>`test_heartbeat_window.py::test_heartbeat_covers_hashing_window` |

## R2-3. 关键证据（原始输出，由 `wb01r2_evidence.py` 产出）

### E1/E2 历史正式产物保护

```
① 历史正式产物（文件仍在）被新 attempt 声明
outcome      : failed ARTIFACT_NOT_OWNED
file exists  : True
revisions    : [(1, 'c5e2e8172a43')]        # 版本与 SHA-256 未变

② 文件丢失后同路径重写（提交事务内引用检查）
outcome      : failed ARTIFACT_HISTORICAL
revisions    : [(1, '74a44075bc24')]        # 未新增版本
```

### E3 心跳覆盖哈希窗口

```
③ handler 秒退，哈希期被拉长（每次 1.4s，共 2 次），并发巡检 lease=2s
outcome      : succeeded | hash calls: 2
beats(entry->exit): 2 -> 16                 # 哈希期间发生 14 次心跳
attempts     : [(1, 'succeeded', None)]     # 未被判 WORKER_LOST
```

### E4 并发「提交 vs 取消」锁真实重叠

```
④
commit tx holds locks: True
cancel thread blocked by lock: True         # 取消线程被行锁阻塞 → 锁真实重叠
commit outcome: succeeded | cancel result: {'err': 'InvalidTransitionError'}
artifacts: 1 | attempt: ['succeeded']       # 终态完整；取消被领域异常拒绝（非 psycopg2）
```
> 无 `DeadlockDetected`、无裸 psycopg2 异常；终态非「完整 succeeded」即「完整 cancelled」。

### E5 `stop()` 语义

```
⑤
stop(timeout=0.2) -> False | running: True            # beat 仍阻塞：保留引用，running 真实
stop(timeout=5)   -> True  | running: False | thread_ref: None   # 可重试并成功收敛
```

### E6 迁移判重（其他 schema 同名约束）

由 `test_migrations.py::test_0002_creates_constraint_despite_same_name_in_other_schema` 覆盖：
在 `cybercut_r2_collision.dummy` 上创建同名 CHECK 约束 → 删除 CyberCut 侧外键与账本记录 → 重新
`apply_migrations` → `('cybercut','runs','f')` 与 `('cybercut_r2_collision','dummy','c')` 同时存在，
证明判重已限定到 `(conname, conrelid)`。

## R2-4. 迁移

| 场景 | 结果 |
|---|---|
| `0001` 未被修改 | 仅新增/收紧 `0002`（文件 mtime：0001=20:02 未变，0002=21:15 后更新） |
| 0002 可重复执行 | 删除账本行后重放成功；再次 apply 返回 `[]` |
| 全新数据库 | 应用顺序 `['0001_init','0002_tenant_consistency']` |
| 其他 schema 同名约束 | CyberCut 侧约束仍被创建（见 E6） |
| 迁移期辅助函数 | `ensure_fk_if_absent` 用后 `DROP`，`cybercut` schema 仅剩 `touch_updated_at` / `forbid_mutation` |

## R2-5. 已知限制（R2 新增/更新）

1. **正式产物路径必须 attempt 唯一**：复用同一路径（跨 Run / 重跑）会被归属或历史引用检查
   fail-closed 拒绝 —— 刻意取舍（内建 handler 已使用 `artifacts/{node_key}/{attempt_id}.json`）。
2. **归属判定依赖执行前快照**：项目根目录遍历（只 `stat`，不读文件内容）；文件数极大时有固定开销。
3. **心跳为专用长连接**：每个进行中的 attempt 占用 1 条 PG 连接（原为每拍新建）；
   连接失效时自动重建并重试一次，仍失败则视为续租失败（拒绝提交）。
4. **进入提交事务前必须成功 join 心跳**：若 beat 异常阻塞导致无法 join → `HEARTBEAT_NOT_STOPPED`，拒绝提交。
5. **「心跳停止 → 提交事务建立连接」窗口消耗租约**：租约回收是周期任务（测试按 0.5s 节奏模拟）；
   若把租约压到与建连耗时同量级（如 1s），该窗口可能触发保守的拒绝提交（fail-closed 而非错误）。

## R2-6. 结论

- R2 六项整改（含 1 项整改中自发现）**全部落地**，并有真实 PostgreSQL 测试与原始证据；
- **178 项测试全绿**（失败 0、跳过 0）；无数据库环境下 75 项纯单元测试通过、103 项集成测试明确 skip；
- `ruff` 对新增代码与原项目门禁**双绿**；
- 未修改 FlowMind、未修改 `0001_init.sql`（仅按 R2 要求收紧 `0002`）、未引入被禁止组件；
- 未 commit / 未 push；未开始 WB-02。

---

# WB-01R3 · 产物暂存与发布模型最终收口（本报告新增章节）

| 项 | 值 |
|---|---|
| 阶段 | WB-01R3（只修复终审问题；不改 FlowMind；不做无关重构；不进入 WB-02） |
| 日期 | 2026-10-07 |
| 环境 | PostgreSQL 18.6 @55432（数据目录 `D:\cybercut-pgdata-r3`）、Python 3.13.12、pytest 9.1.1 |

## R3-1. 验收命令与结果（串行执行）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
```

| 命令 | 输出 | 退出码 |
|---|---|---|
| `pytest tests/cybercut -q` | `198 passed in 627.00s (0:10:26)` | **0** |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | **0** |
| `ruff check src`（原项目门禁） | `All checks passed!` | **0** |

| 模式 | 通过 | 失败 | 跳过 |
|---|---|---|---|
| PostgreSQL 可用 | **198** | **0** | 0 |
| 无数据库 | **88** | 0 | **110** |

R2 基线 178 → R3 净增 **20** 项：`test_artifact_publish.py`(6)、`test_heartbeat_stop.py`(3)、
`test_path_safety.py`(+12)、`test_artifact_ownership.py` 重写(6)、`test_atomic_commit.py` 调整。

## R3-2. 逐项「整改要求 → 修改文件 → 测试用例」对照表

| # | 整改要求 | 修改文件 | 测试用例 |
|---|---|---|---|
| **F1** | 废除「项目文件快照证明所有权」；handler 只能写 `attempt_dir`；越界拒绝 | `src/cybercut/storage/paths.py`（删除 `snapshot_project_files`；新增 `resolve_staged_path`/`sanitize_filename`/`revision_dir`/`artifact_publish_path`）<br>`src/cybercut/contracts/node_handler.py`（`ArtifactSpec.staged_relative_path` 取代 `relative_path`；`attempt_dir` 必填）<br>`src/cybercut/executor/runner.py` | `test_artifact_ownership.py::test_staged_path_escape_is_rejected`<br>`::test_absolute_staged_path_is_rejected`<br>`test_path_safety.py::test_staged_path_*`（6 项） |
| **F2** | 执行器分配正式不可变路径；不覆盖；同名 staged → 不同正式路径；摘要取自 staged | `src/cybercut/executor/runner.py`（`_allocate`/`_succeed` 发布段）<br>`src/cybercut/nodes/{echo,silent,gate}.py`（只写暂存区） | `test_artifact_publish.py::test_concurrent_same_staged_name_publishes_distinct_paths`<br>`::test_published_digest_matches_official_file`<br>`::test_publish_refuses_to_overwrite_existing_target`<br>`test_artifact_ownership.py::test_success_publishes_to_executor_assigned_path` |
| **F3** | 发布与提交顺序；失败仅删本次新分配目录；不按 handler 路径删除；崩溃孤儿容忍 | `src/cybercut/executor/runner.py`（`_cleanup_published` 取代按路径清理；`finally` 清理暂存目录） | `test_artifact_publish.py::test_partial_publish_failure_rolls_back_and_cleans`<br>`::test_handler_exception_after_staging_leaves_no_orphan`<br>`::test_one_commit_fails_other_success_artifact_intact`<br>`test_atomic_commit.py`（2 项路径/目录断言更新） |
| **F4** | 心跳停止失败按进程级致命处理；beat 有界；不在线程存活时 close | `src/cybercut/executor/runner.py`（`_reliably_stop_heartbeat`：中断 → 二次 join → `HeartbeatStuckError`；`_DedicatedBeat.interrupt` + `statement_timeout`）<br>`src/cybercut/executor/worker.py`（`poisoned` + 二次抛出） | `test_heartbeat_stop.py::test_runner_interrupts_blocking_beat_and_stops_cleanly`<br>`::test_runner_does_not_close_resources_while_thread_alive`<br>`::test_fatal_heartbeat_does_not_leave_attempt_succeeded` |
| **F5** | 锁顺序注释统一（projects.py / artifacts.py 旧表述） | `src/cybercut/storage/projects.py`、`storage/artifacts.py` | 全文检索复核 + `test_lock_order.py` 行为验证 |
| **F6** | 文档统一 | `docs/cybercut/WB-01-control-plane.md`、`ADR-0002`（决策 14 取代 + 17/18）、本报告 R3 章节 | — |

## R3-3. 关键证据（原始输出，`wb01r3_evidence.py`）

```
① 两 attempt 并发同名 staged（Barrier 同步）
   outcomes: ['succeeded','succeeded'] | revisions: 2 | distinct paths: 2
   artifacts/d012505d-…/same.json | sha ok: True
   artifacts/488ff2a0-…/same.json | sha ok: True

② 同 revision_id 目标已存在
   first: succeeded | second: failed ARTIFACT_PUBLISH_CONFLICT
   file intact: True | revisions: 1

③ 多产物发布到一半失败
   outcome: failed COMMIT_FAILED | revisions: 0 | official dirs: []

④ handler 写完 staged 后抛异常
   outcome: failed HANDLER_EXCEPTION | official dirs: [] | staging dirs: []

⑤ handler 声明 attempt_dir 外文件
   outcome: failed ARTIFACT_NOT_OWNED | official dirs: []

⑥ 心跳线程不可停止
   raised: HeartbeatStuckError | probe closed: False | poisoned: True
   second step: 仍抛 HeartbeatStuckError（不再领取任务）
```

## R3-4. 结论

- R3 五项整改**全部落地**，并有真实 PostgreSQL 测试与原始证据；
- **198 项测试全绿**；无数据库环境 88 项纯单元通过、110 项集成明确 skip；
- `ruff` 双绿；未修改 FlowMind、未修改 `0001_init.sql`、未引入被禁止组件；
- 未 commit / 未 push；未开始 WB-02。
