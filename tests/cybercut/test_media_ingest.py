"""WB-02 素材接入测试：校验、哈希、去重、恢复、发布冲突（真实 PostgreSQL）。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from cybercut.media.ingest import (
    ASSET_FILE_MISSING,
    ASSET_INGESTING,
    SOURCE_EMPTY,
    SOURCE_INVALID,
    SOURCE_SYMLINK,
    AssetIngestor,
    IngestError,
    ingest_asset,
)
from cybercut.storage import media_assets, projects
from cybercut.storage.db import Database
from tests.cybercut.conftest import resolve_test_dsn
from tests.cybercut.helpers import TENANT

PROJECT = "proj-ingest"


@pytest.fixture
def source_root(data_root):
    path = data_root / "ingest-src"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def video_file(source_root) -> str:
    return _make_source(source_root)


@pytest.fixture
def sink(db, data_root):
    """创建默认项目并返回 `run(source, project_id=None)` 与 `data_root`、`pid`。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name=PROJECT)
    pid = project["project_id"]

    def _run(source, *, project_id: str | None = None, tenant=TENANT, kind=None,
             stale_after_s=900):
        """project_id 为 None → 默认项目；否则以该值作为**名称**新建项目。"""
        if project_id is None:
            project_id = pid
        else:
            with db.transaction() as conn:
                created = projects.create(conn, tenant_id=tenant, name=project_id)
            project_id = created["project_id"]
        return AssetIngestor(db, stale_after_s=stale_after_s).ingest(
            project_id=project_id, tenant_id=tenant, source_path=source,
            media_kind=kind, data_root=data_root,
        )

    return _run, data_root, pid


def _make_source(root, payload: bytes = b"FAKE-VIDEO-PAYLOAD-1",
                 name: str = "clip.mp4") -> str:
    path = Path(root) / f"{uuid.uuid4().hex[:8]}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


def _rows(data_root, project_id: str) -> list[dict]:
    database = Database(resolve_test_dsn())
    with database.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id, status, sha256, source_relative_path, size_bytes"
                "  FROM cybercut.media_assets WHERE project_id = %s"
                " ORDER BY created_at",
                (project_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def _project_root(data_root, project_id: str):
    return data_root / "projects" / project_id


def test_ingest_creates_ready_asset_with_expected_layout(sink, video_file) -> None:
    run, data_root, pid = sink
    asset, created = run(video_file)

    assert created is True
    assert asset["status"] == "ready"
    assert asset["media_kind"] == "video"
    assert asset["original_filename"].endswith("clip.mp4")
    assert asset["source_relative_path"].startswith(
        f"assets/{asset['asset_id']}/original/"
    )
    assert asset["source_relative_path"].endswith("clip.mp4")
    published = _project_root(data_root, pid) / asset["source_relative_path"]
    assert published.is_file()
    assert published.read_bytes() == b"FAKE-VIDEO-PAYLOAD-1"
    tmp_root = _project_root(data_root, pid) / ".ingest_tmp"
    assert not tmp_root.exists() or not any(tmp_root.iterdir()), "临时目录应被清理"


def test_ingest_dedupes_same_content(sink, video_file) -> None:
    run, data_root, pid = sink
    first, created1 = run(video_file)
    second, created2 = run(video_file)

    assert created1 is True and created2 is False
    assert first["asset_id"] == second["asset_id"]
    assert len(_rows(data_root, pid)) == 1


def test_ingest_rejects_directory(source_root, sink) -> None:
    run, _data_root, _pid = sink
    with pytest.raises(IngestError) as excinfo:
        run(source_root)
    assert excinfo.value.code == SOURCE_INVALID


def test_ingest_rejects_empty_file(source_root, sink) -> None:
    run, _data_root, _pid = sink
    empty = source_root / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(IngestError) as excinfo:
        run(str(empty))
    assert excinfo.value.code == SOURCE_EMPTY


@pytest.mark.skipif(os.name == "nt", reason="Windows 符号链接语义与 POSIX 不同")
def test_ingest_rejects_symlink(source_root, sink) -> None:
    run, _data_root, _pid = sink
    real = source_root / "real.mp4"
    real.write_bytes(b"x" * 10)
    link = source_root / "link.mp4"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(IngestError) as excinfo:
        run(str(link))
    assert excinfo.value.code == SOURCE_SYMLINK


def test_ingest_rejects_traversal(source_root, sink) -> None:
    run, _data_root, _pid = sink
    with pytest.raises(IngestError) as excinfo:
        run(str(source_root / ".." / "escape.mp4"))
    assert excinfo.value.code == SOURCE_INVALID


def test_ingest_unknown_extension_rejected(source_root, sink) -> None:
    run, _data_root, _pid = sink
    weird = source_root / "doc.xyz"
    weird.write_bytes(b"data")
    with pytest.raises(IngestError) as excinfo:
        run(str(weird))
    assert excinfo.value.code == SOURCE_INVALID


def test_ingest_publish_conflict_never_overwrites(
    sink, video_file, data_root, monkeypatch
) -> None:
    """目标正式路径已存在时拒绝覆盖（固定 asset_id 复现）。"""
    import types

    run, data_root, pid = sink
    fixed_asset_id = "fixedid" + "0" * 24
    monkeypatch.setattr(
        "cybercut.media.ingest.uuid.uuid4",
        lambda: types.SimpleNamespace(hex=fixed_asset_id),
    )
    target = (
        _project_root(data_root, pid) / "assets" / fixed_asset_id / "original"
        / Path(video_file).name
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"OTHER-CONTENT")

    for _ in range(2):
        with pytest.raises(IngestError) as excinfo:
            run(video_file)
        assert excinfo.value.code == "ASSET_PUBLISH_CONFLICT"

    assert target.read_bytes() == b"OTHER-CONTENT", "既有素材不得被覆盖"
    assert _rows(data_root, pid) == [], "失败接入不得留下半成品行"


def test_ingest_recovery_ready_but_file_missing(sink, video_file) -> None:
    run, data_root, pid = sink
    asset, _ = run(video_file)
    missing = _project_root(data_root, pid) / asset["source_relative_path"]
    missing.unlink()

    with pytest.raises(IngestError) as excinfo:
        run(video_file)
    assert excinfo.value.code == ASSET_FILE_MISSING

    rows = _rows(data_root, pid)
    assert rows[0]["status"] == "failed", "不得留下 ready 但文件不存在"


def test_ingest_takeover_of_stale_ingesting(db, data_root, source_root) -> None:
    """ingesting 崩溃残留（超时）→ 接管并把既有 asset_id 置 ready。"""
    source = _make_source(source_root, payload=b"STALE-PAYLOAD")
    _project = "proj-recover"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=_project,
                        project_id="proj-recover")
    asset, created = ingest_asset(
        db, project_id="proj-recover", tenant_id=TENANT, source_path=source,
        data_root=data_root,
    )
    assert created is True
    published = _project_root(data_root, "proj-recover") / asset["source_relative_path"]
    published.unlink()

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset["asset_id"],),
            )

    asset2, created2 = ingest_asset(
        db, project_id="proj-recover", tenant_id=TENANT, source_path=source,
        data_root=data_root, stale_after_s=0,
    )
    assert created2 is False
    assert asset2["asset_id"] == asset["asset_id"]
    assert asset2["status"] == "ready"
    assert published.is_file()


def test_ingest_recovers_when_official_file_already_exists(
    db, data_root, source_root
) -> None:
    """「文件已移动、数据库提交前崩溃」：正式文件已存在 → 锁 ingesting 行后恢复置 ready。"""
    import hashlib as _hl

    source = _make_source(source_root, payload=b"CRASH-BEFORE-COMMIT")
    project_id = "proj-crash-recover"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(
        db, project_id=project_id, tenant_id=TENANT, source_path=source,
        data_root=data_root,
    )
    published = _project_root(data_root, project_id) / asset["source_relative_path"]
    before = published.read_bytes()
    assert _hl.sha256(before).hexdigest() == asset["sha256"]

    # 伪造：行卡在 stale ingesting，但正式文件已经落盘
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset["asset_id"],),
            )

    asset2, created2 = ingest_asset(
        db, project_id=project_id, tenant_id=TENANT, source_path=source,
        data_root=data_root, stale_after_s=0,
    )
    assert created2 is False
    assert asset2["asset_id"] == asset["asset_id"]
    assert asset2["status"] == "ready"
    assert published.read_bytes() == before, "既有正式文件不得被覆盖"
    assert _hl.sha256(published.read_bytes()).hexdigest() == asset["sha256"]


def test_ingest_existing_target_content_mismatch_conflicts(
    db, data_root, source_root
) -> None:
    """正式目标已存在但内容不符 → ASSET_PUBLISH_CONFLICT，绝不覆盖。"""
    source = _make_source(source_root, payload=b"MISMATCH-SOURCE")
    project_id = "proj-mismatch"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(
        db, project_id=project_id, tenant_id=TENANT, source_path=source,
        data_root=data_root,
    )
    published = _project_root(data_root, project_id) / asset["source_relative_path"]
    original_size = published.stat().st_size
    # 同长度、不同内容 → 只有 sha256 能识别出不一致
    published.write_bytes(b"X" * original_size)
    tampered = published.read_bytes()

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset["asset_id"],),
            )

    with pytest.raises(IngestError) as excinfo:
        ingest_asset(
            db, project_id=project_id, tenant_id=TENANT, source_path=source,
            data_root=data_root, stale_after_s=0,
        )
    assert excinfo.value.code == "ASSET_PUBLISH_CONFLICT"
    assert published.read_bytes() == tampered, "不一致的目标文件不得被覆盖"
    rows = _rows(data_root, project_id)
    assert rows[0]["status"] == "ingesting", "冲突后不得置 ready"


def test_concurrent_takeover_of_stale_ingesting(
    db, data_root, source_root, monkeypatch
) -> None:
    """两个并发接管者竞争同一 stale ingesting 素材：最多一个发布（WB-02R4 §一.6）。"""
    import hashlib as _hl

    source = _make_source(source_root, payload=b"RACE-TAKEOVER-" + b"y" * 64)
    project_id = "proj-takeover-race"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(
        db, project_id=project_id, tenant_id=TENANT, source_path=source,
        data_root=data_root,
    )
    published = _project_root(data_root, project_id) / asset["source_relative_path"]
    published.unlink()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset["asset_id"],),
            )

    publishes: list[str] = []
    original_publish = AssetIngestor._publish

    def _counting_publish(self, temp_file, dest):
        publishes.append(str(dest))
        return original_publish(self, temp_file, dest)

    monkeypatch.setattr(AssetIngestor, "_publish", _counting_publish)

    barrier = threading.Barrier(2)
    results: list[tuple[dict, bool]] = []
    errors: list[BaseException] = []

    def _work():
        try:
            barrier.wait(timeout=20)
            results.append(ingest_asset(
                db, project_id=project_id, tenant_id=TENANT, source_path=source,
                data_root=data_root, stale_after_s=0,
            ))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_work, name=f"takeover-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(not t.is_alive() for t in threads), "所有线程必须已退出"
    assert len(results) + len(errors) == 2, "每个线程都必须有明确结果"
    for exc in errors:
        assert isinstance(exc, IngestError), f"非领域异常外泄：{type(exc).__name__}"
        assert exc.code == "ASSET_TAKEOVER_CONFLICT", (
            f"未取得所有权必须返回 ASSET_TAKEOVER_CONFLICT，实际 {exc.code}")

    assert len(publishes) <= 1, f"最多一个接管者可以发布，实际 {len(publishes)} 次"
    rows = _rows(data_root, project_id)
    ready_rows = [r for r in rows if r["status"] == "ready"]
    assert len(ready_rows) == 1, f"必须恰好一条 ready，实际 {len(ready_rows)}"
    assert ready_rows[0]["asset_id"] == asset["asset_id"]
    assert published.is_file(), "正式文件必须存在"
    assert _hl.sha256(published.read_bytes()).hexdigest() == asset["sha256"]


def test_ingest_unexpired_ingesting_conflicts(db, data_root, source_root) -> None:
    source = _make_source(source_root, payload=b"UNEXPIRED")
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name="proj-recover2",
                        project_id="proj-recover2")
    asset, _ = ingest_asset(
        db, project_id="proj-recover2", tenant_id=TENANT, source_path=source,
        data_root=data_root,
    )
    published = _project_root(data_root, "proj-recover2") / asset["source_relative_path"]
    published.unlink()
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() WHERE asset_id = %s",
                (asset["asset_id"],),
            )
    with pytest.raises(IngestError) as excinfo:
        ingest_asset(
            db, project_id="proj-recover2", tenant_id=TENANT, source_path=source,
            data_root=data_root, stale_after_s=900,
        )
    assert excinfo.value.code == ASSET_INGESTING


def test_concurrent_same_content_yields_single_ready_asset(
    db, data_root, source_root
) -> None:
    """4 并发同 sha -> 恰好一条 ready Asset，正式文件存在且哈希正确。"""
    import hashlib as _hl

    from cybercut.storage import projects as projects_repo

    source = _make_source(source_root, payload=b"RACE-PAYLOAD-" + b"x" * 64)
    with db.transaction() as conn:
        p = projects_repo.create(conn, tenant_id=TENANT, name="race")
    pid = p["project_id"]

    ingestor = AssetIngestor(db, stale_after_s=0)
    barrier = threading.Barrier(4)
    results: list[tuple[dict, bool]] = []
    errors: list[BaseException] = []

    def _work():
        try:
            barrier.wait(timeout=20)
            results.append(
                ingestor.ingest(project_id=pid, tenant_id=TENANT,
                                source_path=source, data_root=data_root)
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_work, name=f"race-{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads), "所有线程必须已退出"
    assert errors == [], f"不得有未处理异常：{errors}"
    assert len(results) == 4, f"应恰好 4 个结果，实际 {len(results)}"

    asset_ids = {r[0]["asset_id"] for r in results}
    assert len(asset_ids) == 1, f"所有结果必须返回同一 asset_id，实际 {asset_ids}"

    created_count = sum(1 for _, c in results if c is True)
    assert created_count == 1, f"应恰好 1 个 created=True，实际 {created_count}"

    rows = _rows(data_root, pid)
    ready_rows = [r for r in rows if r["status"] == "ready"]
    assert len(ready_rows) == 1, f"必须恰好一条 ready，实际 {len(ready_rows)}"
    asset = ready_rows[0]
    published = _project_root(data_root, pid) / asset["source_relative_path"]
    assert published.is_file(), "正式文件必须存在"
    assert _hl.sha256(published.read_bytes()).hexdigest() == asset["sha256"], (
        "正式文件哈希必须正确")


def test_cross_project_same_content_isolated(sink, video_file) -> None:
    run, data_root, pid = sink
    a, created_a = run(video_file, project_id="other-project")
    b, created_b = run(video_file)
    assert created_a is True and created_b is True
    assert a["asset_id"] != b["asset_id"]
    assert a["sha256"] == b["sha256"]
    assert len(_rows(data_root, pid)) == 1


def test_asset_json_export_shape(sink, video_file) -> None:
    run, data_root, pid = sink
    asset, _ = run(video_file)
    exported = media_assets.export_json(asset)
    assert set(exported) == {
        "asset_id", "media_kind", "original_filename", "source_relative_path",
        "sha256", "size_bytes", "status",
    }
    assert json.dumps(exported, sort_keys=True)


# ══════════════════════════════════════════════════════════════════
# WB-02R6 · 发布失败后的清理竞态
# ══════════════════════════════════════════════════════════════════

def _stage_failed_publish(db, data_root, project_id: str, asset_id: str,
                          relative_path: str) -> Path:
    """构造「A 已发布但事务未提交」的现场：行 stale ingesting + 正式文件在盘。"""
    dest = _project_root(data_root, project_id) / relative_path
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset_id,),
            )
    return dest


def test_cleanup_unlinks_only_inside_row_lock(
    db, data_root, source_root, monkeypatch
) -> None:
    """WB-02R6 §1/§3：清理的「确认归属 + unlink」必须发生在同一持锁事务内。

    在 `os.unlink` 被调用的瞬间，从另一条连接以 `FOR UPDATE SKIP LOCKED`
    探测该行：必须取不到（行正被清理者锁定）。若实现退回
    「查询状态 → 释放事务 → unlink」，该断言必然失败。
    """
    import hashlib as _hl

    from cybercut.media import ingest as ingest_module
    from cybercut.media.ingest import _PublishRecord
    from cybercut.storage.db import Database

    source = _make_source(source_root, payload=b"R6-LOCK-SCOPE-" + b"l" * 64)
    project_id = "proj-r6-lock-scope"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(db, project_id=project_id, tenant_id=TENANT,
                            source_path=source, data_root=data_root)
    rel = asset["source_relative_path"]
    dest = _stage_failed_publish(db, data_root, project_id,
                                 asset["asset_id"], rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(Path(source).read_bytes())  # 模拟 A 的 os.replace 结果

    probe: dict[str, object] = {"row_was_locked": None}
    real_unlink = os.unlink
    target = str(dest)

    def probing_unlink(path, *args, **kwargs):
        if str(path) == target:
            other = Database(resolve_test_dsn())
            with other.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT asset_id FROM cybercut.media_assets"
                        " WHERE asset_id = %s AND tenant_id = %s"
                        " FOR UPDATE SKIP LOCKED",
                        (asset["asset_id"], TENANT),
                    )
                    probe["row_was_locked"] = cur.fetchone() is None
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(ingest_module.os, "unlink", probing_unlink)

    record = _PublishRecord(
        publisher_token="test-token",
        asset_id=asset["asset_id"],
        tenant_id=TENANT,
        relative_path=rel,
        dest=dest,
        sha256=asset["sha256"],
        size_bytes=asset["size_bytes"],
        pre_status="ingesting",
    )
    AssetIngestor(db)._discard_own_publish(record)

    assert probe["row_was_locked"] is True, (
        "unlink 时该行必须仍被清理者的 FOR UPDATE 锁定")
    assert not dest.exists(), "归属已证明时应回收孤儿文件"
    assert _hl.sha256(Path(source).read_bytes()).hexdigest() == asset["sha256"]


def test_cleanup_fail_closed_when_identity_mismatch(
    db, data_root, source_root, monkeypatch
) -> None:
    """WB-02R6 §4：无法证明文件归属 ⇒ fail-closed，不删除。"""
    from cybercut.media.ingest import _PublishRecord

    source = _make_source(source_root, payload=b"R6-FAIL-CLOSED-" + b"f" * 64)
    project_id = "proj-r6-fail-closed"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(db, project_id=project_id, tenant_id=TENANT,
                            source_path=source, data_root=data_root)
    rel = asset["source_relative_path"]
    dest = _stage_failed_publish(db, data_root, project_id,
                                 asset["asset_id"], rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 同长度、不同内容 ⇒ 只有 SHA-256 能识别出这不是本次发布写入的文件
    dest.write_bytes(b"Z" * asset["size_bytes"])

    record = _PublishRecord(
        publisher_token="test-token",
        asset_id=asset["asset_id"],
        tenant_id=TENANT,
        relative_path=rel,
        dest=dest,
        sha256=asset["sha256"],
        size_bytes=asset["size_bytes"],
        pre_status="ingesting",
    )
    AssetIngestor(db)._discard_own_publish(record)

    assert dest.is_file(), "文件身份不符时必须保留文件（fail-closed）"
    assert dest.read_bytes() == b"Z" * asset["size_bytes"]


def test_commit_failure_cleanup_never_deletes_other_taker_file(
    db, data_root, source_root, monkeypatch
) -> None:
    """WB-02R6 §5：A 发布后提交失败，其清理绝不删除 B 随后成功发布的正式文件。

    调度：
      1. A 接管、发布正式文件；
      2. A 的 `mark_ready` 抛错（等价于发布后事务提交失败）；
      3. 在 A 的清理窗口内，B 接管并成功置 ready；
      4. A 的清理必须在持锁事务内重新确认归属 —— 因该行已 ready 而放弃回收。
    """
    import hashlib as _hl

    source = _make_source(source_root, payload=b"R6-COMMIT-FAIL-" + b"q" * 64)
    project_id = "proj-r6-commit-fail"
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)

    asset, _ = ingest_asset(db, project_id=project_id, tenant_id=TENANT,
                            source_path=source, data_root=data_root)
    published = _project_root(data_root, project_id) / asset["source_relative_path"]
    published.unlink()  # 模拟「文件被移走后崩溃」→ 需要重新接管发布
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.media_assets SET status = 'ingesting',"
                " updated_at = now() - interval '1 hour' WHERE asset_id = %s",
                (asset["asset_id"],),
            )

    real_mark_ready = media_assets.mark_ready
    calls = {"n": 0}

    def flaky_mark_ready(conn, asset_id, tenant_id, *, source_relative_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated commit failure after publish")
        return real_mark_ready(conn, asset_id, tenant_id,
                               source_relative_path=source_relative_path)

    monkeypatch.setattr(media_assets, "mark_ready", flaky_mark_ready)

    b_result: list = []
    b_error: list = []
    real_cleanup = AssetIngestor._discard_own_publish

    def run_b() -> None:
        try:
            b_result.append(ingest_asset(
                db, project_id=project_id, tenant_id=TENANT, source_path=source,
                data_root=data_root, stale_after_s=0,
            ))
        except BaseException as exc:  # noqa: BLE001
            b_error.append(exc)

    def coordinated_cleanup(self, record):
        """把 B 的接管安排在 A 的清理窗口内，并等 B 完成后再真正清理。"""
        threading.Thread(target=run_b, name="r6-b-taker", daemon=True).start()
        deadline = time.monotonic() + 60
        while not b_result and not b_error and time.monotonic() < deadline:
            time.sleep(0.05)
        return real_cleanup(self, record)

    monkeypatch.setattr(AssetIngestor, "_discard_own_publish",
                        coordinated_cleanup)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        ingest_asset(db, project_id=project_id, tenant_id=TENANT,
                     source_path=source, data_root=data_root, stale_after_s=0)

    assert b_error == [], f"B 的接管不得失败：{b_error}"
    assert len(b_result) == 1
    b_asset, _ = b_result[0]
    assert b_asset["asset_id"] == asset["asset_id"]
    assert b_asset["status"] == "ready"

    rows = _rows(data_root, project_id)
    assert [r["status"] for r in rows] == ["ready"]
    assert published.is_file(), "A 的清理不得删除 B 已成功 ready 的正式文件"
    assert _hl.sha256(published.read_bytes()).hexdigest() == asset["sha256"]
