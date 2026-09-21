"""Migration runner tests.

WHY THESE EXIST: a migration tool is the last thing you want to debug against a
live cluster, and every interesting behaviour here is about what happens WHEN
SOMETHING GOES WRONG - a statement fails halfway through a file, two runs collide,
a run-once file was edited. Those paths never execute on a happy deployment, so
without tests they are code nobody has ever run.

The database is a recording stub rather than a real Postgres. What is being
asserted is the RUNNER's contract - one transaction per file, a record written in
the same transaction as the DDL, a rollback that leaves nothing behind - and that
contract is visible in the ORDER of commit/rollback/execute calls. A live database
would confirm the SQL is valid, which is what ``npm run db-migrate-plan`` against a
deployed cluster is for.

The one thing a stub cannot check is that Postgres really rolls DDL back. It does;
that is why the per-file transaction is worth having.
"""

from __future__ import annotations

import importlib
import ssl
from typing import Any

import pytest


class _StubCursor:
    """Records every statement, and answers the two queries the runner reads."""

    def __init__(self, connection: _StubConnection) -> None:
        self.connection = connection
        self._result: list[tuple] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise RuntimeError("relation already exists")

        self.connection.executed.append((sql.strip(), params))

        if "pg_try_advisory_lock" in sql:
            self._result = [(self.connection.lock_available,)]
        elif "FROM schema_migration" in sql and sql.lstrip().upper().startswith("SELECT"):
            self._result = list(self.connection.applied.items())
        else:
            self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple:
        return self._result[0]

    def close(self) -> None:
        pass


class _StubConnection:
    def __init__(
        self,
        applied: dict[str, str] | None = None,
        lock_available: bool = True,
        fail_on: str | None = None,
    ) -> None:
        self.applied = applied or {}
        self.lock_available = lock_available
        #: Statements containing this substring raise, simulating a failed DDL.
        self.fail_on = fail_on
        self.executed: list[tuple[str, tuple | None]] = []
        #: 'commit' / 'rollback' / 'close', in order. The interesting assertion.
        self.calls: list[str] = []

    def cursor(self) -> _StubCursor:
        return _StubCursor(self)

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")

    def close(self) -> None:
        self.calls.append("close")

    # --- helpers for the assertions ---

    def statements_matching(self, needle: str) -> list[str]:
        return [sql for sql, _ in self.executed if needle in sql]

    def recorded(self) -> list[tuple]:
        return [params for sql, params in self.executed if "INSERT INTO schema_migration" in sql]


@pytest.fixture
def migration_dir(tmp_path):
    """Two migrations: one repeatable, one run-once. The shape sql/ actually has."""
    (tmp_path / "001-init.sql").write_text(
        "-- migration: repeatable\nCREATE TABLE IF NOT EXISTS a ();\nSELECT 1;\n",
        encoding="utf-8",
    )
    (tmp_path / "002-add-thing.sql").write_text(
        "ALTER TABLE a ADD COLUMN b int;\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def runner(migration_dir, monkeypatch):
    """The handler module with its database and secret access replaced.

    Reloaded per test for the same reason test_handlers.py reloads: the module
    builds its boto3 client at import time so a warm Lambda does not pay for it per
    invocation, which means the stub has to be installed after import.
    """
    module = importlib.import_module("corridor_event_hub.handlers.db_migrate")
    importlib.reload(module)
    monkeypatch.setattr(module, "sql_dir", lambda: migration_dir)
    monkeypatch.setenv("SPATIAL_DB_NAME", "corridoreventhub")
    return module


def run(runner, connection: _StubConnection, event: dict[str, Any] | None = None):
    runner._connect = lambda context: connection  # noqa: SLF001 - that is the seam
    return runner.handler(event or {}, None)


class TestPlanning:
    def test_a_dry_run_reports_what_would_apply(self, runner):
        connection = _StubConnection()
        report = run(runner, connection, {"dryRun": True})
        assert [p["filename"] for p in report["pending"]] == ["001-init.sql", "002-add-thing.sql"]
        assert report["applied"] == []

    def test_a_dry_run_writes_NOTHING(self, runner):
        # The whole promise of --plan. A plan that quietly created its own tracking
        # table would still be a write to a production database.
        connection = _StubConnection()
        run(runner, connection, {"dryRun": True})
        assert not connection.recorded()
        assert not connection.statements_matching("ALTER TABLE a")

    def test_a_dry_run_does_not_take_the_lock(self, runner):
        # It must never be able to block a real migration.
        connection = _StubConnection()
        run(runner, connection, {"dryRun": True})
        assert not connection.statements_matching("pg_try_advisory_lock")

    def test_the_report_names_every_file_and_its_kind(self, runner):
        report = run(runner, _StubConnection(), {"dryRun": True})
        kinds = {item["filename"]: item["kind"] for item in report["discovered"]}
        assert kinds == {"001-init.sql": "repeatable", "002-add-thing.sql": "run-once"}

    def test_an_already_applied_file_is_reported_unchanged(self, runner):
        from corridor_event_hub.core.migrations import load_migrations

        applied = {m.filename: m.checksum for m in load_migrations(runner.sql_dir())}
        report = run(runner, _StubConnection(applied=applied), {"dryRun": True})
        assert report["pending"] == []
        assert sorted(report["unchanged"]) == ["001-init.sql", "002-add-thing.sql"]


class TestApplying:
    def test_creates_its_own_tracking_table_first(self, runner):
        # Chicken and egg: the runner reads schema_migration to decide whether 001
        # needs applying, so the table cannot be defined by a migration.
        connection = run_and_return(runner)
        first_ddl = connection.statements_matching("CREATE TABLE IF NOT EXISTS schema_migration")
        assert first_ddl, "the tracking table was never created"
        # ...and lock_timeout is bounded before the runner touches anything, so a
        # blocked DDL fails fast instead of hanging the invocation.
        assert "set_config('lock_timeout'" in connection.executed[0][0]

    def test_applies_every_pending_file(self, runner):
        connection = run_and_return(runner)
        assert connection.statements_matching("CREATE TABLE IF NOT EXISTS a ()")
        assert connection.statements_matching("ALTER TABLE a ADD COLUMN b int")

    def test_applies_them_in_version_order(self, runner):
        connection = run_and_return(runner)
        recorded = [params[0] for params in connection.recorded()]
        assert recorded == ["001-init.sql", "002-add-thing.sql"]

    def test_records_the_checksum_it_applied(self, runner):
        from corridor_event_hub.core.migrations import load_migrations

        expected = {m.filename: m.checksum for m in load_migrations(runner.sql_dir())}
        connection = run_and_return(runner)
        for params in connection.recorded():
            filename, checksum = params[0], params[1]
            assert checksum == expected[filename]

    def test_records_the_file_in_the_SAME_transaction_as_its_ddl(self, runner):
        # The window this closes: DDL committed, record not written, next run applies
        # it again. So the INSERT must be the statement IMMEDIATELY after the file's
        # last DDL, with no commit between them - which is what "same transaction"
        # means when the only other call is connection.commit().
        connection = run_and_return(runner)
        order = [sql for sql, _ in connection.executed]
        alter_at = order.index("ALTER TABLE a ADD COLUMN b int")
        assert "INSERT INTO schema_migration" in order[alter_at + 1]

    def test_one_commit_per_file_plus_setup(self, runner):
        connection = run_and_return(runner)
        # setup, lock, 001, 002, unlock = 5. Asserting the SHAPE - a single commit
        # at the end would mean one giant transaction, and a commit per statement
        # would mean no rollback boundary at all.
        assert connection.calls.count("commit") == 5
        assert connection.calls[-1] == "close"

    def test_reports_what_it_applied(self, runner):
        report = run(runner, _StubConnection(), {})
        assert [item["filename"] for item in report["applied"]] == [
            "001-init.sql",
            "002-add-thing.sql",
        ]
        assert all(item["statements"] > 0 for item in report["applied"])

    def test_releases_the_lock(self, runner):
        connection = run_and_return(runner)
        assert connection.statements_matching("pg_advisory_unlock")


class TestFailure:
    def test_a_failed_statement_rolls_the_whole_file_back(self, runner):
        connection = _StubConnection(fail_on="ALTER TABLE a")
        with pytest.raises(RuntimeError):
            run(runner, connection, {})
        assert "rollback" in connection.calls
        # Nothing recorded for the file that failed.
        assert [params[0] for params in connection.recorded()] == ["001-init.sql"]

    def test_the_error_names_the_FILE_not_just_the_sql(self, runner):
        # A bare Postgres error tells you a column was duplicated. It does not tell
        # you which of six migrations said so.
        connection = _StubConnection(fail_on="ALTER TABLE a")
        with pytest.raises(RuntimeError, match="002-add-thing.sql failed and was rolled back"):
            run(runner, connection, {})

    def test_the_connection_is_closed_even_when_a_file_fails(self, runner):
        connection = _StubConnection(fail_on="ALTER TABLE a")
        with pytest.raises(RuntimeError):
            run(runner, connection, {})
        assert connection.calls[-1] == "close"

    def test_a_second_concurrent_run_refuses_rather_than_waiting(self, runner):
        # Waiting would hold a connection until the Lambda times out, and the
        # operator would see a timeout rather than "someone else is migrating".
        connection = _StubConnection(lock_available=False)
        with pytest.raises(RuntimeError, match="another migration is already running"):
            run(runner, connection, {})
        assert not connection.statements_matching("ALTER TABLE a")

    def test_an_empty_migration_directory_is_an_error(self, runner, tmp_path, monkeypatch):
        # In a Lambda this means the bundle was built without sql/, which otherwise
        # looks identical to "nothing to do".
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(runner, "sql_dir", lambda: empty)
        with pytest.raises(RuntimeError, match="no migrations found"):
            run(runner, _StubConnection(), {})


class TestDrift:
    def test_an_edited_run_once_file_blocks_the_whole_run(self, runner):
        connection = _StubConnection(applied={"002-add-thing.sql": "a-different-checksum"})
        with pytest.raises(RuntimeError, match="refusing to migrate"):
            run(runner, connection, {})

    def test_drift_blocks_BEFORE_anything_is_applied(self, runner):
        # Not "apply what we can, then complain": a partial migration set is how a
        # schema ends up in a state no file describes.
        connection = _StubConnection(applied={"002-add-thing.sql": "a-different-checksum"})
        with pytest.raises(RuntimeError):
            run(runner, connection, {})
        assert not connection.recorded()
        assert not connection.statements_matching("CREATE TABLE IF NOT EXISTS a ()")

    def test_a_dry_run_REPORTS_drift_instead_of_raising(self, runner):
        # Reporting is the entire job of a plan. Raising here would make
        # `npm run db-migrate-plan` unable to tell you the one thing you ran it for.
        connection = _StubConnection(applied={"002-add-thing.sql": "a-different-checksum"})
        report = run(runner, connection, {"dryRun": True})
        assert [item["filename"] for item in report["drifted"]] == ["002-add-thing.sql"]
        assert "NEW numbered file" in report["drifted"][0]["advice"]


def _recorded_cafile(monkeypatch, dbconn):
    """Which file ``ssl_context()`` chose to trust, without needing a valid one.

    ``create_default_context(cafile=...)`` parses the PEM, so asserting the routing with
    a real certificate would mean committing one and testing OpenSSL's parser. The
    defect being guarded against is a routing one - the old code took the unverified branch -
    so the branch taken is the thing worth pinning.
    """
    import ssl as ssl_module

    seen = {}
    real = ssl_module.create_default_context

    def capture(*args, **kwargs):
        seen["cafile"] = kwargs.get("cafile")
        return real()  # a working context, so callers can still inspect it

    monkeypatch.setattr(dbconn.ssl, "create_default_context", capture)
    dbconn.ssl_context()
    return seen.get("cafile")


class TestTlsPosture:
    """Verification is the DEFAULT now; it used to be the opt-in.

    The old behaviour was: no ``SPATIAL_DB_CA_BUNDLE`` set - the common case, because
    it was optional - meant hostname checking and certificate verification both off.
    Encrypted and unauthenticated, reached by doing nothing. These tests pin the
    inversion, including the part that matters most: that the insecure path cannot be
    reached by accident.
    """

    @pytest.fixture(autouse=True)
    def _clean_tls_env(self, monkeypatch, tmp_path):
        # Every input cleared for every test in this class, INCLUDING the bundled CA.
        # Inheriting any of them from the developer's shell - or from whether someone
        # happens to have run `npm run bundle` - would make these pass or fail for the
        # wrong reason. CEH_CERTS_DIR is pointed at an empty directory rather than
        # unset, because unset lets config.rds_ca_bundle() find the real bundle.
        monkeypatch.delenv("SPATIAL_DB_CA_BUNDLE", raising=False)
        monkeypatch.delenv("SPATIAL_DB_TLS_INSECURE", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        empty = tmp_path / "no-certs"
        empty.mkdir()
        monkeypatch.setenv("CEH_CERTS_DIR", str(empty))

    def test_verifies_by_default_with_nothing_configured(self, runner):
        from corridor_event_hub.core import dbconn

        context = dbconn.ssl_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

        report = run(runner, _StubConnection(), {"dryRun": True})
        assert "NOT verified" not in report["tls"]
        assert "verified against the platform trust store" in report["tls"]

    def test_uses_the_bundled_rds_chain_when_present(self, runner, tmp_path, monkeypatch):
        """THE DEPLOYED PATH, and it needs no configuration - which is the point.

        The RDS certificate authorities are SELF-SIGNED PRIVATE ROOTS
        (`Amazon RDS us-west-2 Root CA RSA2048 G1` is its own issuer), so the platform
        trust store cannot verify an Aurora certificate at all. Inverting the client's
        verify flag alone would have failed every connection rather than failing safe.
        scripts/build-lambda.sh fetches the chain into the bundle and asserts it is
        there; this asserts the runtime prefers it.

        WHICH FILE IS TRUSTED is what these tests check, by recording the ``cafile``
        rather than by supplying a real certificate. Embedding a valid PEM would test
        OpenSSL's parser, which works, and the defect being guarded against is a
        ROUTING one - the old code chose the unverified branch.
        """
        from corridor_event_hub.core import config, dbconn

        certs = tmp_path / "certs"
        certs.mkdir()
        pem = certs / config.RDS_CA_BUNDLE_NAME
        pem.write_text("-- placeholder; never parsed, see the docstring --\n", encoding="utf-8")
        monkeypatch.setenv("CEH_CERTS_DIR", str(certs))

        assert config.rds_ca_bundle() == pem
        assert _recorded_cafile(monkeypatch, dbconn) == str(pem)

        report = run(runner, _StubConnection(), {"dryRun": True})
        assert "bundled RDS chain" in report["tls"]

    def test_an_explicit_ca_bundle_beats_the_bundled_one(self, runner, tmp_path, monkeypatch):
        # The override has to win, so a rotated CA can be supplied without a rebuild.
        from corridor_event_hub.core import config, dbconn

        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / config.RDS_CA_BUNDLE_NAME).write_text("placeholder\n", encoding="utf-8")
        monkeypatch.setenv("CEH_CERTS_DIR", str(certs))

        # Inside a subdirectory, not tmp_path itself: `runner` scans tmp_path for
        # migrations, and load_migrations rejects a stray .pem beside the .sql files -
        # deliberately, and it is right to.
        override = certs / "override.pem"
        override.write_text("placeholder\n", encoding="utf-8")
        monkeypatch.setenv("SPATIAL_DB_CA_BUNDLE", str(override))

        assert _recorded_cafile(monkeypatch, dbconn) == str(override)

        report = run(runner, _StubConnection(), {"dryRun": True})
        assert str(override) in report["tls"]
        assert "bundled" not in report["tls"]

    def test_the_bundled_chain_beats_the_insecure_escape_hatch(self, tmp_path, monkeypatch):
        # Order matters: a working verified path must not be pre-empted by a stale
        # SPATIAL_DB_TLS_INSECURE left in someone's shell profile or, worse, in the
        # stack. Checked because getting this order wrong is silent.
        from corridor_event_hub.core import config, dbconn

        certs = tmp_path / "certs"
        certs.mkdir()
        pem = certs / config.RDS_CA_BUNDLE_NAME
        pem.write_text("placeholder\n", encoding="utf-8")
        monkeypatch.setenv("CEH_CERTS_DIR", str(certs))
        monkeypatch.setenv("SPATIAL_DB_TLS_INSECURE", "1")

        assert _recorded_cafile(monkeypatch, dbconn) == str(pem)

    def test_reports_verification_when_a_ca_bundle_is_present(self, runner, tmp_path, monkeypatch):
        # Outside the migration directory: a stray .pem beside the .sql files is
        # itself an error, and load_migrations says so.
        certs = tmp_path / "certs"
        certs.mkdir()
        bundle = certs / "rds-ca.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
        monkeypatch.setenv("SPATIAL_DB_CA_BUNDLE", str(bundle))
        report = run(runner, _StubConnection(), {"dryRun": True})
        assert "verified against" in report["tls"]

    def test_a_ca_bundle_path_that_does_not_exist_raises(self, monkeypatch):
        # It used to fall through to the unverified context, which made a TYPO in the
        # path indistinguishable from a decision not to verify.
        from corridor_event_hub.core import dbconn

        monkeypatch.setenv("SPATIAL_DB_CA_BUNDLE", "/no/such/rds-ca.pem")
        with pytest.raises(RuntimeError, match="does not exist"):
            dbconn.ssl_context()

    def test_the_insecure_escape_hatch_works_locally(self, monkeypatch):
        # A local Postgres with a self-signed certificate is a real need, which is why
        # this path exists at all rather than being deleted.
        from corridor_event_hub.core import dbconn

        monkeypatch.setenv("SPATIAL_DB_TLS_INSECURE", "1")
        context = dbconn.ssl_context()
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

    def test_the_insecure_escape_hatch_is_refused_inside_lambda(self, monkeypatch):
        # THE LOAD-BEARING ASSERTION. An unauthenticated connection to the corridor
        # database must not be reachable by setting an environment variable in the
        # stack - that is the same shape of defect as the original finding.
        from corridor_event_hub.core import dbconn

        monkeypatch.setenv("SPATIAL_DB_TLS_INSECURE", "true")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "corridor-event-hub-normalizer")
        with pytest.raises(RuntimeError, match="deployed function"):
            dbconn.ssl_context()

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  "])
    def test_a_falsey_value_does_not_disable_verification(self, monkeypatch, value):
        # `bool(os.environ.get(...))` reads the string "false" as true, which is a
        # footgun on a variable whose only job is turning a security control off.
        from corridor_event_hub.core import dbconn

        monkeypatch.setenv("SPATIAL_DB_TLS_INSECURE", value)
        assert dbconn.ssl_context().verify_mode == ssl.CERT_REQUIRED

    def test_an_explicit_context_is_always_passed(self):
        # The substantive part, and it was always right: pg8000's default silently
        # falls back to PLAINTEXT if the server does not offer TLS. An explicit
        # context makes that an error.
        from corridor_event_hub.core import dbconn

        assert dbconn.ssl_context() is not None


def run_and_return(runner, **kwargs) -> _StubConnection:
    connection = _StubConnection(**kwargs)
    run(runner, connection, {})
    return connection
