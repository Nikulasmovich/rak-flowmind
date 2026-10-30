"""WB-02 媒体工具层：可注入的命令执行器与 ffmpeg/ffprobe 封装。

规则（WB-02 §六）：
- `subprocess` 一律 `shell=False`，参数以**数组**传递；
- 支持 `executable` 路径配置、`timeout`、**取消检查**（轮询 + kill）；
- 捕获 stdout/stderr，但错误信息**限长**（禁止无限写入数据库）；
- 错误码区分：`TOOL_NOT_FOUND` / `TOOL_TIMEOUT` / `TOOL_CANCELLED` /
  `PROBE_FAILED` / `NORMALIZE_FAILED`；
- ffprobe 只使用 `-print_format json -show_format -show_streams`，**不解析人类可读文本**；
- 模块导入时**不**探测环境、不启动进程、不下载任何内容。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cybercut.contracts.media import ToolCapabilityReport

logger = logging.getLogger(__name__)

#: 写入数据库前错误信息最大长度
MAX_ERROR_CHARS = 2000
DEFAULT_TIMEOUT_S = 120.0
#: 取消检查轮询间隔（秒）
POLL_INTERVAL_S = 0.1

CancelCheck = Callable[[], bool]


def truncate_error(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """限长错误信息（保留尾部，尾部通常包含 ffmpeg 的真实原因）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…[截断]…" + text[-limit:]


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[..., SubprocessResult]


def subprocess_runner(
    *, default_timeout: float = DEFAULT_TIMEOUT_S,
    cancel_check: CancelCheck | None = None, poll_interval: float = POLL_INTERVAL_S,
) -> CommandRunner:
    """构造命令执行器（`shell=False`，参数数组，轮询支持取消与超时）。"""

    def _run(args: list[str], *, timeout: float | None = None,
             cancel: CancelCheck | None = None) -> SubprocessResult:
        argv = [str(a) for a in args]
        deadline = time.monotonic() + (timeout if timeout is not None
                                       else default_timeout)
        try:
            proc = subprocess.Popen(
                argv, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            raise MediaToolError(
                "TOOL_NOT_FOUND", f"可执行文件不存在：{argv[0]}", stderr=str(exc)
            ) from exc

        # WB-02R §四.4：后台线程**持续排空** stdout/stderr，
        # 防止子进程写满管道缓冲区后与父进程互等（PIPE 死锁）。
        chunks: dict[str, list[str]] = {"out": [], "err": []}
        errors: dict[str, BaseException | None] = {"out": None, "err": None}

        def _drain(name: str, stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    chunks[name].append(line)
            except BaseException as exc:  # noqa: BLE001
                errors[name] = exc
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

        threads = [
            threading.Thread(target=_drain, args=("out", proc.stdout), daemon=True),
            threading.Thread(target=_drain, args=("err", proc.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        check_cancel = cancel or cancel_check
        timed_out = False
        cancelled = False
        while proc.poll() is None:
            if check_cancel is not None and check_cancel():
                cancelled = True
                proc.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                break
            time.sleep(poll_interval)

        for thread in threads:
            thread.join(timeout=10)
        proc.wait(timeout=10)
        for name, exc in errors.items():
            if exc is not None:  # pragma: no cover
                raise MediaToolError("PROBE_FAILED", f"读取{name}失败", stderr=str(exc))

        stdout, stderr = "".join(chunks["out"]), "".join(chunks["err"])
        if cancelled:
            raise MediaToolError("TOOL_CANCELLED", "命令被取消", stderr=stderr)
        if timed_out:
            raise MediaToolError(
                "TOOL_TIMEOUT",
                f"命令超时（>{timeout if timeout is not None else default_timeout}s）：{argv[0]}",
                stderr=stderr,
            )
        return SubprocessResult(proc.returncode, stdout, stderr)

    return _run


class MediaToolError(Exception):
    """媒体工具调用失败（`code` 为稳定错误码，`stderr` 已限长）。"""

    def __init__(self, code: str, message: str, *, stderr: str = "",
                 returncode: int | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stderr = truncate_error(stderr)
        self.returncode = returncode
        self.details = {"code": code, **(details or {})}


class FFmpegToolchain:
    """ffmpeg / ffprobe 封装（全部通过注入的 runner 调用，便于测试 fake）。"""

    def __init__(
        self, *, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe",
        runner: CommandRunner | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        self.ffmpeg_path = str(ffmpeg_path)
        self.ffprobe_path = str(ffprobe_path)
        self.timeout = float(timeout)
        self._cancel_check = cancel_check
        self._runner = runner or subprocess_runner(
            default_timeout=self.timeout, cancel_check=cancel_check
        )

    # ── 基础调用 ──

    def run_ffmpeg(self, args: list[str], *, timeout: float | None = None,
                   cancel_check: CancelCheck | None = None) -> SubprocessResult:
        """`cancel_check` 由调用方（NodeHandler）传入 context.cancel_check。"""
        """执行 ffmpeg（自动补 `-y -nostdin`），失败抛 `NORMALIZE_FAILED`。"""
        result = self._runner(
            [self.ffmpeg_path, "-hide_banner", "-nostdin", "-y", *args],
            timeout=timeout or self.timeout, cancel=cancel_check or self._cancel_check,
        )
        if result.returncode != 0:
            raise MediaToolError(
                "NORMALIZE_FAILED", f"ffmpeg 失败（rc={result.returncode}）",
                stderr=result.stderr, returncode=result.returncode,
            )
        return result

    def ffprobe_json(self, media_path: Path, *, timeout: float | None = None,
                     cancel_check: CancelCheck | None = None) -> dict[str, Any]:
        """ffprobe JSON 输出（`-print_format json -show_format -show_streams`）。"""
        probe_path = str(media_path).replace("\\", "/")
        result = self._runner(
            [self.ffprobe_path, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", probe_path],
            timeout=timeout or self.timeout,
            cancel=cancel_check or self._cancel_check,
        )
        if result.returncode != 0:
            raise MediaToolError(
                "PROBE_FAILED", f"ffprobe 失败（rc={result.returncode}）",
                stderr=result.stderr, returncode=result.returncode,
            )
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise MediaToolError(
                "PROBE_FAILED", f"ffprobe 输出不是合法 JSON：{exc}",
                stderr=(result.stdout or "")[:MAX_ERROR_CHARS],
            ) from exc
        if not isinstance(data, dict):
            raise MediaToolError("PROBE_FAILED", "ffprobe JSON 顶层不是对象")
        return data

    # ── 能力探测 ──

    def version(self, *, which: str = "ffmpeg") -> str | None:
        path = self.ffmpeg_path if which == "ffmpeg" else self.ffprobe_path
        try:
            result = self._runner([path, "-version"])
        except MediaToolError as exc:
            if exc.code == "TOOL_NOT_FOUND":
                return None
            raise
        if result.returncode != 0:
            return None
        lines = (result.stdout or "").splitlines()
        return lines[0].strip() if lines else None

    def encoders(self) -> list[str]:
        try:
            result = self._runner([self.ffmpeg_path, "-hide_banner", "-encoders"])
        except MediaToolError:
            return []
        names: list[str] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0][:1] in ("A", "V") and parts[1] != "--":
                names.append(parts[1])
        return names

    def filters(self) -> list[str]:
        try:
            result = self._runner([self.ffmpeg_path, "-hide_banner", "-filters"])
        except MediaToolError:
            return []
        names: list[str] = []
        for line in (result.stdout or "").splitlines():
            if line.startswith(("Filters:", "Segment", "  ")):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0][:1] in ("A", "V", "T", "."):
                names.append(parts[1])
        return names

    def capability_report(self) -> ToolCapabilityReport:
        """生成能力报告（供节点发布为正式产物；探测失败不抛异常）。"""
        from cybercut.contracts.media import ToolCapabilityReport

        ffmpeg_version = self.version(which="ffmpeg")
        ffprobe_version = self.version(which="ffprobe")
        available = ffmpeg_version is not None and ffprobe_version is not None
        return ToolCapabilityReport(
            ffmpeg_path=self.ffmpeg_path if available else None,
            ffprobe_path=self.ffprobe_path if available else None,
            ffmpeg_version=ffmpeg_version,
            ffprobe_version=ffprobe_version,
            encoders=self.encoders() if available else [],
            filters=self.filters() if available else [],
            available=available,
            missing_reason=None if available else "ffmpeg/ffprobe 不可用",
        )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FFmpegToolchain",
    "MAX_ERROR_CHARS",
    "MediaToolError",
    "SubprocessResult",
    "subprocess_runner",
    "truncate_error",
]
