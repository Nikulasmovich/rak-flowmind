# ADR-0003 · 素材资产、媒体标准化与基础分析

| 项 | 值 |
|---|---|
| 状态 | Accepted（WB-02 实施并验证） |
| 日期 | 2026-10-07 |
| 阶段 | WB-02（素材接入、媒体标准化与基础分析） |
| 上游 | `ADR-0002`（决策 1–18，其中 14 已被本篇决策 17 取代） |

## 背景
WB-01 完成后，下游 NodeHandler 无法拿到上游正式产物，也无法接收外部素材。
WB-02 需要打通「本地素材 → 登记 → 探测 → 标准化 → 结构化分析」的完整后端链路。

## 决策

### 1. Run 输入冻结（`runs.input_json`）
`start_run(asset_ids=[...])` 校验归属后把
`{"asset_ids": [...], "assets": [descriptor...]}` 冻结进 `runs.input_json`；
root attempt 的 `input_json` 携带同样内容。Run 一旦启动，输入不再受后续素材变化影响。

### 2. 上游 ArtifactRevision 固化
下游节点入队时，把**直接依赖**每个 `(node_run_id, kind)` 的最新 ArtifactRevision
（`artifact_revision_id/kind/relative_path/sha256/size_bytes`）固化进
`node_runs.input_revision_refs`（**只在首次入队时写入**），并复制进
`node_attempts.input_json.input_artifacts`。
重试复用冻结结果 ⇒ **上游产生新版本后旧 attempt 不漂移**；
handler 也**不允许**自行查询"最新产物"。

### 3. media_assets 表与去重
- `(project_id, sha256)` 唯一索引：同项目相同内容只产生一个逻辑 Asset（`ON CONFLICT DO NOTHING`）；
- `(project_id, tenant_id)` 复合外键 → projects（租户一致性由数据库强制）；
- 状态机 `ingesting → ready | failed`；恢复策略：
  - `ready` 且文件存在 → 复用；`ready` 但文件缺失 → 置 `failed/ASSET_FILE_MISSING`；
  - `ingesting` 未超时 → `ASSET_INGESTING`（另一进程进行中）；超时或 `failed` → **接管**；
  - 接管复用既有 `asset_id` 的确定性路径 ⇒ 永不覆盖既有原始文件。
- **接管原子性（WB-02R5 决策 19）**：接管在**单个数据库事务**内完成
  `lock_for_takeover`（`FOR UPDATE` + 状态 CAS）→ 校验所有权（未命中即
  `ASSET_TAKEOVER_CONFLICT`）→ 持锁期间重新检查正式目标 → 发布或确认文件 →
  `mark_ready` → 提交。禁止在释放行锁后再发布；禁止使用只匹配 `ready` 的行锁
  （`lock_ready` 已从代码中移除）。
  正式目标已存在时：大小与 SHA-256 **一致** ⇒ 判定为「文件已移动、提交前崩溃」的
  残留，直接恢复置 `ready`；**不一致** ⇒ `ASSET_PUBLISH_CONFLICT`，绝不覆盖。
  发布后提交失败 ⇒ 仅回收本次接管者新建的文件（在确认该行仍未 `ready` 的前提下），
  绝不删除历史 `ready` 文件。
- 接入流程：校验（普通文件/非符号链接/非空/拒绝 `..`）→ 复制进项目临时目录（边复制边哈希）
  → 原子移动到 `assets/{asset_id}/original/{safe_filename}`。

### 4. 产物暂存与发布（沿用 R3 模型）
handler 只写 `NodeContext.attempt_dir`，`ArtifactSpec.staged_relative_path` 必须在该目录内；
正式路径由执行器分配为 `artifacts/{artifact_revision_id}/{safe_filename}`。
handler 读取上游产物必须使用 `paths.resolve_official_artifact_path()`（仅允许
`artifacts/{revision}/{file}`，拒绝绝对路径/越界/其它前缀）。

### 5. 媒体工具层可注入
`FFmpegToolchain` 只通过注入的 `CommandRunner` 调用外部进程：
`shell=False` + 参数数组 + `timeout` + 取消检查（轮询 + kill）+ 错误信息限长（2000 字符）；
错误码 `TOOL_NOT_FOUND / TOOL_TIMEOUT / TOOL_CANCELLED / PROBE_FAILED / NORMALIZE_FAILED`；
ffprobe 仅解析 JSON；模块导入零副作用。

### 6. 标准化与镜头
- 视频代理：MP4/H.264/yuv420p/CFR 30fps/AAC 48k 双声道，`setsar=1`，
  `scale=w=1280:h=1280:force_original_aspect_ratio=decrease`（保比例、不裁剪、最长边 ≤1280）；
- 图片：ffmpeg 转 PNG（RGB、保比例）；
- 镜头：PySceneDetect `ContentDetector`（**不自行发明算法**），区间规整为
  有序/不重叠/不越界的半开区间；每个视频镜头提取一张中点关键帧；
- ASR：`AsrAdapter` 协议 + FunASR 延迟导入适配器；无音轨 → 空 transcript + `NO_AUDIO_STREAM`；
  依赖缺失 → `ASR_DEPENDENCY_MISSING` 明确失败（不伪造空结果）；
- 分析：`asset_analysis` **只合并**确定性事实（probe/镜头/关键帧/ASR），不调用 LLM。

### 7. DAG 扩展说明
在任务书 DAG 之外，`asset_analysis` **额外**依赖 `asset_probe`（报告必须包含确定性媒体事实）。
这是任务书要求边的**超集**，不缺失任何边。

### 8. 启动契约与引用形状的唯一权威（WB-02R5 决策 20）
- `contracts/run_input.py` 是 `StartRunInput`（含 `pipeline_version` /
  `requested_from_node` / `asset_ids`）与 `ArtifactRevisionRef` 的**唯一权威定义**；
  `contracts/models.py` 仅导入并重新导出，不再重复定义；
- `NodeRun.input_revision_refs` 的类型为 `list[ArtifactRevisionRef]`（默认 `[]`，
  数据库默认写 `[]` 而非 `{}`）；
- `node_runs.set_input_revision_refs` 在写入前逐项经 `ArtifactRevisionRef.model_validate`
  校验；`ArtifactRevisionRef.sha256` 必须是 **64 位十六进制字符**（大小写归一为小写）。
- 每个 attempt（root 与下游）都携带 Run 冻结的 `asset_ids` / `assets`；下游额外携带
  直接依赖节点的冻结 ArtifactRevision。启动前逐素材校验
  `status == ready` + 正式文件存在 + `size_bytes` 与 SHA-256 一致（1 MiB 分块流式读取）。

### 9. 协作式取消传播（WB-02R5 决策 21）
`SceneDetectHandler` 的代理 `ffprobe_json` 与关键帧 `run_ffmpeg` 均传入
`context.cancel_check`；取消被映射为稳定错误码 `TOOL_CANCELLED`，不得降级为
`HANDLER_EXCEPTION`，且取消后不得继续执行后续关键帧命令。

### 10. 发布失败后的清理归属证明（WB-02R6 决策 22）
发布失败（事务提交/`mark_ready` 失败）后的文件回收**禁止**采用
「查询状态 → 释放事务 → unlink」——该序列无法排除「另一接管者在窗口内完成
ready 发布，随后被误删」的竞态。改为：

- 每次发布生成 `_PublishRecord`（不可混淆身份）：`publisher_token` +
  行身份（`asset_id`/`tenant_id`/`relative_path`/`pre_status`）+
  文件身份（`dest`/`size_bytes`/`sha256`）；
- 回收全程在**同一持锁事务**内（`media_assets.lock_row()`，`FOR UPDATE` 不限状态）
  完成三项确认：
  1. 该行仍处于本次失败事务的**发布前状态**（非 `ready`、非终态）；
  2. `source_relative_path` 仍等于本次发布的正式路径；
  3. 磁盘文件的大小与 SHA-256 与本次发布记录一致；
- **任一不满足 ⇒ fail-closed，不删除**（宁可留下孤儿文件由 GC 处理，绝不删除
  其它接管者已成功发布的正式文件）。

`lock_for_takeover` 带状态过滤，无法表达「行已 ready」分支，故为清理单独提供
`lock_row`。

## 后果
- 产物路径含 UUID 目录 ⇒ 崩溃孤儿目录可保留待 GC；
- 媒体链路依赖 ffmpeg/PySceneDetect/FunASR：缺失时相关真实集成测试 SKIP 并明确报告；
- 媒体契约全部为整数微秒 + 有理数，禁止浮点秒与 NaN/Infinity；
- 接管为单事务原子操作 ⇒ 并发接管者中最多一个能发布，其余明确失败或复用；
- 清理 fail-closed ⇒ 极端情况下可能残留孤儿文件；这是以「绝不误删」换取空间的自愿取舍。
