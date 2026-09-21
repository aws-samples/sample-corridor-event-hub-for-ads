"""Static checks on sql/, for the one bug the SQL cannot catch itself.

WHY THIS FILE EXISTS: `CREATE TABLE IF NOT EXISTS` is a NO-OP once the table
exists. Adding a column to its body therefore changes nothing on a cluster that
has already been through the file - it works perfectly on a fresh database and
fails on every existing one. And it does not fail AT the table: it fails at
whatever later statement first references the missing column, which can be
hundreds of lines away in a view.

That has now happened twice in this schema:

  centerline_m    added to corridor's body only; failed inside conflate_point with
                  "column centerline_m does not exist"
  relation        added to bridge_structure's body only, with the ALTER that
                  creates it sitting in 003 - which runs AFTER 001. 001 failed
                  with "column b.relation does not exist", pointing at
                  corridor_clearances, and rolled the whole migration back

Both were found by applying to a real cluster, which is the expensive place. These
tests find them from the text, with no database.

They are deliberately narrow. They do not typecheck SQL - `npm run db-migrate-plan`
against a deployed cluster is the only thing that really does. They check the ONE
invariant that a repeatable migration has to hold: it must apply to a database
built by an older version of itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
INIT = SQL_DIR / "001-init.sql"

# `ALTER TABLE <t> ... ADD COLUMN [IF NOT EXISTS] <col>` - one ALTER may add several.
_ALTER = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)(.*?);",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL,
)


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def added_columns(sql: str) -> set[tuple[str, str]]:
    """{(table, column)} added by ALTER ... ADD COLUMN anywhere in ``sql``."""
    found = set()
    for table, body in _ALTER.findall(strip_comments(sql)):
        for column in _ADD_COLUMN.findall(body):
            found.add((table.lower(), column.lower()))
    return found


def declared_columns(sql: str) -> set[tuple[str, str]]:
    """{(table, column)} declared in a CREATE TABLE body."""
    found = set()
    for table, body in _CREATE_TABLE.findall(strip_comments(sql)):
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("CONSTRAINT", "PRIMARY KEY", "CHECK", "UNIQUE", "FOREIGN KEY")):
                continue
            name = line.split()[0]
            if name.isidentifier():
                found.add((table.lower(), name.lower()))
    return found


def migrations() -> list[Path]:
    return sorted(p for p in SQL_DIR.glob("*.sql") if re.match(r"^\d+-", p.name))


def baseline_columns() -> set[tuple[str, str]] | None:
    """The columns 001 had when it was first committed, or None without git.

    THIS IS THE ONLY HONEST BASELINE. Whether a column needs an `ALTER ... ADD
    COLUMN IF NOT EXISTS` depends on one thing: could a cluster already exist
    without it? Columns present when the file was first committed cannot be
    missing from any cluster that ran it. Columns added afterwards can, and those
    are the ones that need the ALTER.

    Nothing in the text of the current file distinguishes the two - which is
    precisely why this bug got through twice - so the test reads git rather than
    guessing.
    """
    import subprocess

    repo = SQL_DIR.parent.parent
    # Relative to the repo root, and DERIVED rather than written out: this path has
    # already moved once (skeleton/sql -> src/sql) and a stale literal here does not
    # fail, it returns None and the caller skips - losing the one test that has caught
    # this bug twice, silently. --follow is the other half: without it the rename
    # truncates history at the move and the "first commit" is the move itself, which
    # would treat every existing column as a new one.
    rel = INIT.relative_to(repo).as_posix()
    try:
        first = subprocess.run(
            ["git", "log", "--follow", "--diff-filter=A", "--format=%H", "--", rel],
            cwd=repo, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
        if not first:
            return None
        # The add commit knows the file by its ORIGINAL path, so ask git for the name
        # it had there rather than assuming today's.
        historic = subprocess.run(
            ["git", "log", "--follow", "--name-only", "--diff-filter=A", "--format=", "--", rel],
            cwd=repo, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
        original = subprocess.run(
            ["git", "show", f"{first[-1]}:{historic[-1] if historic else rel}"],
            cwd=repo, capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except Exception:
        return None
    if not original.strip():
        return None
    return declared_columns(original) | added_columns(original)


class TestSelfContainedInit:
    """001 must apply to a cluster built by an older 001."""

    def test_every_column_added_since_the_first_commit_has_an_ALTER(self):
        """The rule that was broken, twice, stated so it fails here instead of there.

        Adding a column to a `CREATE TABLE IF NOT EXISTS` body is a NO-OP on any
        cluster that already ran the file. It works on a fresh database and fails on
        every existing one, at whatever statement first touches the column - which is
        how `relation` surfaced as "column b.relation does not exist" inside a view
        three hundred lines from the table.

        So: any column that is in 001 today but was not in 001 at the first commit
        MUST also be added by an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
        """
        baseline = baseline_columns()
        if baseline is None:
            pytest.skip("no git history available to establish the baseline")

        current_text = INIT.read_text(encoding="utf-8")
        altered = added_columns(current_text)
        new_columns = (declared_columns(current_text) | altered) - baseline

        missing = sorted(
            f"{table}.{column}" for table, column in new_columns if (table, column) not in altered
        )
        assert not missing, (
            "added to 001 after the first commit, but with no ALTER - so these exist "
            "on a fresh cluster and NOT on an existing one:\n  "
            + "\n  ".join(missing)
            + "\n\nAdd `ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <c> <type>;` to "
            "001-init.sql, next to the table it belongs to."
        )

    def test_every_column_any_migration_adds_is_also_added_by_001(self):
        """The rule that was broken, stated so it cannot be broken silently again.

        001 runs FIRST and is the schema of record - its views and functions may
        reference any column in the schema. So a column introduced by a later file's
        ALTER must also be added by an ALTER in 001, or 001 references something that
        does not exist yet on an existing cluster.

        Declaring it in 001's CREATE TABLE body is NOT enough. That is the whole bug.
        """
        in_init = added_columns(INIT.read_text(encoding="utf-8"))
        missing = []
        for path in migrations():
            if path == INIT:
                continue
            for table, column in added_columns(path.read_text(encoding="utf-8")):
                if (table, column) not in in_init:
                    missing.append(f"{table}.{column} is added by {path.name} but not by 001")
        assert not missing, (
            "these columns exist on a fresh cluster and not on an existing one:\n  "
            + "\n  ".join(missing)
            + "\n\nAdd `ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <c> <type>;` to "
            "001-init.sql, next to the table it belongs to."
        )

    def test_every_column_a_view_selects_is_declared_in_001(self):
        """Catches the exact failure: corridor_clearances selecting b.relation.

        A view in 001 may only reference columns 001 itself declares - in a CREATE
        TABLE body for fresh clusters AND in an ALTER for existing ones.
        """
        sql = INIT.read_text(encoding="utf-8")
        known = {c for _, c in declared_columns(sql)} | {c for _, c in added_columns(sql)}

        views = re.findall(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)\s+AS(.*?);",
            strip_comments(sql),
            re.IGNORECASE | re.DOTALL,
        )
        assert views, "no views found in 001 - did they move?"

        unknown = []
        for view_name, body in views:
            # Only `b.<col>` style references: the aliases in this schema are single
            # letters, and function output columns (mp.state) are not table columns.
            for alias, column in re.findall(r"\b([a-z])\.(\w+)\b", body):
                if alias == "b" and column.lower() not in known:
                    unknown.append(f"{view_name} selects b.{column}, which 001 never declares")
        assert not unknown, (
            "\n  ".join(unknown)
            + "\n\nThis is the failure that rolled back a whole migration: the view is "
            "hundreds of lines from the table it is really complaining about."
        )


class TestViewsAreReplaceable:
    def test_every_view_is_dropped_before_it_is_created(self):
        """CREATE OR REPLACE VIEW may only APPEND columns.

        It cannot rename a column or insert one in the middle, and the error blames
        the wrong thing when you try:

          cannot change name of view column "facility_carried" to "relation"  (42P16)

        Nobody wrote a rename. The real cause is a new column inserted at position 9.
        A repeatable migration has to be editable, so views are dropped and recreated
        rather than replaced - otherwise the file carries an unwritten rule that new
        columns may only go at the end.
        """
        offenders = []
        for path in migrations():
            sql = strip_comments(path.read_text(encoding="utf-8"))
            for name in re.findall(
                r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)", sql, re.IGNORECASE
            ):
                dropped = re.search(
                    rf"DROP\s+VIEW\s+IF\s+EXISTS\s+{re.escape(name)}\b", sql, re.IGNORECASE
                )
                created = re.search(
                    rf"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+{re.escape(name)}\b", sql, re.IGNORECASE
                )
                if not dropped or dropped.start() > created.start():
                    offenders.append(f"{path.name}: {name} is created without a preceding DROP")
        assert not offenders, (
            "\n  ".join(offenders)
            + "\n\nAdd `DROP VIEW IF EXISTS <name>;` immediately before it. Without that, "
            "reordering or inserting a column fails with 42P16 on any existing cluster."
        )


class TestFunctionCallTypes:
    def test_conflate_point_calls_cast_geometry_accessors_to_numeric(self):
        """ST_X and ST_Y return double precision; conflate_point takes numeric.

        Postgres does NOT implicitly cast float8 -> numeric when resolving a function
        call, and the error blames the wrong thing:

          function conflate_point(text, double precision, double precision) does not exist

        which reads as a missing function rather than a type mismatch. Grammar checks
        do not catch it either - the call parses perfectly.
        """
        offenders = []
        for path in sorted(SQL_DIR.rglob("*.sql")):
            sql = strip_comments(path.read_text(encoding="utf-8"))
            for call in re.findall(r"conflate_point\s*\(((?:[^()]|\([^()]*\))*)\)", sql):
                if re.search(r"\bST_[XY]\b", call) and "::numeric" not in call:
                    offenders.append(f"{path.name}: conflate_point({call.strip()[:70]}...)")
        assert not offenders, (
            "\n  ".join(offenders)
            + "\n\nCast the coordinates: ST_X(...)::numeric, ST_Y(...)::numeric"
        )


class TestGeneratedMigrations:
    def test_a_generated_migration_declares_no_columns_of_its_own(self):
        """Data files load data; 001 owns the shape.

        003 originally carried the ALTER for its own columns, which put the column
        definition AFTER the view that reads it. Keeping schema in 001 and data in the
        numbered files makes the ordering impossible to get wrong.
        """
        offenders = []
        for path in migrations():
            if path == INIT:
                continue
            text = path.read_text(encoding="utf-8")
            if "DO NOT EDIT" not in text:
                continue  # hand-written migrations may legitimately alter the schema
            for table, column in added_columns(text):
                offenders.append(f"{path.name} adds {table}.{column}")
        assert not offenders, (
            "generated migrations must not add columns:\n  " + "\n  ".join(offenders)
        )


class TestEveryMigrationIsDeclaredRepeatableOrNot:
    @pytest.mark.parametrize("path", migrations(), ids=lambda p: p.name)
    def test_a_generated_migration_is_marked_repeatable(self, path):
        """A generated file's checksum changes whenever it is regenerated.

        Without the directive that is reported as drift and never applied - so the
        next NBI vintage or centerline rebuild would silently not load.
        """
        text = path.read_text(encoding="utf-8")
        if "DO NOT EDIT" not in text:
            pytest.skip("hand-written; run-once is the correct default")
        assert re.search(r"^\s*--\s*migration:\s*repeatable\s*$", text, re.MULTILINE), (
            f"{path.name} is generated but not declared repeatable"
        )
