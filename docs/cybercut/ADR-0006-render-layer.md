# ADR-0006 · 视频合成与渲染层（WB-05）

- 状态：已接受（WB-05）
- 基线：`913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`
- 关联：`contracts/render.py`、`rendering/`、`nodes/render_nodes.py`
- 承接：ADR-0005（编辑规划与模板绑定）、ADR-0004（创作层）

## 背景

WB-04 交付了冻结 TimelineSpec + TemplateBinding（`accepted ⇔ unmet 为空`）。
WB-05 将其转化为**真实可播放的 9:16 MP4 成片**。本阶段不实现 REST/SSE/MCP/前端。

## 决策

### 1. Provider 接口
`rendering/provider.py` 定义 `RenderProvider` 协议：`composite(request) → RenderResult`。
Provider 只做合成，**不得**改写镜头顺序、素材选择、入出点或文案。

首版实现为 `FFmpegRenderer`（基于 `FFmpegToolchain`）。
火山引擎暂缓（D2=B）；接口保留，后续可替换。

### 2. FFmpeg 合成策略
- 每个片段通过 `-ss / -to` 从源素材裁切，`scale` 适配画幅（fit/fill），拼接；
- 字幕/屏幕文字通过 `drawtext`（显式 fontfile，不依赖 fontconfig）；
- 旁白：本版无 TTS（D3=B），`narration_status = "not_available"`；
- BGM：用户未提供合法 BGM 时允许无 BGM 成片；
- 输出：H.264 (libx264) / AAC，1080x1920，帧率取 TimelineSpec.frame_rate。

### 3. QC 与交付
`render.qc` 独立节点（ffprobe 校验 + 封面导出）。QC 失败 ⇒ composite 的 MP4
不作为交付产物。QC 检查项：分辨率、帧率、时长偏差、音视频流存在、
黑帧（`blackdetect`）、异常静音（`silencedetect`）、字幕越界（drawtext 超界
在合成阶段即被 FFmpeg 裁切，不产生越界输出）。

### 4. 版本身份
与创作层一致：执行器在发布前分配 `artifact_revision_id` + `revision_no`；
`accepted` / `unmet` / TimelineSpec / TemplateBinding / 渲染器版本 / 关键参数
存入 ArtifactRevision `metadata_json`。

### 5. 不需要新 migration
渲染产物通过 ArtifactRevision 登记（`metadata_json` 承载结构化元数据）。
不建独立 QC 查询表（WB-05 通过 artifact kind `render.qc` 查询）。

### 6. 节点 DAG
```
template.bind → render.narration → render.composite → render.qc
```
- `render.narration`：TTS Provider 接口（本版 not_available）；
- `render.composite`：FFmpeg 合成（前置校验 accepted/unresolved_shots）；
- `render.qc`：QC + 封面导出。

### 7. 前置门槛（审计确定）
- `TemplateBinding.accepted == False` → `RENDER_BINDING_NOT_ACCEPTED`；
- `unresolved_shots` 非空 → `RENDER_UNRESOLVED_SHOTS`（列出 shot_id）；
- 冻结引用交叉验证（TimelineSpec / TemplateBinding 必须来自同一 Run）。

## 已知限制
1. 速度固定 1x（不重定时）；
2. 转场为 FFmpeg concat（硬切）或 drawtext 淡入淡出，不做高级动效；
3. 无 TTS/BGM 时不产生音频轨道（除非源素材自带音频）；
4. 封面为第一帧截取（`-frames:v 1`），不做智能封面选择；
5. 不做分布式渲染、GPU 加速、HDR。
