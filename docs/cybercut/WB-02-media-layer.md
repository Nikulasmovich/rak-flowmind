# WB-02 · 素材接入、媒体标准化与基础分析

| 项 | 值 |
|---|---|
| 阶段 | WB-02 |
| 基线 | `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（WB-01R3 基线提交） |
| 基础设施 | 仍只有 PostgreSQL；媒体工具为外部可执行文件（ffmpeg/ffprobe），非服务依赖 |
| 关联 ADR | `ADR-0003-media-assets-and-analysis.md` |

## 1. 数据模型

- **新增迁移 `0003_media_assets.sql`**（不修改 0001/0002）：
  - `ALTER TABLE runs ADD COLUMN input_json jsonb`（冻结 Run 输入）；
  - `CREATE TABLE cybercut.media_assets`：素材登记/去重/状态机；
  - 约束：`UNIQUE(project_id, sha256)`（去重锚点）、
    `(project_id, tenant_id) → projects` 复合外键（租户一致性）、
    `(asset_id, tenant_id)` 复合唯一索引、kind/status/size CHECK。
- **不建立** `asset_segments` 可变表：分段以版本化 JSON Artifact 为事实源。
- **接入流程**：校验（普通文件 / 拒绝符号链接 / 拒绝空文件 / 拒绝 `..` 穿越）→
  复制进项目临时目录（边复制边哈希）→ 同文件系统原子移动到正式路径 → 置 `ready`。
- **接管原子性（R5）**：stale `ingesting` / `failed` 的接管在**单事务**内完成
  `FOR UPDATE` 行锁 + 状态 CAS → 校验所有权（`ASSET_TAKEOVER_CONFLICT`）→
  持锁内重新检查正式目标 → 发布或确认 → `mark_ready` → 提交；代码中**不提供**
  只匹配 `ready` 的行锁。目标已存在且大小/SHA-256 一致 ⇒ 恢复置 `ready`；
  不一致 ⇒ `ASSET_PUBLISH_CONFLICT`，绝不覆盖。
- **发布失败清理（R6）**：每次发布生成 `_PublishRecord`（发布者 token + 行身份 +
  文件大小/SHA-256）。失败回收**全程在同一持锁事务内**（`media_assets.lock_row()`
  `FOR UPDATE`，不限状态）重新确认「行仍处于发布前状态 ∧ 正式路径未改写 ∧
  文件身份一致」，任一不满足即 **fail-closed 不删除**；严禁
  「查询状态 → 释放事务 → unlink」。
- **启动校验**：`asset-analysis-v1` 的 `asset_ids` 为 `None` 或 `[]` 均拒绝启动；
  启动前逐素材校验 `status == ready` + 正式文件为普通文件 + `size_bytes` 与
  SHA-256 一致（1 MiB 分块流式读取）。`TEST_PIPELINE` 仍允许无素材运行。

## 2. 文件布局

```
data/projects/{project_id}/
├── assets/{asset_id}/original/{safe_filename}     # 素材原始文件（不可覆盖）
├── artifacts/{artifact_revision_id}/{safe_filename}  # 正式产物（不可覆盖）
└── .attempts/{attempt_id}/                        # attempt 独占暂存（结束即清理）
```

## 3. 节点与产物 kind

| 节点 | 输入 | 产物 kind |
|---|---|---|
| `asset_probe` | 冻结 asset_ids（读 `assets/…/original/`） | `asset.probe.{asset_id}` |
| `asset_normalize` | **素材原始文件** `assets/{id}/original/…`（probe JSON 仅提供媒体事实，绝不作 FFmpeg 输入） | `asset.proxy.{asset_id}` |
| `scene_detect` | `asset.proxy.{id}` | `asset.scenes.{id}` + `asset.keyframe.{id}.{n}` |
| `speech_transcribe` | `asset.proxy.{id}` | `asset.asr.{id}` |
| `asset_analysis` | probe + scenes + asr + keyframes | `asset.analysis.{id}` |

DAG：`asset_probe → asset_normalize → {scene_detect, speech_transcribe} → asset_analysis`，
另加 `asset_probe → asset_analysis`（报告需要确定性媒体事实，任务书 DAG 的超集）。

**注册表**：`nodes/media_registry.py::build_media_registry(toolchain=…, detector=…, asr_adapter=…,
require_asr=…, funasr_model_path=…)` 注册上述五个 handler；默认实现为
`FFmpegToolchain` / `PySceneDetectAdapter` / （仅在提供本地模型路径时的）`FunASRAdapter`，
三者均可注入 fake 以便测试。`nodes/__init__.py` 与 `orchestrator/__init__.py` 均导出。

## 4. 媒体契约（Pydantic v2，extra=forbid）

`MediaAsset` / `MediaProbeReport` / `VideoStreamInfo` / `AudioStreamInfo` /
`TranscriptSegment` / `AssetSegment` / `AssetAnalysisReport` / `ToolCapabilityReport`

- 时间统一**整数微秒**，区间为半开区间 `[start_us, end_us)`；
- 图片时间字段为 `null`（不伪造时长）；
- 帧率/time_base 用有理数 `{"num","den"}`；
- 禁止 NaN/Infinity（转换层把非法 ffprobe 值归一为 `None`）；
- 所有报告携带 `schema_version`。

## 5. 工具层与 ASR

- `CommandRunner`：`shell=False` + 参数数组 + 轮询（超时/取消即 kill）+ 错误限长 2000 字符；
- **PIPE 排空**：子进程运行期间由两个后台线程持续排空 stdout/stderr，避免写满
  管道缓冲后互等（>64 KiB 输出已验证）；超时/取消后 `wait` 子进程并确认排空线程退出；
- 错误码：`TOOL_NOT_FOUND` / `TOOL_TIMEOUT` / `TOOL_CANCELLED` / `PROBE_FAILED` / `NORMALIZE_FAILED`；
- `FFmpegToolchain.ffprobe_json()` 只解析 JSON；
- 所有 FFmpeg/ffprobe 调用均接入 `context.cancel_check`（`asset_probe` /
  `asset_normalize` / `scene_detect` 的代理探测与关键帧 / `speech_transcribe`）；
- `SceneDetector` 协议 + `PySceneDetectAdapter`（延迟导入）；依赖/模型缺失 →
  稳定错误码 `SCENE_DEPENDENCY_MISSING`，不降级为 `HANDLER_EXCEPTION`；
- `AsrAdapter` 协议 + `FunASRAdapter`（延迟导入）；**只接受明确存在的本地模型目录**，
  拒绝远程模型别名与自动下载；依赖缺失 → `ASR_DEPENDENCY_MISSING`；
- 模块导入零副作用。

## 6. 依赖变化

| 依赖 | 用途 | 许可证 |
|---|---|---|
| `scenedetect` | 镜头切分 | BSD-3-Clause |
| `opencv-python-headless` | PySceneDetect 的视频后端 | Apache-2.0 |
| `funasr`（可选） | ASR | MIT（模型权重另计） |

已同步 `environment.yml`（conda 段）与 `pyproject.toml`（`media` / `media-asr` 可选依赖），
并把 `src/cybercut` 纳入 wheel 打包范围。

## 7. 已知限制

1. 本机无 ffmpeg/ffprobe → 真实媒体链路（probe/normalize/scene/keyframe/analysis 全链路）
   未能执行；端到端测试使用可注入 fake 实现，Linux 真实链路验收命令见
   `WB-02-test-report.md` §3。
2. FunASR/torch 未安装 → 真实 ASR 适配器仅做延迟导入、本地模型路径校验与失败语义验证。
3. 无说话人分离；未引入 WhisperX。
4. 崩溃孤儿 revision 目录允许存在，GC 留待后续阶段。
5. 媒体节点在单个 attempt 内逐素材串行处理，未做并行。
