"""项目目录与产物路径安全（WB-01 §八 / WB-01R3）。

规则：
- 项目根目录：`{data_root}/projects/{project_id}/`
- **暂存**：handler 只能写 `{project_root}/.attempts/{attempt_id}/`；
  `ArtifactSpec.staged_relative_path` 必须是该目录内的相对路径（越界即拒绝）。
- **正式**：执行器分配 `{project_root}/artifacts/{artifact_revision_id}/{safe_filename}`，
  入库的 `relative_path` 即该正式路径（不可变、不覆盖）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from cybercut.contracts.errors import PathEscapeError, ValidationError

#: data_root 环境变量（未设置时回落到仓库内 .cybercut-data，便于本地与测试）
DATA_ROOT_ENV = "CYBERCUT_DATA_ROOT"
_DEFAULT_DATA_ROOT = Path(".cybercut-data")

#: attempt 独占暂存目录名（不属于正式产物区）
ATTEMPTS_DIRNAME = ".attempts"


def default_data_root() -> Path:
    """数据根目录（env -> 默认），始终返回绝对路径。"""
    raw = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_DATA_ROOT


def projects_root(data_root: Path | str | None = None) -> Path:
    """projects 根目录：`{data_root}/projects`。"""
    base = Path(data_root) if data_root is not None else default_data_root()
    return (base / "projects").resolve()


def project_root(project_id: str, data_root: Path | str | None = None) -> Path:
    """单个项目根目录（未创建）。"""
    if not project_id or "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
        raise ValidationError(f"非法 project_id：{project_id!r}")
    return projects_root(data_root) / project_id


def ensure_project_dir(project_id: str, data_root: Path | str | None = None) -> Path:
    """确保项目根目录存在（幂等），返回绝对路径。"""
    root = project_root(project_id, data_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_artifact_path(
    project_root_path: Path | str, relative_path: str
) -> Path:
    """把 relative_path 解析为项目根内的绝对路径；越界抛 `PathEscapeError`。

    - 拒绝绝对路径与盘符（Windows `C:\\` / POSIX `/`）；
    - 拒绝 `..` 穿越（resolve 后前缀校验）；
    - 允许文件尚不存在（登记前由 handler 写入）。
    """
    if not relative_path or not relative_path.strip():
        raise ValidationError("relative_path 不能为空")
    rel = relative_path.replace("\\", "/").strip()
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise PathEscapeError(f"relative_path 不允许绝对路径：{relative_path!r}")

    root = Path(project_root_path).resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            f"relative_path 逃出项目目录：{relative_path!r}（root={root}）"
        ) from exc
    return candidate


def relative_to_project(project_root_path: Path | str, path: Path | str) -> str:
    """绝对路径 -> 相对项目根的 POSIX 形式（越界抛 PathEscapeError）。"""
    root = Path(project_root_path).resolve()
    target = Path(path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError as exc:
        raise PathEscapeError(f"路径不在项目目录内：{target}") from exc


# ══════════════════════════════════════════════════════════════════
# attempt 独占工作目录与所有权（WB-01R2 §一）
# ══════════════════════════════════════════════════════════════════


def attempt_dir(project_root_path: Path | str, attempt_id: str) -> Path:
    """attempt 的**独占**工作目录：`{project_root}/.attempts/{attempt_id}/`。

    该目录是「文件归属」的显式锚点（不依赖文件名启发式）：
    目录内文件一律归本 attempt 所有；该目录在 attempt 结束时整体清理。
    """
    if not attempt_id or "/" in attempt_id or "\\" in attempt_id or attempt_id in {".", ".."}:
        raise ValidationError(f"非法 attempt_id：{attempt_id!r}")
    return Path(project_root_path).resolve() / ATTEMPTS_DIRNAME / attempt_id


def ensure_attempt_dir(project_root_path: Path | str, attempt_id: str) -> Path:
    """创建并返回 attempt 独占工作目录（幂等）。"""
    root = attempt_dir(project_root_path, attempt_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_within_attempt_dir(
    path: Path | str, project_root_path: Path | str, attempt_id: str
) -> bool:
    """`path` 是否位于本 attempt 的独占目录内（resolve 后前缀校验）。"""
    try:
        target = Path(path).resolve()
        root = attempt_dir(project_root_path, attempt_id)
    except (ValidationError, OSError):
        return False
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


# ══════════════════════════════════════════════════════════════════
# 暂存 -> 正式发布路径（WB-01R3）
# ══════════════════════════════════════════════════════════════════

#: 正式产物区（相对 project_root）
ARTIFACTS_DIRNAME = "artifacts"
#: 安全文件名允许的字符（其余替换为 `_`）
_SAFE_CHAR = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]")
_MAX_FILENAME = 128


def sanitize_filename(name: str) -> str:
    """把 staged 文件名规约为**安全文件名**（无路径分隔、无盘符、长度受限）。

    仅保留字母/数字/CJK/`. _ -`；去除首部 `.`；空则回落到 `artifact.bin`。
    """
    base = PurePosixPath(str(name).replace("\\", "/")).name
    cleaned = _SAFE_CHAR.sub("_", base).lstrip(".")
    cleaned = cleaned[:_MAX_FILENAME]
    if not cleaned or cleaned in {".", ".."}:
        return "artifact.bin"
    return cleaned


def revision_dir(project_root_path: Path | str, artifact_revision_id: str) -> Path:
    """正式不可变产物目录：`{project_root}/artifacts/{artifact_revision_id}/`。"""
    if not artifact_revision_id or any(
        ch in artifact_revision_id for ch in ("/", "\\", "..")
    ):
        raise ValidationError(f"非法 artifact_revision_id：{artifact_revision_id!r}")
    return Path(project_root_path).resolve() / ARTIFACTS_DIRNAME / artifact_revision_id


def artifact_publish_path(
    project_root_path: Path | str, artifact_revision_id: str, filename: str
) -> Path:
    """执行器分配的**正式不可变路径**：`artifacts/{artifact_revision_id}/{safe_filename}`。"""
    return revision_dir(project_root_path, artifact_revision_id) / sanitize_filename(filename)


# ══════════════════════════════════════════════════════════════════
# 正式路径解析（WB-02 §三.8：只允许正式区，拒绝越界/绝对路径/其它前缀）
# ══════════════════════════════════════════════════════════════════

#: 素材原始文件区（相对 project_root）
ASSETS_DIRNAME = "assets"


def _resolve_within(prefix: Path, relative_path: str) -> Path:
    raw = str(relative_path or "")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive:
        raise PathEscapeError(f"路径不得为绝对路径：{raw!r}")
    target = (prefix / candidate).resolve()
    try:
        target.relative_to(prefix)
    except ValueError as exc:
        raise PathEscapeError(f"路径越出允许前缀：{raw!r}") from exc
    return target


def resolve_official_artifact_path(
    project_root_path: Path | str, relative_path: str
) -> Path:
    """解析 handler 读取的**正式 Artifact** 路径。

    `relative_path` 相对项目根，且必须位于
    `artifacts/{artifact_revision_id}/{filename}` 之内（WB-02 §三.8）；
    绝对路径、`..` 越界与**非 artifacts/ 前缀**一律拒绝。
    """
    root = Path(project_root_path).resolve()
    target = _resolve_within(root, relative_path)
    rel = target.relative_to(root).as_posix()
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != ARTIFACTS_DIRNAME:
        raise PathEscapeError(
            f"正式 Artifact 路径必须为 artifacts/{{revision}}/{{file}}：{relative_path!r}"
        )
    return target


def resolve_official_asset_path(
    project_root_path: Path | str, source_relative_path: str
) -> Path:
    """解析 handler 读取的**素材原始文件**路径。

    `source_relative_path` 相对项目根，且必须位于
    `assets/{asset_id}/original/{filename}` 之内（WB-02 §四）。
    """
    root = Path(project_root_path).resolve()
    target = _resolve_within(root, source_relative_path)
    rel = target.relative_to(root).as_posix()
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != ASSETS_DIRNAME or parts[2] != "original":
        raise PathEscapeError(
            f"素材路径必须为 assets/{{asset_id}}/original/{{file}}：{source_relative_path!r}"
        )
    return target


def asset_original_path(
    project_root_path: Path | str, asset_id: str, filename: str
) -> Path:
    """素材原始文件正式路径：`assets/{asset_id}/original/{safe_filename}`。"""
    if not asset_id or any(ch in asset_id for ch in ("/", "\\", "..")):
        raise ValidationError(f"非法 asset_id：{asset_id!r}")
    return (
        Path(project_root_path).resolve() / ASSETS_DIRNAME / asset_id
        / "original" / sanitize_filename(filename)
    )


def resolve_staged_path(attempt_dir_path: Path | str, staged_relative_path: str) -> Path:
    """解析 staged 相对路径 -> `attempt_dir` 内的绝对路径（越界即拒绝）。

    WB-01R3 §一：handler **只能**把待发布产物写入当前 attempt 的独占暂存目录；
    本函数是唯一的准入校验点（拒绝绝对路径、盘符、`..` 与任何越界解析）。
    """
    raw = str(staged_relative_path or "")
    if not raw:
        raise ValidationError("staged_relative_path 不能为空")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.drive:
        raise PathEscapeError(f"staged 路径不得为绝对路径：{raw!r}")
    root = Path(attempt_dir_path).resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            f"staged 路径越出 attempt 独占目录：{raw!r}"
        ) from exc
    return target


__all__ = [
    "ARTIFACTS_DIRNAME",
    "ASSETS_DIRNAME",
    "ATTEMPTS_DIRNAME",
    "DATA_ROOT_ENV",
    "artifact_publish_path",
    "asset_original_path",
    "attempt_dir",
    "default_data_root",
    "ensure_attempt_dir",
    "ensure_project_dir",
    "is_within_attempt_dir",
    "project_root",
    "projects_root",
    "relative_to_project",
    "resolve_artifact_path",
    "resolve_official_artifact_path",
    "resolve_official_asset_path",
    "resolve_staged_path",
    "revision_dir",
    "sanitize_filename",
]
