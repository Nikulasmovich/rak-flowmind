# WB-04 · 测试报告（编辑规划与模板绑定层）

> 本报告记录 **WB-04（编辑规划与模板绑定层）** 的实际测试结果。所有数字来自本机实测输出。
> 承接 WB-03R2-复审基线：`477 passed, 1 skipped`。

| 项 | 值 |
|---|---|
| 阶段 | WB-04（编辑规划与模板绑定层） |
| 日期 | 2026-10-09 |
| 基线 commit | `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（未 commit、未 push） |
| 数据库 | PostgreSQL 18.6 @55432（数据目录 `D:\cybercut-pgdata-n2`，库 `cybercut_test`） |
| 测试 DSN | `postgresql://postgres@127.0.0.1:55432/cybercut_test` |
| 关联文档 | `ADR-0005-edit-planning.md` |

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
| `pytest tests/cybercut -q -W error::…ThreadExceptionWarning` | **548 passed, 1 skipped**，`0 failed`，`0 warnings`，耗时 **7311.05s（2:01:51）** | 0 |
| `ruff check src/cybercut tests/cybercut` | `All checks passed!` | 0 |
| `ruff check src` | `All checks passed!` | 0 |
| `git diff --check` | 无 diff 冲突 | 0 |

唯一 SKIP：`test_media_ingest.py::test_ingest_rejects_symlink`（Windows 无
`SeCreateSymbolicLinkPrivilege`，WB-02 既有用例，未新增 SKIP）。

## 2. WB-04 新增/调整测试分项

| 测试文件 | 用例数 | 说明 |
|---|---|---|
| `test_editing_contracts.py` | 41 | 契约 + 帧换算 + 模板清单/绑定（含 accepted 语义一致性） |
| `test_edit_nodes.py` | 14 | 编辑节点 handler 级 |
| `test_edit_pipeline_e2e.py` | 13 | 真实 PostgreSQL 端到端 |
| 既有 WB-03 测试适配 | — | 9→11 节点、6 kind 版本表、descendants 链扩展 |

## 2.1 WB-04 审计修正（accepted 语义）

`TemplateBinding.accepted` 语义明确为**整体可接受**：当且仅当 `unmet`
（阻断性未满足约束）为空；契约校验强制 `accepted ⇔ unmet 为空`。

| 约束 | 阻断（进入 unmet） | accepted |
|---|---|---|
| 画幅/帧率不支持、文案超限、转场不支持 | 是 | False |
| **必需**槽位为空且策略 `fail` | 是 | False |
| **必需**槽位为空且策略 `warn` | 否（仅告警） | True |
| **可选**槽位为空（任何策略） | 否（`OPTIONAL_SLOT_EMPTY` 告警） | True |

新增专项测试：可选槽位空不阻断、必需槽位空 warn/fail 分支、
帧率不支持阻断、转场不支持阻断、`accepted ⇔ unmet` 跨模板一致性。
FakeCreativeProvider 的 proof 旁白同步改为单条卖点事实（修正测试夹具超长文案）。

## 3. 覆盖要点

- 半开区间 `[in, out)` / `[start, end)`：非法区间拒绝、连续性校验、帧数=区间长度
- 帧率换算：整数 floor、约分校验、非整除偏差显式记录（`FRAME_ROUNDING_DRIFT`）
- 溯源：shot → match 选中 → AssetSegment → asset，每片段携带三层 artifact_revision_id
- 源区间越界（超出媒体时长）拒绝；素材短于目标时长如实缩短 + `SOURCE_TOO_SHORT`
- 图片：源区间恒 `[0,0)`，播放时长来自目标时长，`IMAGE_NO_SOURCE_DURATION` 告警
- 未匹配镜头 → `unresolved_shots` 显式保留，绝不凑数
- 确定性：相同冻结输入字节级一致（含 30000/1001 非整除帧率）
- 模板：三个清单 schema 有效、版本 1.0.0、确定性默认选择（template_id 升序）
- 绑定不改写：match 选择、事实性文案、时间线内容在绑定前后逐字节一致
- 缺槽策略：`warn` → accepted；`fail` → not accepted + `SLOT_UNFILLED`
- 文案超限：`TEXT_LIMIT_EXCEEDED` 结构化报告，绝不改写
- hybrid timeline gate：等待 → 批准 → 完成；驳回 → rejected + 不产生后续产物
- manual：六道闸门依次审批
- 局部修订：storyboard/match 修订级联失效 edit.plan/template.bind；
  template.bind 修订不触碰上游（attempt 数不变）
- 冻结重试不漂移；不回归项目锁序、租户约束、跨 Run 项目状态聚合

## 4. 声明

- 未实现渲染/字幕/TTS/BGM/混音/导出/QC/REST/SSE/MCP/前端（WB-05+）
- 未修改 `src/flowmind/`、`0001`–`0005` 迁移
- 未引入被禁组件；无真实 LLM 网络调用
- **未 commit、未 push**

## 5. 阶段收尾核对（WB-04 范围冻结）

| 核对项 | 结果 |
|---|---|
| `src/flowmind/` | `git status --porcelain src/flowmind/` 为空；基线 45 个 .py 文件逐一对照无差异 |
| 迁移 0001–0002（基线已跟踪） | `git diff HEAD --stat` 为空，零改动 |
| 迁移 0003–0005（WB-02/03 交付） | 未跟踪新文件，本阶段未触碰 |
| 迁移 0006（WB-04 交付） | 未跟踪新文件 |
| 18 个已跟踪改动 | 全部归属 WB-02–WB-04（媒体接入、创作层、编辑层、聚合/修订/审批），无意外文件 |
| 未跟踪（口径修正） | Git 状态记录 43 条；递归展开**实际 59 个文件**：docs 8、migrations 4、src 31（contracts 4 + creative 8 + editing 5 + media 6 + nodes 6 + storage 2）、tests 16，全部归属 WB-02–WB-04 |
| HEAD | `913093334a6692b9c5d5f4ba9ce9a5fde5afaf34`（未 commit、未 push） |
| `git diff --check` | exit 0 |
