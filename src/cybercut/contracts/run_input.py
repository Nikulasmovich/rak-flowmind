"""WB-02R §一/§二：Run 输入冻结与启动契约（Pydantic v2，extra=forbid）。

本模块是以下契约的**唯一权威定义**（WB-02R3 §六 / WB-02R5 §四）：
- `StartRunInput`：`Orchestrator.start_run` 的输入；
- `ArtifactRevisionRef`：`node_runs.input_revision_refs` 的统一元素形状。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cybercut.contracts.errors import ValidationError

_HEX_DIGITS = set("0123456789abcdef")


class ArtifactRevisionRef(BaseModel):
    """冻结的上游产物引用（`node_runs.input_revision_refs` 的统一元素形状）。"""

    model_config = ConfigDict(extra="forbid")

    artifact_revision_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    node_run_id: str = Field(min_length=1)

    @field_validator("sha256")
    @classmethod
    def _sha_hex(cls, v: str) -> str:
        """WB-02R5 §五：严格 64 位十六进制字符（大小写归一为小写）。"""
        normalized = v.lower()
        if not set(normalized) <= _HEX_DIGITS:
            raise ValueError(f"sha256 必须是 64 位十六进制字符：{v!r}")
        return normalized


class StartRunInput(BaseModel):
    """`start_run` 的输入契约（WB-02R3 §六：权威定义）。"""

    model_config = ConfigDict(extra="forbid")

    pipeline_version: str | None = None
    requested_from_node: str | None = None
    asset_ids: list[str] = Field(default_factory=list)

    @field_validator("asset_ids")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for item in value:
            if item in seen:
                raise ValueError(f"重复的 asset_id：{item}")
            seen.add(item)
        return value


def freeze_run_input(
    assets: list[dict], *, require_assets: bool = False, pipeline_name: str = "",
    creative_input: dict | None = None, approval_policy: dict | None = None,
) -> dict:
    """构造 `runs.input_json` 的冻结内容（保持首次顺序、确定性去重）。

    WB-03：额外承载创作层输入（`creative_input`）与审批策略（`approval_policy`），
    两者随 Run 一起冻结，并在**每个** attempt 的输入中下发。
    """
    seen: set[str] = set()
    frozen: list[dict] = []
    for asset in assets:
        asset_id = asset["asset_id"]
        if asset_id in seen:
            continue  # 确定性去重：保持首次出现顺序
        if asset.get("status") != "ready":
            raise ValidationError(
                f"素材 {asset_id} 状态为 {asset.get('status')}（仅 ready 可启动）",
                details={"asset_id": asset_id},
            )
        seen.add(asset_id)
        frozen.append({
            "asset_id": asset_id,
            "media_kind": asset["media_kind"],
            "original_filename": asset["original_filename"],
            "source_relative_path": asset["source_relative_path"],
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
        })
    if require_assets and not frozen:
        raise ValidationError(
            f"管线 {pipeline_name!r} 至少需要一个素材",
            details={"pipeline": pipeline_name},
        )
    frozen_input: dict = {
        "asset_ids": [a["asset_id"] for a in frozen],
        "assets": frozen,
    }
    if creative_input is not None:
        frozen_input["creative_input"] = dict(creative_input)
    if approval_policy is not None:
        frozen_input["approval_policy"] = dict(approval_policy)
    return frozen_input


def validate_start_assets(assets: list[dict], *, data_root=None) -> None:
    """启动前校验每个素材：ready + 正式文件存在 + size_bytes/sha256 一致。

    WB-02R3 §5 / WB-02R5 §一：路径解析统一走 `storage.paths`，
    禁止手工拼接；SHA-256 使用 1 MiB 分块流式计算。
    """
    import hashlib
    from pathlib import Path as _Path

    from cybercut.storage import paths as _sp
    from cybercut.storage.paths import default_data_root

    root = _Path(data_root) if data_root else default_data_root()
    for asset in assets:
        aid = asset.get("asset_id", "")
        if asset.get("status") != "ready":
            raise ValidationError(
                f"素材 {aid} 状态非 ready",
                details={"asset_id": aid, "status": asset.get("status")},
            )
        rel = asset.get("source_relative_path", "")
        if not rel:
            raise ValidationError(
                "素材缺少 source_relative_path", details={"asset_id": aid})
        if not rel.startswith("assets/"):
            raise ValidationError(
                f"素材路径非法（必须以 assets/ 开头）：{rel!r}",
                details={"asset_id": aid, "path": rel},
            )
        project_id = asset.get("project_id", "")
        if not project_id:
            raise ValidationError(
                "素材缺少 project_id（无法解析正式路径）",
                details={"asset_id": aid},
            )
        full = _sp.resolve_official_asset_path(
            _sp.project_root(project_id, root), rel
        )
        if not full.is_file():
            raise ValidationError(
                f"素材 {aid} 正式文件缺失：{rel!r}",
                details={"asset_id": aid, "path": str(full)},
            )
        size = asset.get("size_bytes")
        if not size or size <= 0:
            raise ValidationError(
                f"素材 {aid} size_bytes 非法：{size}", details={"asset_id": aid})
        actual_size = full.stat().st_size
        if actual_size != size:
            raise ValidationError(
                f"素材 {aid} 大小不符：磁盘 {actual_size} vs 记录 {size}",
                details={"asset_id": aid, "actual": actual_size, "expected": size},
            )
        digest = hashlib.sha256()
        with open(full, "rb") as fh:
            while True:
                block = fh.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        actual_sha = digest.hexdigest()
        if actual_sha != asset.get("sha256"):
            raise ValidationError(
                f"素材 {aid} SHA-256 不符",
                details={"asset_id": aid, "expected": asset.get("sha256"),
                         "actual": actual_sha},
            )


__all__ = [
    "ArtifactRevisionRef",
    "StartRunInput",
    "freeze_run_input",
    "validate_start_assets",
]
