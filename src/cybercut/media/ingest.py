"""WB-02 素材接入：登记、哈希、去重、项目沙箱、原子发布与恢复策略。

流程：
  1. 校验源文件（普通文件、非符号链接、非空、拒绝 `..` 段）；
  2. 复制进项目临时目录 `{project_root}/.ingest_tmp/{uuid}/`（同文件系统）；
  3. 复制的同时计算 sha256；
  4. `INSERT ... ON CONFLICT (project_id, sha256) DO NOTHING` —— 并发去重锚点；
  5. 成功插入（本 attempt 拥有该行）→ 原子移动到
     `assets/{asset_id}/original/{safe_filename}` → 置 `ready`；
  6. 冲突（已有同 sha 行）→ 按其状态恢复：
     - `ready` 且正式文件存在 → 复用既有 Asset；
     - `ready` 但文件缺失 → 置 `failed/ASSET_FILE_MISSING`（绝不留下“ready 但文件不存在”）；
     - `ingesting` 且未超时 → `ASSET_INGESTING`（另一进程正在进行）；
     - `ingesting` 且已超时（崩溃残留）或 `failed` → **接管**：把文件发布到既有
       asset_id 的确定性路径并置 `ready`。
  7. 任何失败 → 临时目录清理；已 `ready` 的既有数据绝不覆盖。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cybercut.contracts.errors import (
    CyberCutError,
    NotFoundError,
    ValidationError,
)
from cybercut.storage import media_assets
from cybercut.storage import paths as storage_paths
from cybercut.storage import projects as storage_projects

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
DEFAULT_STALE_AFTER_S = 900.0

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

SOURCE_INVALID = "SOURCE_INVALID"
SOURCE_EMPTY = "SOURCE_EMPTY"
SOURCE_SYMLINK = "SOURCE_SYMLINK"
ASSET_FILE_MISSING = "ASSET_FILE_MISSING"
ASSET_INGESTING = "ASSET_INGESTING"


class IngestError(CyberCutError):
    """素材接入失败（`code` 为稳定错误码）。"""

    code = "INGEST_FAILED"

    def __init__(self, code: str, message: str, *,
                 details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.code = code


@dataclass(frozen=True)
class _PublishRecord:
    """一次发布的不可混淆身份记录（WB-02R6）。

    失败清理阶段必须能证明「磁盘上的这个文件确由本次发布写入、
    且没有任何接管者已经把它接管为 ready」，否则 fail-closed 不删除。
    仅靠「查询状态 → 释放事务 → unlink」无法排除竞态，故此处固化：
    - `publisher_token`：本次发布的唯一标识（便于日志/审计回溯）；
    - 行身份：`asset_id` / `tenant_id` / `relative_path` / `pre_status`；
    - 文件身份：`dest` / `size_bytes` / `sha256`。
    """

    publisher_token: str
    asset_id: str
    tenant_id: str
    relative_path: str
    dest: Path
    sha256: str
    size_bytes: int
    pre_status: str


def infer_media_kind(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix in VIDEO_EXTS:
        return "video"
    if suffix in IMAGE_EXTS:
        return "image"
    raise IngestError(
        SOURCE_INVALID,
        f"无法从扩展名识别媒体类型：{suffix!r}（支持 {sorted(VIDEO_EXTS | IMAGE_EXTS)}）",
        details={"suffix": suffix},
    )


def _copy_with_hash(source: Path, dest: Path,
                    chunk: int = _CHUNK) -> tuple[int, str]:
    """复制文件并同时计算 sha256（流式，避免双读）。"""
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, open(dest, "wb") as dst:
        while True:
            block = src.read(chunk)
            if not block:
                break
            dst.write(block)
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


class AssetIngestor:
    """素材接入服务（项目沙箱 + 去重 + 原子发布 + 恢复）。"""

    def __init__(self, db: Any, *, stale_after_s: float = DEFAULT_STALE_AFTER_S,
                 chunk: int = _CHUNK) -> None:
        self._db = db
        self.stale_after_s = float(stale_after_s)
        self.chunk = int(chunk)

    # ── 校验 ──

    @staticmethod
    def _validate_source(source_path: Path) -> Path:
        raw = str(source_path)
        if not raw:
            raise IngestError(SOURCE_INVALID, "源路径为空")
        parts = [p for p in Path(raw).parts if p not in (".",)]
        if ".." in parts:
            raise IngestError(
                SOURCE_INVALID, f"源路径包含 '..' 段，拒绝路径穿越：{raw!r}"
            )
        original = Path(raw).expanduser()
        if original.is_symlink():
            raise IngestError(SOURCE_SYMLINK, f"拒绝符号链接：{raw!r}")
        resolved = original.resolve()
        if not resolved.exists():
            raise IngestError(SOURCE_INVALID, f"源文件不存在：{raw!r}")
        if not resolved.is_file():
            raise IngestError(
                SOURCE_INVALID, f"只接受普通文件：{raw!r}",
                details={"is_dir": resolved.is_dir()},
            )
        if resolved.stat().st_size <= 0:
            raise IngestError(SOURCE_EMPTY, f"拒绝空文件：{raw!r}")
        return resolved

    # ── 主流程 ──

    def ingest(
        self, *, project_id: str, tenant_id: str, source_path: Path | str,
        media_kind: str | None = None, data_root: Path | str | None = None,
    ) -> tuple[dict, bool]:
        """接入一个本地素材；返回 `(asset, created)`。

        `created=False` 表示命中去重（返回既有 Asset，可能被标记为 failed）。
        """
        resolved = self._validate_source(Path(source_path))
        kind = media_kind or infer_media_kind(resolved)
        if kind not in ("video", "image"):
            raise ValidationError(f"非法 media_kind：{kind!r}")

        root = Path(data_root) if data_root is not None else _data_root()
        with self._db.transaction() as conn:
            if storage_projects.get(conn, project_id, tenant_id) is None:
                raise NotFoundError(
                    "project 不存在（素材必须挂到已存在的项目）",
                    details={"project_id": project_id, "tenant_id": tenant_id},
                )
        project_root = storage_paths.ensure_project_dir(project_id, root)
        temp_dir = project_root / ".ingest_tmp" / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / storage_paths.sanitize_filename(resolved.name)
        asset_id = uuid.uuid4().hex
        try:
            size, sha = _copy_with_hash(resolved, temp_file, self.chunk)
            if size <= 0:
                raise IngestError(SOURCE_EMPTY, f"拒绝空文件：{resolved}")
            original = storage_paths.asset_original_path(project_root, asset_id,
                                                         resolved.name)
            rel = storage_paths.relative_to_project(project_root, original)

            with self._db.transaction() as conn:
                owned = media_assets.insert_ingesting(
                    conn,
                    project_id=project_id, tenant_id=tenant_id, media_kind=kind,
                    original_filename=resolved.name,
                    source_relative_path=rel, sha256=sha, size_bytes=size,
                    asset_id=asset_id,
                )
                if owned is not None:
                    try:
                        self._publish(temp_file, original)
                        asset = media_assets.mark_ready(
                            conn, owned["asset_id"], tenant_id,
                            source_relative_path=rel,
                        )
                    except Exception as exc:
                        media_assets.mark_failed(
                            conn, owned["asset_id"], tenant_id,
                            error_code=getattr(exc, "code", "INGEST_IO_ERROR"),
                            message=str(exc),
                        )
                        raise
                    return asset, True
                existing = media_assets.get_by_sha(conn, project_id, tenant_id, sha)
            if existing is None:  # pragma: no cover - ON CONFLICT 后必可读到
                raise IngestError(SOURCE_INVALID, "去重冲突后未能读取既有素材")
            return self._recover_or_reuse(
                project_root=project_root, temp_file=temp_file, existing=existing,
                tenant_id=tenant_id, source_name=resolved.name,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _publish(self, temp_file: Path, dest: Path) -> None:
        """同文件系统原子移动；目标必须不存在（原始文件发布后不可覆盖）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise IngestError(
                "ASSET_PUBLISH_CONFLICT",
                f"素材正式路径已存在，拒绝覆盖：{dest}",
            )
        os.replace(temp_file, dest)

    def _recover_or_reuse(
        self, *, project_root: Path, temp_file: Path, existing: dict,
        tenant_id: str, source_name: str,
    ) -> tuple[dict, bool]:
        asset_id = existing["asset_id"]
        rel = existing["source_relative_path"]
        dest = storage_paths.resolve_official_asset_path(project_root, rel)
        status = existing["status"]

        if status == media_assets.READY:
            if dest.is_file():
                return existing, False
            with self._db.transaction() as conn:
                media_assets.mark_failed(
                    conn, asset_id, tenant_id,
                    error_code=ASSET_FILE_MISSING, message="ready 但正式文件缺失",
                )
            raise IngestError(
                ASSET_FILE_MISSING,
                "既有素材标记为 ready 但文件缺失（已置 failed）",
                details={"asset_id": asset_id, "missing": str(dest)},
            )

        if status == media_assets.INGESTING:
            age = time.monotonic() - _to_monotonic(existing["updated_at"])
            if age < self.stale_after_s:
                raise IngestError(
                    ASSET_INGESTING,
                    "同内容素材正在由其它进程接入（尚未超时）",
                    details={"asset_id": asset_id, "age_s": round(age, 1)},
                )
            logger.warning("接管超时的 ingesting 素材 asset=%s", asset_id)

        # ── WB-02R5 §一 / WB-02R6：接管必须在**单个事务内**完成 ──
        # lock_for_takeover(FOR UPDATE)
        #   → 校验并取得所有权（未命中即 ASSET_TAKEOVER_CONFLICT）
        #   → 持锁期间重新检查正式目标（存在且一致 == 崩溃残留恢复）
        #   → 发布或确认正式文件
        #   → mark_ready
        #   → 提交
        # 禁止在释放行锁之后再调用 _publish()；也禁止调用只匹配 ready 的 lock_ready()。
        record: _PublishRecord | None = None
        try:
            with self._db.transaction() as conn:
                locked = media_assets.lock_for_takeover(
                    conn, asset_id, tenant_id,
                    allowed_statuses=(media_assets.INGESTING,
                                      media_assets.FAILED),
                    stale_after_s=(self.stale_after_s
                                   if status == media_assets.INGESTING else None),
                )
                if locked is None:
                    raise IngestError(
                        "ASSET_TAKEOVER_CONFLICT",
                        f"素材 {asset_id} 接管失败（行锁或 CAS 未命中）",
                        details={"asset_id": asset_id, "status": status},
                    )
                logger.info(
                    "接管素材 asset=%s (status=%s)", asset_id, locked["status"]
                )

                if dest.is_file():
                    # 「文件已移动、数据库提交前崩溃」的恢复：持锁内重新校验
                    on_disk_size = dest.stat().st_size
                    on_disk_sha = _sha_of(dest)
                    if (on_disk_size == locked["size_bytes"]
                            and on_disk_sha == locked["sha256"]):
                        asset = media_assets.mark_ready(
                            conn, asset_id, tenant_id, source_relative_path=rel
                        )
                        return asset, False
                    raise IngestError(
                        "ASSET_PUBLISH_CONFLICT",
                        "素材正式路径已存在且内容不一致，拒绝覆盖",
                        details={"asset_id": asset_id, "target": str(dest)},
                    )

                self._publish(temp_file, dest)
                # WB-02R6 §2：记录本次发布的**不可混淆标记**
                # （发布者 token + 行/路径/内容身份），供失败清理时校验归属。
                record = _PublishRecord(
                    publisher_token=uuid.uuid4().hex,
                    asset_id=asset_id,
                    tenant_id=tenant_id,
                    relative_path=rel,
                    dest=dest,
                    sha256=locked["sha256"],
                    size_bytes=locked["size_bytes"],
                    pre_status=locked["status"],
                )
                media_assets.set_source_path(
                    conn, asset_id, tenant_id, source_relative_path=rel
                )
                asset = media_assets.mark_ready(
                    conn, asset_id, tenant_id, source_relative_path=rel
                )
        except BaseException:
            # WB-02R4 §一.5 / WB-02R6：提交失败时只回收**归属可证**的本文件；
            # 无法证明归属（他人已 ready 发布、文件身份不符）⇒ fail-closed 不删除。
            if record is not None:
                self._discard_own_publish(record)
            raise
        return asset, False

    def _discard_own_publish(self, record: _PublishRecord) -> None:
        """回收本次失败事务发布但未提交的文件（WB-02R6）。

        严格约束：
        - **禁止**「查询状态 → 释放事务 → unlink」：本方法全程在同一持锁事务内；
        - 清理前必须在 `FOR UPDATE` 下重新确认三件事：
          1. 该行仍处于本次失败事务的**发布前状态**（非 `ready`、非终态）；
          2. 正式路径仍对应本次接管者发布（`source_relative_path` 未被改写）；
          3. 磁盘文件身份（大小 + SHA-256）与本次发布记录一致；
        - 任一条件不满足 ⇒ **fail-closed，不删除**（宁可留下孤儿文件，由后续 GC 处理，
          也绝不删除其它接管者已成功发布的正式文件）。
        """
        try:
            with self._db.transaction() as conn:
                row = media_assets.lock_row(
                    conn, record.asset_id, record.tenant_id
                )
                if row is None:
                    logger.warning(
                        "放弃回收（无法证明归属：行不存在）asset=%s",
                        record.asset_id,
                    )
                    return
                if row["status"] == media_assets.READY:
                    # 其它接管者已完成 ready 发布 ⇒ 文件归其所有
                    logger.info(
                        "放弃回收（该 Asset 已由其它接管者置 ready）asset=%s",
                        record.asset_id,
                    )
                    return
                if row["status"] != record.pre_status:
                    logger.warning(
                        "放弃回收（行状态已变更：%s ≠ %s）asset=%s",
                        row["status"], record.pre_status, record.asset_id,
                    )
                    return
                if row["source_relative_path"] != record.relative_path:
                    logger.warning(
                        "放弃回收（正式路径已改写）asset=%s", record.asset_id
                    )
                    return
                if not record.dest.is_file():
                    return  # 已被其它路径处理，无需回收
                if record.dest.stat().st_size != record.size_bytes:
                    logger.warning(
                        "放弃回收（文件大小不符）asset=%s", record.asset_id
                    )
                    return
                if _sha_of(record.dest) != record.sha256:
                    logger.warning(
                        "放弃回收（文件内容不符）asset=%s", record.asset_id
                    )
                    return
                # 归属已证明：文件确为本次发布且未被任何接管者接管
                os.unlink(record.dest)
                logger.warning(
                    "已回收本次失败事务发布的文件：%s（token=%s）",
                    record.dest, record.publisher_token,
                )
        except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始异常
            logger.exception("回收发布文件失败：%s", record.dest)


def _sha_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _to_monotonic(db_timestamp: Any) -> float:
    """把数据库时间戳近似映射到本进程的 monotonic 时钟（仅用于超时判断）。"""
    import datetime as dt

    if isinstance(db_timestamp, dt.datetime):
        if db_timestamp.tzinfo is not None:
            stamp = db_timestamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
        else:
            stamp = db_timestamp
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        delta = now - stamp
        return time.monotonic() - max(0.0, delta.total_seconds())
    return time.monotonic()


DATA_ROOT_HINT: Path | None = None


def set_data_root_hint(root: Path | None) -> None:
    """供上层注入数据根目录（与 executor 的 data_root 保持一致）。"""
    global DATA_ROOT_HINT
    DATA_ROOT_HINT = root


def _data_root() -> Path:
    from cybercut.storage.paths import default_data_root

    return DATA_ROOT_HINT if DATA_ROOT_HINT is not None else default_data_root()


def ingest_asset(
    db: Any, *, project_id: str, tenant_id: str, source_path: Path | str,
    media_kind: str | None = None, data_root: Path | str | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> tuple[dict, bool]:
    """便捷入口：构造 `AssetIngestor` 并接入素材。"""
    return AssetIngestor(db, stale_after_s=stale_after_s).ingest(
        project_id=project_id, tenant_id=tenant_id, source_path=source_path,
        media_kind=media_kind, data_root=data_root,
    )


__all__ = [
    "ASSET_FILE_MISSING",
    "ASSET_INGESTING",
    "AssetIngestor",
    "IngestError",
    "SOURCE_EMPTY",
    "SOURCE_INVALID",
    "SOURCE_SYMLINK",
    "infer_media_kind",
    "ingest_asset",
    "set_data_root_hint",
]
