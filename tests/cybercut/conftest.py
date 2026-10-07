"""CyberCut 测试夹具。

- 纯单元测试（状态机、路径安全）**不依赖数据库**，无 PG 也能运行；
- 集成测试显式请求 `db` 夹具：PG 不可用时**明确 skip**（打印原因），
  可通过环境变量 `CYBERCUT_TEST_PG_DSN` 指定测试库。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# ── 让 `src/` 可导入（仓库未把 cybercut 装成包）──
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cybercut.storage import migrate  # noqa: E402
from cybercut.storage.db import Database  # noqa: E402

DEFAULT_TEST_DSN = "postgresql://postgres@127.0.0.1:55432/cybercut_test"

_TRUNCATE_TABLES = (
    "events",
    "idempotency_keys",
    "approvals",
    "artifact_revisions",
    "node_attempts",
    "node_runs",
    "runs",
    "projects",
)


def test_dsn() -> str:
    return (
        os.environ.get("CYBERCUT_TEST_PG_DSN", "").strip()
        or os.environ.get("CYBERCUT_PG_DSN", "").strip()
        or DEFAULT_TEST_DSN
    )


@pytest.fixture(scope="session")
def db() -> Database:
    """会话级数据库（不存在则 skip）。"""
    database = Database(test_dsn())
    if not database.ping():
        pytest.skip(
            f"PostgreSQL 不可用（{test_dsn()}）；设置 CYBERCUT_TEST_PG_DSN 后重跑集成测试"
        )
    migrate.apply_migrations(database)
    return database


@pytest.fixture(autouse=True)
def _truncate_for_db_tests(request: pytest.FixtureRequest):
    """仅对请求了 `db` 的测试清空业务表（保持测试隔离）。"""
    if "db" not in request.fixturenames:
        yield
        return
    database: Database = request.getfixturevalue("db")
    with database.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE "
                + ", ".join(f"cybercut.{t}" for t in _TRUNCATE_TABLES)
                + " RESTART IDENTITY CASCADE"
            )
    yield


#: 测试用数据根目录（**不使用 pytest tmp_path**：其清理逻辑在受限环境下会失败，
#:  且被测代码只需要一个可写的 ASCII 目录）
_DATA_ROOT_BASE = Path(os.environ.get("CYBERCUT_TEST_TMP", "D:/cybercut-wb01-tmp"))


@pytest.fixture
def data_root() -> Path:
    """每个测试一个独立数据目录（保留产物，便于排障；不参与自动清理）。"""
    root = _DATA_ROOT_BASE / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root
