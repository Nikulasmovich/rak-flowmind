from cybercut.executor.claim import claim_next_attempt


class _Cursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return None


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance


class _Database:
    def __init__(self):
        self.connection = _Connection()

    def transaction(self):
        return self.connection


def test_run_scoped_claim_filters_by_run_id():
    db = _Database()

    assert claim_next_attempt(db, "worker-1", run_id="run-1") is None

    cursor = db.connection.cursor_instance
    assert "nr.run_id = %s" in cursor.sql
    assert cursor.params == ("run-1", "run-1", "worker-1")


def test_unscoped_claim_remains_available_for_regular_workers():
    db = _Database()

    assert claim_next_attempt(db, "worker-1") is None
    assert db.connection.cursor_instance.params == (None, None, "worker-1")
