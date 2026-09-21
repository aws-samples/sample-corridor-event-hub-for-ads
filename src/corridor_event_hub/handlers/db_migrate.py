"""Apply pending schema migrations to the spatial database.

A bastion-free one-shot function that reaches the isolated-subnet cluster without
standing up an access path someone then has to justify in a security review. No SSH
keys, no EC2 instance, no port forwarding, nothing to tear down - and it works from
CI, which a laptop with ``psql`` does not. See docs/SPATIAL-DB.md section 2.

WHAT THIS FIXES THAT scripts/db.sh DOES NOT. db.sh applies a file statement by
statement over the Data API and keeps going after a failure, which leaves the
schema HALF-APPLIED and re-running as the only remedy. It also has no record of
what has run, so a numbered file can be applied twice or never and nothing says
which. This function:

  1. records every application in ``schema_migration`` (which it creates itself -
     see below), so "has 002 run?" has an answer;
  2. wraps each file in ONE transaction. Postgres has transactional DDL, so a
     failure rolls the whole file back rather than stopping midway. A migration
     either happened or it did not;
  3. refuses to re-run an edited run-once file, and says what to do instead
     (core/migrations.py explains the drift rules);
  4. takes a session advisory lock, so two operators - or an operator and CI -
     cannot apply concurrently.

It does NOT depend on the Data API, which matters: docs/SPATIAL-DB.md recommends a
production DOT deployment set ``enableDataApi: false``, and that is exactly when a
migration path needs to still exist.

DELIBERATELY NOT WIRED TO DEPLOY. A CDK custom resource could invoke this on every
``cdk deploy``, and this function stays a manual/CI step instead, for two reasons:
the Provider framework injects its own Lambdas that run outside the VPC unless
explicitly placed (ADR 0001, and scripts/check-vpc.sh would fail), and a schema
change that happens as a side effect of deploying application code is the wrong
default for a system a state DOT operates. Migrating is a decision, so it is a
command.

THE DRIVER IS pg8000, which is PURE PYTHON. psycopg is the more conventional
choice and would be defensible here, but the bundle's one hard-won lesson is that
a compiled wheel built for the wrong platform deploys perfectly and fails at
invoke (see scripts/build-lambda.sh). A migration runner does no query-heavy work,
so there is nothing to buy with a C driver and one whole class of failure to avoid
by not having one. Nothing here constrains what PostgisConflator later uses.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from typing import Any

from ..core import dbconn
from ..core.config import sql_dir
from ..core.migrations import Migration, load_migrations, plan

# One arbitrary but STABLE key. Any process applying migrations takes this lock,
# so a second one fails fast instead of interleaving DDL with the first.
# 0x4143434C is 'ACCL'.
_ADVISORY_LOCK_KEY = 0x4143434C

# Long enough for an index build on a reference table, short enough that a lock
# held by someone else's open transaction fails with a clear error rather than
# consuming the whole Lambda timeout.
_LOCK_TIMEOUT = "15s"

_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
  filename    text PRIMARY KEY,
  checksum    text NOT NULL,
  statements  integer NOT NULL,
  repeatable  boolean NOT NULL DEFAULT false,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_ms  integer,
  applied_by  text
)
"""

# WHY THE RUNNER CREATES ITS OWN TABLE rather than 001-init.sql doing it: the
# runner has to read this table to decide whether 001 needs applying, so a
# tracking table defined inside a migration cannot exist the first time it is
# needed. It is infrastructure for the migrations, not part of the schema they
# describe.


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """Apply everything pending.

    ``{"dryRun": true}`` reports the plan and touches nothing - which is the safe
    thing to run first, and what ``npm run db-migrate-plan`` does.

    Raises on drift or on a failed statement. A migration that did not fully apply
    must be a FAILED invocation: returning 200 with a sad field in the body is how
    a broken schema ends up looking healthy on a dashboard.
    """
    event = event or {}
    dry_run = bool(event.get("dryRun"))

    directory = sql_dir()
    migrations = load_migrations(directory)
    if not migrations:
        raise RuntimeError(
            f"no migrations found in {directory}. In a Lambda this means the bundle "
            "was built without sql/ - run: npm run bundle"
        )

    connection = _connect(context)
    try:
        with closing(connection.cursor()) as cursor:
            # set_config() rather than SET because SET takes no bind parameters, so it
            # can only be written as string interpolation - which reads like SQL
            # injection to a scanner and would be the real thing if either value ever
            # stopped being a local constant.
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", (_LOCK_TIMEOUT,))
            statement_timeout_ms = _statement_timeout_ms(context)
            if statement_timeout_ms:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(statement_timeout_ms),),
                )
            cursor.execute(_TRACKING_TABLE_DDL)
        connection.commit()

        applied_before = _read_applied(connection)
        the_plan = plan(migrations, applied_before)

        report: dict[str, Any] = {
            "database": os.environ.get("SPATIAL_DB_NAME", "?"),
            "migrationDir": str(directory),
            "dryRun": dry_run,
            "discovered": [
                {"filename": m.filename, "kind": m.kind, "statements": len(m.statements)}
                for m in migrations
            ],
            "unchanged": list(the_plan.unchanged),
            "drifted": [
                {
                    "filename": d.filename,
                    "appliedChecksum": d.applied_checksum,
                    "fileChecksum": d.file_checksum,
                    "advice": d.advice,
                }
                for d in the_plan.drifted
            ],
            "pending": [
                {"filename": p.migration.filename, "reason": p.reason} for p in the_plan.pending
            ],
            "applied": [],
            "tls": dbconn.tls_posture(),
        }

        if dry_run:
            # Drift is still reported, and reporting is the whole job of a plan.
            # Failing here would make `db-migrate-plan` unable to TELL you about
            # drift, which is the one thing you ran it to find out.
            _log("migration_plan", report)
            return report

        if the_plan.drifted:
            _log("migration_blocked_by_drift", report)
            raise RuntimeError(
                "refusing to migrate - a run-once migration was edited after it was "
                "applied:\n  " + "\n  ".join(d.advice for d in the_plan.drifted)
            )

        # The lock is taken only for a real run: a dry run reads nothing it could
        # race with, and should never be able to block an actual migration.
        _acquire_lock(connection)
        try:
            for pending in the_plan.pending:
                report["applied"].append(
                    _apply(connection, pending.migration, pending.reason, context)
                )
        finally:
            _release_lock(connection)

        _log("migration_complete", report)
        return report
    finally:
        # Closing also drops any advisory lock still held - belt and braces for the
        # case where the release itself fails.
        connection.close()


def _apply(
    connection: Any, migration: Migration, reason: str, context: Any
) -> dict[str, Any]:
    """Apply one file in ONE transaction, then record it.

    The record is written inside the same transaction as the DDL. Anything else
    leaves a window where the schema changed and nothing knows - and that window is
    where the double-apply lives.
    """
    started = time.monotonic()
    try:
        with closing(connection.cursor()) as cursor:
            for statement in migration.statements:
                cursor.execute(statement)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            cursor.execute(
                """
                INSERT INTO schema_migration
                    (filename, checksum, statements, repeatable, applied_at, applied_ms, applied_by)
                VALUES (%s, %s, %s, %s, now(), %s, %s)
                ON CONFLICT (filename) DO UPDATE SET
                    checksum   = EXCLUDED.checksum,
                    statements = EXCLUDED.statements,
                    repeatable = EXCLUDED.repeatable,
                    applied_at = EXCLUDED.applied_at,
                    applied_ms = EXCLUDED.applied_ms,
                    applied_by = EXCLUDED.applied_by
                """,
                (
                    migration.filename,
                    migration.checksum,
                    len(migration.statements),
                    migration.repeatable,
                    elapsed_ms,
                    _applied_by(context),
                ),
            )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        # Name the FILE. A bare Postgres error in a log tells you a column was
        # duplicated; it does not tell you which of six migrations said so.
        raise RuntimeError(f"{migration.filename} failed and was rolled back: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result = {
        "filename": migration.filename,
        "reason": reason,
        "kind": migration.kind,
        "statements": len(migration.statements),
        "ms": elapsed_ms,
    }
    _log("migration_applied", result)
    return result


def _read_applied(connection: Any) -> dict[str, str]:
    with closing(connection.cursor()) as cursor:
        cursor.execute("SELECT filename, checksum FROM schema_migration")
        return {row[0]: row[1] for row in cursor.fetchall()}


def _acquire_lock(connection: Any) -> None:
    """Session advisory lock. Fails fast rather than queueing behind another run.

    ``pg_try_advisory_lock`` returns false instead of waiting, which is what we
    want: a second concurrent migration should stop and say so, not sit on a
    connection until the Lambda times out and leaves the operator guessing.
    """
    with closing(connection.cursor()) as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        got_lock = cursor.fetchone()[0]
    connection.commit()  # session-scoped: the lock survives this commit
    if not got_lock:
        raise RuntimeError(
            "another migration is already running (advisory lock held). Wait for it "
            "rather than forcing this one - two processes applying DDL to the same "
            "schema is how you get a half-migrated database."
        )


def _release_lock(connection: Any) -> None:
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        connection.commit()
    except Exception as exc:  # noqa: BLE001 - closing the connection releases it anyway
        _log("advisory_unlock_failed", {"error": str(exc)})


def _connect(context: Any) -> Any:
    """The shared connection. Kept as a named seam so tests can replace it.

    The credential path, the endpoint resolution and the TLS posture all moved to
    core/dbconn.py once the conflator needed them too: two clients meant two chances
    to get them subtly different, and the one that was wrong would have been the one
    nobody exercised.
    """
    return dbconn.connect()


def _statement_timeout_ms(context: Any) -> int | None:
    """Bound a single statement inside the invocation.

    Without this a statement blocked on a lock runs until Lambda kills the whole
    function, and the only evidence is a timeout with no indication of which
    statement. With it, Postgres cancels and names the statement.
    """
    remaining = _remaining_seconds(context)
    if remaining <= 0:
        return None
    return max(1000, (remaining - 5) * 1000)


def _remaining_seconds(context: Any) -> int:
    remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
    if remaining_ms is None:
        return 0  # invoked outside Lambda; let the caller's own limits apply
    return int(remaining_ms() / 1000)


def _applied_by(context: Any) -> str:
    """Enough to find the invocation in CloudWatch from a table row."""
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local")
    request_id = getattr(context, "aws_request_id", None)
    return f"{function_name}:{request_id}" if request_id else function_name


def _log(message: str, payload: dict[str, Any]) -> None:
    print(json.dumps({"msg": message, **payload}, default=str))


def main() -> int:
    """Run against a reachable database from a shell, for local Postgres work.

    Not the deployed path - Aurora sits in an isolated subnet, so this only reaches
    a local or tunnelled server. Kept because a migration runner you cannot try
    without deploying is one nobody tries.
    """
    import argparse

    parser = argparse.ArgumentParser(description="apply pending schema migrations")
    parser.add_argument("--dry-run", action="store_true", help="report the plan, change nothing")
    args = parser.parse_args()

    report = handler({"dryRun": args.dry_run})
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["drifted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
