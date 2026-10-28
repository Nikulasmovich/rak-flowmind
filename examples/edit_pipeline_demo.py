"""edit_* 技能演示 —— Agent 智能视频剪辑全流程（mock 流水线）。

运行：PYTHONPATH=<repo>/src python examples/edit_pipeline_demo.py

展示：
1. 全自动：素材 → 理解→文案→脚本→分镜→渲染 → 成片（mp4+SRT+中间产物）
2. 审批回路：分镜闸暂停 → revise（带新提示词重跑）→ 再暂停 → approve → 完成
3. 错误路径：URL 素材被拒（VALIDATION）；不存在的文件 → 项目 failed

mock 方式：
- FLOWMIND_EDIT_MOCK=1：创意 LLM 走内置桩；渲染走 ffmpeg lavfi 真渲染
  （占位成片，验证时间线/字幕/编码全链路）——需要本机 ffmpeg；
- patch 技能模块级 get_task_manager 为内存 fake（任务内联执行），
  不依赖 PG / MQTT；项目存储走文件回落（data_dir/editing/）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── demo 环境（必须先于 flowmind 导入设置）──
os.environ["FLOWMIND_ALLOW_ANY_PATH"] = "1"   # 仅 demo 放行沙箱（生产禁设）
os.environ["FLOWMIND_EDIT_MOCK"] = "1"        # 创意 LLM + 渲染走桩

_TMP = Path(tempfile.mkdtemp(prefix="flowmind-edit-demo-"))
_DATA = _TMP / "data"
_DATA.mkdir(parents=True, exist_ok=True)
# 独立 config：data_dir 指向 demo 临时目录（存储回落与产物都落这里）
(_TMP / "flowmind.config.toml").write_text(
    '[localizer]\n'
    f'data_dir = "{_DATA.as_posix()}"\n'
    'max_pending_tasks = 100\n'
    'task_ttl_seconds = 86400\n'
    '[editing]\n'
    'mock = true\n'
    'subtitle_font = "Arial"\n',
    encoding="utf-8")
os.chdir(_TMP)          # get_config 读 cwd 下的 flowmind.config.toml

import flowmind.skills.edit_project as eproj  # noqa: E402
import flowmind.skills.edit_review as erev  # noqa: E402
from flowmind.skill import invoke  # noqa: E402
from flowmind.tasks import (  # noqa: E402
    TaskContext,
    reset_task_context,
    set_task_context,
)


def section(title: str) -> None:
    print(f"\n{'─' * 64}\n  {title}\n{'─' * 64}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ── 内存 fake：submit 内联执行 pipeline（带 TaskContext 进度收集）──

class _FakeManager:
    def __init__(self) -> None:
        self._seq = 0
        self.progress_events: list[tuple[str, float, str]] = []

    def submit(self, skill_id: str, args: dict, tenant_id=None) -> str:
        self._seq += 1
        tid = f"fake-task-{self._seq:03d}"
        ctx = TaskContext(
            task_id=tid,
            workdir=_DATA / "tasks" / tid / "work",
            data_dir=_DATA,
            progress_cb=lambda stage, pct, message="": self.progress_events.append(
                (stage, round(pct, 1), message)),
            cancel_check=lambda: False,
        )
        token = set_task_context(ctx)
        try:
            result = invoke(skill_id, args)
            print(f"    [inline {tid}] {skill_id} → ok={result.ok} "
                  f"degraded={result.metrics.degraded}")
        finally:
            reset_task_context(token)
        return tid


FAKE = _FakeManager()
eproj.get_task_manager = lambda: FAKE
erev.get_task_manager = lambda: FAKE


def _make_assets() -> tuple[str, str]:
    """生成 demo 素材：3s 测试视频 + 1 张图片（ffmpeg lavfi）。"""
    video = _TMP / "sample.mp4"
    image = _TMP / "sample.png"
    for cmd in (
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=15:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:duration=1", "-frames:v", "1",
         str(image)],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            sys.exit("demo 需要本机 ffmpeg（mock 渲染走真实编码）；请先安装")
    return str(video), str(image)


def _pdir(project_id: str) -> Path:
    return _DATA / "editing" / project_id


# ════════════════════════ 场景 1：全自动 ════════════════════════

def scenario_full_auto(video: str, image: str) -> None:
    section("场景 1：全自动（无审批闸）—— 素材 → 成片")
    r = invoke("edit_project", {
        "assets": [video, image],
        "theme": "测试产品快剪",
        "style": "明快",
        "target_duration_s": 10.0,
        "aspect_ratio": "16:9",
        "review_stages": [],          # 空 = 全自动直通
        "extra_brief": "demo 用",
    })
    check(r.ok, f"edit_project 应成功：{r.error}")
    pid = r.data.project_id
    print(f"    project_id={pid} task_id={r.data.task_id}")

    sr = invoke("edit_status", {"project_ids": [pid]})
    st = sr.data.projects[0]
    print(f"    state={st.state} stage={st.stage} outputs={list(st.outputs)}")
    check(st.state == "succeeded", f"全自动应到 succeeded，实际 {st.state}")
    check(all(st.stages[s] == "done" for s in
              ("understand", "copy", "script", "storyboard", "render")),
          f"阶段未全部 done：{st.stages}")
    check("output.mp4" in st.outputs, "缺少成片 output.mp4")
    pdir = _pdir(pid)
    for name in ("understand.json", "copy.json", "script.json",
                 "storyboard.json", "timeline.json", "output.srt"):
        check((pdir / name).exists(), f"缺少中间产物 {name}")
    check((pdir / "output.mp4").stat().st_size > 1024, "成片文件过小")
    events = [e for e in FAKE.progress_events if "done" in e[2]]
    check(any("stage_done" in e[2] for e in events), "无阶段完成事件")
    print(f"    进度事件共 {len(FAKE.progress_events)} 条（样例 3 条）：")
    for e in FAKE.progress_events[-3:]:
        print(f"      {e}")


# ════════════════════════ 场景 2：审批回路 ════════════════════════

def scenario_review(video: str) -> None:
    section("场景 2：审批回路 —— 分镜闸暂停 → revise 重跑 → approve → 完成")
    r = invoke("edit_project", {
        "assets": [video],
        "theme": "带审批的剪辑",
        "review_stages": ["storyboard"],
    })
    check(r.ok, f"edit_project 应成功：{r.error}")
    pid = r.data.project_id

    st = invoke("edit_status", {"project_ids": [pid]}).data.projects[0]
    check(st.state == "awaiting_review", f"应停在 awaiting_review：{st.state}")
    check(st.review and st.review["gate"] == "storyboard",
          f"闸门应为 storyboard：{st.review}")
    print(f"    暂停于闸门 {st.review['gate']}；摘要：{st.review['summary']}")

    # revise：带新提示词打回分镜重跑 → 重新回到同一闸（还没 approve）
    rr = invoke("edit_review", {
        "project_id": pid, "stage": "storyboard", "action": "revise",
        "feedback": "镜头节奏加快，多用特写"})
    check(rr.ok and rr.data.task_id, f"revise 应提交续跑：{rr.error}")
    st = invoke("edit_status", {"project_ids": [pid]}).data.projects[0]
    check(st.state == "awaiting_review", "revise 后应重新停在审批闸")
    sb = (_pdir(pid) / "storyboard.json").read_text(encoding="utf-8")
    check("【修订1】" in sb, "revise 反馈未体现在重跑产物里")
    print("    revise 生效：分镜 note 带修订标记，闸门再次暂停 ✓")

    # approve：放行 → 渲染 → 完成
    ar = invoke("edit_review", {
        "project_id": pid, "stage": "storyboard", "action": "approve"})
    check(ar.ok, f"approve 应成功：{ar.error}")
    st = invoke("edit_status", {"project_ids": [pid]}).data.projects[0]
    check(st.state == "succeeded", f"approve 后应完成：{st.state}")
    check("output.mp4" in st.outputs, "approve 后缺少成片")
    print("    approve 放行 → 渲染完成 ✓")


# ════════════════════════ 场景 3：错误路径 ════════════════════════

def scenario_errors(video: str) -> None:
    section("场景 3：错误路径 —— URL 拒绝 / 素材不可读")
    bad = invoke("edit_project", {
        "assets": ["https://example.com/a.mp4"], "theme": "x"})
    check(not bad.ok and bad.error.code == "VALIDATION",
          f"URL 素材应 VALIDATION：{bad.error}")
    print(f"    URL 拒绝 ✓（{bad.error.message[:60]}…）")

    missing = _TMP / "not_exists.mp4"
    r = invoke("edit_project", {
        "assets": [str(missing)], "theme": "坏素材",
        "review_stages": []})
    check(r.ok, "提交本身应受理（失败发生在任务内）")
    st = invoke("edit_status", {"project_ids": [r.data.project_id]}
                ).data.projects[0]
    check(st.state == "failed", f"坏素材项目应 failed：{st.state}")
    print(f"    素材不可读 → 项目 failed ✓（error：{st.error[:70]}…）")


def main() -> None:
    section("环境")
    print(f"    demo 临时目录：{_TMP}")
    print(f"    mock={os.environ['FLOWMIND_EDIT_MOCK']} "
          f"sandbox_bypass={os.environ['FLOWMIND_ALLOW_ANY_PATH']}")
    video, image = _make_assets()
    print(f"    素材：{video} / {image}")

    scenario_full_auto(video, image)
    scenario_review(video)
    scenario_errors(video)

    section("ALL PASS ✅")


if __name__ == "__main__":
    main()
