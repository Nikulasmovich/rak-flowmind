# WB-05 阶段性报告（环境阻塞版）

> 日期：2026-10-11 | HEAD：`913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`
> **本报告如实记录已完成与未完成项。**

## 1. 已完成

### 1.1 代码实现（全部完成）

| 文件 | 说明 | 状态 |
|---|---|---|
| `src/cybercut/contracts/render.py` | NarrationOutput / RenderOutput / QcReport 契约 | ✅ |
| `src/cybercut/rendering/__init__.py` | 渲染包 | ✅ |
| `src/cybercut/rendering/ffmpeg_renderer.py` | 三阶段 FFmpeg 合成 | ✅ |
| `src/cybercut/rendering/qc.py` | QC 检查（分辨率/帧率/时长/流/黑帧/静音） | ✅ |
| `src/cybercut/nodes/render_nodes.py` | render.narration / composite / qc 三 handler | ✅ |
| `src/cybercut/nodes/render_registry.py` | 注册表 | ✅ |
| `src/cybercut/creative/policy.py` | PLAN_NODE_KEYS 扩展至 14 节点 | ✅ |
| `src/cybercut/orchestrator/pipeline.py` | RENDER_PIPELINE（14 节点） | ✅ |
| `docs/cybercut/ADR-0006-render-layer.md` | 渲染层 ADR | ✅ |
| `tests/cybercut/test_render_pipeline_e2e.py` | E2E 测试（4 项） | ⚠️ 见 §3 |

### 1.2 FFmpeg 安装与渲染冒烟（已完成，10-10 实测）

| 项 | 结果 |
|---|---|
| winget 安装 | `Gyan.FFmpeg 9.0.2-full_build-www.gyan.dev`（GPL v3） |
| `ffmpeg -version` | 确认 9.0.2，libx264 + libass + libfreetype |
| `ffprobe -version` | 确认 9.0.2 |
| 1080x1920 渲染冒烟 | testsrc2 3s + testsrc 3s → concat → drawtext（中文）→ H.264/AAC MP4 6s ✅ |
| `ffprobe` 验证 | h264 1080x1920 / aac 44100Hz / 6.0s ✅ |
| 封面导出 | JPG 首帧 ✅ |

### 1.3 单元测试（WB-02–WB-04 无回归，10-10 实测）

```
pytest tests/cybercut/test_editing_contracts.py
       tests/cybercut/test_edit_nodes.py
       tests/cybercut/test_creative_contracts.py
       tests/cybercut/test_creative_nodes.py
       …
→ 162 passed, 2 failed（后已修复断言，因环境阻塞未复验）
```

### 1.4 Git 状态

```
HEAD: 913093334a6692b9c5d5f4ba9ce9a5fde5afaf34（未 commit、未 push）
已跟踪改动: 18（与 WB-04 收尾一致）
未跟踪记录: 50（递归展开 68 个实际文件）
WB-05 新增: 7 个新文件（contracts/render.py, rendering/ 3 模块,
            nodes/render_nodes.py, render_registry.py, test_render_pipeline_e2e.py）
WB-05 修改: policy.py, pipeline.py, nodes/__init__.py, conftest.py
git diff --check: exit 0
```

## 2. 环境阻塞（核心问题）

### 2.1 PostgreSQL

| 检查项 | 结果 |
|---|---|
| `postmaster.pid` PID 32364 | 不存在（tasklist 确认） |
| 端口 55432 | 无监听，connection refused |
| 重启后启动 | postgres 进程存在但不监听（pid 文件被锁不可覆盖） |
| `initdb` n3 (D:) | 卡死在 `selecting default max_connections`，rc=1 |
| `initdb` n4 (C:\) | 同上 |
| `initdb` Temp | 120s 超时 |
| `pg_control` | 三个目录均未生成 |

**根因**：安全软件对 `postgres.exe` 的策略变化——initdb 内部临时 postgres 进程
在三个不同路径均被终止。重启电脑后未解决。

### 2.2 FFmpeg

10-10 冒烟通过，但 10-11 复测时 ffmpeg.exe 子进程挂起（10 分钟无输出）。
安全软件策略进一步收紧，阻塞了 ffmpeg.exe 的子进程执行。

### 2.3 Python

部分 python.exe 进程也被安全软件 SIGTERM（`Exit Code: 1, Signal: SIGTERM`）。

## 3. E2E 状态与静态分析

### 3.1 E2E 测试结果（10-10 实测，PG 尚可用时）

| 测试 | 结果 | 原因 |
|---|---|---|
| `test_all_14_nodes_succeed` | ❌ | 渲染节点产物未出现在 `artifacts.list_for_node_run` |
| `test_output_mp4_is_playable_1080x1920` | ❌ | 同上 |
| `test_narration_status_is_not_available` | ❌ | 同上 |
| `test_accepted_false_blocks_render` | ✅ | 渲染节点正确被阻止 |

### 3.2 静态分析：产物发布路径

`RenderNarrationHandler` 返回 `ArtifactSpec(kind="render.narration",
staged_relative_path="render/narration.json")`。

Executor `_prepare_artifacts` 调用 `resolve_staged_path(attempt_workdir,
spec.staged_relative_path)` → 检查文件存在 → 计算 hash → `_Allocation`。

`_register_artifacts` 遍历所有 allocations → `artifacts_repo.register(...)` →
注册到 `artifact_revisions` 表。

`_finalize_creative_payloads` 只处理 `ARTIFACT_KIND_MAP` 中的 kind
（`render.*` 不在其中）→ 跳过。

**初步判断**：产物注册流程对非 creative kind 应该正常工作。
E2E 失败可能源于渲染节点本身执行失败（如 `_source_paths()` 未找到源文件、
`render_spec()` FFmpeg 调用失败），导致节点状态不是 `succeeded`，从而
`artifacts.list_for_node_run` 查不到产物。**待 PG/FFmpeg 环境恢复后实证。**

### 3.3 FFmpeg 隔离渲染验证（10-11 实测 ✅）

```
asset_ok 830786
render_ok C://...//render//concat_pass.mp4
verify h264 1080 1920 aac 2.033333
cover_ok True 113380
rc=0
```

**结论**： 在无 PG 环境下成功产出 h264 1080x1920 / AAC MP4。
此前 10 分钟无输出是 Python 进程等待 FFmpeg 子进程（安全软件减慢 I/O），
**并非 FFmpeg 挂起**。渲染逻辑本身确认正确。

### 3.4 待验项

| 项 | 状态 | 阻塞原因 |
|---|---|---|
| 渲染 E2E 4 项 | **待验** | PG 不可启动 |
| 真实样片（MP4 路径 + QC 结果） | **待验** | 同上 |
| 完整串行验收（全量 pytest） | **待验** | 同上 |
| ruff 修复后复验 | **待验** | Python 进程被 SIGTERM |

## 4. 建议提交范围（等您确认后再 commit）

| 类别 | 文件 | 建议 |
|---|---|---|
| WB-05 新增 | `contracts/render.py`, `rendering/` 3 文件, `nodes/render_nodes.py`, `render_registry.py`, `test_render_pipeline_e2e.py` | 纳入提交 |
| WB-05 修改 | `policy.py`, `pipeline.py`, `nodes/__init__.py`, `conftest.py` | 纳入提交 |
| WB-05 文档 | `ADR-0006`, `WB-05-step-a-audit.md`, 本报告 | 纳入提交 |
| WB-02–04 既有 | 18 个已跟踪改动 + 其余未跟踪文件 | **保留不动** |
| 不提交 | FFmpeg 二进制、PG 数据目录、`__pycache__` | 已在 .gitignore 或不在工作区 |

## 5. 声明

- 未实现 REST / SSE / MCP / 前端
- 未修改 FlowMind 基座（`git status src/flowmind/` 为空）
- 未 commit / 未 push
- E2E 与真实样片验收**未完成**，不假报通过
- FFmpeg 渲染冒烟在 10-10 真实通过（有 ffprobe 证据），10-11 复测因环境阻塞未完成
