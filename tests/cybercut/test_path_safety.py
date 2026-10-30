"""路径安全单元测试（不依赖数据库）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from cybercut.contracts.errors import PathEscapeError, ValidationError
from cybercut.storage import paths


def test_project_root_layout(data_root: Path) -> None:
    root = paths.project_root("proj-1", data_root)
    assert root == (data_root / "projects" / "proj-1").resolve()


def test_ensure_project_dir_is_idempotent(data_root: Path) -> None:
    a = paths.ensure_project_dir("p", data_root)
    b = paths.ensure_project_dir("p", data_root)
    assert a == b and a.is_dir()


@pytest.mark.parametrize("bad_id", ["", "../evil", "a/b", "a\\b", ".", ".."])
def test_illegal_project_id_rejected(data_root: Path, bad_id: str) -> None:
    with pytest.raises(ValidationError):
        paths.project_root(bad_id, data_root)


def test_normal_relative_path_resolves(data_root: Path) -> None:
    root = paths.ensure_project_dir("p", data_root)
    resolved = paths.resolve_artifact_path(root, "artifacts/a.json")
    assert resolved == (root / "artifacts" / "a.json").resolve()


@pytest.mark.parametrize(
    "evil",
    [
        "../outside.json",
        "artifacts/../../outside.json",
        "/etc/passwd",
        "C:/Windows/system32/drivers/etc/hosts",
        "..\\outside.json",
        "",
    ],
)
def test_traversal_and_absolute_paths_rejected(data_root: Path, evil: str) -> None:
    root = paths.ensure_project_dir("p", data_root)
    with pytest.raises((PathEscapeError, ValidationError)):
        paths.resolve_artifact_path(root, evil)


def test_relative_to_project_roundtrip(data_root: Path) -> None:
    root = paths.ensure_project_dir("p", data_root)
    target = paths.resolve_artifact_path(root, "artifacts/x.bin")
    assert paths.relative_to_project(root, target) == "artifacts/x.bin"


def test_relative_to_project_rejects_outside(data_root: Path) -> None:
    root = paths.ensure_project_dir("p", data_root)
    with pytest.raises(PathEscapeError):
        paths.relative_to_project(root, data_root / "elsewhere.bin")


# ── WB-01R3：暂存路径与执行器分配正式路径 ──


def test_staged_path_inside_attempt_dir(data_root: Path) -> None:
    root = paths.ensure_project_dir("p", data_root)
    workdir = paths.ensure_attempt_dir(root, "att-1")
    resolved = paths.resolve_staged_path(workdir, "sub/out.json")
    assert resolved == (workdir / "sub" / "out.json").resolve()
    assert paths.is_within_attempt_dir(resolved, root, "att-1")


@pytest.mark.parametrize(
    "evil",
    ["../escape.json", "a/../../escape.json", "/etc/passwd",
     "C:/Windows/evil.json", "", ".."],
)
def test_staged_path_escape_rejected(data_root: Path, evil: str) -> None:
    root = paths.ensure_project_dir("p", data_root)
    workdir = paths.ensure_attempt_dir(root, "att-1")
    with pytest.raises((PathEscapeError, ValidationError)):
        paths.resolve_staged_path(workdir, evil)


def test_artifact_publish_path_is_executor_owned(data_root: Path) -> None:
    root = paths.ensure_project_dir("p", data_root)
    rev = "11111111-2222-3333-4444-555555555555"
    dest = paths.artifact_publish_path(root, rev, "../../evil name.json")
    assert dest.parent == paths.revision_dir(root, rev)
    assert dest.parent.parent == root / paths.ARTIFACTS_DIRNAME
    assert dest.name == "evil_name.json"
    assert paths.relative_to_project(root, dest) == (
        f"artifacts/{rev}/evil_name.json"
    )


@pytest.mark.parametrize("bad_id", ["", "../x", "a/b", "a\\b"])
def test_revision_dir_rejects_bad_id(data_root: Path, bad_id: str) -> None:
    root = paths.ensure_project_dir("p", data_root)
    with pytest.raises(ValidationError):
        paths.revision_dir(root, bad_id)
