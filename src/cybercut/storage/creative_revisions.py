"""`cybercut.*_revisions` 仓储：创作层**版本化**结构化产物（WB-03）。

四类逻辑产物（kind → 表 / 主键 / 对应 ArtifactRevision kind）：

    brief       -> creative_brief_revisions  / brief_revision_id       / creative.brief
    script      -> script_revisions          / script_revision_id      / creative.script
    storyboard  -> storyboard_revisions      / storyboard_revision_id  / creative.storyboard
    match       -> asset_match_revisions     / match_revision_id       / creative.match

不变式：
- `revision_no` 在**同一 project** 内按逻辑产物单调递增；
  分配前必须 `projects.lock(...)`（锁序中 Project 为最内层）；
- `UNIQUE (project_id, tenant_id, revision_no)` ⇒ **旧版本永不被覆盖**；
- `payload_json` 只保存经 Pydantic 校验的结构化产物（不含原始思维链）。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.editing import TemplateBinding, TimelineSpec
from cybercut.contracts.errors import ConflictError, ValidationError
from cybercut.storage import projects as projects_repo
from cybercut.storage._util import UNSET, new_id, require_row

#: kind -> (表名, 主键列, ArtifactRevision kind)
KINDS: dict[str, tuple[str, str, str]] = {
    "brief": ("creative_brief_revisions", "brief_revision_id", "creative.brief"),
    "script": ("script_revisions", "script_revision_id", "creative.script"),
    "storyboard": (
        "storyboard_revisions", "storyboard_revision_id", "creative.storyboard",
    ),
    "match": ("asset_match_revisions", "match_revision_id", "creative.match"),
    "edit_plan": ("edit_plan_revisions", "edit_plan_revision_id", "edit.plan"),
    "template_bind": (
        "template_bind_revisions", "template_bind_revision_id", "template.bind",
    ),
}

#: 允许的版本状态
#: - `draft`：**待审批**（人工模式下的新版本，尚未生效）
#: - `active`：当前生效版本（同 project + kind 唯一）
#: - `superseded`：被更新版本取代
#: - `rejected`：人工驳回
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REJECTED = "rejected"

_STATUSES = frozenset({STATUS_DRAFT, STATUS_ACTIVE, STATUS_SUPERSEDED,
                       STATUS_REJECTED})


def _spec(kind: str) -> tuple[str, str, str]:
    try:
        return KINDS[kind]
    except KeyError as exc:  # pragma: no cover - 编程错误
        raise ValidationError(f"未支持的创作产物 kind：{kind!r}") from exc


def _cols(kind: str) -> str:
    table, pk, _ = _spec(kind)
    return (
        f"{pk}, project_id, run_id, tenant_id, revision_no, status, payload_json,"
        " prompt_version, model_id, created_by, supersedes_id, created_at, updated_at"
    )


def artifact_kind(kind: str) -> str:
    """该逻辑产物对应的 ArtifactRevision `kind`。"""
    return _spec(kind)[2]


def next_revision_no(
    conn: Any, kind: str, project_id: str, tenant_id: str
) -> int:
    """分配下一个 `revision_no`（**调用方须已持有 project 锁**，见模块 docstring）。"""
    table, _, _ = _spec(kind)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT coalesce(max(revision_no), 0) + 1 AS n FROM cybercut.{table}"
            " WHERE project_id = %s AND tenant_id = %s",
            (project_id, tenant_id),
        )
        return int(cur.fetchone()["n"])


def insert(
    conn: Any,
    kind: str,
    *,
    project_id: str,
    run_id: str,
    tenant_id: str,
    payload: dict,
    prompt_version: str,
    model_id: str,
    created_by: str = "system",
    status: str = STATUS_ACTIVE,
    revision_no: int | None = None,
    supersedes_id: Any = UNSET,
    revision_id: str | None = None,
) -> dict:
    """登记一个新版本。

    - `revision_no` 缺省时自动分配（先 `projects.lock`，串行化并发分配），
      并自动与本 project 上一版本建立 `supersedes_id` 链；
    - 重复 `(project, tenant, revision_no)` -> `ConflictError`（**旧版本不可覆盖**）。
    """
    table, pk, _ = _spec(kind)
    if status not in _STATUSES:
        raise ValidationError(f"非法 status：{status!r}")
    rid = revision_id or new_id()

    if revision_no is None:
        projects_repo.lock(conn, project_id, tenant_id)
        rno = next_revision_no(conn, kind, project_id, tenant_id)
    else:
        rno = int(revision_no)

    if supersedes_id is UNSET:
        previous = latest(conn, kind, project_id, tenant_id)
        supersedes_id = previous[pk] if previous else None

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO cybercut.{table} ({pk}, project_id, run_id, tenant_id,"
                " revision_no, status, payload_json, prompt_version, model_id,"
                " created_by, supersedes_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_cols(kind)}",
                (rid, project_id, run_id, tenant_id, rno, status, Json(payload or {}),
                 prompt_version, model_id, created_by, supersedes_id),
            )
            row = dict(cur.fetchone())
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "UniqueViolation":
            raise ConflictError(
                f"创作产物版本已存在且不可覆盖：project={project_id}"
                f" kind={kind} revision_no={rno}",
                details={"project_id": project_id, "kind": kind,
                         "revision_no": rno},
            ) from exc
        raise

    # 自动模式（无审批门）：新版本直接生效，并把旧的 active 版本置 superseded；
    # 人工模式下新版本为 `draft`（待审批），不改变任何既有 active 版本。
    if status == STATUS_ACTIVE:
        supersede_others(conn, kind, project_id, tenant_id, keep_id=rid)
    return row


def supersede_others(
    conn: Any, kind: str, project_id: str, tenant_id: str, *, keep_id: str
) -> int:
    """把同 `(project, tenant, kind)` 的其它 `active` 版本置 `superseded`。

    返回被取代的行数（`active` 在 `(project, tenant, kind)` 内**唯一**）。
    """
    table, pk, _ = _spec(kind)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.{table} SET status = %s, updated_at = now()"
            " WHERE project_id = %s AND tenant_id = %s"
            f"   AND status = %s AND {pk} <> %s",
            (STATUS_SUPERSEDED, project_id, tenant_id, STATUS_ACTIVE, keep_id),
        )
        return int(cur.rowcount or 0)


def activate(
    conn: Any, kind: str, revision_id: str, tenant_id: str
) -> dict:
    """审批通过：把该版本置 `active`，旧的 `active` 版本置 `superseded`。"""
    row = require_row(get(conn, kind, revision_id, tenant_id),
                      what=f"{kind}_revision")
    updated = set_status(conn, kind, revision_id, tenant_id,
                         status=STATUS_ACTIVE)
    supersede_others(conn, kind, row["project_id"], tenant_id,
                     keep_id=revision_id)
    return updated


def mark_rejected(
    conn: Any, kind: str, revision_id: str, tenant_id: str
) -> dict:
    """审批驳回：把该版本置 `rejected`（不改变既有 active 版本）。"""
    return set_status(conn, kind, revision_id, tenant_id, status=STATUS_REJECTED)


def latest_for_run(
    conn: Any, kind: str, project_id: str, tenant_id: str, *, run_id: str
) -> dict | None:
    """**该 Run 内**该逻辑产物的最新版本（定向修订的版本守卫基准）。"""
    return latest(conn, kind, project_id, tenant_id, run_id=run_id)


def get(conn: Any, kind: str, revision_id: str, tenant_id: str) -> dict | None:
    table, pk, _ = _spec(kind)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_cols(kind)} FROM cybercut.{table}"
            f" WHERE {pk} = %s AND tenant_id = %s",
            (revision_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def require(conn: Any, kind: str, revision_id: str, tenant_id: str) -> dict:
    return require_row(
        get(conn, kind, revision_id, tenant_id), what=f"{kind}_revision"
    )


def latest(
    conn: Any, kind: str, project_id: str, tenant_id: str, *,
    run_id: str | None = None, status: str | None = None,
) -> dict | None:
    """最新版本（可选按 run / status 过滤），按 `revision_no` 降序取一。"""
    table, _, _ = _spec(kind)
    clauses = ["project_id = %s", "tenant_id = %s"]
    params: list[Any] = [project_id, tenant_id]
    if run_id is not None:
        clauses.append("run_id = %s")
        params.append(run_id)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_cols(kind)} FROM cybercut.{table}"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY revision_no DESC LIMIT 1",
            tuple(params),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_project(
    conn: Any, kind: str, project_id: str, tenant_id: str, *,
    run_id: str | None = None,
) -> list[dict]:
    table, _, _ = _spec(kind)
    clauses = ["project_id = %s", "tenant_id = %s"]
    params: list[Any] = [project_id, tenant_id]
    if run_id is not None:
        clauses.append("run_id = %s")
        params.append(run_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_cols(kind)} FROM cybercut.{table}"
            f" WHERE {' AND '.join(clauses)} ORDER BY revision_no",
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]


def set_status(
    conn: Any, kind: str, revision_id: str, tenant_id: str, *, status: str
) -> dict:
    """迁移版本状态（`draft` / `active` / `superseded` / `rejected`）。"""
    table, pk, _ = _spec(kind)
    if status not in _STATUSES:
        raise ValidationError(f"非法 status：{status!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.{table} SET status = %s, updated_at = now()"
            f" WHERE {pk} = %s AND tenant_id = %s"
            f" RETURNING {_cols(kind)}",
            (status, revision_id, tenant_id),
        )
        row = cur.fetchone()
    return require_row(dict(row) if row else None, what=f"{kind}_revision")


def payload_of(row: dict) -> dict:
    """取出结构化产物（`payload_json`）。"""
    value = row.get("payload_json")
    if not isinstance(value, dict):  # pragma: no cover - 数据库列类型保证
        raise ValidationError("payload_json 必须是 JSON 对象")
    return value


# ══════════════════════════════════════════════════════════════════
# ArtifactRevision -> 创作产物表（执行器在同一事务内调用）
# ══════════════════════════════════════════════════════════════════

#: ArtifactRevision.kind -> 创作产物 kind
ARTIFACT_KIND_MAP: dict[str, str] = {
    "creative.brief": "brief",
    "creative.script": "script",
    "creative.storyboard": "storyboard",
    "creative.match": "match",
    "edit.plan": "edit_plan",
    "template.bind": "template_bind",
}

_DEFAULT_PROMPT_VERSIONS: dict[str, str] = {
    "brief": "creative-brief-prompt.v1",
    "script": "creative-script-prompt.v1",
    "storyboard": "creative-storyboard-prompt.v1",
    "match": "deterministic-match.v1",
    "edit_plan": "edit-plan.v1",
    "template_bind": "template-bind.v1",
}
_DEFAULT_MODEL_IDS: dict[str, str] = {
    "brief": "unknown",
    "script": "unknown",
    "storyboard": "unknown",
    "match": "deterministic:asset-match-v1",
    "edit_plan": "deterministic:edit-plan-v1",
    "template_bind": "deterministic:template-bind-v1",
}

#: kind -> payload 中的权威版本 id 字段名
ID_FIELDS: dict[str, str] = {
    "brief": "brief_revision_id",
    "script": "script_revision_id",
    "storyboard": "storyboard_revision_id",
    "match": "match_revision_id",
    "edit_plan": "edit_plan_revision_id",
    "template_bind": "template_bind_revision_id",
}

#: 节点侧写出的**临时**身份前缀（执行器发布前必然被权威身份覆盖）
PENDING_ID_PREFIX = "pending:"

#: kind -> 权威 Pydantic 模型（落库前**再次**校验，守住 schema 边界）
_MODELS: dict[str, Any] = {
    "brief": CreativeBrief,
    "script": ScriptRevision,
    "storyboard": StoryboardRevision,
    "match": AssetMatchResult,
    "edit_plan": TimelineSpec,
    "template_bind": TemplateBinding,
}


def id_field(kind: str) -> str:
    """payload 中承载权威版本 id 的字段名。"""
    _spec(kind)  # 校验 kind 合法
    return ID_FIELDS[kind]


def provisional_identity(
    kind: str, *, run_id: str, attempt_no: int
) -> dict[str, Any]:
    """节点侧产出的**临时**身份（执行器会用数据库分配的权威值覆盖）。

    `attempt_no` **仅**用于构造可辨识的临时 id，**绝不**作为业务 `revision_no`。
    """
    return {
        id_field(kind): f"{PENDING_ID_PREFIX}{kind}-{run_id}-{attempt_no}",
        "revision_no": 1,
    }


def finalize_payload(
    kind: str,
    payload: dict,
    *,
    revision_id: str,
    revision_no: int,
    project_id: str,
    run_id: str,
) -> dict:
    """把数据库分配的**权威身份**写入正式 payload（返回新 dict）。

    权威身份 = 该产物 ArtifactRevision 的 `artifact_revision_id` 与 `revision_no`，
    从而保证「产物文件 / ArtifactRevision / 创作版本表 / payload」四者一一对应。
    """
    _spec(kind)
    if not revision_id:
        raise ValidationError("revision_id 不能为空")
    if int(revision_no) < 1:
        raise ValidationError(f"revision_no 必须 >= 1：{revision_no}")
    finalized = dict(payload)
    finalized[id_field(kind)] = str(revision_id)
    finalized["revision_no"] = int(revision_no)
    finalized["project_id"] = str(project_id)
    finalized["run_id"] = str(run_id)
    return finalized


def assert_payload_identity(
    kind: str,
    payload: dict,
    *,
    revision_id: str,
    revision_no: int,
    project_id: str,
    run_id: str,
) -> None:
    """强制校验 payload 身份与数据库身份**完全一致**（不一致即拒绝落库）。"""
    expected = {
        id_field(kind): str(revision_id),
        "revision_no": int(revision_no),
        "project_id": str(project_id),
        "run_id": str(run_id),
    }
    mismatched = {
        field: {"expected": value, "actual": payload.get(field)}
        for field, value in expected.items()
        if payload.get(field) != value
    }
    if mismatched:
        raise ValidationError(
            f"创作产物 payload 身份与数据库不一致：{kind}",
            details={"kind": kind, "mismatched": mismatched},
        )


def validate_payload(kind: str, payload: dict) -> None:
    """用权威模型校验结构化负载；失败即抛 `ValidationError`。"""
    model = _MODELS[kind]
    try:
        model.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 统一映射为领域校验错误
        raise ValidationError(
            f"创作产物负载未通过 schema 校验：{kind}",
            details={"kind": kind, "reason": str(exc)[:300]},
        ) from exc


def persist_for_artifact(
    conn: Any,
    *,
    artifact_row: dict,
    payload: dict,
    revision_id: str | None = None,
    revision_no: int | None = None,
    status: str = STATUS_ACTIVE,
    created_by: str = "system:executor",
) -> dict | None:
    """把创作类 ArtifactRevision 的结构化负载登记为版本化创作产物。

    - 非创作 kind（如 `asset.analysis.*`）直接返回 `None`（空操作）；
    - 与 ArtifactRevision 登记处于**同一事务**，任一步失败则整体回滚；
    - 落库前再次通过权威 Pydantic 模型校验，并**强制** payload 身份与
      `ArtifactRevision` 身份一致（不传 `revision_id`/`revision_no` 时取自该行）。
    """
    kind = ARTIFACT_KIND_MAP.get(str(artifact_row.get("kind") or ""))
    if kind is None:
        return None
    if not isinstance(payload, dict):
        raise ValidationError("创作产物负载必须是 JSON 对象")
    validate_payload(kind, payload)

    resolved_id = str(revision_id or artifact_row["artifact_revision_id"])
    resolved_no = int(
        revision_no if revision_no is not None else artifact_row["revision_no"]
    )
    assert_payload_identity(
        kind, payload,
        revision_id=resolved_id, revision_no=resolved_no,
        project_id=artifact_row["project_id"], run_id=artifact_row["run_id"],
    )
    prompt_version = str(
        payload.get("prompt_version") or _DEFAULT_PROMPT_VERSIONS[kind]
    )
    model_id = str(payload.get("model_id") or _DEFAULT_MODEL_IDS[kind])
    return insert(
        conn, kind,
        project_id=artifact_row["project_id"],
        run_id=artifact_row["run_id"],
        tenant_id=artifact_row["tenant_id"],
        payload=payload,
        prompt_version=prompt_version,
        model_id=model_id,
        created_by=created_by,
        status=status,
        revision_id=resolved_id,
        revision_no=resolved_no,
    )


__all__ = [
    "ARTIFACT_KIND_MAP",
    "ID_FIELDS",
    "KINDS",
    "PENDING_ID_PREFIX",
    "STATUS_ACTIVE",
    "STATUS_DRAFT",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "activate",
    "artifact_kind",
    "assert_payload_identity",
    "finalize_payload",
    "get",
    "id_field",
    "insert",
    "latest",
    "latest_for_run",
    "list_for_project",
    "mark_rejected",
    "next_revision_no",
    "payload_of",
    "persist_for_artifact",
    "provisional_identity",
    "require",
    "set_status",
    "supersede_others",
    "validate_payload",
]
