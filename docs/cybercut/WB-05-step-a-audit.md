# WB-05 · 步骤 A：只读审计报告

> 基线 `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`；61 项工作区变更全部归属 WB-02–WB-04，无意外文件。
> 本报告为只读审计结论，**未开始编码**。等待用户确认后才进入步骤 B/C。

## 1. 文档与边界确认

| 文档 | 关键约束（对本阶段的影响） |
|---|---|
| AGENTS.md | FlowMind 基座维护冻结；验证靠 examples 冒烟（基座无 pytest）；CyberCut 侧另有 pytest 体系 |
| ADR-0001 | FlowMind **不是**节点执行器；CyberCut 内部 NodeHandler **不注册** `@skill`；渲染与 QC 业务逻辑归 CyberCut |
| ADR-0005 | TimelineSpec 为编辑计划权威来源；冻结引用只读；`accepted ⇔ unmet 为空` |
| WB-02 media layer | `FFmpegToolchain` 可注入 runner/cancel_check；`asset_normalize` 依赖 ffmpeg 生成代理 |
| WB-04 test report | 11 节点管线全绿；6 类产物版本表；timeline gate（hybrid/manual）已测 |

## 2. 当前基础设施可复用项

| 设施 | 现状 | WB-05 复用方式 |
|---|---|---|
| `FFmpegToolchain` | 可注入 runner/cancel_check/probe_json/run_ffmpeg | 作为 FFmpeg 渲染 Provider 的底层执行器 |
| NodeHandler / NodeAttempt | 内部协议，`attempt_dir` 暂存 + 执行器发布 | 渲染节点沿用同一模式 |
| ArtifactRevision | 不可覆盖、`metadata_json` JSONB | 渲染器信息/QC 摘要/来源引用存 metadata |
| 取消/恢复 | `cancel_check` + `worker.recover()` | 渲染节点传入 `context.cancel_check`；远端 Provider 需持久化 task_id |
| 审批 gate | `ApprovalPolicy.gates` + gate node 模式（WB-04 已测） | timeline gate 已有；渲染层可按需扩展 |
| 局部重跑 | `revise_node` / `creative_descendants` 链 | 渲染节点追加到链尾 |

## 3. 外部依赖审计

### 3.1 FFmpeg（**阻塞**）

| 检查项 | 结果 |
|---|---|
| `PATH` 中 `ffmpeg` / `ffprobe` | 未找到 |
| conda 环境（cybercut-pg / linzi / model / base） | 均无 |
| `imageio_ffmpeg`（cybercut-wb00 venv） | 未安装 |
| `winget` | **可用**（`winget install Gyan.FFmpeg`） |
| `choco` | 不可用 |
| 常见安装目录 | 均无 |

**影响**：双重阻塞——
1. WB-05 FFmpeg 渲染无法真实执行；
2. WB-02 `asset_normalize`（代理生成）也无法对真实素材执行。

### 3.2 火山引擎（**无法 PoC**）

| 凭证变量 | 状态 |
|---|---|
| `VOLCENGINE_ACCESS_KEY` / `VOLCENGINE_SECRET_KEY` | 未配置 |
| `VOLC_ACCESS_KEY` / `VOLC_SECRET_KEY` | 未配置 |
| `VESITHUB_ACCESS_KEY` / `VESITHUB_SECRET_KEY` | 未配置 |
| `.env` 文件 | 不存在 |

即使后续配置凭证，火山**智能剪辑 API 基于自然语言**，无法按冻结 TimelineSpec
确定性控制素材顺序、入出点与多轨——用户已明确不得将其默认当作渲染器。
火山的云端合成/导出 API 需真实 PoC 才能确认能力矩阵。当前判定：**不可验证**。

### 3.3 TTS（**无可用凭证**）

`DASHSCOPE_API_KEY` / `AZURE_SPEECH_KEY` / `ELEVENLABS_API_KEY` 均未配置。
无 TTS 时成片允许无旁白（QC 如实记录 `narration: not_available`），不伪造音频。

### 3.4 测试素材（**均为假字节**）

E2E 测试写入 `b"VIDEO"` / `b"IMAGE"`，ffmpeg 无法解码。
真实出片需先用 ffmpeg 生成可播放的测试素材（testsrc / color bars）。

## 4. Provider 选择依据与阻塞

| 路径 | 可行性 | 阻塞原因 |
|---|---|---|
| 火山引擎 | **当前不可验证** | 无凭证；智能剪辑 API 非确定性；需真实 PoC |
| 本机 FFmpeg | **当前不可执行** | ffmpeg 未安装 |
| mock / FakeProvider | 仅用于单元测试 | 用户明确禁止以 mock 报告"真实出片通过" |

**两条真实路径当前均阻塞。**

## 5. 需要用户决策的事项

| # | 决策 | 选项 |
|---|---|---|
| D1 | FFmpeg 安装方式 | A: 用户自行安装并加入 PATH；B: 授权 `winget install Gyan.FFmpeg`；C: 授权下载静态构建到项目 `tools/`（GPL v3，许可明确） |
| D2 | 火山路径 | A: 配置凭证后走真实 PoC；B: 本阶段放弃火山，采用 FFmpeg 首版 |
| D3 | TTS | A: 配置凭证接入 TTS；B: 本阶段无旁白（成片无声或仅 BGM），QC 如实记录 |

## 6. 最小 WB-05 DAG 提案（供确认后实施）

```
template.bind (WB-04 既有)
    → render.narration   TTS 旁白（Provider 接口；无凭证 → 空意图 + 告警，不伪造）
    → render.composite   FFmpeg 合成（画面拼接 + 裁切/画幅适配 + 转场
                                        + 字幕烧录 + BGM 混音/ducking）
    → render.qc          QC（ffprobe：分辨率/帧率/时长/流/黑帧/静音/字幕越界
                                       + 封面导出）
```

DAG 拆分依据：
- `render.narration` 与 `render.composite` 依赖不同外部资源（云 TTS vs 本地 FFmpeg），可独立重跑；
- `render.qc` 独立以实现「QC 失败不交付」（QC 失败则 composite 的 MP4 不作为交付产物）。

前置校验（在 `render.narration` / `render.composite` 入口统一执行）：
- `TemplateBinding.accepted == False` → `RENDER_BINDING_NOT_ACCEPTED`（列出 unmet）；
- `TimelineSpec.unresolved_shots` 非空 → `RENDER_UNRESOLVED_SHOTS`（列出 shot_id）；
- 冻结 `input_artifacts` 中的 TimelineSpec revision 与 TemplateBinding revision 交叉验证。

## 7. 文件清单提案

| 类型 | 路径 | 说明 |
|---|---|---|
| 新增 | `src/cybercut/contracts/render.py` | RenderRequest / RenderOutput / QcReport 契约 |
| 新增 | `src/cybercut/rendering/__init__.py` | 渲染包 |
| 新增 | `src/cybercut/rendering/provider.py` | RenderProvider 协议（提交/查询/取消/归档生命周期） |
| 新增 | `src/cybercut/rendering/ffmpeg_renderer.py` | FFmpeg 本地合成（首版 Provider） |
| 新增 | `src/cybercut/rendering/qc.py` | QC 检查逻辑 |
| 新增 | `src/cybercut/nodes/render_nodes.py` | 三个渲染节点 handler |
| 新增 | `src/cybercut/nodes/render_registry.py` | 注册表 |
| 修改 | `src/cybercut/creative/policy.py` | `PLAN_NODE_KEYS` + `RENDER_NODE_KEYS` |
| 修改 | `src/cybercut/orchestrator/pipeline.py` | 管线追加三节点 |
| 修改 | `src/cybercut/nodes/__init__.py` | 导出 |
| 新增 | `tests/cybercut/test_render_contracts.py` | 契约测试 |
| 新增 | `tests/cybercut/test_render_nodes.py` | 节点测试 |
| 新增 | `tests/cybercut/test_render_pipeline_e2e.py` | E2E（真实 MP4） |
| 修改 | `tests/cybercut/conftest.py` | 真实素材 fixture |
| 新增 | `docs/cybercut/ADR-0006-render-layer.md` | 渲染层 ADR |

**不需要新 migration**：渲染产物通过 ArtifactRevision 登记；渲染器信息、
来源引用、QC 摘要存入 `metadata_json`（JSONB）。

## 8. 真实/模拟边界声明

| 能力 | 真实可行 | 原因 |
|---|---|---|
| FFmpeg 合成出片 | **待 D1 确认** | ffmpeg 未安装 |
| 火山 PoC | **不可验证** | 无凭证 |
| TTS 旁白 | **待 D3 确认** | 无凭证 |
| 字幕烧录 | 依赖 FFmpeg 出片 | |
| BGM 混音 | 依赖 FFmpeg 出片；无合法 BGM 时允许无 BGM | |
| 封面导出 | 依赖 FFmpeg 出片 | |
| QC | 依赖 ffprobe | |
| 契约/帧换算/确定性/模板绑定单元测试 | 可先行（无外部依赖） | |
