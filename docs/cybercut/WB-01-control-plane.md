# WB-01 · CyberCut 控制面与可靠任务执行器

| 项 | 值 |
|---|---|
| 阶段 | WB-01（控制面 + 领域状态机 + PostgreSQL NodeAttempt 队列）+ **WB-01R / R2 / R3 可靠性收口** |
| 基座 commit | `c8ea1c6e025cbc697a87d5626c2a300ef78504ce`（FlowMind，只读引用，未修改） |
| 代码位置 | `src/cybercut/`（业务代码）· `migrations/cybercut/`（纯 SQL 迁移）· `tests/cybercut/`（测试） |
| 数据库对象 | 仅 `cybercut` schema（不动 `flowmind` schema / `tasks` 表） |
| 迁移 | `0001_init.sql`（建表）+ `0002_tenant_consistency.sql`（复合唯一键 + 复合外键） |
| 关联 ADR | `ADR-0001`（基座集成边界）、`ADR-0002`（节点执行与 claim 协议，决策 8–16） |
| 本阶段**不含** | 素材上传、AI 脚本、分镜、FFmpeg、ASR、渲染、SSE、MCP 工具 |

> **WB-01R 整改要点**
> 1. **独立周期心跳**：`executor/heartbeat.py`，与 `progress_cb` 解耦；续租失败拒绝最终提交；
> 2. **原子成功提交**：产物登记 + attempt/node_run 终态 + Approval + Event **同一事务**，全成或全滚；
> 3. **数据库强制租户一致性**：0002 复合外键（`events` 刻意除外，见 ADR-0002 决策 8）；
> 4. **并发幂等**：`INSERT ... ON CONFLICT DO NOTHING` 原子占位，消除先查后插竞争；
> 5. **产物版本号串行化**：提交事务内锁 Project 行后分配 `revision_no`。
>
> **WB-01R2 整改要点**
> 6. **产物所有权可证明**：attempt 独占工作目录 `.attempts/{attempt_id}/` + 执行前快照证明「本 attempt 新建」；
>    正式产物路径必须 attempt 唯一（`artifacts/{node_key}/{attempt_id}.json`）；
> 7. **历史正式产物保护**：提交事务内校验路径未被任何 `ArtifactRevision` 引用，命中即整体回滚；清理同样双重校验；
> 8. **心跳窗口延长**：覆盖 handler 执行 → 路径校验 → sha256/大小计算；进入提交事务前停止并**成功 join**；
> 9. **唯一锁顺序**：`run → node_run → node_attempt → project`（R1 的反向顺序已消除）；
> 10. **`stop()` 语义修正**：join 超时不清线程引用、`running` 真实、可重试；**0002 判重**限定 `(conname, conrelid)`。
>
> **WB-01R3 整改要点（产物暂存与发布模型最终收口）**
> 11. **废除快照证明所有权**：`snapshot_project_files` 已移除；handler **只能**把待发布产物写入
>     `NodeContext.attempt_dir`，`ArtifactSpec.staged_relative_path` 越界即 `ARTIFACT_NOT_OWNED`；
> 12. **执行器分配正式不可变路径**：`artifacts/{artifact_revision_id}/{safe_filename}`，
>     DB 记录该正式路径；同名 staged 因 id 不同而落到不同正式路径；
> 13. **原子发布 + 严格失败清理**：同文件系统 `os.replace`；发布前断言目标不存在（绝不覆盖）；
>     失败仅删除**本次新分配**的 revision 目录，绝不按 handler 路径删除、绝不扫描他人文件；
> 14. **心跳停止失败 = 进程级致命**：中断有界 I/O → 二次 join → 仍失败抛 `HeartbeatStuckError`，
>     worker 置 `poisoned` 且不再领取任务；beat 连接带 `connect_timeout` + `statement_timeout`。

---

## 1. 目标与边界

**目标**：让 CyberCut 拥有独立的「项目控制面 + 领域状态机 + 可靠任务执行器」，可在多进程下安全并发、
进程重启后继续执行，且**不依赖** FlowMind 的任务引擎。

**硬边界（WB-01 §一）**

| 约束 | 落实方式 |
|---|---|
| 不修改 FlowMind 核心文件 | `src/cybercut/**` 与 `flowmind` 无 import 关系；全仓未改动 `contracts.py` / `skill.py` / `server*.py` / `tasks/*` |
| 不修复 F-01、不调用 `localize_video`、不注册 `@skill` | 代码中无任何 `flowmind` 导入（测试辅助亦不导入） |
| 不引入 Redis/Celery/Kafka/Milvus/工作流框架 | 唯一外部依赖为 PostgreSQL（`psycopg2`） |
| 不改 `flowmind` schema / `tasks` 表 | 迁移只 `CREATE SCHEMA IF NOT EXISTS cybercut` 与 cybercut 内对象 |
| 业务代码只在 `src/cybercut/` | 见目录结构 |
| DB 对象只在 `cybercut` schema | 全部 DDL 以 `cybercut.` 前缀 |
| SQL 参数化、标识符不接受输入 | 全部值走 `%s` 占位符；表/列名为代码字面量 |
| 先写测试 | `tests/cybercut/` 覆盖 18 项要求（见测试报告） |

---

## 2. 模块结构

```
src/cybercut/
├── contracts/            # 领域契约（无 IO）
│   ├── enums.py          # 状态枚举 + 终态集合
│   ├── errors.py         # 领域异常（NotFound/Conflict/InvalidTransition/PathEscape/...）
│   ├── state_machine.py  # 集中转换表 + assert_transition / can_transition
│   ├── models.py         # Pydantic v2 实体模型 + 输入契约（extra=forbid）
│   └── node_handler.py   # NodeContext / ArtifactSpec / NodeResult / NodeHandler 协议
├── storage/              # PostgreSQL 仓储（短连接 + 事务）
│   ├── db.py             # Database（连接/事务/DSN 解析）
│   ├── migrate.py        # 纯 SQL 迁移执行器（幂等）
│   ├── paths.py          # 项目目录 + 产物路径穿越防护
│   └── projects.py / runs.py / node_runs.py / node_attempts.py /
│       artifacts.py / approvals.py / events.py / idempotency.py
├── executor/             # 执行器
│   ├── claim.py          # 单语句原子 claim
│   ├── lease.py          # 心跳续约 / 租约恢复 / worker 判死
│   ├── runner.py         # 单 attempt 生命周期 + 事务性产物登记
│   └── worker.py         # 进程内 worker 循环（step / drain / recover）
├── orchestrator/         # 编排
│   ├── pipeline.py       # 管线定义（NodeSpec / PipelineSpec）
│   └── engine.py         # Orchestrator：Run 生命周期 / 依赖推进 / 审批 / 失效 / 聚合
└── nodes/                # 内部 NodeHandler（不进 FlowMind registry）
    ├── registry.py       # NodeRegistry
    ├── echo.py           # 测试用 echo handler
    ├── fail.py           # 可控失败 handler
    └── gate.py           # 审批闸门 handler
```

**重要**：`src/cybercut/` 与 `src/flowmind/` 完全解耦。内部节点是 **CyberCut `NodeHandler`**，
**不写入** `flowmind/skills/`、**不追加** `flowmind/skills/__init__.py`，因此基座
`register_all()`（无差别暴露整个 `_REGISTRY`）**不会**把它们暴露为 MCP 工具。

---

## 3. 领域模型（ER）

```mermaid
erDiagram
    PROJECTS ||--o{ RUNS : "project_id"
    RUNS ||--o{ NODE_RUNS : "run_id"
    NODE_RUNS ||--o{ NODE_ATTEMPTS : "node_run_id"
    NODE_RUNS ||--o{ ARTIFACT_REVISIONS : "node_run_id"
    NODE_RUNS ||--o{ APPROVALS : "node_run_id"
    PROJECTS ||--o{ ARTIFACT_REVISIONS : "project_id"

    PROJECTS {
        text project_id PK
        text tenant_id
        text name
        jsonb goal_json
        jsonb automation_policy_json
        text status
        text current_run_id FK
        int  version "乐观锁"
        text idempotency_key
        timestamptz archived_at
        timestamptz created_at
        timestamptz updated_at
    }
    RUNS {
        text run_id PK
        text project_id FK
        text tenant_id
        int  run_no "UNIQUE(project_id,run_no)"
        text status
        text pipeline_version
        text requested_from_node
        timestamptz started_at
        timestamptz finished_at
        timestamptz cancel_requested_at
        text failure_code
        text failure_message
    }
    NODE_RUNS {
        text node_run_id PK
        text run_id FK
        text tenant_id
        text node_key "UNIQUE(run_id,node_key)"
        text status
        jsonb depends_on
        jsonb input_revision_refs
        int  latest_attempt_no
        bool approval_required
        timestamptz invalidated_at
        text invalidated_reason
    }
    NODE_ATTEMPTS {
        text attempt_id PK
        text node_run_id FK
        text tenant_id
        int  attempt_no "UNIQUE(node_run_id,attempt_no)"
        text status
        text executor_kind "cybercut_worker|flowmind_task"
        text executor_ref "仅 flowmind_task 时填"
        jsonb input_json
        jsonb output_json
        text claimed_by
        timestamptz claimed_at
        timestamptz heartbeat_at
        timestamptz available_at
        timestamptz started_at
        timestamptz finished_at
        text error_code
        text error_message
        jsonb error_details
        numeric progress
        text stage
        text message
    }
    ARTIFACT_REVISIONS {
        text artifact_revision_id PK
        text project_id FK
        text run_id
        text node_run_id
        text attempt_id
        text tenant_id
        text kind "UNIQUE(project_id,kind,revision_no)"
        int  revision_no
        text relative_path
        text sha256
        bigint size_bytes
        jsonb metadata_json
        text supersedes_id
        text status
    }
    APPROVALS {
        text approval_id PK
        text project_id
        text run_id
        text node_run_id
        text artifact_revision_id
        text tenant_id
        text status
        text feedback
        jsonb scope_json
        int  expected_revision "UNIQUE(node_run_id,expected_revision)"
        text decided_by
        timestamptz decided_at
    }
    EVENTS {
        bigserial event_id PK "单调递增"
        text tenant_id
        text project_id
        text run_id
        text node_run_id
        text event_type
        jsonb payload_json
        timestamptz created_at
    }
    IDEMPOTENCY_KEYS {
        text tenant_id PK
        text operation PK
        text idempotency_key PK
        text request_hash
        jsonb response_json
        text resource_id
        timestamptz created_at
        timestamptz expires_at
    }
```

**不可变性（数据库触发器保证）**

| 表 | 约束 | 触发器 |
|---|---|---|
| `artifact_revisions` | 不可覆盖（版本用新行 + `supersedes_id` 表达） | `BEFORE UPDATE OR DELETE` → `cybercut.forbid_mutation()` |
| `events` | 只追加，不更新不删除 | 同上 |

**其它约束**：`UNIQUE(project_id, run_no)`、`UNIQUE(run_id, node_key)`、`UNIQUE(node_run_id, attempt_no)`、
`UNIQUE(project_id, kind, revision_no)`、`UNIQUE(node_run_id, expected_revision)`、
`UNIQUE(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL`。

**租户一致性（0002 新增）**：父表 `(主键, tenant_id)` 复合唯一索引 + 子表复合外键
（`runs→projects`、`node_runs→runs`、`node_attempts→node_runs`、
`artifact_revisions→projects/runs/node_runs/node_attempts` 及自引用 `supersedes_id`、
`approvals→projects/runs/node_runs/artifact_revisions`）。`events` 例外（见 §8 与 ADR-0002 决策 8）。

---

## 4. 状态机

### 4.1 转换表（唯一真源：`contracts/state_machine.py`）

```mermaid
stateDiagram-v2
    direction LR
    state "Project" as P {
      [*] --> draft
      draft --> active
      active --> completed
      active --> failed
      active --> cancelled
      completed --> [*]
      failed --> [*]
      cancelled --> [*]
    }
```

```mermaid
stateDiagram-v2
    direction LR
    state "Run" as R {
      [*] --> queued
      queued --> running
      running --> waiting_approval
      running --> succeeded
      running --> failed
      running --> cancelling
      waiting_approval --> running
      waiting_approval --> failed
      waiting_approval --> cancelling
      cancelling --> cancelled
      succeeded --> [*]
      failed --> [*]
      cancelled --> [*]
    }
```

```mermaid
stateDiagram-v2
    direction LR
    state "NodeRun" as N {
      [*] --> blocked
      blocked --> ready
      ready --> running
      running --> waiting_approval
      running --> succeeded
      running --> failed
      running --> cancelled
      waiting_approval --> ready
      waiting_approval --> succeeded
      waiting_approval --> cancelled
      succeeded --> invalidated
      invalidated --> ready
      failed --> ready : 显式重试
      cancelled --> [*]
    }
```

```mermaid
stateDiagram-v2
    direction LR
    state "NodeAttempt" as A {
      [*] --> queued
      queued --> claimed
      queued --> cancelled
      claimed --> queued : 租约超时恢复
      claimed --> running
      claimed --> cancelled
      running --> succeeded
      running --> failed
      running --> cancelled
      succeeded --> [*]
      failed --> [*]
      cancelled --> [*]
    }
```

**校验**：所有迁移必须经 `state_machine.assert_transition(entity, from, to)`；
非法迁移抛 `InvalidTransitionError`（携带 `entity/from/to/allowed`），**绝不静默忽略**。
幂等自反（`from == to`）**不**视为合法，避免掩盖逻辑错误。

---

## 5. worker claim 协议

```mermaid
sequenceDiagram
    participant W as Worker(进程)
    participant DB as PostgreSQL
    participant H as NodeHandler

    W->>DB: BEGIN
    DB->>DB: SELECT ... WHERE status='queued' AND available_at<=now()<br/>ORDER BY available_at,created_at LIMIT 1<br/>FOR UPDATE SKIP LOCKED
    DB->>DB: UPDATE ... SET status='claimed', claimed_by, claimed_at, heartbeat_at
    W->>DB: COMMIT  （① 提交后才执行）
    W->>DB: BEGIN; UPDATE status='running'（claimed→running）; COMMIT  （② 执行前）
    W->>H: execute(context, input_json)
    H-->>W: NodeResult(output, artifacts, decision_trace, warnings)
    W->>DB: BEGIN; 校验路径+算 sha256/size; INSERT artifact_revisions; UPDATE status='succeeded'; COMMIT
    Note over W,DB: 期间周期性 UPDATE heartbeat_at（续约）
```

**规则对照（WB-01 §五）**

| 要求 | 实现 |
|---|---|
| 1. 选 `queued` 且 `available_at<=now()` 的最早任务 | `ORDER BY available_at, created_at LIMIT 1` |
| 2. 锁行后原子更新为 `claimed` + 写 `claimed_by/claimed_at/heartbeat_at` | 单语句 CTE + `UPDATE ... FROM picked` |
| 3. 提交事务后才执行 handler | `claim_next_attempt()` 内部 `with db.transaction()` 完成后返回 |
| 4. 执行前 `claimed → running` | `runner._start()` → `node_attempts.mark_running()` |
| 5. worker 定期更新 `heartbeat_at` | `LeaseManager.heartbeat()`；进度上报亦刷新心跳 |
| 6. `claimed` 租约超 60s 无心跳 → 恢复 `queued` | `recover_expired_claims()` |
| 7. `running` 心跳超时 → `failed/WORKER_LOST` + 新 attempt | `fail_lost_running_attempts()` + `engine._apply_worker_lost_retry()` |
| 8. 单次 claim 只取一条 | `LIMIT 1` |
| 9. 多 worker 并发不重复 | `FOR UPDATE SKIP LOCKED`（并发测试验证） |
| 10.（R）**独立周期心跳**：handler 执行期间由独立线程 + 独立事务续租，不依赖 `progress_cb` | `executor/heartbeat.py::HeartbeatMonitor`；间隔 `min(lease/3, 10s)` |
| 11.（R）**续租失败 → 拒绝最终提交** | `monitor.lease_lost` → `error_code=LEASE_LOST`，不产生正式产物 |
| 12.（R）**提交前再次校验 running + 租约归属** | `node_attempts.require_running_owned()`（`FOR UPDATE`） |

---

## 5.1 关键事务边界（WB-01R 修正后）

```mermaid
sequenceDiagram
    participant W as Worker
    participant DB as PostgreSQL
    participant H as NodeHandler

    W->>DB: 【事务1】claim：queued -> claimed（FOR UPDATE SKIP LOCKED）
    W->>DB: 【事务2】run/node_run -> running + claimed -> running + attempt.started 事件（锁序 run→node_run→node_attempt）
    Note over W: 建 attempt 独占暂存目录 .attempts/{attempt_id}/
    W->>H: execute(context, input_json)   ← 心跳线程【独立事务】周期续租
    H-->>W: NodeResult（artifacts 声明 staged_relative_path，均在 attempt_dir 内）
    Note over W,DB: 心跳窗口继续覆盖：staged 路径/归属校验 + sha256/大小计算（事务外）
    W->>DB: 停止心跳（stop + 中断 + 二次 join；失败则致命）
    W->>DB: 【事务3·原子】run→node_run→node_attempt 加锁 → require_running_owned<br/>→ 分配 artifact_revision_id 与正式路径 → os.replace 原子发布<br/>→ 登记全部 ArtifactRevision（内部锁 project）→ attempt=succeeded<br/>→ node_run=succeeded|waiting_approval → Approval → attempt.succeeded 事件
    Note over W,DB: 失败 → 回滚 + 仅删除本次新分配的 revision 目录 + 清理 attempt 暂存目录
```

- **事务 3 是唯一的成功提交点**：不存在「产物已登记但 attempt 未成功」或反之的中间态；
- staged 路径校验、归属校验与哈希计算**不进事务**，但**受心跳覆盖**；
- 进入事务 3 之前必须**可靠停止**心跳线程（join 成功），随后由行锁重新校验 running + `claimed_by`；
- **正式路径由执行器分配**：handler 只能写 `attempt_dir`，不能决定也不能写入正式区。

---

## 6. NodeHandler 协议

```python
class NodeHandler(Protocol):
    node_key: str
    def execute(self, context: NodeContext, input_json: dict) -> NodeResult: ...

@dataclass(frozen=True)
class NodeContext:
    attempt_id / node_run_id / run_id / project_id / tenant_id / node_key / attempt_no
    project_root: Path        # data/projects/{project_id}/（已创建）
    attempt_dir: Path | None  # .attempts/{attempt_id}/（独占临时目录，结束即清理）
    data_root: Path
    worker_id: str
    progress_cb: Callable[[float, str, str], None]   # 落库 + 事件
    cancel_check: Callable[[], bool]                 # 协作式取消

class ArtifactSpec(BaseModel):   # 只是「待登记文件」描述
    kind: str
    relative_path: str
    metadata: dict

class NodeResult(BaseModel):
    output: dict
    artifacts: list[ArtifactSpec]
    decision_trace: list[dict]
    warnings: list[str]
    awaiting_approval: bool = False   # True -> node_run 置 waiting_approval
```

**契约（WB-01R3 暂存/发布模型）**：
- handler **不写数据库**，也**不决定正式路径**；
- **只能写暂存区**：待发布产物必须写入 `context.attempt_dir`
  （`{project_root}/.attempts/{attempt_id}/`），并在 `ArtifactSpec.staged_relative_path`
  中以**相对该目录**的路径声明；
  越界（`..` / 绝对路径 / 盘符）→ `ARTIFACT_NOT_OWNED`，staged 文件不存在 → `ARTIFACT_MISSING`；
- **执行器分配正式路径**：`artifacts/{artifact_revision_id}/{safe_filename}`（UUID4 + `sanitize_filename`）；
  DB 的 `relative_path` 记录该路径；同名 staged 因 id 不同而发布到**不同**正式路径；
- **哈希来源**：SHA-256 与大小从 attempt 暂存目录内的 staged 文件计算（心跳窗口内）；
- **发布**：同文件系统 `os.replace` 原子移动；发布前断言目标目录/文件均不存在，
  否则 `ARTIFACT_PUBLISH_CONFLICT`（**绝不覆盖**既有文件或目录）；
- **原子提交**：发布与「登记产物 / attempt 终态 / node_run 终态 / Approval / Event」在同一事务边界内，
  任一失败整体回滚，且**仅删除本次新分配的 revision 目录**（绝不按 handler 路径删除、绝不扫描他人文件）；
- **崩溃容忍**：进程崩溃留下的无 DB 引用目录视为孤儿，允许保留待 GC；因 id 唯一且发布前断言不存在，
  历史产物**不可能被覆盖**；
- **心跳停止失败**：`stop` 超时 → 中断有界 beat I/O → 二次 join；仍失败 → `HeartbeatStuckError`
  （进程级致命，worker 不再领取任务），且**不释放**心跳线程仍可能使用的资源；
- 未知 `node_key` → `HANDLER_NOT_FOUND`；handler 抛 `NodeExecutionError(error_code=...)` → 保留稳定错误码。

---

## 7. 最小编排闭环

管线（`orchestrator/pipeline.py::TEST_PIPELINE`）：

```
node_a ──► node_b ──► approval_gate ──► node_c
```

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant W as Worker
    participant DB as PostgreSQL

    O->>DB: create_project（draft）
    O->>DB: start_run（project→active, run=queued, 生成 4 个 node_runs；root=ready）
    W->>DB: claim node_a → running → succeeded
    O->>DB: advance：node_b blocked→ready，入队 attempt
    W->>DB: claim node_b → succeeded
    O->>DB: advance：approval_gate blocked→ready，入队 attempt
    W->>DB: claim approval_gate → awaiting_approval → node_run=waiting_approval, run=waiting_approval
    Note over O,DB: 人工审批
    O->>DB: approve(expected_revision) → gate=succeeded，node_c→ready，入队
    W->>DB: claim node_c → succeeded
    O->>DB: advance：全部 succeeded → run=succeeded, project=completed
```

**驳回路径**

```
approval_gate waiting_approval --reject--> ready
node_b succeeded --invalidated--> ready（新建 attempt）
node_b 的后代（含已 succeeded 的并行分支）--invalidated--> ...
```

**保证的不变量**

1. 依赖未全部 `succeeded` 的节点**不会**入队（`ready` 状态也会被守卫拦下）；
2. **已完成的前置节点不会因局部重跑重复执行**（`node_runs` 保持 `succeeded`，不产生新 attempt）；
3. 重试一律**新建 attempt**，历史保留。

---

## 8. 存储与安全

| 项 | 实现 |
|---|---|
| 项目根目录 | `{CYBERCUT_DATA_ROOT}/projects/{project_id}/`（默认 `.cybercut-data`）；本阶段只创建目录 |
| 路径穿越防护 | `paths.resolve_artifact_path()`：拒绝绝对路径/盘符，`resolve()` 后前缀校验，越界抛 `PathEscapeError` |
| `relative_path` 入库 | 一律经 `relative_to_project()` 归一为项目根相对的 POSIX 路径 |
| 租户隔离 | 每个仓储函数的 WHERE 均含 `tenant_id`；跨租户与不存在**同形**（`None` / `NotFoundError`） |
| 产物不可覆盖 | DB 唯一键 + 不可变触发器；应用层无 update/delete 接口 |
| 哈希与大小 | 登记时流式计算 `sha256` 与字节数 |
| 正式产物位置 | 项目目录内（**不进入** FlowMind `tasks/`/`outputs/`，不受基座 TTL GC 影响） |
| 时间 | 连接级 `SET TIME ZONE 'UTC'`，全部列 `timestamptz` |
| ID | UUID4 字符串 |
| JSON | `jsonb`（`psycopg2.extras.Json` 适配），不使用 TEXT 编码 JSON |
| **租户一致性（R）** | 0002 复合外键：`(子.tenant_id, 子.父id) → (父.tenant_id, 父.id)`；跨租户引用被**数据库**拒绝 |
| **可空引用（R）** | `artifact_revisions.supersedes_id`、`approvals.artifact_revision_id` 允许 NULL（`MATCH SIMPLE`） |
| **事件表例外（R）** | `events` **不设外键**（追加型审计日志与引用解耦；理由见 ADR-0002 决策 8），租户一致性由应用层写入约定保证 |
| **锁顺序（R2）** | 唯一全序 `run → node_run → node_attempt → project`；Project 锁为最内层（由 `artifacts.register` 获取）；不使用 advisory lock |
| **产物所有权（R3）** | handler 只能写 attempt 独占暂存目录 `.attempts/{attempt_id}/`；`staged_relative_path` 越界即 `ARTIFACT_NOT_OWNED`（无快照启发式） |
| **正式路径分配（R3）** | 执行器分配 `artifacts/{artifact_revision_id}/{safe_filename}`（UUID4 + `sanitize_filename`）；handler 无法决定也无法写入正式区 |
| **发布语义（R3）** | 同文件系统 `os.replace` 原子移动；发布前断言目标不存在；冲突即 `ARTIFACT_PUBLISH_CONFLICT`，**绝不覆盖** |
| **失败清理（R3）** | 仅删除本次新分配的 revision 目录 + 本次 attempt 的暂存目录；不按 handler 路径删除、不扫描他人文件；崩溃孤儿目录留待 GC |

---

## 9. 迁移

| 版本 | 文件 | 内容 |
|---|---|---|
| `0001_init` | `migrations/cybercut/0001_init.sql` | 建 `cybercut` schema、9 张表、索引、`updated_at` 触发器、不可变/只追加触发器 |
| `0002_tenant_consistency` | `migrations/cybercut/0002_tenant_consistency.sql` | 父表 `(主键, tenant_id)` 复合唯一索引；子表改复合外键；移除 0001 的单列外键；判重条件限定 `(conname, conrelid)`（R2） |

- 执行器：`storage/migrate.py::apply_migrations(db, migrations_dir=None)`，账本表 `cybercut.schema_migrations`；
- **幂等**：账本命中即跳过；**且**所有 DDL 自身幂等（`IF NOT EXISTS` / `DROP CONSTRAINT IF EXISTS` / `DO` 块判重），
  账本丢失亦可安全重放；
- **升级路径**：只应用过 0001 的库再执行 `apply_migrations` 即自动补 0002；全新库按 `0001 → 0002` 顺序应用。
  两种路径均有测试覆盖（`test_migrations.py`）。

```bash
python -c "from cybercut.storage.db import Database; from cybercut.storage import migrate; \
print(migrate.apply_migrations(Database()))"
```

---

## 10. 运行方式

```bash
# 依赖：PostgreSQL；设置连接串
export CYBERCUT_PG_DSN="postgresql://user:pass@host:5432/db"

# 迁移
python -c "from cybercut.storage.db import Database; from cybercut.storage import migrate; \
db=Database(); print(migrate.apply_migrations(db))"

# 测试（纯单元无需 PG；集成测试需 CYBERCUT_TEST_PG_DSN）
python -m pytest tests/cybercut -q
```

最小用法示例：

```python
from cybercut.storage.db import Database
from cybercut.orchestrator.engine import Orchestrator
from cybercut.executor.worker import Worker
from cybercut.nodes.registry import build_test_registry

db = Database()                      # 读 CYBERCUT_PG_DSN
orch = Orchestrator(db, data_root="D:/cybercut-data")
worker = Worker(db, build_test_registry(), data_root="D:/cybercut-data",
                orchestrator=orch)

project, _ = orch.create_project(tenant_id="t1", name="demo")
run = orch.start_run(project_id=project["project_id"], tenant_id="t1")
for _ in range(10):
    if worker.step() is None:
        break
```

---

## 11. 已知限制

1. **Run 终态不可复活**：`Run` 的 `succeeded/failed/cancelled` 为终态，状态机不含 `failed → running`。
   失败后要重跑**应新建一个 Run**（`Project 1:N Run` 语义）。
2. **无优先级/配额**：claim 严格按 `available_at, created_at` FIFO。
3. **无主动中断**：长跑 handler 不能被杀，只能靠心跳超时判死。
4. **短连接**：每次操作一条新连接（与 PgBouncer 事务模式口径一致）；高吞吐场景需引入连接池。
5. **`blocked`/`ready` 节点不参与取消**：状态机未定义 `blocked→cancelled` / `ready→cancelled`，
   取消 Run 时这两类节点保持原状态，由 Run 自身的 `cancelled` 语义覆盖。
6. **`executor_kind=flowmind_task` 已预留但未启用**：表结构与字段就绪，本阶段不接线基座任务
   （见 ADR-0001：基座 `TaskManager` 只作可选适配器，且需隔离进程）。
7. **单 worker 内串行**：并行度靠多 Worker 实例 / 多进程。
8. **`pytest` 未加入项目依赖**：WB-01/WB-01R 未修改 `pyproject.toml`（避免触碰基座元数据），
   测试环境需自行安装 `pytest`。
9. **提交事务较重（R）**：按锁序加锁 `run → node_run → node_attempt` 并在事务内登记全部产物；
   产物数量大时事务较长。
10. **`events` 无外键（R）**：引用完整性由应用层写入约定保证（ADR-0002 决策 8）。
11. **租约依赖时钟一致（R）**：`heartbeat_at` 与数据库 `now()` 比较，要求 worker 与 DB 同源时钟。
12. **正式路径含 UUID 目录（R3）**：每个 ArtifactRevision 一个目录，便于发布原子性与失败清理；
    目录数量随版本数增长，长期需配合 GC（孤儿目录无法从 DB 反推时按目录 mtime 清理）。
13. **崩溃孤儿目录（R3）**：进程在发布后、提交前崩溃会留下无 DB 引用的 revision 目录；
    本阶段允许保留（绝不覆盖历史产物），GC 策略留待后续阶段。
14. **心跳不可停止即致命（R3）**：此时不释放心跳资源、worker 置 `poisoned` 并停摆，
    需由外部（进程管理器）重启 worker；这是刻意的 fail-stop 取舍。
