# WB-00 · FlowMind 基座能力矩阵

| 项 | 值 |
|---|---|
| 阶段 | WB-00 + **WB-00R 定向整改** |
| 基座 commit | `c8ea1c6e025cbc697a87d5626c2a300ef78504ce`（`main`） |
| 分类定义 | `DIRECT_REUSE`＝可直接使用；`ADAPTER_REQUIRED`＝需适配层；`EXTENSION_REQUIRED`＝需扩展；`REPLACE`＝不应复用；`NOT_PRESENT`＝方案曾假设存在但源码中不存在 |
| 判定依据 | 仅据源码 + 实测（见 `WB-00-base-audit.md` §三）。凡实测失败项标注 ⚠️ |
| R 阶段修正 | T-01/T-05 改类、T-06 拆分、新增 H-11/T-17/T-18、统计重算（详见 §七） |

> 阅读约定：「方案假设」列指 CyberCut `技术方案.md` 假设需要的能力；「基座实情」为源码/运行事实；「分类」为结论。带「R 修正」字样者为 WB-00R 改动项。

---

## 一、框架层（Skill SDK / 契约 / 发现）

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| F-01 | 一次 `@skill` 定义即成为 MCP 工具 | `skill()` 装饰器 + `_REGISTRY` + `server.register_all` 遍历注册；实测 `list_tools()`=7（`skill.py:96-139`、`server.py:44-54`） | **DIRECT_REUSE** |
| F-02 | 输入/输出 JSON schema 自动生成 | `model_json_schema()`，discover 与 manifest 共用 `skill_entry`（`discover.py:18-35`、`manifest.py:11-15`） | **DIRECT_REUSE** |
| F-03 | 统一结果信封（ok/data/error/trace/metrics/reasoning） | `SkillResult` / `SkillOutput` 结构完整（`contracts.py:68-87`）；实测 trace_id 透传、NOT_FOUND/VALIDATION/INTERNAL 生效 | **DIRECT_REUSE** |
| F-04 | 四段式因果推理链 | `ReasoningChain`（conclusion/triggered_rules/evidence/causal_analysis/risk_note）+ `rules.evaluate_rules`（`contracts.py:33-40`、`rules.py:27-35`）；实测键序一致 | **DIRECT_REUSE** |
| F-05 | 全链路 trace_id | `TraceContext` + `new_trace()` 透传（`contracts.py:52-57,90-100`）；实测 `WB00-TRACE-123` 原样返回 | **DIRECT_REUSE** |
| F-06 | 失败分类与重试判定 | `FailureCategory`(environment/video/transient/unknown) + `is_retriable()`（`errors.py:26-92`） | **DIRECT_REUSE** |
| F-07 | 技能声明式规则（阈值/告警） | `Rule` + `evaluate_rules` 自动产出 RuleHit/Evidence（`rules.py:13-35`） | **DIRECT_REUSE** |
| F-08 | 技能原生支持长任务（long_running） | 装饰器/契约/`invoke` 均为**同步**，无 long_running/async/进度语义（`skill.py:96-199`） | **NOT_PRESENT**（长任务由外挂 TaskManager 承担，见 T-*） |
| F-09 | Agent 自助发现（不读源码） | `discover()` / `field_names()` / `build_manifest()` + `GET /api/v1/manifest[/{id}]`（`discover.py`、`server_rest.py`） | **DIRECT_REUSE** |
| F-10 | 新增技能「零改框架」 | 需新增技能文件 + **手工在 `skills/__init__.py` 白名单追加 import**（显式列表，非自动扫描，`skills/__init__.py:13-21`） | **ADAPTER_REQUIRED**（注册需人工登记；framework 层本身不改） |
| F-11 | 技能注册表运行时元数据（category/tags） | `SkillSpec.category/tags` 由后端声明（`skill.py:39-40`）；现有 7 技能**全部** `category="通用"`、`tags=[]`（实测） | **EXTENSION_REQUIRED**（分类体系空缺，需 CyberCut 自行填充） |

---

## 二、HTTP / MCP 通道

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| H-01 | MCP over HTTP 单端点 | `/mcp` Streamable HTTP，FastMCP v1（`server_http.py:309`）；实测 7 工具 | **DIRECT_REUSE** |
| H-02 | REST 长任务通道 | `POST /api/v1/tasks`(202/429) → `GET /{id}` → `GET /{id}/download`（`server_tasks.py`） | **DIRECT_REUSE** |
| H-03 | REST 发现面 | `GET /api/v1/manifest[/{id}]`（`server_rest.py`）；实测 TestClient 200/404 正确 | **DIRECT_REUSE** |
| H-04 | 健康探针 | `GET /api/v1/health`，组件状态 pg/mqtt/milvus，恒 200（`server_tasks.py:166-204`） | **DIRECT_REUSE** |
| H-05 | 素材上传通道 | **无上传端点**；仅约定 `data_dir/uploads/` 作沙箱根，接收端需自建（`localize_submit.py:128-150`） | **NOT_PRESENT** |
| H-06 | 产物流式下载 | basename 白名单匹配 `output_paths`，防路径穿越（`server_tasks.py:142-164`） | **DIRECT_REUSE** |
| H-07 | 输入路径沙箱（防穿越/防外带） | `resolve()` 后须在 `data_dir/uploads/` 内；收口于 `_split_paths`；`FLOWMIND_ALLOW_ANY_PATH=1` 可放行（`localize_submit.py:133-177`） | **ADAPTER_REQUIRED**（仅覆盖「提交通道」，且是软隔离） |
| H-08 | SSE 实时事件通道 | **无 SSE 端点**；实时性靠 MQTT 外部订阅（`README.md` 方案 §2 称 `:8080 /api/v1/stream`，源码中不存在） | **NOT_PRESENT** |
| H-09 | CORS | `CORSMiddleware` 默认 localhost 源 + 暴露 `mcp-session-id`（`server_http.py:141-149,191-201`） | **DIRECT_REUSE** |
| H-10 | 统一网关单一入口 | 前端契约假设「统一网关 :8080」；该 Go 网关**不在本仓库**（无 `go-kernel/`） | **NOT_PRESENT**（本仓库内） |
| H-11 | **MCP 公开白名单 / 内外技能分离** | `register_all()` **无差别遍历 `_REGISTRY`**，把每个技能都登记为 MCP 工具；`SkillSpec` 无可见性字段；无 include/exclude 过滤（`server.py:44-54`、`skill.py:29-40`） | **NOT_PRESENT**（隔离须靠「CyberCut 内部节点**不注册**」实现；首版不改 `server.py`） |

---

## 三、鉴权与租户

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| A-01 | 调用方凭证校验 | `RakAuthMiddleware`（纯 ASGI）+ HMAC Bearer token；空密钥=关闭（`server_http.py:56-104`、`auth.py`）；实测 `auth_token_demo` 全 PASS | **DIRECT_REUSE** |
| A-02 | 多租户 `tenant_id` | `tasks.tenant_id` 列 + `tenant_scope()` 三态 fail-closed + 跨租户 404 语义（`store.py:50`、`auth.py:94-110`、`manager.py:198-206`） | **DIRECT_REUSE** |
| A-03 | 鉴权豁免面（探针/发现） | `/api/v1/health`、`/api/v1/manifest` 豁免前缀（`config.py:357-358`） | **DIRECT_REUSE** |
| A-04 | 用户登录/账号体系 | **无**（只做「凭证校验」，不签发 token、无用户库） | **NOT_PRESENT**（上游签发，须外部系统） |

---

## 四、异步任务引擎

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| T-01 | TaskManager 作为通用节点执行器 | 单技能 `{skill_id,args_json}`、`workers=1` 全局串行、构造即 `recover_running()` 全局副作用（`manager.py:70-107,91`） | **ADAPTER_REQUIRED**（⚠️ R 修正：**不得作 CyberCut 默认 NodeRun 执行器**；仅用于 `localize_*` 兼容或作可选适配器 `executor_kind=flowmind_task`） |
| T-02 | CAS 状态守卫（终态 first-writer-wins） | `UPDATE … WHERE … status = ANY(%s)`（`store.py:192-237`） | **DIRECT_REUSE**（模式可借鉴；CyberCut 队列 claim 建议同形实现） |
| T-03 | 协作式取消 | `cancel()` + 阶段边界 `cancel_check`→`CancelledError`（`manager.py:150-177`、`localize_video.py:241-248`）；子进程不强杀 | **DIRECT_REUSE**（契约可借鉴） |
| T-04 | 失败重试 | `localize_retry` 复制原 args 重提（`localize_retry.py`） | **DIRECT_REUSE**（仅限 `localize_*` 任务；**CyberCut 重试须新建 NodeAttempt，不覆盖旧尝试**） |
| T-05 | 重启恢复 | `recover_running()` 把 queued/running → `interrupted`（`manager.py:103`、`store.py:239-263`）；**不重放** | **ADAPTER_REQUIRED**（⚠️ R 修正：是 `TaskManager` 构造的**全局副作用**，多进程共用会互毁在途任务；须隔离为专用进程/库或自建恢复） |
| T-06a | TTL 清理（临时执行产物） | GC 线程删 `tasks/<id>/`、`outputs/<id>/`，**DB 行保留**（`manager.py:363-393`） | **DIRECT_REUSE**（仅对「临时执行产物」成立） |
| T-06b | TTL 清理（项目正式产物） | 同上 GC 会删除 `outputs/<id>/`——正式产物若落基座 TTL 目录则 1h 后被清 | **REPLACE**（⚠️ R 修正：项目正式产物**必须**落 CyberCut 自有 `data/projects/.../artifacts/`，**不得依赖基座 TTL 目录存活**） |
| T-07 | 背压 | `max_pending_tasks`→`TaskQueueFull`→429/INTERNAL（`manager.py:128-133`、`server_tasks.py:101-120`） | **DIRECT_REUSE**（**单进程内**） |
| T-08 | MQTT 进度事件 | `mcp-base-gpu/tasks/{id}/events`，QoS1，终态 retain（`events.py:143-160`） | **DIRECT_REUSE** |
| T-09 | **Project / Run / NodeRun 建模** | `tasks` 表只有 `{task_id, skill_id, args_json, status, …}` 一层，**无** project/run/node/dag/depends_on/parent 列 | **REPLACE**（须 CyberCut 独立建模，见 ADR-0001 §2） |
| T-10 | **DAG / 节点依赖 / 编排** | **无**任何图结构或步骤依赖；批量提交仅循环平级 `submit` | **NOT_PRESENT** |
| T-11 | **步骤间中间产物传递** | `args_json` 提交即冻结，任务间不互通 | **NOT_PRESENT** |
| T-12 | **人在环审批断点** | 任务状态机无 `waiting_approval` 等人工态；无审批队列 | **NOT_PRESENT** |
| T-13 | **局部重跑（按节点/阶段）** | 重试粒度=整任务；取消粒度=阶段边界；无「从某步骤重跑」。`localize_video` 内部阶段不可寻址 | **NOT_PRESENT** |
| T-14 | 批量生产 | `localize_submit` 循环建 N 个平级 task | **ADAPTER_REQUIRED**（平铺批量可用；父子/批次聚合需自建） |
| T-15 | 水平扩展（多实例） | `workers=1` 全局串行 + 进程内锁；PgBouncer 事务模式禁用 advisory lock；无分布式队列 | **REPLACE**（不可多实例；需自建分布式调度） |
| T-16 | 全局并发调度 | `ThreadPoolExecutor(max_workers=1)`，GPU 显存预算驱动（`manager.py:91`） | **ADAPTER_REQUIRED**（非 GPU 并行须绕开基座线程池） |
| T-17 | **多产物登记** | `_classify_and_finish` **只取单个** `data.output_path` 落 `output_paths`（至多 1 条），无 `outputs[]`/命名/版本化产物（`manager.py:278-283`） | **NOT_PRESENT**（须 CyberCut 用 `ArtifactRevision` 独立登记多产物） |
| T-18 | **多进程可靠任务领取** | **无**分布式队列/claim：`workers=1` + 进程内 `_submit_lock`；PgBouncer 事务模式明确禁用 advisory lock；无 `SELECT … FOR UPDATE SKIP LOCKED` 式领取（`manager.py:91,128-137`、`store.py:3-8`） | **NOT_PRESENT**（CyberCut 须自建 PG 队列 + worker claim） |

---

## 五、基础设施

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| I-01 | PostgreSQL 任务存储 | **硬依赖**；缺配置 → `TaskStoreError`（`store.py:66-103`）；统一库 `rak` 的 `flowmind` schema | **DIRECT_REUSE**（注意 schema 归属） |
| I-02 | MQTT 事件（EMQX） | 可选；未配置降级纯落库；QoS/retain 契约真实（`events.py`） | **DIRECT_REUSE** |
| I-03 | GPU 串行 lane | `gpu_lane()=Semaphore(1)` + `model_cache_guard()`（`tasks/gpu.py`）；仅本地 ASR/LaMa/TTS 占槽 | **DIRECT_REUSE** |
| I-04 | Milvus 向量库 | **服务不在仓库**（仅客户端 `tasks/vectors.py`）；URI 空=显式禁用 | **ADAPTER_REQUIRED**（客户端可用，服务须外部部署） |
| I-05 | BGE 嵌入服务 | **服务不在仓库**（仅 HTTP 客户端 `_bge_embed.py`，TEI/OpenAI 双形状）；空=显式禁用 | **ADAPTER_REQUIRED** |
| I-06 | Go 联邦网关 | **不在仓库**（无 `go-kernel/`）；仓库内仅有自注册客户端 `federation/`（QoS 1/0/1，实测 PASS） | **NOT_PRESENT**（本仓库内）/ 需外部仓库 |
| I-07 | 配置系统（env→toml→默认） | `FlowmindConfig`=`LocalizerConfig`+`InfraConfig`+`AuthConfig`+`FederationBackendConfig`（`config.py:462-479`） | **DIRECT_REUSE**（但见 F-01：`LocalizerConfig` 字段不全） |
| I-08 | 部署可移植（Windows） | GPU 段 Linux-only；本机无 ffmpeg；`import pymilvus` 崩溃（环境产物） | **REPLACE**（生产须 Linux） |

---

## 六、视频/媒体能力

| # | 方案假设能力 | 基座实情（证据） | 分类 |
|---|---|---|---|
| V-01 | **视频本地化流水线（ASR→译→擦→TTS→混）** | `localize_video.run_localization_pipeline` 存在，但 **`cfg.asr_backend` AttributeError → 100% 不可执行**（F-01，实测 FAIL） | **REPLACE**（⚠️ 当前不可用；修复后至多 ADAPTER_REQUIRED） |
| V-02 | ASR（本地/云） | `_local_asr`（faster-whisper）/`_cloud_asr`（dashscope 流式）（`_local_asr.py`、`_cloud_asr.py`） | **ADAPTER_REQUIRED**（私有模块，强耦合 LocalizerConfig + GPU） |
| V-03 | TTS/配音（本地克隆/云预设） | `_local_tts`（Qwen3-TTS 克隆）/`_cloud_tts`（dashscope）（`_local_tts.py`、`_cloud_tts.py`） | **ADAPTER_REQUIRED** |
| V-04 | 字幕定位（OCR） | `_local_ocr`（RapidOCR）/`_cloud_ocr`（qwen3.5-ocr）（`_local_ocr.py`、`_cloud_ocr.py`） | **ADAPTER_REQUIRED** |
| V-05 | 字幕擦除 | `_inpaint`（LaMa 逐帧）/ ffmpeg `delogo`（`_inpaint.py`、`_media.burn_subtitles`） | **ADAPTER_REQUIRED** |
| V-06 | 字幕烧录/重绘 | `localize_video._write_ass` 生成 ASS + ffmpeg `ass` 滤镜（`localize_video.py:773-809`） | **ADAPTER_REQUIRED** |
| V-07 | FFmpeg 媒体操作 | `_media.py` 薄封装 + 多处直接 `ffmpeg`/`ffprobe`/`fc-match` 调用；**二进制为系统依赖，未随仓库分发** | **ADAPTER_REQUIRED**（二进制须自行部署） |
| V-08 | 翻译（LLM） | `_llm_translate`（Anthropic/OpenAI 双协议，默认 LongCat）（`_llm_translate.py`） | **ADAPTER_REQUIRED** |
| V-09 | 人声分离（BGM 净化） | `_music_sep`（demucs htdemucs 子进程）（`_music_sep.py`） | **ADAPTER_REQUIRED** |
| V-10 | **素材分析 / 选题 / 文案 / 脚本 / 分镜 / 素材匹配 / 时间线 / 渲染合成 / 质检** | 注册表仅 7 个 `localize_*`；无任何上述节点；`content_hot_boards` 等模块**仅存在于源码文件但不注册**（见 V-11） | **NOT_PRESENT**（须 CyberCut 自建） |
| V-11 | 内容创作域技能（热榜/文案/生图等） | `skills/content_hot_boards.py` 存在但**依赖缺失**（`_content_common`、`_hot_topics_client` 均不存在，实测 ImportError）→ 未注册（`skills/__init__.py:5-11` 明示） | **NOT_PRESENT** |
| V-12 | 语义分段（smart split） | `_split_long_segments` 仅按标点 20 字硬切，非语义分镜（`localize_video.py:642-674`） | **REPLACE** |
| V-13 | 中间产物契约 Script/Storyboard/Timeline | 基座无此类模型；只有 `LocalizeVideoReport` 等单技能载荷（`contracts.py`/各技能） | **NOT_PRESENT** |
| V-14 | 人工审批 / 提示词改写 / 断点续跑 | 基座无审批态、无提示词改写接口、无断点续跑（重试=整任务） | **NOT_PRESENT** |

---

## 七、汇总统计（WB-00R 重算）

> 统计口径：按矩阵行项逐个计数（共 **67** 行）。T-06 按「临时执行产物 / 项目正式产物」拆为 T-06a / T-06b 两行分别归类。

| 分类 | 数量 | 代表性条目 |
|---|---|---|
| **DIRECT_REUSE** | **27** | 契约/信封/推理链/trace、JSON schema、发现面、MCP 工具注册（机制本身）、REST 任务通道、发现/健康端点、CORS、鉴权租户、CAS 守卫（模式）、协作取消（契约）、`localize_*` 重试、TTL（临时产物）、背压（单进程）、MQTT 事件、PG 存储、配置系统、GPU lane |
| **ADAPTER_REQUIRED** | **16** | **TaskManager 作执行器（R 修正）**、**重启恢复（R 修正）**、新增技能登记、输入沙箱、批量生产、全局并发调度、Milvus/BGE 服务、全部媒体 helper（ASR/TTS/OCR/擦除/字幕/FFmpeg/翻译/分离） |
| **EXTENSION_REQUIRED** | **1** | 技能 category/tags 分类体系（当前全为「通用」） |
| **REPLACE** | **6** | Project/Run/NodeRun 建模、水平扩展（多实例）、**TTL 对项目正式产物（R 修正）**、`localize_video` 流水线（当前不可用）、Windows 生产运行、语义分段 |
| **NOT_PRESENT** | **17** | long_running、上传通道、SSE、Go 网关（本仓内）、**MCP 公开白名单（R 新增）**、用户登录、DAG/依赖、步骤间产物传递、HITL 审批、局部重跑、**多产物登记（R 新增）**、**多进程可靠任务领取（R 新增）**、素材分析/文案/脚本/分镜/匹配/时间线/渲染/质检、内容域技能、Script/Storyboard/Timeline 契约 |
| **合计** | **67** | — |

> 与 WB-00 首版的差异（本 R 阶段修正）：`T-01` DIRECT_REUSE→ADAPTER_REQUIRED；`T-05` DIRECT_REUSE→ADAPTER_REQUIRED；`T-06` 拆分（临时 DIRECT_REUSE / 正式产物 REPLACE）；新增 `H-11`（MCP 白名单 NOT_PRESENT）、`T-17`（多产物登记 NOT_PRESENT）、`T-18`（多进程可靠任务领取 NOT_PRESENT）。**旧统计（24/14/1/6/14）已作废。**

### 一句话结论（R 修正版）

基座可直接复用的是一层「**Agent 化的技能外壳 + 发现/契约/信封 + MCP/HTTP 暴露机制 + 可观测 + 鉴权租户**」；但**执行器（TaskManager）只作兼容/可选适配器**、**MCP 暴露无内외分离**、**无多产物登记、无多进程可靠领取**。CyberCut 的**业务能力（编排、领域模型、中间产物、分镜/文案/时间线/渲染/质检、审批、局部重跑、批量聚合）几乎全部属于 `NOT_PRESENT` 或 `REPLACE`**，必须自建；媒体 helper 可经**适配层**复用，但唯一真正"跑视频"的 `localize_video` 当前**不可用（F-01，不阻塞首版）**。
