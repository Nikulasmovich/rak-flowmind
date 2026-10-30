# ADR-0002 · 节点执行与任务领取（NodeAttempt Claim 协议）

| 项 | 值 |
|---|---|
| 状态 | **Accepted**（WB-01 实施并验证；R1 新增决策 8–13；R2 改写 9 + 新增 14–16；**R3：决策 14 被 17 取代，新增 17–18**） |
| 日期 | 2026-10-07（WB-01R3 修订） |
| 阶段 | WB-01 + WB-01R + WB-01R2 + **WB-01R3（产物暂存与发布模型最终收口）** |
| 上游依据 | `ADR-0001-flowmind-integration-boundary.md` |
| 影响范围 | `src/cybercut/executor/`、`src/cybercut/orchestrator/`、`src/cybercut/storage/`、`src/cybercut/contracts/`、`src/cybercut/nodes/` |

> **WB-01R 修订摘要（决策 8–13）**
> | # | 整改项 | 决策 |
> |---|---|---|
> | 1 | 独立周期心跳 | 决策 10：心跳与 `progress_cb` 解耦，独立线程 + 独立事务；续租失败拒绝提交 |
> | 2 | 原子成功提交 | 决策 11：产物登记 / attempt 终态 / node_run 终态 / Approval / Event 同一事务，全成或全滚 |
> | 3 | 数据库强制租户一致性 | 决策 8（events 例外说明）+ `0002_tenant_consistency.sql` 复合外键 |
> | 4 | 并发幂等 | 决策 12：`INSERT ... ON CONFLICT DO NOTHING` 原子占位 |
> | 5 | 产物版本并发分配 | 决策 13：锁 Project 行后分配 `revision_no`（不用 advisory lock） |
>
> **WB-01R2 修订摘要（决策 9 改写 + 14–16）**
> | # | 整改项 | 决策 |
> |---|---|---|
> | 1 | 保护历史正式产物 | 决策 14：attempt 独占工作目录 + 执行前快照归属证明 + 引用状态双重校验 |
> | 2 | 延长心跳保护范围 | 决策 15：心跳覆盖 handler/校验/哈希；提交事务前停止并 join |
> | 3 | 统一数据库行锁顺序 | 决策 9（改写）：`run → node_run → node_attempt → project` |
> | 4 | 修复 `HeartbeatMonitor.stop` | 决策 10 补充：超时不清引用、`running` 真实、可重试 stop |
> | 5 | 收紧 0002 约束判重 | 决策 16：判重限定 `(conname, conrelid)` |
>
> **WB-01R3 修订摘要（决策 14 被 17 取代；新增 17–18）**
> | # | 整改项 | 决策 |
> |---|---|---|
> | 1 | 废除「项目文件快照证明所有权」 | 决策 17.1：`snapshot_project_files` 不再作为依据；handler 只能写 `attempt_dir` |
> | 2 | 执行器分配正式不可变路径 | 决策 17.2/17.4：`artifacts/{artifact_revision_id}/{safe_filename}`，同文件系统原子移动 |
> | 3 | 发布与 DB 提交顺序 + 失败清理 | 决策 17.6/17.7：同事务提交；失败仅删本次新分配目录 |
> | 4 | 心跳停止失败 | 决策 18：中断 I/O → 二次 join → 仍失败抛 `HeartbeatStuckError`（进程级致命，worker poisoned） |
> | 5 | 锁顺序注释统一 | 决策 9 + 代码/文档统一为 `run → node_run → node_attempt → project` |

---

## 背景（Context）

WB-01 需要一条**可靠、可并发、可重启恢复**的节点执行通道。已有的事实约束全部来自 WB-00/ADR-0001 的实测结论：

1. 基座 `TaskManager` 是「单技能、单进程、重启即中断」的执行器：其构造函数**无条件**调用 `recover_running()`，会把该 PG 库里**所有** `queued`/`running` 行打成 `interrupted`（`tasks/manager.py:102-107`、`tasks/store.py:246-254`）。
   → 任何「API 进程 + worker 进程」各自初始化一个 `TaskManager` 的组合都会互相误杀在途任务。
2. 基座 `workers=1` + 进程内锁，**无多进程可靠领取**（能力矩阵 T-18 = `NOT_PRESENT`）；PgBouncer 事务模式明确禁用 advisory lock（`tasks/store.py:3-8`）。
3. 基座只识别**单个** `data.output_path`，且产物目录受 TTL GC 删除（能力矩阵 T-06b/T-17）。
4. WB-01 边界要求：不引入 Redis / Celery / Kafka / Milvus / 工作流框架；PostgreSQL 为唯一基础设施。

因此必须自行设计「多进程可靠领取」协议。

---

## 决策（Decision）

### 决策 1：领取采用 `SELECT ... FOR UPDATE SKIP LOCKED` 的**单语句原子 claim**

```sql
-- src/cybercut/storage/node_attempts.py::_CLAIM_SQL
WITH picked AS (
    SELECT attempt_id
      FROM cybercut.node_attempts
     WHERE status = 'queued'
       AND available_at <= now()
     ORDER BY available_at, created_at
     LIMIT 1
     FOR UPDATE SKIP LOCKED          -- 跳过他人已锁的行 -> 不重复领取
)
UPDATE cybercut.node_attempts AS a
   SET status = 'claimed', claimed_by = %s, claimed_at = now(),
       heartbeat_at = now(), updated_at = now()
  FROM picked
 WHERE a.attempt_id = picked.attempt_id
RETURNING ...;
```

理由：
- **原子性**：`SELECT ... FOR UPDATE` 与 `UPDATE` 在**同一条语句**内完成，不存在「选中后被插队」的窗口；
- **互斥**：`SKIP LOCKED` 让并发 worker 直接跳过已锁行，无需应用层分布式锁（避开 PgBouncer 不支持 advisory lock 的限制）；
- **公平/有序**：按 `available_at, created_at` 升序取最早任务；
- **单条领取**：`LIMIT 1` 明确禁止一次锁住批量任务（WB-01 §五.8）；
- **索引友好**：`idx_attempts_claimable (available_at, created_at) WHERE status='queued'` 为部分索引，只覆盖可领取行。

### 决策 2：领取与执行**分离**——claim 事务先提交，再执行 handler

`Claimed → Running` 在执行前于**独立事务**完成（`executor/runner.py::_start`）。
理由：handler 可能运行数分钟（媒体/LLM），若在 claim 事务内执行，会长期持有行锁并占用连接，
阻塞其它 worker 领取**其它**任务。

### 决策 3：两级租约语义 —— `claimed` 可回收，`running` 只判死不重跑

| 状态 | 心跳超时（默认 60s） | 语义 |
|---|---|---|
| `claimed` | **恢复为 `queued`**（`recover_expired_claims`） | 尚未执行 → 无副作用 → 可安全重领 |
| `running` | **标记 `failed / WORKER_LOST`**（`fail_lost_running_attempts`） | 可能已产生副作用 → **不得直接重跑** |

`running` 判死后，新尝试由**重试策略**另行创建（`orchestrator._apply_worker_lost_retry`：
`node_run.failed → ready`，随后入队 `attempt_no + 1`）。
理由：重复执行一个可能已写盘的 handler 会造成重复副作用（重复产物、重复外呼）；
「新建 attempt」把不确定的旧尝试保留为历史（`failed / WORKER_LOST`），可审计。

### 决策 4：worker 丢失同时把 `node_run` 置 `failed`

`fail_lost_running_attempts` 在同一事务内把对应 `node_run` 从 `running` 置为 `failed`
（`executor/lease.py`）。理由：否则 `node_run` 会永久停留在 `running`，重试策略无从触发。

### 决策 5：单进程内不维护任何执行真相，全部状态在 PostgreSQL

- worker/orchestrator 实例可随时销毁重建（重启恢复测试验证）；
- 无内存队列、无内存锁、无本地文件状态机；
- 幂等与并发控制全部落在数据库约束（`UNIQUE(node_run_id, attempt_no)`、主键、CAS UPDATE）。

### 决策 6：`NodeResult.artifacts` 只是描述，登记由 executor 在**同一事务**完成

handler 只写文件并返回相对路径；executor 负责
①路径穿越校验 → ②计算 `sha256`/`size_bytes` → ③登记 `ArtifactRevision`。
理由：
- handler 不接触数据库（职责单一、可测试、可 mock）；
- 哈希与大小**在登记时计算**（WB-01 §八），满足不可覆盖与可校验要求；
- WB-01R 进一步收紧：登记与终态写入**同事务**（见决策 11），不存在「登记成功但终态未写」的中间态。

### 决策 7：未知 handler 一律 `HANDLER_NOT_FOUND` 失败，绝不静默跳过

`executor/runner.py` 在注册表中找不到 `node_key` 时直接把 attempt 落 `failed`
（`error_code=HANDLER_NOT_FOUND`）。理由：静默跳过会让 Run 卡在 `ready` 而无人察觉。

---

## WB-01R 新增决策

### 决策 8：租户一致性由**数据库**强制；`events` 刻意不设外键

`0002_tenant_consistency.sql` 为每个父表建立 `(主键, tenant_id)` 复合唯一索引，
并把子表外键改为 `(父主键, tenant_id)` 复合外键：

| 子表 | 复合外键（全部同租户强约束） |
|---|---|
| `runs` | → `projects` |
| `node_runs` | → `runs` |
| `node_attempts` | → `node_runs` |
| `artifact_revisions` | → `projects` / `runs` / `node_runs` / `node_attempts` / 自引用 `supersedes_id` |
| `approvals` | → `projects` / `runs` / `node_runs` / `artifact_revisions`（可空） |

采用默认 `MATCH SIMPLE`：可空引用（`supersedes_id`、`approvals.artifact_revision_id`）为 `NULL` 时自然放行。

**`events` 不设外键（明确决策）**，理由：
1. `events` 是**追加型审计日志**（`BEFORE UPDATE OR DELETE` 触发器禁止改写/删除），
   其 `project_id` / `run_id` / `node_run_id` 是**价值性引用**而非结构依赖——
   审计记录必须能在被引用资源变化后依然完整可读，不应受 FK 约束与级联删除影响；
2. `events` 行一经写入不可篡改，因此不存在「引用被改写到别租户」的风险；
3. **应用层保证方式**：`events.tenant_id` 一律取自当前调用上下文（`NodeContext.tenant_id` /
   orchestrator 方法的 `tenant_id` 参数），写入路径唯一收敛于 `storage/events.append()`，
   其他列仅作检索维度，不参与业务判定。测试 `test_events_has_no_foreign_key_by_design`
   与 `test_tenant_isolation.py` 共同锁定该约定。

### 决策 9（R2 改写）：唯一加锁顺序 `run → node_run → node_attempt → project`

**任何**同时操作 Run / NodeRun / NodeAttempt / Project 的事务，必须按该全序获取行锁。
R1 曾在提交事务中使用 `project → attempt → node_run` 的反向顺序（因为版本号分配需要先锁 Project），
与 `advance_run`（`run → node_run → node_attempt → project`）形成潜在死锁环；R2 统一为单一全序，
并把 Project 锁下沉为**最内层**（由 `artifacts.register` 在需要分配 `revision_no` 时获取）。

各执行路径的加锁序列（均为该全序的**子序列**）：

| 路径 | 加锁序列 | 位置 |
|---|---|---|
| `AttemptRunner._start`（claimed→running） | `run` → `node_run` → `node_attempt` | `executor/runner.py` |
| `AttemptRunner._succeed`（最终提交） | `run` → `node_run` → `node_attempt` → `project` | `executor/runner.py` |
| `AttemptRunner._fail` | `run` → `node_run` → `node_attempt` | `executor/runner.py` |
| `Orchestrator.advance_run` | `run` → `node_run`(逐个, by node_key) → `node_attempt` → `project` | `orchestrator/engine.py` |
| `Orchestrator._apply_cancellation` | `run` → `node_run` → `node_attempt` | `orchestrator/engine.py` |
| `Orchestrator._aggregate` / `_finish_project` | `run` → … → `project` | `orchestrator/engine.py` |
| `Orchestrator.start_run` / `update_status` | `project`（不与其他三类兼取；单表事务） | `orchestrator/engine.py`、`storage/projects.py` |
| `artifacts.register`（版本号分配） | `project`（由调用方保证此前已按序锁 run/node_run/attempt） | `storage/artifacts.py` |
| `storage/runs.update_status` / `node_runs.set_status` / `node_attempts.*` | 单表行锁 | `storage/*` |

**不使用** advisory lock（PgBouncer 事务模式亦不支持）。

> 注：`start_run` 是**单表事务**（只写 projects/runs/node_runs 的新行，不锁既有 run/node_run/attempt），
> 与外层全序无冲突；`retry_node`、`_decide` 的多步操作分布在**多个短事务**中，锁在事务间释放，不构成锁环。

### 决策 10：独立周期心跳，与 `progress_cb` 解耦

WB-01 的续租只发生在 `progress_cb` 内；silent handler（长时间不报进度）会因
`heartbeat_at` 陈旧而被巡检误判 `WORKER_LOST`。

修正：
- `executor/heartbeat.py::HeartbeatMonitor` 在 handler 执行期间以**独立线程 + 独立数据库事务**
  周期续租；间隔 `min(lease_seconds/3, 10s)`，测试可注入更短间隔；
- `finally` 中 `stop()` 并 `join()`，**不遗留后台线程**；
- `beat()` 返回 `False`（续租失败 / 租约易主）→ 置 `lease_lost`、写 `attempt.lease_lost` 审计事件，
  调用方据此**拒绝最终提交**（`error_code=LEASE_LOST`），并最佳努力清理未登记文件；
- `progress_cb` 仍刷新进度，但**不再承担唯一续租职责**。

**R2 补充（`stop()` 语义，决策 10 的一部分）**：
- `stop()` 返回 `bool`（是否已可靠停止）。`join` 超时且线程仍存活时 **不清空线程引用**，
  且 `running` 继续真实反映线程状态；此后可再次调用 `stop()` 重试；
- 仅当线程确已退出才清除引用；
- 心跳覆盖范围见决策 15（含校验与哈希阶段），并在进入提交事务前收取。

### 决策 11：成功提交是**单事务全成或全滚**

`executor/runner.py::_succeed` 在**一个事务**内依次完成：

1. `projects.lock`（串行化版本号分配，见决策 13）；
2. `node_attempts.require_running_owned`：再次 `FOR UPDATE` 锁定并校验 attempt **仍为本 worker 的 running**
   （非 running → `ConflictError`；租约易主 → `LeaseConflictError`）；
3. 分配并登记**全部** `ArtifactRevision`；
4. `attempt → succeeded`（携 output）；
5. `node_run → succeeded | waiting_approval`；
6. 创建 `Approval`（若 `awaiting_approval`）；
7. 写入 `attempt.succeeded` 事件。

任一失败 → **整体回滚**：不留 `ArtifactRevision`、不留半成品 `Approval`、不写成功事件、
attempt 不会被覆盖为 `succeeded`。路径校验与哈希计算在事务**外**完成（I/O 不进事务）。
未登记文件做**最佳努力清理**（`unlink(missing_ok=True)`），数据库不视其为正式产物。

### 决策 12：幂等键使用 `INSERT ... ON CONFLICT DO NOTHING` 原子占位

原实现「先 SELECT 再普通 INSERT」存在竞争窗口（两个并发请求都读到"不存在"）。

修正（`storage/idempotency.py::check_or_reserve`）：
```sql
INSERT INTO cybercut.idempotency_keys (...) VALUES (...)
ON CONFLICT (tenant_id, operation, idempotency_key) DO NOTHING
RETURNING ...
```
- 写入成功 → 本次为新占位，返回 `None`；
- 未写入（冲突）→ 读取既有记录：同 `request_hash` → 规范重放；不同 → `IdempotencyConflictError`。
- 若并发方事务未提交，本语句会**阻塞等待**其结束，因此不存在「双方都读到不存在」的窗口；
- 任何驱动异常（含 `UniqueViolation`）统一转换为领域异常，**不外泄 psycopg2 类型**。

`Orchestrator.create_project` 的重放分支同时收紧：命中既有记录但资源不可用时**拒绝新建**（防重复 Project）。

### 决策 13：产物版本号由 Project 行锁串行化

`revision_no = max(revision_no) + 1` 在并发下会竞争。修正：
- `storage/artifacts.py::register` 在 `revision_no` 缺省时先 `projects.lock(conn, project_id, tenant_id)`
  （`SELECT ... FOR UPDATE`）再计算下一版本；
- 位于成功提交事务内（决策 11），因此与终态写入共享同一原子边界；
- **不使用** PostgreSQL advisory lock，**不引入**任何额外服务；
- 同一事务内重复加锁为空操作，故 runner 与直接调用方都安全。

---

## WB-01R2 新增决策

### 决策 14（**已被决策 17 取代**）：产物所有权可证明；历史正式产物受保护

> **R3 说明**：本决策的「执行前项目文件快照（`snapshot_project_files`）证明归属」方案已在 WB-01R3 中**废除**
> —— 该机制仍是启发式的所有权推断。R3 改为「暂存区 + 执行器分配正式路径」的**结构性**保证（决策 17）。
> 本决策中仍然有效的部分：产物路径必须由执行器决定、历史正式产物不可覆盖（在 R3 中因 handler 无法触达正式区而**结构性成立**）。
> 清理必须同时校验归属与数据库引用状态的**精神**由决策 17 的「只删除本次新分配的 revision 目录 / 只删除本次 attempt 的暂存目录」替代。

**问题**：R1 的 `_cleanup_unregistered` / `_cleanup_declared` 直接删除 handler 声明的路径，
但**未证明文件属于当前 attempt**；若 handler 声明了历史正式产物路径，清理可能删除他人文件。

**决策**：

1. **attempt 独占工作目录**：`{project_root}/.attempts/{attempt_id}/`（`NodeContext.attempt_dir`），
   在 handler 执行前创建、在 attempt 结束时整体清理；**正式产物不得声明在该目录内**
   （否则会被随目录一起删除）——声明即拒绝（`ARTIFACT_NOT_OWNED`）。
2. **归属证明（不依赖文件名）**：handler 执行**前**对项目根做一次文件快照
   （`paths.snapshot_project_files`，排除 `.attempts/`，只遍历不读内容）。
   声明路径**不在快照中** ⇔ 由本 attempt 新建 ⇒ 归属成立；
   若路径已存在（历史文件 / 他人 attempt 遗留）⇒ 拒绝提交（`ARTIFACT_NOT_OWNED`，fail-closed）。
   —— 这可证明地排除「仅凭文件名看起来包含 attempt_id」的启发式。
3. **历史正式产物保护**：在**最终提交事务内**（已按决策 9 持锁）查询
   `artifact_revisions` 是否引用了任一待登记 `relative_path`；命中即抛
   `ArtifactHistoricalError`（`ARTIFACT_HISTORICAL`）→ 事务整体回滚，既不覆盖也不删除历史文件。
4. **清理双重校验**：删除前必须同时满足
   (a) 路径归属成立（不在执行前快照中）；(b) 数据库引用状态为空。
   引用状态查询失败时**不删除任何文件**（fail-closed）。
5. **产物路径必须 attempt 唯一**：内建 handler 使用
   `artifacts/{node_key}/{attempt_id}.json`，避免「跨 Run / 重跑复用同一路径」触发 fail-closed 拒绝。

### 决策 15：心跳窗口覆盖「校验 + 哈希」，并在进入提交事务前收取

R1 的心跳在 handler 返回后即停止，**路径校验 / sha256 / 大小计算**这一段未被覆盖；
若哈希耗时较长，租约可能陈旧甚至被巡检判死。

**决策**：心跳线程的生命周期为
`handler.execute` → 路径与归属校验 → `sha256`/大小计算 → 提交前准备；
在**进入最终数据库提交事务之前**：

1. `monitor.stop()` 并**成功 join**（`stop()` 返回 `True`）；未成功 join 则拒绝提交
   （`error_code=HEARTBEAT_NOT_STOPPED`，fail-closed）；
2. 随后在提交事务的**首部**（决策 9 的锁序第 1–3 步）用行锁**重新校验** attempt 仍为
   `running` 且 `claimed_by` 属于当前 worker（`require_running_owned`）；
3. 校验失败 → 拒绝提交并回滚。

### 决策 16：`0002` 约束判重限定 `(conname, conrelid)`

R1 的 `0002` 仅按 `conname` 判重：若**其他 schema** 存在同名约束，会误判「已存在」而跳过创建，
导致 CyberCut 侧缺少租户约束。

**决策**：判重条件必须同时限定 `conname` 与 `conrelid`：

```sql
IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = p_conname
       AND conrelid = p_table          -- regclass：精确到目标表
) THEN ... END IF;
```

实现方式：迁移内定义临时辅助函数 `cybercut.ensure_fk_if_absent(conname, table regclass, definition)`，
12 条复合外键统一经它创建，文件末尾 `DROP FUNCTION`（不在 `cybercut` schema 留额外对象）。
未修改 `0001`。

---

## WB-01R3 新增决策

### 决策 17：产物「暂存 → 执行器分配正式路径 → 原子发布」模型

**问题（R2 遗留）**：R2 用「执行前项目文件快照」推断归属，仍是启发式；
且 handler 直接参与决定正式路径（`ArtifactSpec.relative_path`），正式区存在被误写/误删的风险面。

**决策（结构性保证，取代决策 14 的推断式保证）**：

1. **唯一写入区**：`NodeHandler` 只能把待发布产物写入 `NodeContext.attempt_dir`
   （`{project_root}/.attempts/{attempt_id}/`）；`ArtifactSpec.staged_relative_path`
   必须解析到该目录内，越界（`..` / 绝对路径 / 盘符）→ `ARTIFACT_NOT_OWNED`。
2. **执行器分配正式路径**：执行器为每条产物预生成 `artifact_revision_id`（UUID4）并分配
   `{project_root}/artifacts/{artifact_revision_id}/{safe_filename}`；
   `safe_filename` 由 `paths.sanitize_filename()` 从 staged 文件名规约。
   DB 中 `artifact_revisions.relative_path` 记录该**执行器最终分配**的正式路径。
3. **暂存文件哈希**：SHA-256 与大小从 attempt 暂存目录内的 staged 文件计算（心跳窗口内）。
4. **原子发布**：`os.replace(staged, official)` —— 同一文件系统内的原子移动；
   同名 staged 因 `artifact_revision_id` 不同而落到**不同**正式路径。
5. **不覆盖**：发布前断言目标目录与目标文件**均不存在**（`exist_ok=False`），
   否则 `ARTIFACT_PUBLISH_CONFLICT`（既不覆盖既有文件，也不复用既有目录）。
6. **提交顺序**：加锁（`run → node_run → node_attempt`）→ 校验租约 → 分配 id/正式路径 →
   原子移动 → 登记全部 `ArtifactRevision`（内部锁 `project`）→ attempt/node_run/approval/event
   同事务提交 → 最后清理 attempt 暂存目录。
7. **失败清理（严格受限）**：
   - DB 回滚；**仅**删除「本次新分配」的 `artifact_revision_id` 目录；
   - **绝不**依据 handler 提供的任何项目路径删除文件；
   - **绝不**扫描或删除其它 attempt 的文件；
   - attempt 暂存目录在 `finally` 中整体删除（它完全属于本 attempt）。
8. **崩溃容忍**：进程崩溃可能留下**无 DB 引用**的孤立 revision 目录 —— 允许保留，交由后续 GC；
   由于 id 为 UUID4 且发布前断言目标不存在，**绝不覆盖历史产物**。

### 决策 18：心跳停止失败按**进程级致命错误**处理

**问题（R2 遗留）**：R2 在 `stop()` 返回 `False`（join 超时且线程仍存活）时仍继续走
「拒绝提交 → 返回失败的 attempt」路径，worker 会继续领取新任务 —— 此时心跳线程可能仍在访问数据库资源。

**决策**：
1. 进入提交事务前 `stop(timeout=heartbeat_stop_timeout)`（默认 `max(2s, interval×2)`，可注入）；
2. 首次 join 失败 → `beat.interrupt()`（PostgreSQL `conn.cancel()` + 中断标志）→ 二次 `stop()`；
3. 二次仍失败 → 抛 `HeartbeatStuckError`：**不得 close** 其资源（线程可能仍在使用），
   `AttemptRunner.execute` 直接抛出该异常而**不**降级为普通的 attempt 失败；
4. `Worker.step()` 捕获该异常后置 `poisoned` 并**继续抛出**；此后任何 `step()` 立即抛错，
   即 worker 不再领取新任务（进程应退出并由外部重启）；
5. 生产 beat 必须**有界**：连接 `connect_timeout`（`Database` 注入，5s）+ 专用连接上
   `SET statement_timeout = 5000`，保证单次 beat 不会无限阻塞；
6. 正常路径下 `stop()` 返回 `True` 才释放资源，runner 返回前心跳线程**必然已退出**。

---

## 备选方案与被否原因

| 备选 | 否因 |
|---|---|
| 复用基座 `TaskManager` 作执行器 | 进程级 `recover_running()` 全局副作用 → 多进程互毁（ADR-0001 §1/§3）；且无多进程领取 |
| `pg_advisory_lock` 分布式锁 | PgBouncer 事务模式显式禁用（`tasks/store.py:3-8`） |
| `SELECT ... FOR UPDATE` + `UPDATE` 两条语句 | 存在「选中后被并发抢占」窗口；需额外重试与错误处理 |
| Redis / Celery / Kafka 队列 | WB-01 §一.3 明确禁止引入；且引入第二套基础设施 |
| 内存队列 + DB 仅作日志 | 重启即丢在途任务，违背「重启后 queued 仍可领取」要求 |
| `running` 心跳超时后直接重跑同一 attempt | 重复副作用不可控 |
| 把 claim 与执行放在同一事务 | 长事务持锁，阻塞其它 worker 领取其它任务 |
| 续租只依赖 `progress_cb`（WB-01 原实现） | silent handler 不报进度 → 心跳陈旧 → 被误判 `WORKER_LOST`（WB-01R 修复） |
| 幂等「先 SELECT 再普通 INSERT」（WB-01 原实现） | 并发下存在双写窗口，可能创建重复 Project（WB-01R 修复） |
| advisory lock 分配 `revision_no` | PgBouncer 事务模式禁用；且需额外会话状态 |
| `revision_no` 用 `max()+1` 不加锁 | 并发竞争导致版本号重复或合法请求失败（WB-01R 修复） |
| 为 `events` 加外键 | 审计日志不应受级联删除/引用约束影响（见决策 8） |
| 心跳失败后仍提交终态 | 可能出现「产物已登记但租约已易主」的重复执行（WB-01R 禁止） |

---

## 后果（Consequences）

**正面**

- 多 worker 并发**零重复执行**（并发 claim 测试：2 worker 争 1 条任务 → 恰好 1 胜）；
- 进程崩溃/重启后 `queued` 任务仍可被任意新 worker 领取（无需重放日志）；
- `claimed` 与 `running` 的差异化处理同时保证了「吞吐」与「无重复副作用」；
- 仅依赖 PostgreSQL，无新增基础设施；
- 执行真相全部可 SQL 审计（`node_attempts` 保留全部历史尝试，含失败与丢失）。

**负面 / 代价**

- 每次领取/心跳/进度上报都是一次短连接往返（当前实现为短连接；高吞吐场景需连接池）；
- 租约依赖时钟：`heartbeat_at` 与数据库 `now()` 比较，要求 worker 与 DB 时间一致（同集群部署）；
- `running` 判死后的处理是「保守但耗资源」的：不确定是否已产生副作用，只能整条重试；
- 缺少「执行幂等键」传递到 handler：handler 自身若做外部写操作，仍需自行保证幂等
  （WB-01 范围内 handler 只写项目目录内文件，风险受控）。

**已知限制（WB-01/01R 边界内可接受）**

1. 无跨节点优先级/配额（FIFO by `available_at`）；
2. 无「任务级超时」强制中断：长跑 handler 只能靠租约判死，不能主动 kill；
3. 单 worker 内为串行消费（`Worker.step()` 一次一条）；并行度靠多进程/多 Worker 实例；
4. **提交事务较重**：锁 project → attempt → node_run 并在其中登记全部产物；产物数量多时事务较长
   （本阶段单节点产物数量少，可接受；后续如需大批量产物可考虑两阶段登记 + 补偿）；
5. **租约依赖时钟一致**：`heartbeat_at` 与数据库 `now()` 比较，要求 worker 与 DB 同源时钟；
6. **`events` 无外键**：引用完整性由应用层写入约定保证（决策 8），不享受数据库层校验；
7. **心跳线程为进程内线程**：若 handler 阻塞在不可中断的 C 调用上，心跳仍会独立续租
   （这正是期望行为），但无法中断 handler 本身。
