# CyberCut · 会话交接文档（WB-00 → WB-01R3 已完成）

> 写给下一个对话窗口。本文档自足，读完即可继续工作，无需原对话上下文。
> 最后更新：2026-10-07

---

## 0. 一句话状态

`CyberCut`（多租户视频自动化剪辑项目的自建控制面 + 可靠任务执行器）已完成
**WB-00（基座审计）→ WB-01（控制面）→ WB-01R（可靠性整改）→ WB-01R2（最终收口）→
WB-01R3（产物暂存与发布模型）**，全部测试通过，**未 commit、未 push，WB-02 未开始**。

---

## 1. 工作目录与仓库状态

- 工作目录：`C:\Users\黄嘉林\Desktop\智能agent剪辑\cybercut`（一个 git 仓库，
  其上游是 FlowMind 基座，commit 锁定 `c8ea1c6e025cbc697a87d5626c2a300ef78504ce`）
- **git 状态**：只有 4 个**新增未跟踪**目录
  `?? docs/  ?? migrations/  ?? src/cybercut/  ?? tests/`
  —— `git diff --stat` 为空，**未修改任何已跟踪文件**（FlowMind 零改动是硬性验收项）
- 关键目录：
  - `src/cybercut/` 35 个模块 / 4714 行（contracts·storage·executor·orchestrator·nodes）
  - `migrations/cybercut/` 0001_init.sql（建表）+ 0002_tenant_consistency.sql（复合外键）
  - `tests/cybercut/` 26 文件 / 3666 行（178 用例）
  - `docs/cybercut/` 7 篇：`WB-00-base-audit.md`、`WB-00-capability-matrix.md`、
    `ADR-0001-flowmind-integration-boundary.md`、`ADR-0002-node-execution-and-claim.md`、
    `WB-01-control-plane.md`、`WB-01-test-report.md`、`WB-01R-test-report.md`
- 证据脚本（仓库外）：`D:/cybercut-wb01-tmp/wb01r3_evidence.py` 等

---

## 2. 环境与数据库（⚠️ 最重要的操作信息）

| 项 | 值 |
|---|---|
| Python | `C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe`（受管 venv，pytest 9.1.1 + psycopg2 已装） |
| PostgreSQL | 18.6，二进制在 `D:\c\miniconda\envs\cybercut-pg\Library\bin\` |
| **当前数据目录** | **`D:\cybercut-pgdata-r3`**（新建，含 `cybercut_test` 库，信任认证） |
| 旧数据目录 | `D:\cybercut-pgdata-wb01`（已弃用：存在无法删除的陈旧 `postmaster.pid`，勿再使用） |
| 端口 | 55432 |
| DSN | `postgresql://postgres@127.0.0.1:55432/cybercut_test` |
| 测试数据目录 | `D:\cybercut-wb01-tmp\<uuid>`（pytest `tmp_path` 不可用，见 §6） |

### 2.1 启动 PostgreSQL（每次新会话都要做）

上一会话的 postgres 是**后台任务**，会话结束即终止。**数据目录在磁盘上持久**，只需重新启动：

```bash
PGBIN="/d/c/miniconda/envs/cybercut-pg/Library/bin"
"$PGBIN/postgres" -D "D:/cybercut-pgdata-r3" -p 55432 \
  -c listen_addresses=127.0.0.1 > "D:/cybercut-wb01-tmp/pg-r3.log" 2>&1
```
（用工具的 `run_in_background=true` 执行；`pg_ctl -w start` 会因继承 stdout 把工具挂住）

验证：
```bash
(timeout 3 bash -c "</dev/tcp/127.0.0.1/55432" && echo "PG OPEN" || echo "PG CLOSED")
```

若数据库被清空/迁移丢失：测试夹具会在首次运行时自动 `apply_migrations`（幂等，纯 SQL）。

### 2.2 验收命令（必须串行，禁止两个 pytest 共用同一库）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
```

最近一次（WB-01R3）结果：`198 passed in 627.00s`，两条 ruff 均 `All checks passed!`。
无数据库模式：`88 passed, 110 skipped`（纯单元 88 项可独立跑，集成测试明确 skip 并提示设置 DSN）。

---

## 3. 已完成的阶段性内容

### WB-00 基座审计（结论）
FlowMind 基座不可复用为多进程执行器：`TaskManager` 单技能单进程、`recover_running()` 会在
API 与 worker 各自初始化时互相误杀在途任务、无多进程领取、产物目录受 TTL GC 删除。
详见 `docs/cybercut/WB-00-base-audit.md` 与 `WB-00-capability-matrix.md`。

### WB-01 控制面
- 领域模型：`Project 1:N Run 1:N NodeRun 1:N NodeAttempt`；NodeRun 1:N ArtifactRevision、0:N Approval
- 状态机集中在 `contracts/state_machine.py`（5 张转换表，非法转换抛异常）
- claim 协议：单语句 `WITH picked AS (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE ... RETURNING`
- 租约：`claimed` 超时→恢复 `queued`；`running` 超时→`failed/WORKER_LOST` + 重试策略新建 attempt
- 幂等：`INSERT ... ON CONFLICT DO NOTHING` 原子占位
- 编排器：依赖推进 / 审批（approve·reject + `expected_revision` 冲突）/ 失效连锁 / 取消 / 聚合

### WB-01R / R2 / R3（可靠性收口）
- **独立周期心跳**（与 `progress_cb` 解耦，专用持久连接、`statement_timeout=5s` 有界 beat）；
  心跳窗口覆盖 handler → staged 校验 → sha256 计算；提交前 `stop()` 必须可靠 join
- **原子成功提交**：发布 + 登记产物 + attempt/node_run 终态 + Approval + Event 同一事务
- **租户一致性由数据库强制**：0002 复合外键（`events` 刻意无外键，理由在 ADR 决策 8）
- **统一锁顺序**：`run → node_run → node_attempt → project`（project 最内层）
- **R3 暂存/发布模型**：handler 只能写 `NodeContext.attempt_dir`
  （`.attempts/{attempt_id}/`），`ArtifactSpec.staged_relative_path` 越界即 `ARTIFACT_NOT_OWNED`；
  执行器分配正式不可变路径 `artifacts/{artifact_revision_id}/{safe_filename}`；
  同文件系统 `os.replace` 原子发布；失败只删本次新分配目录；孤儿目录留待 GC
- **心跳不可停止 = 进程级致命**：`HeartbeatStuckError`，`Worker.poisoned`，不再领取任务

设计决策的完整脉络见 `ADR-0002`（决策 1–18；14 已被 17 取代）。

---

## 4. 测试覆盖（178 项，26 文件）

| 文件 | 覆盖 |
|---|---|
| `test_state_machine.py`(54) `test_path_safety.py`(29) | 纯单元，无需数据库 |
| `test_migrations.py`(8) | 幂等/重放/升级路径/其他 schema 同名约束 |
| `test_claim.py` `test_lease.py` `test_restart.py` `test_retry.py` `test_cancel.py` | 并发领取互斥、租约、worker 丢失、重启恢复、取消 |
| `test_artifacts.py` `test_artifact_concurrency.py` `test_artifact_publish.py` `test_artifact_ownership.py` | 版本链、并发分配、发布/回滚/孤儿、暂存边界 |
| `test_approvals.py` `test_invalidation.py` `test_aggregates.py` | 审批、失效、聚合 |
| `test_idempotency.py` `test_idempotency_concurrency.py` | 幂等重放与 8 并发竞争 |
| `test_tenant_isolation.py` `test_tenant_consistency.py` | 应用层与数据库层隔离 |
| `test_heartbeat.py` `test_heartbeat_window.py` `test_heartbeat_stop.py` | 心跳窗口/stop 语义/致命停止 |
| `test_atomic_commit.py` `test_lock_order.py` `test_pipeline_e2e.py` | 原子提交、并发提交-取消无死锁、端到端闭环 |

---

## 5. 硬性约束（验收红线，勿违反）

1. 不修改 `src/flowmind/`；不注册任何 FlowMind `@skill`
2. 不引入 Redis / Celery / Kafka / Milvus / 工作流框架（PostgreSQL 是唯一基础设施）
3. 不修改 `migrations/cybercut/0001_init.sql`（数据库修复必须新增 `0003_*.sql`）
4. 不进入 WB-02 的范围（除非总指挥官明确放行）
5. 不 commit / 不 push（除非总指挥官明确要求）
6. 每阶段交付：代码 + 测试 + 文档更新 + 「缺陷→修改文件→测试用例」对照表 + 原始证据

---

## 6. 本机环境坑（实测，务必遵守）

1. **pytest 的 `tmp_path` 不可用**：WorkBuddy 的 safe-delete 垫片对 `\\?\D:\...` 报
   `OSError: [Errno 53] 找不到网络路径`。测试统一使用自定义 `data_root` 夹具
   （`D:/cybercut-wb01-tmp/<uuid>`，定义在 `tests/cybercut/conftest.py`）。
2. **删除/重命名 `D:` 上的文件会被垫片拦截**（EPERM）。若 PostgreSQL 残留 `postmaster.pid`，
   不要用 bash `rm`/`mv`；直接**新建数据目录 + initdb** 最省事。
3. **`pg_ctl -w start` 会挂起**（postgres 继承 stdout 管道）。启动时要么完整重定向
   （`> log 2>&1`），要么直接以 `postgres` 进程后台运行。
4. **后台任务随会话终止**：postgres 需要在每个会话重新启动（数据目录持久）。
5. **完整测试约 10–14 分钟**，长命令务必 `run_in_background=true`。
6. **两个 pytest 进程不得共用 `cybercut_test`**（验收要求串行）。
7. 心跳测试的租约回收巡检要按真实节奏（0.5s 一次），高频热循环建连会让 PG 建连
   耗时升到秒级，制造伪 `WORKER_LOST`。

---

## 7. 下一步（WB-02 待办，尚未开始）

- 素材上传与管理（素材资产表、上传接口、生命周期）
- ffprobe / FFmpeg 集成、ASR、AI 脚本、分镜
- 渲染管线与产物校验
- SSE 进度推送、MCP 工具暴露（注意：内部节点**不得**进入 FlowMind `_REGISTRY`）
- 产物 GC（清理无 DB 引用的孤儿 revision 目录，按目录 mtime 策略）
- 连接池（当前为短连接；心跳已用专用长连接）

---

## 8. 交接后第一步建议

1. 读 `docs/cybercut/WB-01-control-plane.md`（架构）+ `ADR-0002`（18 条决策）
2. 启动 PostgreSQL（§2.1）
3. 跑一次验收命令（§2.2）确认 178 passed
4. `git status --porcelain` 确认只有 4 个新增未跟踪目录
5. 等待总指挥官下发 WB-02 任务书
