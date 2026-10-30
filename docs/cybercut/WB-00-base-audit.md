# WB-00 · FlowMind 基座审计报告

| 项 | 值 |
|---|---|
| 阶段 | WB-00 基座审计 + **WB-00R 定向整改**（仅改文档，不改源码） |
| 被审计仓库 | `VANexus/rak-flowmind`（包名 `mcp-base-gpu` v0.2.0），工作目录 `C:\Users\黄嘉林\Desktop\智能agent剪辑\cybercut` |
| Commit | `c8ea1c6e025cbc697a87d5626c2a300ef78504ce`（`chore: 清理本地测试视频残留…`） |
| 分支 | `main`（远端另有 `origin/rak-stable`、`origin/HEAD -> origin/main`） |
| 审计日期 | 2026-10-06 |
| 结论等级 | **条件性可用**：框架层（`@skill` / 契约 / MCP 暴露 / 发现面）真实可用；**视频流水线本体在当前 checkout 无法执行**（F-01）；**MCP 暴露为全量无差别，无内外分离机制**；`TaskManager` 为单技能、单进程、重启即中断的执行器，**不作 CyberCut 默认执行器** |
| 方法论 | 逐文件读源码 + 实际运行；README/CLAUDE 的声明一律标注「文档声明」，仅「已核实」项视为事实 |

> 证据约定：下文所有结论均给出「文件:行」或「命令 + 实测输出」。凡未运行验证者，一律标注为「未核实」。
>
> WB-00R 修正项均已内联标注「（本 R 阶段修正项）」，涉及：MCP 暴露结论、执行器结论、领域模型、产物策略、能力矩阵统计。

---

## 一、基座信息（任务二.1）

### 1.1 版本与身份

| 项 | 值 | 证据 |
|---|---|---|
| Commit SHA | `c8ea1c6e025cbc697a87d5626c2a300ef78504ce` | `git rev-parse HEAD` |
| 分支 | `main` | `git branch -a`（`* main` / `remotes/origin/main` / `remotes/origin/rak-stable`） |
| 包名 / 版本 | `mcp-base-gpu` / `0.2.0` | `pyproject.toml:2-3` |
| 许可证 | **MIT License** | `LICENSE:1`（`Copyright (c) 2026 Thecnfor and FlowMind contributors`） |

### 1.2 Python 版本要求（存在口径不一致）

| 来源 | 声明 | 位置 |
|---|---|---|
| pyproject 元数据 | `requires-python = ">=3.11"` | `pyproject.toml:6` |
| conda 依赖真源 | `python=3.12` | `environment.yml:22` |
| ruff target | `target-version = "py311"` | `pyproject.toml:42` |
| 开发文档 | 「**Python 3.12**（conda env `flowmind`）」 | `CLAUDE.md:31` |
| 版本固定文件 | `3.11` | `.python-version` |

**事实**：项目实际注册了一个不存在的 `flowmind` conda 环境（`environment.yml:18`）。本机 `conda env list` 仅有 `base` / `linzi` / `model`，**无 `flowmind`**。Python 版本四处声明不一致（3.11 / 3.12），属配置卫生问题；运行时若严格按 `environment.yml` 会得到 3.12。

### 1.3 主要依赖

依赖真源 = `environment.yml`（`environment.yml:2` 注明「uv.lock 已废弃删除」）；`pyproject.toml:9-22` 仅为 pip 兜底快照。

| 类别 | 依赖 | 声明位置 |
|---|---|---|
| MCP / 服务 | `mcp>=1.27,<2`、`uvicorn>=0.30`、`pydantic>=2`、`python-dotenv>=1.2.2` | `pyproject.toml:10-16` |
| 云 / 网络 | `requests>=2.34.2`、`httpx>=0.27`、`dashscope>=1.27.1`、`numpy>=1.26` | `pyproject.toml:12-15` |
| 任务引擎基础设施 | `psycopg2-binary>=2.9`、`paho-mqtt>=2.0`、`pymilvus>=2.4` | `pyproject.toml:19-21` |
| GPU 栈（仅 environment.yml） | `torch==2.5.1+cu121`、`nvidia-cublas-cu12==12.1.3.1`、`nvidia-cudnn-cu12==9.1.0.70`、`torchaudio==2.5.1+cu121` | `environment.yml:27-46` |
| 本地模型栈（仅 environment.yml） | `faster-whisper>=1.0`、`rapidocr-onnxruntime>=1.3`、`qwen-tts>=0.1.1`、`demucs>=4.0` | `environment.yml:34-47` |
| 非 conda 管辖 | `simple-lama-inpainting>=0.1.2`（`--no-deps` 单独装）；系统级 `sox` / `fonts-noto-cjk` / `ffmpeg` | `environment.yml:36-53` |

**未声明但被 import 的依赖（缺陷）**：`src/flowmind/config.py:12` 有 `import tomli_w`，但 `tomli_w` **不在 `pyproject.toml` 也不在 `environment.yml`**（全仓 `grep -rn "tomli"` 仅命中 config.py 自身）。当前可见性依赖传递依赖偶然带上；若缺失，`import flowmind.config` 直接失败，将导致全仓不可导入。建议上游补声明（WB-00 不做修改）。

### 1.4 测试数量与结果

| 项 | 结果 | 证据 |
|---|---|---|
| 单元测试文件数 | **0** | 无 `tests/` 目录、无 `test_*.py`、无 `conftest.py`（`find` 搜索为空）；`CLAUDE.md:33`「不用 Makefile / Docker / pytest（tests/ 已删除）」 |
| 测试框架 | 无（pytest 未列入依赖） | 同上 |
| 声称的验证方式 | ruff + `examples/*_demo.py` | `README.md:185-190`、`AGENTS.md:200-203` |
| **lint 结果** | ✅ `ruff 0.16.10` → `All checks passed!`（exit 0） | 命令见 §三.2 |
| **demo 结果** | **8 PASS / 2 FAIL**（见 §三.3） | 逐脚本运行 |
| 文档声称 | 「9 个 demo 全 PASS」 | `AGENTS.md:21`、`CLAUDE.md:140,170` |
| 实际 demo 文件数 | **10 个** `examples/*_demo.py` | `ls examples/` |
| 代码内引用的测试 | `errors.py:47` 注释「测试覆盖见 `tests/test_errors.py`」——**该文件不存在** | 文档/注释漂移 |

> 关键：README/CLAUDE/AGENTS 反复强调「9 个 demo 全 PASS 是合并底线」，但 (a) 实际是 10 个文件；(b) 实测 2 个失败。**「全 PASS」的文档声明与事实不符**。

---

## 二、能力审计

### 2.1 @skill 技能框架（任务二.2）

#### 2.1.1 装饰器真实签名

`src/flowmind/skill.py:96-139`：

```python
def skill(*, id: str, name: str, version: str,
          category: str = "通用", tags: list[str] | None = None) -> Callable:
```

- 全部为**关键字参数**（`*` 强制）；被装饰函数**必须**至少一个参数，且**首参注解必须是 `pydantic.BaseModel` 子类**（`skill.py:118-128`）；否则 `TypeError`。
- 重复 `id` 抛 `ValueError`（`skill.py:129-130`）。
- `description` 从函数 docstring 首段首行自动提取（`skill.py:46-60`）。
- `category` / `tags` 由后端显式声明，前端只渲染（`skill.py:113-115`）。
- 注册表：模块级 `_REGISTRY: dict[str, SkillSpec]`（`skill.py:43`）。

#### 2.1.2 输入/输出 schema 如何生成

- 输入：`SkillSpec.input_model = hints[first_param_name]`（`typing.get_type_hints` 解析，`skill.py:123-127`）。
- 输出：从返回注解 `SkillOutput[T]` 提取 `T`（`_extract_output_model`，`skill.py:63-93`）——同时兼容 `typing.get_origin/get_args` 与 pydantic `__pydantic_generic_metadata__['args']` 两条路径。
- 对外 JSON Schema：`skill_entry()` 调 `spec.input_model.model_json_schema()` / `output_model.model_json_schema()`（`discover.py:27-28`），`build_manifest()` 复用同一构造器（`manifest.py:15`）保证 discover 与 manifest 不漂移。

**已核实**：`discover("localize_status")` 返回键 `['id','name','version','description','category','tags','input_schema','output_schema','reliability_profile']`。

#### 2.1.3 SkillResult / trace / metrics / reasoning 真实结构

`src/flowmind/contracts.py`：

| 结构 | 字段 | 位置 |
|---|---|---|
| `SkillResult[T]` | `ok, skill, version, trace, data, reasoning, metrics, error` | `contracts.py:78-87` |
| `TraceContext` | `trace_id, source, target, timestamp` | `contracts.py:52-57` |
| `ReliabilityMetrics` | `latency_ms, confidence, sample_size, degraded, degradation_reason` | `contracts.py:43-49` |
| `ReasoningChain`（四段式） | `conclusion, triggered_rules[], evidence[], causal_analysis, risk_note, confidence` | `contracts.py:33-40` |
| `RuleHit` / `Evidence` | `rule_id,name,expression,hit` / `metric,value,threshold,comparison,window` | `contracts.py:16-30` |
| `SkillError` | `code, message, retriable, details` | `contracts.py:60-65` |
| `SkillOutput[T]`（技能内部产出） | `data, reasoning[], confidence, sample_size, degraded, degradation_reason` | `contracts.py:68-75` |

- `invoke()` 是唯一执行点（`skill.py:159-199`）：`new_trace()` 缺省生成 `trace_id`；入参校验失败 → `VALIDATION`；技能抛异常 → `INTERNAL` + `error.details.traceback`（尾部 4000 字符）；未知 id → `NOT_FOUND`。
- **四段式链是真的**：实测 `reasoning[0]` 键序 = `['conclusion','triggered_rules','evidence','causal_analysis','risk_note','confidence']`。
- `errors.py` 另定义 `FailureCategory`（`environment/video/transient/unknown`）+ `is_retriable()`（仅 `transient` 为真，`errors.py:90-92`）。

#### 2.1.4 MCP 工具如何自动注册（**全量公开语义，关键**）

`src/flowmind/server.py:17-54`：`_make_tool(spec)` 构造闭包 `async def tool(inp)`，通过**覆写 `tool.__annotations__ = {"inp": input_model, "return": SkillResult}`** 让 FastMCP v1 从注解推断 schema，再 `server.add_tool(...)`；技能体经 `anyio.to_thread.run_sync` 调度到工作线程（`server.py:33-35`），避免阻塞事件循环。

**核心事实（本 R 阶段修正项）**：`register_all(mcp)` **无条件遍历整个 `_REGISTRY`**，把**每一个**已注册技能都登记为 MCP 工具：

```python
# src/flowmind/server.py:44-54
def register_all(server: FastMCP) -> None:
    for spec in registry().values():          # ← 全注册表，无过滤、无白名单
        server.add_tool(_make_tool(spec), name=spec.id, description=spec.name)

register_all(mcp)                              # ← 模块导入即执行，无开关
```

即：**「注册为 `@skill`」与「对外暴露为 MCP 工具」在当前基座中是同一件事，不存在内外分离机制**。`server.py:1-4` 的 docstring（「技能只需 `@skill` 定义，无需改动本文件即被暴露」）即为该语义的声明。

**已核实**：`await mcp.list_tools()` 恰好返回 **7 个** `localize_*` 工具（与 `_REGISTRY` 的 7 项一一对应，无多无少）。
**已核实（关键兼容细节）**：MCP 工具 `inputSchema` 的**顶层 `properties` 只有一个键 `inp`**（入参模型被多包一层）——与 `AGENTS.md:133`「入参 schema 多一层 `inp`」一致。消费方必须按 `{"inp": {...}}` 传参。

**推论（对 CyberCut 的直接约束）**：由于暴露是**无差别**的，任何「希望仅内部使用、不对外公开」的能力，**绝不能在首版以基座 `@skill` 形式注册**——否则它会自动出现在 `/mcp` 工具列表与 `/api/v1/manifest` 中。基座**没有**「MCP 工具白名单 / 内外技能分离」机制（详见 §2.3.9）。

#### 2.1.5 是否原生支持 long_running

**不支持（NOT_PRESENT）**。`skill()` 装饰器、`SkillSpec`、`SkillOutput`、`SkillResult` 均无 `long_running` / async / 进度语义；`invoke()` 是**同步**函数（`skill.py:159`）。长任务能力不由技能层提供，而由外挂的 `TaskManager`（PG + 线程池）提供——技能只是被线程池调用的同步函数，进度靠 contextvar 注入的 `progress_cb` 上报（见 §2.4）。

#### 2.1.6 新增 CyberCut skill 需要修改哪些文件

依 `AGENTS.md:73-81` 与实测（`skills/__init__.py:13-21` 的显式 import 列表）：

1. 新增 `src/flowmind/skills/<name>.py`（写 `@skill` 函数，返回 `SkillOutput[T]`）；
2. **必须**在 `src/flowmind/skills/__init__.py` 按字母序追加 `import`（否则不注册——`@skill` 只登记，不含自动发现）；
3. 如需新可调参数：`src/flowmind/config.py` 增 Config 类并纳入 `FlowmindConfig`（`config.py:462-479`）；
4. 建议新增 `examples/<name>_demo.py`；
5. `ruff check src` 全绿。

**注意（对本项目的直接影响）**：`skills/__init__.py` 是**显式白名单**（非 pkgutil 扫描），且其注释明言「禁止幽灵 import 入库」（`skills/__init__.py:5-11`）——该文件是新增技能时的**冲突热点**。

**追加约束（本 R 阶段修正项）**：在该白名单里登记 = 既进入 `_REGISTRY`、又**自动成为对外 MCP 工具**（`server.py:44-54` 无差别暴露，见 §2.1.4）。因此「新增 CyberCut 技能」这一动作**同时意味着对外公开**；若某能力只需内部使用，则**不应**走这条路径（见 §2.3.9 的内外分离决策）。

### 2.2 HTTP 与 MCP（任务二.3）

#### 2.2.1 服务入口

| 入口 | 形式 | 位置 |
|---|---|---|
| `mcp-base-gpu` | 单端口（MCP + REST 同端口同进程） | `pyproject.toml:30` → `flowmind.server_http:main` |
| `flowmind-mcp` | stdio 调试 | `pyproject.toml:31` → `flowmind.server:main` |

`server_http.main()`（`server_http.py:281-309`）顺序：加载 `.env` → 读 host/port → `register_rest_routes(mcp)` → `register_task_routes(mcp)` → `_add_middlewares()`（CORS + `RakAuthMiddleware`）→（可选）联邦自注册 → `mcp.run(transport="streamable-http")`。

#### 2.2.2 实际端口与路由

- 默认 **`127.0.0.1:8002`**（`FLOWMIND_MCP_HOST` / `FLOWMIND_MCP_PORT` 可覆盖，`server_http.py:290-291`）；非 8002 会告警（网关约定后端为 8002）。
- **已核实**（应用路由表 + `TestClient`）：

| 方法 | 路径 | 位置 |
|---|---|---|
| （MCP） | `/mcp` | `server.py:14`；`server_http.py:309` |
| GET | `/api/v1/manifest` | `server_rest.py:28-31` |
| GET | `/api/v1/manifest/{skill_id}` | `server_rest.py:33-45` |
| POST | `/api/v1/tasks` | `server_tasks.py:71-130` |
| GET | `/api/v1/tasks/{task_id}` | `server_tasks.py:132-140` |
| GET | `/api/v1/tasks/{task_id}/download` | `server_tasks.py:142-164` |
| GET | `/api/v1/health` | `server_tasks.py:166-204` |

状态码约定见 `server_tasks.py:13-18`（202/400/422/429/404；健康探针恒 200）。

#### 2.2.3 MCP 传输方式

**Streamable HTTP**（`mcp.run(transport="streamable-http")`，`server_http.py:309`）；另有 stdio 入口 `server.py:57-59`。底层为 FastMCP v1（`mcp>=1.27,<2`）。`_make_tool` 依赖 v1 的 `__annotations__` 反射技巧，**升 v2 会破**（`server.py:20-23` 明确警告）。

#### 2.2.4 manifest / discover 接口

- HTTP：`GET /api/v1/manifest`（全量，含每个技能的 input/output schema）、`GET /api/v1/manifest/{id}`（单个；未知 → 404 `{"error":"unknown_skill","available":[...]}`）。
- Python：`flowmind.build_manifest()`、`discover(id)`、`field_names(id)`（`__init__.py:2-4`），另有更细的 `discover.field_names()` 把 output schema 摊成「字段路径 → 字段名」。
- **已核实**：manifest 技能数 = 7；`/api/v1/manifest` 经 `TestClient` 返回 200（见 §三.4）。

#### 2.2.5 上传、下载、路径沙箱

| 能力 | 结论 | 证据 |
|---|---|---|
| 上传端点 | **不存在** | 路由表无任何 POST upload；`README.md:64` 只提「本地路径必须位于 `data_dir/uploads/` 内」，并没有实现的接收端 |
| `uploads/` 目录 | 仅作为**沙箱白名单根**约定，目录需部署方自建 | `localize_submit.py:128-150` |
| 下载 | 有，`GET .../tasks/{id}/download?file=<basename>`，**basename 白名单**：只在 `output_paths` 中按 `Path(p).name == name` 匹配，`../` 必落空 → 404 | `server_tasks.py:142-164` |
| 输入沙箱 | 提交通道的**本地路径**必须 `resolve()` 后位于 `data_dir/uploads/` 内；URL 不受限；`uploads` 不存在 → 拒绝 | `localize_submit.py:133-150`；收口于 `_split_paths`（MCP `localize_submit` 与 REST `POST /tasks` 共用，`server_tasks.py:88`） |
| 逃生舱 | `FLOWMIND_ALLOW_ANY_PATH=1` 全放行（仅本地测试） | `localize_submit.py:144` |

> 沙箱**不是**默认开启的强隔离——仅在「鉴权未启用」时充当隔离层（`README.md:92`）。且它只覆盖「提交」通道，不覆盖 `invoke()` 直连。

#### 2.2.6 鉴权机制与 tenant 支持

- 中间件 `RakAuthMiddleware`（纯 ASGI，非 BaseHTTPMiddleware，为让 contextvar 跨线程可见，`server_http.py:56-104`）。
- 开关：`FLOWMIND_AUTH_SECRET` / config `auth.secret`（`config.py:341-364`）；**空 = 鉴权整体关闭**（dev）。
- 豁免前缀：默认 `/api/v1/health`、`/api/v1/manifest`（`config.py:357-358`）。
- Token 格式：`<base64url(JSON)>.<hex HMAC-SHA256>`，payload 键序固定 `{"sub","tid","exp"}`，无填充（`auth.py:1-17,113-153`）；**先验签再解 JSON**（`auth.py:118-131`）。
- tenant：`tasks` 表有 `tenant_id` 列（`store.py:39-52`）；`tenant_scope()` 三态 fail-closed（`auth.py:94-110`）；跨租户读返回 `None`（对外 404 语义，不返回 403，防枚举，`manager.py:179-206`）。
- **已核实**：`auth_token_demo.py` 全 PASS（含「自填 `X-Rak-Tenant` 无效」「无凭证 fail-closed」「`/mcp` 无法匿名绕过」）。

### 2.3 异步任务（任务二.4）

#### 2.3.1 Task 数据结构与数据库表

表 `flowmind.tasks`（schema 硬编码，`store.py:33-58`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `task_id` | TEXT PK | `uuid4().hex`（`manager.py:127`） |
| `skill_id` | TEXT NOT NULL | 被执行的技能 id（**单技能**，见 2.3.7） |
| `args_json` | TEXT NOT NULL | 原始入参 JSON |
| `status` | TEXT NOT NULL | 状态机见 2.3.2 |
| `stage` / `progress` | TEXT / REAL | 进度 |
| `error` | TEXT | 终态原因 |
| `created_at` / `started_at` / `finished_at` | TIMESTAMPTZ | |
| `tenant_id` | TEXT | 租户归属（鉴权启用时） |
| `output_paths` | TEXT(JSON) | 产物白名单 |

索引：`idx_tasks_status_created (status, created_at DESC)`（`store.py:54-57`）。建表为幂等 `IF NOT EXISTS`（`store.py:136-152`）。**无任何 Project/Run/NodeRun/DAG/父子/依赖列**。

#### 2.3.2 状态机及合法迁移

`src/flowmind/tasks/__init__.py:39-48`：

```
状态：queued, running, succeeded, failed, cancelled, interrupted
TERMINAL     = {succeeded, failed, cancelled, interrupted}
NON_TERMINAL = {queued, running}
```

合法迁移（由代码强制）：

| 迁移 | 触发 | 守卫 |
|---|---|---|
| `queued → running` | worker 取任务 | CAS `from_statuses=('queued',)`（`manager.py:231-233`） |
| `queued/running → succeeded` | 技能 ok 且无 failure_category | CAS `from_statuses=NON_TERMINAL`（`manager.py:278-283`） |
| `queued/running → failed` | 技能 ok 但 failure_category / 技能 ok=False / worker 崩溃 | 同上（`manager.py:288-299`） |
| `queued/running → cancelled` | `cancel()` 或阶段边界 `CancelledError` | 同上（`manager.py:170-171,284-287`） |
| `queued/running → interrupted` | **进程启动**时清理 | `recover_running()`（`store.py:239-263`） |

#### 2.3.3 worker 并发模型

`ThreadPoolExecutor(max_workers=self.workers)`，`workers=1`（**单 GPU 串行，禁止放宽**，`manager.py:91-93`）。第一道闸=任务间串行；第二道闸=进程级 `gpu_lane()` Semaphore(1)（`tasks/gpu.py:22,28-32`）；模型懒加载另有 `model_cache_guard()` 双检锁（`gpu.py:36-46`）。`get_task_manager()` 为进程级惰性单例（`manager.py:398-408`）。

#### 2.3.4 重启时 queued/running 如何处理

`TaskManager.__init__` 调 `store.recover_running()`（`manager.py:103`），把遗留 `queued` **和** `running` 一律置 `interrupted`（`store.py:246-254`，`include_queued=True` 默认）。**不重放、不续跑**——遗留任务需人工/上层用 `localize_retry` 重提。

#### 2.3.5 取消 / 重试 / TTL / 背压 / CAS 是否真实存在

| 能力 | 结论 | 证据 |
|---|---|---|
| 取消 | ✅ 真实（**协作式**）：`cancel()` 置 `threading.Event` + CAS 落 `cancelled`；流水线在**阶段边界**互查 `cancel_check` 抛 `CancelledError`（`manager.py:150-177`；`localize_video.py:241-248`）。ffmpeg/demucs **子进程不强杀**（`manager.py:13-14`） |
| 重试 | ✅ 真实：`localize_retry` 读原行 `args_json` 复制后重新 `submit`；仅 `failed/cancelled/interrupted` 可重提（`localize_retry.py:29,70-89`） |
| TTL | ✅ 真实：daemon GC 线程按 `task_ttl_seconds`（默认 3600）删 `tasks/<id>/` 与 `outputs/<id>/`，**DB 行保留**（`manager.py:363-393`）；另有 7 天孤儿目录清扫 |
| 背压 | ✅ 真实（**单进程内**）：`max_pending_tasks`（默认 100）→ `TaskQueueFull`；REST 映射 429，MCP 映射 `INTERNAL`+message（`manager.py:128-133`、`server_tasks.py:101-120`） |
| CAS | ✅ 真实：`set_status(..., from_statuses=...)` → `UPDATE … WHERE task_id=%s AND status = ANY(%s)`，`rowcount==1` 判定，first-writer-wins（`store.py:192-237`） |

> 背压的 `count_pending()` + `create_task()` 用 `_submit_lock` 保证**进程内**原子（`manager.py:128-133`）；**多实例并发会失效**（无分布式锁，PgBouncer 事务模式明确禁用 advisory lock，`store.py:3-8`）。

#### 2.3.6 是否适合作为 Project 级工作流（结论：**不适用**）

**不适合，且 `TaskManager` 亦不得作为 CyberCut 默认 NodeRun 执行器（本 R 阶段修正项）。** 理由：

1. 粒度只有一层：一个 task = `{skill_id, args_json}`，即**单个技能**的一次调用（`store.py:156-173`）。没有节点图、没有步骤 DAG、没有步骤间依赖、没有父子/编排列。
2. 无「项目」概念：无 Project/Run 聚合、无批次语义（批量提交只是循环 `submit` N 个平级 task，`localize_submit.py:269-281`）。
3. 无跨任务状态交接：中间产物不能在任务间传递（`args_json` 在提交时冻结）。
4. 全局串行：`workers=1` 由 GPU 显存预算决定（`manager.py:91`），非 GPU 任务也会被串行化，不适合 Project 级并行编排。
5. **重启即中断，且是进程级副作用**：`TaskManager.__init__` 无条件执行 `recover_running()`（`manager.py:103`），把**当前库中所有** `queued`/`running` 行标为 `interrupted`——该动作**不区分是哪个进程创建的行**（见下「两进程风险」）。

##### 「两进程风险」（本 R 阶段新增，关键）

`recover_running()` 是 **`TaskManager` 构造函数的无条件副作用**（`manager.py:102-107` → `store.recover_running()` → `store.py:239-263`），其 SQL 为：

```sql
-- src/flowmind/tasks/store.py:250-254（对全表、全状态匹配的行生效）
UPDATE flowmind.tasks SET status = 'interrupted', finished_at = now()
WHERE status = ANY(%s)          -- %s = ['queued','running']（include_queued 默认 True）
```

**结论：任何进程只要初始化一次 `TaskManager`（含通过 `get_task_manager()` 惰性单例），就会把该 PG 库里所有处于 `queued`/`running` 的基座任务打成 `interrupted`。**

因此，**绝不允许 CyberCut 的 API 进程与 worker 进程各自初始化同一个 FlowMind `TaskManager`**：

- 若 API 进程先起、worker 后起 → worker 的 `recover_running()` 会把 API 刚提交、尚在 `queued` 的任务**误判为中断**，任务静默丢失（且 `interrupted` 是终态，不会被重放）。
- 若两者交替重启 → 每次都为对方的在途任务制造一次伪中断。
- 该行为是**进程级全局副作用**，无法通过参数关闭（`include_queued` 虽可传 `False`，但 `running` 仍会被清；且 `manager.py:103` 调用点未暴露该开关）。

这构成「不要让基座 `TaskManager` 承担 CyberCut 主执行职责」的**决定性工程理由**（详见 ADR-0001 §1/§3 与能力矩阵 T-18）。

#### 2.3.7 TaskManager 在 CyberCut 中的复用定位（本 R 阶段修正项）

**修正后的定位：`TaskManager` 不是 CyberCut 的默认执行器；它只用于「兼容既有 `localize_*` 任务」，或作为**可选适配器**（`executor_kind=flowmind_task`）挂接单个节点。**

| 用途 | 是否采用 | 说明 |
|---|---|---|
| CyberCut 项目级节点执行（主路径） | ❌ **不采用** | 由 CyberCut 自建「基于 PostgreSQL 的 NodeAttempt 队列 + worker claim 机制」（见 §2.3.8 与 ADR-0001 §1/§2） |
| 兼容既有 `localize_*` 任务 | ✅ 采用 | 复用基座 7 个 `localize_*` 技能与任务 REST 通道，保持向后兼容（如外部仍按 `POST /api/v1/tasks` 提交本地化任务） |
| 可选适配器（`executor_kind=flowmind_task`） | ⚠️ 可选 | 仅当某节点**确实**要执行一个基座技能、且**不存在多进程并发初始化**风险时，可将该 NodeAttempt 的 `executor_ref` 指向基座 `task_id` |

**基座中可借鉴、但需经 CyberCut 侧重新实现/适配的机制**（不作为直接复用）：

- `TaskStore` 的 **CAS 状态守卫**思想（`store.py:192-237`）——CyberCut 的 NodeAttempt claim 应实现同类「单条 UPDATE + `from_statuses` 守卫」的原子领取；
- **协作式取消**（阶段边界轮询 `cancel_check`，`manager.py:150-177`）——CyberCut 自建执行器应提供等价取消契约；
- **终态分类**（`_classify_and_finish`，`manager.py:274-299`）——CyberCut 需自行定义成功/失败/降级语义；
- `TaskEventPublisher` 的 MQTT 进度/终态 retain 约定（`events.py:143-160`）——可作为 CyberCut 事件面的参考。

**可原样复用（与执行器无关）**：`tasks/gpu.py` 双道闸（`gpu_lane()` / `model_cache_guard()`）、`tasks/vectors.py` 与 `_bge_embed.py` 客户端、`auth.py` 的租户上下文。

**结论**：基座的「执行器」是**单技能、单进程、重启即中断**的执行事实来源；它可作为**兼容层/可选适配器**，但**不得**作为 CyberCut 项目级工作流或默认节点执行器。

#### 2.3.8 产物路径与 Artifact 策略（本 R 阶段修正项，关键）

**基座事实：`TaskManager` 只自动识别「单一产物」`data.output_path`。**

```python
# src/flowmind/tasks/manager.py:278-283
if result.ok and not failure_category:
    out = getattr(data, "output_path", None) if data is not None else None
    output_paths = [out] if isinstance(out, str) and out else None   # ← 仅取一个 output_path
    self._finish(task_id, STATUS_SUCCEEDED, output_paths=output_paths, ...)
```

即：任务成功时，`output_paths` 列**至多登记一个**路径（来自 `SkillResult.data.output_path`）。基座**没有**多产物登记能力（如 `outputs[]`、命名产物、版本化产物）。

**产物落位与生命周期事实**：

- `localize_video` 的产物路径推导：显式 `output_path` > 本地输入源文件旁 `<stem>_localized.mp4` > URL 输入落 `data_dir/outputs/<task_id>/output.mp4`（`localize_video.py:538-558`）。
- 该产物目录受**基座 TTL GC 管辖**：终态任务超 `task_ttl_seconds`（默认 3600s）后，`data_dir/tasks/<id>/` 与 `data_dir/outputs/<id>/` 被 `shutil.rmtree` 删除（**DB 行保留**），`manager.py:363-393`。
- `localize_download` 只列 `output_paths` 中已登记的条目（`localize_download.py:80-88`）。

**CyberCut 修正后的产物策略**：

1. **多产物必须由 CyberCut 独立登记**：引入 `ArtifactRevision`（见 ADR-0001 §2）承载 CyberCut 节点产出的全部正式产物（成片、字幕、音频、分镜 JSON、QC 报告……）；**不依赖**基座 `output_paths`。
2. **正式产物必须复制/移动到 CyberCut 自有目录**：
   `data/projects/{project_id}/artifacts/{artifact_id}/`
   该目录**不受基座 TTL GC 影响**（基座 GC 只扫 `data_dir/tasks/`、`data_dir/outputs/`，`manager.py:380-383`）。
3. **不允许项目产物依赖 FlowMind task 的 TTL 目录存活**——否则任务完成 1 小时后产物即被删除，项目交付物失效。
4. **基座 `output_paths` 仅视为「临时执行产物索引」**：用于任务完成后的**短期搬运来源**（CyberCut worker 在基座 task 成功后**立即**把 `data.output_path` 指向的文件复制进 `data/projects/.../artifacts/`），而非长期存储。

> 该策略意味着：`executor_kind=flowmind_task` 的适配器必须在任务终态与 TTL 之间完成产物搬运，形成「临时产物 → 正式产物」的显式迁移步骤。

#### 2.3.9 MCP 公开白名单 / 内外技能分离（本 R 阶段修正项）

**结论：基座 `NOT_PRESENT`（不存在该机制）。**

- `register_all()` 无差别暴露全注册表（§2.1.4），**无白名单、无 `public: bool` 标记、无 include/exclude 过滤**（`server.py:44-54`）。
- 因此「只公开一部分技能、隐藏内部技能」在基座内**无法表达**；`SkillSpec`（`skill.py:29-40`）也无任何可见性字段。

**CyberCut 隔离方案（不修改基座 `server.py`）**：

- **只有明确要公开的项目级 MCP 能力**才使用 FlowMind `@skill`（并登记进 `skills/__init__.py`）；
- **素材分析、ASR、脚本、分镜、素材匹配、Timeline、渲染、QC** 等一律采用 **CyberCut 内部 `NodeHandler`**（CyberCut 自有代码，见 ADR-0001 §4），**不进 FlowMind `_REGISTRY`**，因而**天然不会被 `register_all()` 暴露**；
- 首版**不修改** FlowMind `server.py`、不新增可见性机制——以「**不注册**」实现隔离。

### 2.4 基础设施（任务二.5）

| 组件 | 是否硬依赖 | 实情 | 证据 |
|---|---|---|---|
| **PostgreSQL** | ✅ **硬依赖** | `TaskStore` 构造即需连接与建表；未配置 → `TaskStoreError`（显式失败）。health `pg=error → status=degraded` | `store.py:66-103`；`server_tasks.py:190-199` |
| MQTT | ❌ 可选 | 未配置 → 发布器永久禁用（纯 PG 落库），零开销；health `mqtt=disabled` | `events.py:39-71,91-94,100-104` |
| GPU lane | 条件必需 | `gpu_lane()` = `threading.Semaphore(1)`；仅本地 ASR / LaMa / 本地 TTS 占槽；云阶段不占 | `gpu.py:22-46`；`localize_video.py:357-359,414-417,471-476` |
| Milvus | ❌ 可选 | **服务不在仓库**；仅客户端 `tasks/vectors.py` 在仓库。URI 空 = 显式禁用（`search/upsert` 抛 `VectorStoreError`） | `vectors.py:45-89` |
| BGE 嵌入 | ❌ 可选 | **服务不在仓库**；仅 HTTP 客户端 `skills/_bge_embed.py`（TEI /embed 与 OpenAI /v1/embeddings 双形状自适应）。空 = 显式禁用 | `_bge_embed.py:53-129` |
| Go 网关 | — | **不在本仓库**：无 `go-kernel/` 目录（`ls` 不存在）。README/CLAUDE 称其为「独立仓库，.gitignore 已忽略」（`README.md:145-147`、`CLAUDE.md:194`）。仓库内只有**自注册客户端** `src/flowmind/federation/` | `ls go-kernel` → No such file |

#### MQTT 事件格式、QoS、retain 及降级

- 任务事件主题：`mcp-base-gpu/tasks/{task_id}/events`；payload `{"task_id","stage","pct","message","status","ts"}`；**QoS=1**；**终态 retain=True**（`events.py:36,143-160`）。
- 联邦主题：`mcp-base-gpu/federation/{register|heartbeat|unregister}`，**QoS 1 / 0 / 1**（`federation/mqtt.py:33-42`）；payload 见 `register.py:77-88`。
- 降级：发布失败只记日志、绝不抛、绝不阻塞任务（`events.py:10-11,165-171`）；`connect_async + loop_start` 非阻塞、首次最多等 2s（`events.py:106-133`）。**已核实**：`federation_register_demo.py` 全 PASS，输出含真实 register payload / heartbeat / unregister。

#### Windows 环境是否能完整运行

**不能完整运行。** 分层结论：

| 层 | Windows 可运行性 | 依据 |
|---|---|---|
| 框架层（@skill / invoke / discover / registry / MCP 工具注册） | ✅ 可运行 | §三 实测通过 |
| 任务 REST / 鉴权 | ⚠️ 代码可运行；本机 ad-hoc 环境下 uvicorn 传输栈异常（**环境产物**，见 §三.4） | 对照实验 |
| 视频流水线本体 | ❌ 当前 checkout 直接报错（代码缺陷 F-01，与 OS 无关） | §三.3 |
| GPU 栈安装 | ❌ `environment.yml` 的 `nvidia-cublas-cu12==12.1.3.1` / `nvidia-cudnn-cu12==9.1.0.70` 是 **Linux-only wheel**，Windows 上该 pip 段无法满足；且硬件为 P104-100（Pascal） | `environment.yml:31-32` |
| 系统依赖 | ❌ 本机 **无 `ffmpeg` / `ffprobe`**（`which` 为空）；`fc-match`/`sox` 亦未确认 | §三.1 |
| pymilvus 导入 | ❌ 本机 `import pymilvus` 崩溃（`pymilvus` 在 import 时无条件 `load_dotenv()`，经 cwd 上溯读到用户主目录 `C:/Users/黄嘉林/.env`（UTF-16 BOM）→ `UnicodeDecodeError`）。**环境产物**，非仓库代码缺陷 | §三.5 |

### 2.5 现有视频能力（任务二.6）

#### 2.5.1 localize_* 技能输入输出

| 技能 | 入参模型 | 出参模型 | 备注 |
|---|---|---|---|
| `localize_submit` | `SubmitInput`（`videos[]`, target/source_lang, voice_id, keep_background_audio, tts_backend, voice_ref_*, erase_backend） | `SubmitReport`（task_ids, accepted, rejected_*, failure_category, retriable, warning） | 批量提交，逐视频一 task（`localize_submit.py:51-123`） |
| `localize_status` | `StatusInput`（task_ids[], stall_threshold_seconds） | `StatusReport`（tasks[], 各状态计数, all_terminal…） | 读 PG，并发轮询（`localize_status.py:35-77`） |
| `localize_retry` | `RetryInput`（task_id） | `RetryReport` | 复制原 args 重提（`localize_retry.py:34-51`） |
| `localize_cancel` | `CancelInput`（task_id） | `CancelReport` | 协作式取消（`localize_cancel.py:27-42`） |
| `localize_download` | `DownloadInput`（task_id） | `DownloadReport`（files[]: filename/local_path/url） | 只列产物，不塞二进制（`localize_download.py:35-57`） |
| `localize_search` | `SearchInput`（query, top_k, task_id?） | `SearchReport`（hits[]: text/时间轴/score） | BGE+Milvus 语义检索（`localize_search.py:31-59`） |
| `localize_video` | `LocalizeVideoInput`（video_path, target/source_lang, voice_id, output_path, keep_background_audio, tts_backend, voice_ref_*, erase_backend） | `LocalizeVideoReport`（output_path, duration_seconds, asr_segment_count, subtitle_region_erased, voice_used, asr_backend, ocr_backend, erase_backend, vectorized, failure_category…） | **流水线本体**（`localize_video.py:61-123`） |

#### 2.5.2 ASR / TTS / 字幕 / FFmpeg 分别由谁实现

| 能力 | 本地实现（仓库内） | 云实现（仓库内客户端） | 归属 |
|---|---|---|---|
| ASR | `_local_asr.transcribe_local`（faster-whisper / CTranslate2，`small` int8，CUDA） | `_cloud_asr.transcribe_local`（dashscope 流式，模型 `qwen-audio-3.0-asr-flash-streaming`） | **仓库内代码**，模型与云服务外部 |
| TTS（配音） | `_local_tts`（Qwen3-TTS-12Hz-0.6B-Base 零样本克隆，fp32+sdpa） | `_cloud_tts`（dashscope `SpeechSynthesizer`，预设音色） | **仓库内代码** |
| 字幕定位（OCR） | `_local_ocr`（RapidOCR / onnxruntime，CPU） | `_cloud_ocr`（`qwen3.5-ocr`） | **仓库内代码** |
| 字幕擦除 | `_inpaint.erase_regions`（LaMa 逐帧；掩码含白字笔画精修） | ffmpeg `delogo` 滤镜（`_media.burn_subtitles`） | **仓库内代码** |
| 字幕重绘 | `localize_video._write_ass`（生成 ASS，字号/位置匹配原字幕）+ ffmpeg `ass` 滤镜烧录 | — | **仓库内代码** |
| FFmpeg | `_media.py` 薄封装（`extract_audio/probe_media/extract_frame/burn_subtitles/mix_audio`）+ `localize_video` 内多处直接 `ffmpeg` 调用（`_replace_audio/_build_timed_audio/_strip_audio/_concat_wavs`）+ `_inpaint` 抽帧重编码；字体探测走 `fc-match` | — | **二进制为系统依赖（未随仓库分发）** |
| 翻译 | — | `_llm_translate`（Anthropic/OpenAI 双协议，默认 LongCat） | **仓库内代码** |
| 人声分离 | `_music_sep`（demucs htdemucs 子进程） | — | **仓库内代码** |

#### 2.5.3 哪些是本仓库能力，哪些依赖外部 video-localizer 服务

**关键结论：不存在外部 `video-localizer` 服务依赖。** 7 个 `localize_*` 工具即「本仓库的 SaaS 服务端」，其能力由「仓库内 Python 代码 + 本地模型 + 云端 API（dashscope / LongCat）」构成。所谓「本地化」全部在本仓库内实现，无远程 `video-localizer` 调用。

外部依赖仅限：**模型权重**（HF 缓存/首次下载）、**云 API**（dashscope、LongCat）、**基础设施服务**（PG / MQTT / Milvus / BGE HTTP）。

#### 2.5.4 degraded 成功语义

`localize_*` 统一「degraded SkillOutput 模式」（`CLAUDE.md:147-161`；实测一致）：

- 失败**不 raise**，而是 `ok=True` + `metrics.degraded=True` + `data.failure_category ∈ {environment,video,transient,unknown}` + `data.retriable`（仅 transient 为真）。
- 例外：`localize_submit` 队列全满 → `raise TaskQueueFull` → `invoke` 兜底 `ok=False / INTERNAL`（即 429 语义）。
- `TaskManager._classify_and_finish` 把信封映射为终态（`manager.py:274-299`，**已核实**）：
  - `ok=True` 且 `failure_category` 为空 → **succeeded**（含 `empty_asr` 无人声空结果、含 degraded 空产物）；
  - `ok=True` 且 `failure_category` 非空 → **failed**（`error` 记 warning）；
  - `ok=False` → **failed**。

#### 2.5.5 能否复用于 CyberCut 及复用边界

**可复用（作为原子媒体能力）**：`_media`、`_local_asr`、`_cloud_asr`、`_local_tts`、`_cloud_tts`、`_local_ocr`、`_cloud_ocr`、`_inpaint`、`_llm_translate`、`_bge_embed`、`_music_sep`、`tasks/gpu.py` 双道闸。

**复用形态（本 R 阶段修正项，关键）**：上述 helper **仅通过 CyberCut 自有的 `NodeHandler` / `adapters/` 调用**，**不**以基座 `@skill` 形式注册进 `_REGISTRY`（否则会被 `register_all()` 无差别暴露为 MCP 工具，见 §2.1.4/§2.3.9）。同时，`localize_video` 的 F-01 缺陷**不阻塞** CyberCut 首版——首版不依赖 `localize_video` 流水线（只按需调用 `_` helper）。

**边界（须适配，勿直接依赖）**：

1. 全部为**私有模块**（`_` 前缀），无公开 API 承诺；
2. 强耦合 `LocalizerConfig`（且如 F-01 所示，其字段集当前不完整）；
3. 依赖 ffmpeg/字幕字体/GPU/torch 等外部条件；
4. `localize_video` 的**编排**是「单语言本地化」专用流程，**不可**当作 CyberCut 分镜/时间线节点直接复用；
5. 无语义分段模型（`_split_long_segments` 仅按标点 20 字硬切，`localize_video.py:642-674`）。

### 2.6 许可证审计（任务二.7）

#### 2.6.1 基座许可证

**MIT**（`LICENSE:1-21`）。宽松，可商用、可闭源衍生。

#### 2.6.2 核心依赖许可证（已实测本机安装元数据）

| 依赖 | 版本 | License | 类别 |
|---|---|---|---|
| `mcp` | 1.30.0 | MIT | 宽松 |
| `requests` | 2.34.2 | Apache-2.0 | 宽松 |
| `httpx` | 0.28.1 | BSD-3-Clause | 宽松 |
| `python-dotenv` | 1.2.4 | BSD-3-Clause | 宽松 |
| `dashscope` | 1.27.7 | Apache-2.0 | 宽松 |
| `pymilvus` | 3.0.2 | Apache-2.0（classifier） | 宽松 |
| `tomli-w` | 1.2.0 | MIT | 宽松 |
| `pydantic` / `starlette` / `uvicorn` / `anyio` / `numpy` / `ruff` | — | 元数据无 License 字段；上游分别为 MIT / BSD-3 / BSD-3 / MIT / BSD-3 / MIT | 宽松（**未在本机元数据核实**） |
| **`psycopg2-binary`** | 2.9.13 | **LGPL with exceptions（LGPL-3.0）** | ⚠️ **弱 copyleft** |
| **`paho-mqtt`** | 2.1.0 | **EPL-2.0 OR BSD-3-Clause**（双许可） | ⚠️ 需**主动选择 BSD-3** 方可视为宽松 |

#### 2.6.3 GPL / AGPL / 特殊商业许可 / 未知许可风险

| 风险项 | 判定 | 说明 |
|---|---|---|
| **GPL / AGPL** | **未发现** | 已声明的直接依赖中无 GPL/AGPL（best-effort，未递归审计传递依赖） |
| **LGPL（弱 copyleft）** | `psycopg2-binary` | 仅动态 `import psycopg2`，通常可满足 LGPL；但该 wheel **捆绑 libpq / OpenSSL**，分发时须履行 LGPL 通知与可替换义务。**须法务确认** |
| **EPL（弱 copyleft）** | `paho-mqtt` | 双许可，商用应显式选 **BSD-3-Clause** 分支，避免落入 EPL-2.0 义务 |
| **特殊/未知（关键，须人工核实）** | `simple-lama-inpainting` + `big-lama` 权重 | 包与**模型权重**授权未经本机验证；LaMa 上游（saic-mdal / advimman）为 Apache-2.0，但权重条款常与代码分离。**列为未知风险** |
| **模型权重授权（独立于代码）** | Qwen3-TTS、faster-whisper 所用模型、htdemucs、BGE | 代码许可 ≠ 权重许可。商用前须逐模型核实（Qwen 系列多 Apache-2.0；HTDemucs 为 MIT） |
| **Go 网关 `go-kernel`** | 未知 | 独立仓库、不在本 checkout，**无法在本阶段核实其许可证**（若 CyberCut 依赖它，须单独审计） |
| **未声明的依赖** | `tomli_w` | 未在依赖清单声明（见 §1.3），其 MIT 许可无风险，但**依赖声明缺失本身是供应链卫生问题** |

---

## 三、运行验证（任务三）

### 环境现状（先决事实）

| 项 | 实测 |
|---|---|
| `conda` | 26.1.1（`/d/c/miniconda`）；**无 `flowmind` 环境**（仅 base/linzi/model） |
| 活动 Python | 3.13.14（系统）；`.python-version` 声明 3.11；`environment.yml` 要求 3.12 |
| `ffmpeg` / `ffprobe` | **未安装**（`which` 为空）——记为环境问题，未安装任何系统软件 |
| 网络 | pip 全局 index 指向清华镜像**不可达**；官方 `pypi.org` 经 `http_proxy=127.0.0.1:56123` 可达（HTTP 200） |
| 隔离环境 | 为不污染用户环境，用受管 Python 3.13.12 建独立 venv：`~/.workbuddy/binaries/python/envs/cybercut-wb00` |

### 3.1 仓库规定的安装命令（原样执行 + 结果）

```bash
# 仓库规定（AGENTS.md:15-17 / README.md:19-25）
conda env update -n flowmind -f environment.yml
conda run -n flowmind pip install --no-deps "simple-lama-inpainting>=0.1.2"
conda run -n flowmind pip install -e . --no-deps
```

- `conda env update -n flowmind -f environment.yml`：**未完整执行**。改为 `--dry-run` 干跑，结果：conda 段可解（解析出 `python 3.12.15` 等 21 个包），**但 conda 的 dry-run 不解析 `pip:` 段**，无法验证 GPU 依赖组合。
- **判定**：该命令在 **Windows 上无法满足 pip 段**——`environment.yml:31-32` 的 `nvidia-cublas-cu12==12.1.3.1` / `nvidia-cudnn-cu12==9.1.0.70` 是 **Linux-only 发行**。属**平台/系统依赖**限制，非代码缺陷。
- 为获得可运行的验证结果，改用隔离 venv 安装「`pyproject.toml` 声明的 pip 兜底依赖 + `tomli-w`」，命令：

```bash
python -m pip install --index-url https://pypi.org/simple \
  "mcp>=1.27,<2" "pydantic>=2" numpy requests httpx python-dotenv dashscope \
  uvicorn psycopg2-binary paho-mqtt pymilvus tomli-w ruff
# → EXIT 0；实测版本：mcp 1.30.0 / pydantic 2.13.5 / starlette 1.7.0 / uvicorn 0.54.0 / ruff 0.16.10
```

> GPU 栈（torch / faster-whisper / qwen-tts / simple-lama / demucs）**未安装**：demo 全部 mock 之，验证不需要；且其安装受 Windows 平台限制。此偏差已明确记录。

### 3.2 lint

```bash
PYTHONPATH=src <venv>/python -m ruff check src
# 输出：All checks passed!        （exit 0，ruff 0.16.10）
```

✅ **通过**。

### 3.3 demo（`examples/*_demo.py`，共 10 个）

```bash
for f in examples/*_demo.py; do PYTHONPATH=$PWD/src <venv>/python "$f"; done
```

| # | demo | 结果 | 备注 |
|---|---|---|---|
| 1 | `localize_video_demo` | ❌ **FAIL (exit 1)** | **代码缺陷 F-01**（见下） |
| 2 | `localize_submit_demo` | ✅ PASS | 队列背压 429/partial 语义正确 |
| 3 | `localize_status_demo` | ✅ PASS | stalled 判定、not_found、store 失败 INTERNAL 正确 |
| 4 | `localize_download_demo` | ✅ PASS | degraded/404 分类正确 |
| 5 | `localize_retry_demo` | ✅ PASS | 重提准入与拒绝正确 |
| 6 | `localize_cancel_demo` | ✅ PASS | 幂等取消正确 |
| 7 | `localize_search_demo` | ✅ PASS | 服务不可用 → ok=False 正确 |
| 8 | `auth_token_demo` | ✅ PASS | 鉴权 9 项断言全 PASS |
| 9 | `federation_register_demo` | ✅ PASS | 双通道注册/心跳/注销契约正确 |
| 10 | `api_server_demo` | ❌ FAIL (exit 1) | **环境产物**（见 §3.4） |

**合计：8 PASS / 2 FAIL**（与文档「9 个全 PASS」不符）。

#### ❌ F-01：`localize_video` 流水线本体不可执行（**代码缺陷**）

- 现象：`localize_video_demo` happy path 抛 `AttributeError: 'NoneType' object has no attribute 'output_path'`（因信封 `ok=False`、`data=None`）。
- 直接复现（关键堆栈）：

```
error.code = INTERNAL
error.msg  = 'LocalizerConfig' object has no attribute 'asr_backend'
  File "src/flowmind/skills/localize_video.py", line 568, in _select_asr
    chosen = (cfg.asr_backend or "auto").lower()
AttributeError: 'LocalizerConfig' object has no attribute 'asr_backend'
```

- 根因：`LocalizerConfig`（`config.py:88-141`）**缺失 7 个被代码引用的字段**。实测（`load_config().localizer`）逐字段验证：

| 字段 | 访问方式 | 结果 |
|---|---|---|
| `asr_backend` | **直接属性**（`localize_video.py:568`） | ❌ AttributeError |
| `ocr_backend` | **直接属性**（`localize_video.py:598`） | ❌ AttributeError |
| `tts_backend` | `getattr(..., "auto")` | 有兜底 |
| `erase_backend` | `getattr(..., "auto")` | 有兜底 |
| `bgm_vocal_sep` | `getattr(..., True)` | 有兜底 |
| `local_asr_model` | **直接属性**（`localize_video.py:572`） | ❌ AttributeError |
| `local_asr_device` | **直接属性**（`localize_video.py:572`） | ❌ AttributeError |

- 影响：**`localize_video`（唯一执行真实视频处理的技能）100% 无法运行**；进而 `localize_submit` 提交的任务在 worker 侧必然 `INTERNAL` 失败。换言之，**基座的「视频能力」当前是死的**。
- 归因与「文档漂移」：`CLAUDE.md:41-48` 以表格**明确宣称** `asr_backend / ocr_backend / erase_backend / tts_backend / bgm_vocal_sep` 是 `flowmind.config.toml [localizer]` 的受支持开关——代码与文档均假设字段存在，但 `LocalizerConfig` 未定义。结合 git 历史（`d1497c8`、`c675ac4` 均为「config 重构丢失字段」修复），本项**高度疑似为又一次 config 重构回归**。
- **WB-00 不修改业务代码**：仅记录，未修复（修复属 WB-01+ 决策）。

#### ❌ `api_server_demo` 失败（**环境产物，非代码缺陷**）

- 现象：`GET /api/v1/manifest` **首次 200、之后全部 404**（空体）。
- 已排除代码缺陷，证据链：
  1. 应用路由表完整：`['/mcp','/api/v1/manifest','/api/v1/manifest/{skill_id}','/api/v1/tasks','/api/v1/tasks/{task_id}','/api/v1/tasks/{task_id}/download','/api/v1/health']`；
  2. `starlette.testclient.TestClient` 下多次请求**全部正常**（manifest 200 返回 7 技能）；
  3. **对照实验**：用一个**与 FlowMind 完全无关的普通 Starlette 应用**，在同一 uvicorn 子线程下表现**完全相同**（首请求 200、之后 404）→ 责任在本机 ad-hoc 传输栈（uvicorn 0.54.0 + starlette/httpx 组合 + Windows）。
  4. 降级 `starlette==0.47.2` 后现象**不变** → 非 starlette 1.x 单一因素。
- **判定**：`api_server_demo` 的端到端 REST 验证在本机**不可得（INCONCLUSIVE）**；路由注册本身已用「路由表 + TestClient」证实正确。记为**环境/依赖版本问题**，建议 W B-01 固定 uvicorn/starlette 版本后复验。

### 3.4 demo 之外的定向验证（全部通过）

| 验证项 | 命令要点 | 结果 |
|---|---|---|
| registry 登记 | `registry()` | 7 技能：cancel/download/retry/search/status/submit/video |
| discover / manifest | `discover("localize_status")` / `build_manifest()` | entry 键含 input/output schema；manifest 技能数=7 |
| **MCP tools/list** | `await mcp.list_tools()` | **恰好 7 个工具**；`inputSchema` 顶层 `properties == ['inp']`（多一层包裹，已核实） |
| 失败返回 + trace_id | `invoke("no_such_skill", {}, new_trace(trace_id="WB00-TRACE-123"))` | `ok=False / NOT_FOUND`；`trace.trace_id=WB00-TRACE-123`（**透传生效**） |
| 入参校验 | `invoke("localize_status", {"task_ids": []})` | `ok=False / VALIDATION`，`details.errors[0].type=too_short` |
| **纯计算/无外部依赖技能** | 注入 fake manager 后 `invoke("localize_status", …)` | `ok=True`，`trace=WB00-TRACE-456`，`latency_ms≈0.337`，四段式链完整 |
| REST 发现面 | `TestClient(app).get("/api/v1/manifest[,…]")` | manifest 200 / 单技能 200 / 未知 id 404 `unknown_skill` |

> 说明：注册表中**没有**「纯计算技能」——7 个技能全部需要 PG 或外部服务。上表用「注入 fake manager 的 `localize_status`」作为「不依赖外部服务的技能」验证样本（与 `localize_status_demo` 同法）。

### 3.5 环境问题清单（非仓库代码缺陷）

| 编号 | 问题 | 证据 | 分类 |
|---|---|---|---|
| E-01 | 无 `flowmind` conda 环境 | `conda env list` | 环境 |
| E-02 | 无 `ffmpeg` / `ffprobe` | `which` 为空 | 系统依赖（按要求未安装） |
| E-03 | `environment.yml` GPU 段在 Windows 不可解（nvidia-*-cu12 为 Linux wheel） | `environment.yml:31-32`；platform | 平台/系统依赖 |
| E-04 | pip 全局 index（清华镜像）不可达；官方 PyPI 经代理可达 | `pip config list` / `curl` | 网络 |
| E-05 | `import pymilvus` 崩溃：pymilvus 无条件 `load_dotenv()`，读到用户主目录 `C:/Users/黄嘉林/.env`（UTF-16 BOM） | 完整堆栈 | 环境产物（用户主目录 `.env` 编码 + 三方库行为） |
| E-06 | 本机 ad-hoc venv 下 uvicorn 传输栈异常（首请求 200 后 404） | 对照实验（普通 Starlette 亦复现） | 依赖版本/环境 |

---

## 四、交付物清单

| 文件 | 状态 |
|---|---|
| `docs/cybercut/WB-00-base-audit.md` | ✅ 本文件（新增） |
| `docs/cybercut/WB-00-capability-matrix.md` | ✅ 新增 |
| `docs/cybercut/ADR-0001-flowmind-integration-boundary.md` | ✅ 新增 |

**未修改任何业务源码**。`git status --short` 为空（审计前）→ 新增 3 个文档后仅新增文件。

---

## 五、关键发现（≤10 条）

1. **F-01（阻塞基座视频能力，代码缺陷）**：`LocalizerConfig` 缺 `asr_backend/ocr_backend/local_asr_model/local_asr_device` 等 7 字段，`localize_video.py:568/598/572` 直接属性访问 → `AttributeError` → **`localize_video` 流水线本体 100% 不可执行**，`localize_submit` 提交的任务在 worker 侧必失败。（**不阻塞 CyberCut 首版**——首版不依赖该流水线。）
2. **「视频能力是死的（基座侧）」**：7 个 `localize_*` 中，唯一真正处理视频的 `localize_video` 不可用；其余 6 个仅管理任务元数据。
3. **零单元测试**：无 `tests/`、无 pytest；验证仅 ruff + demo；且 `errors.py:47` 引用了不存在的 `tests/test_errors.py`。
4. **文档与事实不符**：README/CLAUDE/AGENTS 三处称「9 个 demo 全 PASS」，实测 **8 PASS / 2 FAIL**，且 demo 实际为 **10 个**。
5. **框架层真实可用，但 MCP 暴露是「全量无差别」的**：`@skill` → 注册表 → JSON schema → MCP 工具 → manifest/discover 自动链已实测贯通（7 工具，schema 顶层多一层 `inp`）；`SkillResult/trace/metrics/四段式推理链` 结构真实，`trace_id` 透传生效。**关键：`register_all()` 无差别遍历 `_REGISTRY`（`server.py:44-54`），不存在「MCP 公开白名单 / 内外技能分离」机制（NOT_PRESENT）**——注册即对外公开。
6. **执行器定位修正**：任务引擎是**单技能、单进程、重启即中断**的执行事实来源；`TaskManager` **不得作为 CyberCut 默认 NodeRun 执行器**。`recover_running()` 是构造函数的**全局副作用**（任何进程初始化 `TaskManager` 都会把该库全部 `queued`/`running` 打成 `interrupted`），故**严禁 API 与 worker 两进程各自初始化同一 `TaskManager`**。CyberCut 须自建 PG 队列 + worker claim（多进程可靠任务领取 = NOT_PRESENT）。
7. **产物策略修正**：`TaskManager` **只自动识别单个** `data.output_path`（`manager.py:278-283`）；多产物登记 = NOT_PRESENT。且基座产物目录受 **TTL GC**（默认 3600s 删除，`manager.py:363-393`）。CyberCut 正式产物必须由 `ArtifactRevision` 独立登记并复制到 `data/projects/{project_id}/artifacts/{artifact_id}/`，**不得依赖基座 TTL 目录存活**；`output_paths` 仅视为临时执行产物索引。
8. **PG 硬依赖；MQTT/Milvus/BGE 可选且外部；Go 网关不在仓库**：PG 缺失即 `TaskStoreError`；MQTT 未配置降级纯落库；Milvus 与 BGE **服务不在仓库**（仅客户端）；`gpu_lane()=Semaphore(1)` 双道闸防 OOM；无 `go-kernel/`，仓库内仅有联邦自注册客户端（QoS 1/0/1，实测 PASS）。
9. **许可证总体宽松，两处需法务确认**：基座 MIT；核心依赖无 GPL/AGPL；`psycopg2-binary` 为 **LGPL-3.0**（弱 copyleft，捆绑 libpq）、`paho-mqtt` 为 **EPL-2.0 OR BSD-3**（须主动选 BSD-3）；`simple-lama-inpainting`/`big-lama` 权重与 `go-kernel` 许可**未核实（未知风险）**。
10. **依赖声明缺失与配置卫生**：`config.py:12` 导入的 `tomli_w` **未在 `pyproject.toml`/`environment.yml` 声明**；Python 版本四处声明不一致（3.11/3.12）；`skills/__init__.py` 为手工白名单（冲突热点，且登记即对外公开）。

---

## 六、阻塞项

> 说明：下表「与 CyberCut 的关系」已按本 R 阶段决策修正——**B-01（F-01）不阻塞 CyberCut 首版**，因为首版不依赖 `localize_video` 流水线。

| 编号 | 阻塞项 | 影响 | 与 CyberCut 的关系 |
|---|---|---|---|
| B-01 | **F-01：`localize_video` 不可执行**（基座缺陷） | **基座**视频能力不可用 | **不阻塞 CyberCut 首版**（首版不依赖该流水线，节点由 CyberCut 自建 `NodeHandler` 承担）；若后续要复用 `localize_video`，须上游修复或经 `adapters/` 规避 |
| B-02 | **无可用 `flowmind` 环境 / 无 GPU 栈 / 无 ffmpeg** | 本机无法端到端跑真实视频链路 | CyberCut 的算力与部署基线需在 WB-01 明确（Linux + NVIDIA Pascal + ffmpeg + PG + MQTT） |
| B-03 | **`environment.yml` 在 Windows 不可解（GPU 段）** | Windows 无法作为生产运行平台 | 生产/CI 须 Linux；Windows 仅作开发/编排侧 |
| B-04 | **`api_server_demo` 端到端 REST 未能验证**（本机环境产物） | 本机无法复现基座 REST 通道全链路 | 需在固定依赖版本的 Linux 环境复验；不影响 CyberCut 自建 REST 面 |
| B-05 | **基座无 CI / 无测试** | 上游变更无回归网 | CyberCut 须自建契约快照测试 + 锁基座 commit |
| B-06 | **`TaskManager` 重启即中断 + 两进程风险**（基座行为） | 若误用基座 `TaskManager` 作多进程执行器，在途任务被静默打成 `interrupted` | **不采用**基座 `TaskManager` 作默认执行器即规避（CyberCut 自建 PG 队列 + claim） |
| B-07 | **基座无「MCP 公开白名单 / 内外技能分离」** | 无法在基座内隐藏内部技能 | CyberCut 侧以「**不注册内部节点**」实现隔离（首版不改 `server.py`） |

---

## 七、对 WB-01 控制面设计的具体建议（本 R 阶段修正版）

> 修正后的基座定位：**FlowMind 只负责「公开 MCP 技能契约 + trace 信封 + 既有 `localize_*` 兼容能力」**；CyberCut 的 REST、SSE、控制面、内部节点执行**全部归 CyberCut**。基座 `TaskManager` **不是** CyberCut 默认执行器。

1. **三层领域模型必须独立建模**（CyberCut 侧，基座不感知）：
   `Project 1:N Run 1:N NodeRun 1:N NodeAttempt`，另加 `NodeRun 1:N ArtifactRevision`、`NodeRun 0:N Approval`。
   - `NodeRun` = 逻辑节点 + 依赖状态；`NodeAttempt` = 一次实际执行；
   - **重试必须新建 `NodeAttempt`，绝不覆盖旧尝试**；
   - `executor_kind ∈ {cybercut_worker, flowmind_task}`；`executor_ref` **仅在** `executor_kind=flowmind_task` 时填写（指向基座 `task_id`）。
2. **CyberCut 自建执行器**：基于 PostgreSQL 的 **NodeAttempt 队列 + worker claim 机制**（建议用与基座 CAS 同形的「单条 `UPDATE … WHERE status='queued' AND … RETURNING`」原子领取）。
   **不采用**基座 `TaskManager` 作为默认 NodeRun 执行器。
3. **严禁 API 进程与 worker 进程各自初始化同一个 FlowMind `TaskManager`**：`TaskManager.__init__` 会无条件执行 `recover_running()`，把该库全部 `queued`/`running` 打成 `interrupted`（见 §2.3.6「两进程风险」）。若确需兼容 `localize_*`，须**单独隔离**为一个专用进程/专用库，且不与 CyberCut 主队列共用状态。
4. **MCP 公开面用「不注册」实现隔离**：只有**明确要公开的项目级 MCP 能力**才写成 FlowMind `@skill` 并登记 `skills/__init__.py`（登记 = 对外公开，`server.py:44-54`）。**素材分析、ASR、脚本、分镜、素材匹配、Timeline、渲染、QC 一律用 CyberCut 内部 `NodeHandler`，不进 `_REGISTRY`**。首版**不修改** `server.py`、不新增可见性机制。
5. **CyberCut 自建对外面**：REST、SSE/事件面、控制面 API、内部节点执行，均归 CyberCut；基座 REST/`/mcp` 只用于「公开 MCP 技能 + `localize_*` 兼容」。
6. **产物策略必须显式**：CyberCut 用 `ArtifactRevision` 独立登记**多产物**；正式产物复制/移动到 `data/projects/{project_id}/artifacts/{artifact_id}/`；**不得依赖基座 TTL 目录**（基座默认 3600s 删除）。基座 `output_paths` 仅作**临时执行产物索引**，worker 须在基座任务成功后立即搬运。
7. **PG 作为共享持久层**：基座已硬依赖 PG（`flowmind.tasks`，统一库 `rak`）。CyberCut 同库异 schema（**`rak.cybercut`**），**不建立跨 schema 外键**（避免耦合基座 schema 演化），`executor_ref` 仅作软引用；所有业务表带 `tenant_id`。
8. **不修改基座核心契约**：`contracts.py`/`skill.py`/`server.py`/`manifest.py`/`tasks/*` 一律不改（基座维护冻结，`CLAUDE.md:15-17`）。首版**不改 `server.py`**；CyberCut 只**新增**自己的代码与（公开 MCP 的）技能。
9. **鉴权复用基座既有方案**：`RakAuthMiddleware` + Bearer token（`<b64url>.<hex HMAC>`）+ `tenant_scope()` fail-closed + `tenant_id` 列；CyberCut 的 Project/Run/NodeRun 表同样带 `tenant_id`。
10. **MCP 入参按 `{"inp": {...}}` 传**：MCP 工具入参 schema 顶层多一层 `inp`（已实测），CyberCut 的 MCP 客户端与契约测试须对齐该形状。
11. **依赖与可升级性**：基座无 CI、无测试、依赖未锁星（如 `starlette` 无上界）。CyberCut 应 (a) 锁基座 commit/tag；(b) 自建契约快照测试（对 `contracts.py` 结构、MCP schema、`/api/v1/manifest` 形状、`tasks` 列集/状态机、MQTT 主题 QoS）；(c) 对 `tomli_w` 等未声明依赖显式补齐。
12. **F-01 不阻塞首版**：首版不依赖 `localize_video`；若后续复用其 helper，走 `adapters/` 隔离，并同步上报上游。
