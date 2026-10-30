# WB-01 · 测试报告

| 项 | 值 |
|---|---|
| 阶段 | WB-01（控制面与可靠任务执行器） |
| 日期 | 2026-10-06 |
| 代码版本 | `src/cybercut/` 3916 行（26 个模块）+ `migrations/cybercut/0001_init.sql` + `tests/cybercut/` 18 个文件 |
| 关联文档 | `WB-01-control-plane.md`、`ADR-0002-node-execution-and-claim.md` |

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| Python | 3.13.12（受管隔离 venv，满足项目 `requires-python >= 3.11`） |
| pytest | 9.1.1（**未加入项目依赖**，见 §6 已知限制） |
| PostgreSQL | **18.6**（x86_64-windows），由 conda-forge `postgresql` 提供，实例目录 `D:/cybercut-pgdata-wb01`，端口 **55432** |
| 测试库 | `cybercut_test`（DSN `postgresql://postgres@127.0.0.1:55432/cybercut_test`） |
| 数据目录 | `D:/cybercut-wb01-tmp/<uuid>`（不落在仓库内，不使用 pytest `tmp_path`） |

> 说明：本机原始环境**无 PostgreSQL / 无 docker / 无 psql**（WB-00 已记录）。为产出**真实集成测试证据**，
> 以 conda 安装了 PostgreSQL 服务端（可逆、不修改系统），未引入任何被禁止的组件（Redis/Celery/Kafka/Milvus）。

启动方式（复现用）：

```bash
PGBIN="/d/c/miniconda/envs/cybercut-pg/Library/bin"
"$PGBIN/initdb" -D D:/cybercut-pgdata-wb01 -U postgres --encoding=UTF8 --locale=C --auth=trust
"$PGBIN/pg_ctl" -D D:/cybercut-pgdata-wb01 \
  -o "-p 55432 -c listen_addresses=127.0.0.1" -l D:/cybercut-pgdata-wb01/server.log start
"$PGBIN/createdb" -h 127.0.0.1 -p 55432 -U postgres cybercut_test
```

---

## 2. 测试命令与结果

### 2.1 完整套件（PostgreSQL 可用）

```bash
cd cybercut
export CYBERCUT_TEST_PG_DSN="postgresql://postgres@127.0.0.1:55432/cybercut_test"
python -m pytest tests/cybercut -q
```

```
........................................................................ [ 57%]
......................................................                   [100%]
126 passed in 318.97s (0:05:18)
```

| 指标 | 数量 |
|---|---|
| **通过** | **126** |
| 失败 | **0** |
| 跳过 | 0 |
| 错误 | 0 |

### 2.2 无数据库模式（验证「纯单元测试必须运行」）

```bash
export CYBERCUT_TEST_PG_DSN="postgresql://postgres@127.0.0.1:9/none"   # 不可达
python -m pytest tests/cybercut -q
```

```
ssssssssssssssssssssssssssssssssssssssssssss.................ssssssss... [ 57%]
...................................................sss                   [100%]
71 passed, 55 skipped in 2.37s
```

| 指标 | 数量 | 说明 |
|---|---|---|
| 通过 | **71** | 纯单元测试（状态机 54 + 路径安全 17），**无 PG 亦可运行** |
| 跳过 | **55** | 集成测试，**明确 skip** 并给出原因（提示设置 `CYBERCUT_TEST_PG_DSN`） |
| 失败 | **0** | — |

### 2.3 lint

```bash
python -m ruff check src/cybercut tests/cybercut
# -> All checks passed!
python -m ruff check src          # 原项目质量门（确认未破坏）
# -> All checks passed!
```

---

## 3. 18 项要求覆盖对照

| # | 要求 | 覆盖测试 | 结果 |
|---|---|---|---|
| 1 | migration 幂等执行两次 | `test_migrations.py::test_migration_is_idempotent`、`::test_raw_sql_replay_is_safe`、`::test_rows_survive_reapply` | ✅ |
| 2 | 合法和非法状态转换 | `test_state_machine.py`（54 项：36 合法 + 16 非法 + 自反/终态） | ✅ |
| 3 | 两个 worker 并发 claim 不重复 | `test_claim.py::test_concurrent_claim_never_duplicates`、`::test_concurrent_claim_distributes_across_attempts` | ✅ |
| 4 | claimed 租约超时恢复 | `test_lease.py::test_expired_claim_is_requeued`、`::test_fresh_claim_is_not_requeued` | ✅ |
| 5 | running worker 丢失后的失败与新 attempt | `test_lease.py::test_worker_lost_fails_attempt_and_node`、`::test_worker_lost_creates_new_attempt_via_retry_policy` | ✅ |
| 6 | 重试创建新 attempt 且保留历史 | `test_retry.py::test_retry_creates_new_attempt_and_preserves_history`、`::test_retry_of_non_failed_node_is_rejected` | ✅ |
| 7 | Project/Run/NodeRun 聚合状态 | `test_aggregates.py`（4 项） | ✅ |
| 8 | approve/reject 与 expected_revision 冲突 | `test_approvals.py`（6 项） | ✅ |
| 9 | 修改脚本类产物使下游失效 | `test_invalidation.py`（2 项） | ✅ |
| 10 | 幂等键同请求重放返回同资源 | `test_idempotency.py::test_same_key_same_request_returns_same_project` | ✅ |
| 11 | 相同幂等键不同 request_hash 返回冲突 | `test_idempotency.py::test_same_key_different_request_conflicts` | ✅ |
| 12 | tenant 隔离 | `test_tenant_isolation.py`（3 项） | ✅ |
| 13 | ArtifactRevision 不可覆盖 | `test_artifacts.py`（6 项，含 2 项数据库触发器验证） | ✅ |
| 14 | 路径穿越被拒绝 | `test_path_safety.py`（17 项） | ✅ |
| 15 | 事件按 event_id 稳定递增 | `test_events.py::test_event_ids_are_strictly_increasing`、`::test_events_are_append_only` | ✅ |
| 16 | 服务重启后 queued 任务仍可领取 | `test_restart.py::test_queued_attempt_is_claimable_after_restart`、`::test_running_attempt_survives_restart_until_lease_expires` | ✅ |
| 17 | 取消 queued 与 running 任务 | `test_cancel.py`（5 项） | ✅ |
| 18 | echo 测试管线完整闭环 | `test_pipeline_e2e.py`（3 项，含 `test_echo_pipeline_full_loop`） | ✅ |

---

## 4. 关键证据（原始输出）

> 由 `wb01_evidence.py` 直接运行产出（未写入仓库）。

### 4.1 迁移幂等

```
① migration 幂等（apply 两次）
apply #1 -> []
apply #2 -> []
ledger   -> ['0001_init']
```

### 4.2 并发 claim（**3 worker 争 1 条 → 恰好 1 胜**）

```
② 并发 claim：3 worker 争 1 条 attempt
seeded attempt: b7c5160b-9df3-4a3b-94b9-1df62ca4230f
  w0: CLAIMED b7c5160b-9df3-4a3b-94b9-1df62ca4230f
  w1: None (skip-locked)
  w2: None (skip-locked)
DB final status: claimed | claimed_by: w0
winner count = 1
```

### 4.3 并发 claim 分配（**3 worker / 3 条 → 全覆盖且无重复**）

```
③ 并发 claim：3 worker / 3 条 attempt（无重复）
seeded: 3 attempts
claimed ids      : ['05f18a49-...', '49d72918-...', 'dd3285de-...']
distinct claimed : 3 of 3
unclaimed        : []
```

### 4.4 租约恢复

```
④ claimed 租约超时 -> 恢复 queued
recover_expired_claims(60s) -> ['501b51c4-037c-44a2-9520-f84fc9406b22']
after recovery  : queued | claimed_by: None
re-claim by w-again -> 501b51c4-037c-44a2-9520-f84fc9406b22 | claimed_by: w-again
```

### 4.5 worker 丢失（判死 + 新建 attempt，旧 attempt 保留为历史）

```
⑤ running 心跳超时 -> failed/WORKER_LOST + 重试新建 attempt
attempt#1 before: 1 queued
worker_lost     -> ['dd887fca-d880-4813-951c-4dae17cdaf0c']
attempts after retry policy:
  #1 failed    error_code=WORKER_LOST
  #2 queued    error_code=None
node_a status   : ready
```

### 4.6 重启恢复（**全新进程内对象继续消费 queued**）

```
⑥ 重启恢复：全新实例继续消费 queued
before restart  : {'approval_gate': 'blocked', 'node_a': 'succeeded', 'node_b': 'ready', 'node_c': 'blocked'}
queued attempts : ['queued']
after restart   : claimed nodes -> ['node_b', 'approval_gate']
after restart   : {'approval_gate': 'waiting_approval', 'node_a': 'succeeded', 'node_b': 'succeeded', 'node_c': 'blocked'}
```

**说明**：`worker_after` / `orch_after` / `db_after` 均为**新建对象**，旧对象已被 `del`；
执行依据完全来自 PostgreSQL（`node_attempts` 中 `queued` 行 + `node_runs` 依赖状态）。

---

## 5. 被测行为要点（与证据对应）

| 行为 | 实现位置 | 证据 |
|---|---|---|
| 单语句原子 claim | `storage/node_attempts.py::_CLAIM_SQL` | §4.2 / §4.3 |
| 并发互斥（不重复执行同一 attempt） | `FOR UPDATE SKIP LOCKED` | §4.2 |
| 领取与执行分离 | `executor/claim.py`（claim 事务先提交） | §4.6 |
| `claimed` 可回收 / `running` 只判死 | `executor/lease.py` | §4.4 / §4.5 |
| 重试新建 attempt、保留历史 | `orchestrator/engine.py::_apply_worker_lost_retry`、`storage/node_runs.bump_attempt_no` | §4.5 |
| 状态机集中校验 | `contracts/state_machine.py` | §2.1（54 项） |
| 产物登记（哈希/大小/不可覆盖） | `executor/runner.py::_register_artifacts`、`storage/artifacts.py` | `test_artifacts.py` |
| 事件只追加 | 迁移内 `cybercut.forbid_mutation()` 触发器 | `test_events.py` |

---

## 6. 已知限制与未覆盖项

1. **`pytest` 未写入项目依赖**：WB-01 未修改 `pyproject.toml`（避免触碰基座元数据），
   运行测试需环境自带 pytest。
2. **`executor_kind=flowmind_task` 未接线**：字段/表结构就绪，但本阶段不调用基座任务引擎
   （ADR-0001 决策：仅作可选适配器，且需进程隔离）；无对应集成测试。
3. **未测试多进程（真 OS 进程）**：并发 claim 用线程 + **独立数据库连接**验证（等价并发路径），
   未启动多个 Python 进程。理由：claim 的互斥由数据库行锁保证，与是否同进程无关。
4. **未测试极端规模的 heartbeat 抖动 / 时钟漂移**：租约依赖 worker 与 DB 时钟一致（同集群部署假设）。
5. **未测试 `approval` 的 `scope_json` 复杂语义**：本阶段驳回采用「上游依赖 + 后代连锁」的固定策略，
   `scope_json` 仅作记录。
6. **测试耗时较长**（126 项约 5 分钟）：每个测试之间 `TRUNCATE ... RESTART IDENTITY CASCADE` 需
   `ACCESS EXCLUSIVE` 锁，叠加 Windows 上的短连接开销；后续可换 schema-per-test 或事务回滚隔离。
7. **无 CI**：本阶段未引入 CI 流水线（仓库自身亦无 CI），测试需手工执行。

---

## 7. 结论

- **18 项要求全部覆盖并通过**；
- **126 项集成测试全绿**（真实 PostgreSQL 18.6）；
- 无数据库环境下 **71 项纯单元测试仍全部通过**，集成测试明确 skip；
- `ruff` 对 `src/cybercut` + `tests/cybercut` 全绿，**且未破坏原项目 `ruff check src` 门禁**；
- 未修改任何 FlowMind 源码，未触碰 `flowmind` schema / `tasks` 表，未引入被禁止的组件。
