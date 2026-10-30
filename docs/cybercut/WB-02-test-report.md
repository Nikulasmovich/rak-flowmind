# WB-02 · 测试报告

> 本文档记录 **WB-02 → WB-02R → WB-02R2 → WB-02R3 → WB-02R4 → WB-02R5 → WB-02R6** 的实际测试结果。
> 所有数字均来自本机实测输出；未执行项一律显式标注为「未执行 / 环境阻塞」。

| 项 | 值 |
|---|---|
| 阶段 | WB-02（素材接入、媒体标准化与基础分析）+ R1–R6 收口 |
| 日期 | 2026-10-08 |
| 基线 | `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（WB-01R3 基线提交，未 commit / 未 push） |
| 数据库 | PostgreSQL 18.6 @55432（数据目录 `D:\cybercut-pgdata-r3`，库 `cybercut_test`，auth=trust） |
| 测试 DSN | `postgresql://postgres@127.0.0.1:55432/cybercut_test` |
| Python / pytest | 3.13.12（受管 venv）/ pytest 9.1.1 |
| 关联文档 | `WB-02-media-layer.md`、`ADR-0003-media-assets-and-analysis.md` |

## 1. 验收命令与结果（串行执行，实测输出）

```powershell
$py='C:\Users\黄嘉林\.workbuddy\binaries\python\envs\cybercut-wb00\Scripts\python.exe'
$env:CYBERCUT_TEST_PG_DSN='postgresql://postgres@127.0.0.1:55432/cybercut_test'
& $py -m pytest tests/cybercut -q -W error::pytest.PytestUnhandledThreadExceptionWarning
& $py -m ruff check src/cybercut tests/cybercut
& $py -m ruff check src
git diff --check
```

| 命令 | 实测结果 | 退出码 |
|---|---|---|
| `pytest tests/cybercut -q -W error::…ThreadExceptionWarning` | **275 passed, 1 skipped**，`0 failed`，`0 warnings`，耗时 **405.24s（6:45）** | 0 |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | 0 |
| `ruff check src` | `All checks passed!` | 0 |
| `git diff --check` | 无 diff 冲突（仅 `core.autocrlf` 的 LF→CRLF 提示） | 0 |

- 未处理线程异常以 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 提升为错误执行，结果 **0 error**。
- 唯一 SKIP：`test_media_ingest.py::test_ingest_rejects_symlink` —— 当前 Windows 环境无 `SeCreateSymbolicLinkPrivilege`，无法创建符号链接，测试内显式 `pytest.skip`，非失败。

## 2. 媒体层分项结果（WB-02 新增/重构的 6 个测试文件合并运行）

| 测试文件 | 用例数 | 通过 | 失败 | 跳过 |
|---|---|---|---|---|
| `test_media_contracts.py` | 23 | 23 | 0 | 0 |
| `test_media_ingest.py` | 20 | 19 | 0 | 1（符号链接权限） |
| `test_media_toolchain.py` | 10 | 10 | 0 | 0 |
| `test_media_nodes.py` | 10 | 10 | 0 | 0 |
| `test_media_pipeline_e2e.py` | 8 | 8 | 0 | 0 |
| `test_media_migration.py` | 7 | 7 | 0 | 0 |
| **合计** | **78** | **77** | **0** | **1** |

（全套件共收集 276 项 = 275 passed + 1 skipped；媒体层之外的 198 项为 WB-01 系列回归，全部通过。）

### 2.1 `test_media_nodes.py` 结果（10 passed）

```
test_probe_handler_publishes_one_report_per_asset            PASSED
test_normalize_reads_original_and_uses_expected_args         PASSED
test_scene_detect_publishes_monotonic_segments_and_keyframes PASSED
test_scene_detect_dependency_missing_maps_to_stable_code     PASSED
test_transcribe_without_audio_returns_empty_with_warning     PASSED
test_transcribe_missing_adapter_is_stable_error              PASSED
test_transcribe_dependency_missing_is_stable_error           PASSED
test_transcribe_normal_timestamps                            PASSED
test_analysis_merges_deterministic_facts                     PASSED
test_cancel_propagates_to_scene_detect                       PASSED
```

`test_cancel_propagates_to_scene_detect` 断言三件事：`ffprobe_json` 与关键帧 `run_ffmpeg` 均收到 `context.cancel_check`；取消映射为稳定错误码 `TOOL_CANCELLED`；取消后关键帧命令仅尝试 1 次（不继续生成后续关键帧）。

### 2.2 `test_media_pipeline_e2e.py` 结果（8 passed）

```
test_e2e_five_nodes_succeed            PASSED
test_e2e_per_asset_artifacts           PASSED
test_e2e_video_keyframes               PASSED
test_e2e_image_proxy_is_png            PASSED
test_e2e_analysis_report_pydantic      PASSED
test_e2e_normalize_input_is_original   PASSED
test_e2e_no_drift                      PASSED
test_registry_has_five_media_nodes     PASSED
```

经 PostgreSQL `Orchestrator` + `Worker` 跑完五节点，断言：五个 NodeRun 全部 `succeeded`；每个输入素材都产出 `asset.probe / asset.proxy / asset.scenes / asset.asr / asset.analysis`；视频每个镜头都有关键帧 revision；图片代理为 `.proxy.png`；最终 `AssetAnalysisReport` 通过 Pydantic 校验；下游 `assets` 未丢失；normalize 的 FFmpeg `-i` 输入位于素材 `original/` 正式区（非 probe JSON、非暂存区）；上游新增 revision 后旧 attempt 的 `input_revision_refs` 不漂移；registry 恰含五个媒体节点。

**说明：本文件的 E2E 使用可注入的 fake toolchain / fake scene detector / fake ASR adapter，不构成真实媒体链路验证。**

### 2.3 并发接入测试结果

| 用例 | 结果 | 断言要点 |
|---|---|---|
| `test_concurrent_same_content_yields_single_ready_asset` | PASSED | 4 线程 `Barrier` 同步；所有线程退出；`errors == []`；`len(results) == 4`；四个结果 `asset_id` 完全一致；恰好 1 个 `created=True`；数据库恰好一条 `ready`；正式文件存在且 SHA-256 正确 |
| `test_concurrent_takeover_of_stale_ingesting` | PASSED | 2 线程竞争同一 stale ingesting 素材；所有线程退出；未取得所有权者仅返回 `ASSET_TAKEOVER_CONFLICT`；`_publish` 调用次数 **≤ 1**（最多一个接管者发布）；最终恰好一条 `ready` 且文件哈希正确 |
| `test_ingest_recovers_when_official_file_already_exists` | PASSED | 「文件已移动、提交前崩溃」：锁 ingesting 行后重新校验大小与 SHA-256 → 恢复置 `ready`，既有文件字节完全未变 |
| `test_ingest_existing_target_content_mismatch_conflicts` | PASSED | 同长度但内容不符 → `ASSET_PUBLISH_CONFLICT`，目标文件未被覆盖，行仍为 `ingesting` |
| `test_ingest_takeover_of_stale_ingesting` / `test_ingest_unexpired_ingesting_conflicts` | PASSED | 超时接管成功；未超时的 ingesting 返回 `ASSET_INGESTING` |

### 2.4 大输出子进程测试结果

| 用例 | 结果 | 说明 |
|---|---|---|
| `test_large_output_no_deadlock` | PASSED | 子进程写 **200,000 字节** stdout（远超 64 KiB 管道缓冲），正常结束，`returncode == 0`，捕获长度 ≥ 200,000 |
| `test_large_stderr_no_deadlock` | PASSED | 同上，stderr 200,000 字节 |
| `test_real_timeout_path` / `test_real_cancel_path` | PASSED | 真实子进程超时 → `TOOL_TIMEOUT`；真实取消 → `TOOL_CANCELLED`；超时/取消后均 `wait` 子进程并确认两个排空线程退出 |

### 2.5 发布失败后的清理竞态（WB-02R6 §5，3 项全部 PASSED）

| 用例 | 结果 | 断言要点 |
|---|---|---|
| `test_cleanup_unlinks_only_inside_row_lock` | PASSED | 在 `os.unlink` 被调用的瞬间，从**另一条连接**以 `FOR UPDATE SKIP LOCKED` 探测该行，必须取不到 → 证明「确认归属 + 删除」处于**同一持锁事务**内；同时断言孤儿文件确实被回收 |
| `test_cleanup_fail_closed_when_identity_mismatch` | PASSED | 文件大小相同但 SHA-256 不符 ⇒ **fail-closed 不删除**，文件字节保持原样（§4） |
| `test_commit_failure_cleanup_never_deletes_other_taker_file` | PASSED | A 持锁发布 → A 的 `mark_ready` 抛错（等价提交失败）→ 在 A 的清理窗口内 B 接管并成功 ready → 最终行 `ready`、正式文件存在、SHA-256 正确，**A 未删除 B 的文件** |

**判别力实证**（独立脚本，非测试套件，验证后已删除）：

```
旧模式 unlink 时行是否被锁定 : False
新模式 unlink 时行是否被锁定 : True
判别成立（新=True 且 旧=False）: True
```

即 `test_cleanup_unlinks_only_inside_row_lock` 对「查询状态 → 释放事务 → unlink」的实现**必然失败**，构成有效的回归防护。

**实现要点**：`_PublishRecord` 固化每次发布的不可混淆身份（`publisher_token` + `asset_id`/`tenant_id`/`relative_path`/`pre_status` 行身份 + `dest`/`size_bytes`/`sha256` 文件身份）；`_discard_own_publish(record)` 全程在 `lock_row()`（`FOR UPDATE`）事务内完成三项确认，任一不满足即放弃回收。

## 3. 真实 ffmpeg / ffprobe / PySceneDetect / FunASR 执行情况

**本机全部未执行。** 实测探测结果：

```
ffmpeg:      NOT FOUND（PATH 中不存在）
ffprobe:     NOT FOUND
scenedetect: MISSING
cv2 (OpenCV): MISSING
funasr:      MISSING
torch:       MISSING
```

按 WB-02 §十一：**不安装任何系统软件**，未联网下载模型 / 素材 / 工具。因此：

- `test_media_pipeline_e2e.py` 的端到端链路 **全部基于 fake toolchain / fake detector / fake ASR**，**不是**真实媒体链路验证；
- `test_media_toolchain.py::test_real_timeout_path` / `test_real_cancel_path` 使用 `python -c` 构造真实子进程，验证的是**进程管理与取消/超时语义**，不涉及 ffmpeg 解码；
- `FunASRAdapter` 的真实转写、`PySceneDetectAdapter` 的真实镜头检测 **均未验证**。

在具备 ffmpeg 的环境复跑真实链路的验收命令（Linux）：

```bash
# 1) 安装系统依赖（本机未执行）
sudo apt-get update && sudo apt-get install -y ffmpeg
pip install 'scenedetect>=0.6,<0.7' 'opencv-python-headless>=4.9'

# 2) 生成约 3 秒、两段颜色 + 短音频的测试视频
ffmpeg -y -f lavfi -i "color=c=red:s=640x480:d=1.5" \
       -f lavfi -i "color=c=blue:s=640x480:d=1.5" \
       -f lavfi -i "sine=frequency=440:duration=3" \
       -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[v]" \
       -map "[v]" -map 2:a -c:v libx264 -pix_fmt yuv420p -r 30 \
       -c:a aac -ar 48000 -ac 2 -t 3 /tmp/wb02-real.mp4

# 3) 跑真实链路（需将可注入的 fake 实现替换为真实 toolchain/detector 后执行）
PYTHONPATH=src python -m pytest tests/cybercut/test_media_pipeline_e2e.py -q

# 4) 用 ffprobe 校验代理文件：h264 / yuv420p / 30fps / aac 48000Hz
ffprobe -v error -select_streams v:0 -show_entries \
  stream=codec_name,pix_fmt,avg_frame_rate -of json <proxy.mp4>
```

在该环境复跑通过之前，**不得宣称真实媒体链路已通过**——本报告不作出该声明。

## 4. 依赖与许可证变化

| 依赖 | 变更 | 声明位置 | 许可证 |
|---|---|---|---|
| `scenedetect` | 新增可选依赖 `media`（`>=0.6,<0.7`） | `pyproject.toml [project.optional-dependencies]`、`environment.yml` | BSD-3-Clause |
| `opencv-python-headless` | 新增可选依赖 `media`（`>=4.9`） | 同上 | Apache-2.0（OpenCV 4.x） |
| `funasr` | 新增可选依赖 `media-asr`（`>=1.1`） | 同上 | MIT（模型权重另有许可，需单独确认） |
| 打包范围 | wheel `packages` 增加 `src/cybercut` | `pyproject.toml` | — |

- 新增依赖均为**可选**，未加入运行时强制依赖；未引入 Redis / Celery / Kafka / Milvus / 工作流框架。
- OpenCV 采用 headless 变体，避免 GUI 运行库依赖。

## 5. 已知风险与未完成事项

1. **真实媒体链路未验证**（缺 ffmpeg）：部署前必须在具备 ffmpeg 的环境复跑，并核对代理文件编码 / 像素格式 / 帧率 / 音频采样率；
2. **FunASR 真实转写未验证**；适配器仅接受明确存在的本地模型目录，拒绝远程别名与自动下载；
3. **ArtifactRevision 产物 GC 未实现**：正式路径含 UUID 目录，孤儿目录需后续 GC 策略；
4. **无说话人分离**，未引入 WhisperX；
5. **媒体节点单进程串行执行**：单个 attempt 内逐素材顺序处理，未做并行；
6. **符号链接拒绝测试在 Windows 上 SKIP**：需在具备符号链接权限的环境（Linux/macOS）复跑；
7. **清理为 fail-closed**：当无法证明归属（他人已 ready、文件身份不符、状态已变更）时**刻意不删除**，可能留下孤儿文件——这是有意的取舍，孤儿由后续 GC 阶段处理；绝不删除其它接管者已成功发布的正式文件。

## 6. 声明

- **未进入 WB-03**；未实现 REST / SSE / MCP / FlowMind `@skill`；
- 未实现脚本、文案、智能分镜、素材语义匹配、Timeline、模板、字幕样式、TTS、BGM、渲染、成片 QC；
- **未 commit、未 push**；`git diff --check` 退出码 0；
- 未修改 `src/flowmind/`、`migrations/cybercut/0001_init.sql`、`0002_tenant_consistency.sql`；
- 未联网下载模型、素材或系统软件；
- 本报告不含任何未实际执行的「已通过」声明；所有未执行项均在第 3 节显式标注。
