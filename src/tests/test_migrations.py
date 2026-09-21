"""Migration planning and statement splitting.

WHY THESE EXIST: both halves of this module fail SILENTLY and expensively.

A splitter bug does not raise - it sends Postgres half a function body, which
either errors somewhere unrelated or, worse, succeeds as something different. The
version this replaces tracked only `$$` and would have cut a `$func$` body in
two; that is not a hypothetical, it is why docs/SPATIAL-DB.md carried a "stick to
$$" warning.

A planning bug is worse: applying a run-once migration twice, or deciding an
edited file was already applied, leaves the cluster and the repository disagreeing
about the schema with nothing reporting it. Every rule in `plan()` is here.

The real sql/001-init.sql is exercised at the bottom, because the splitter's
actual job is that file and a fixture cannot go stale in the same way.
"""

from __future__ import annotations

import re

import pytest

from corridor_event_hub.core.config import sql_dir
from corridor_event_hub.core.migrations import (
    Migration,
    checksum,
    is_repeatable,
    load_migrations,
    plan,
    split_statements,
)


def migration(filename: str, sql: str) -> Migration:
    return Migration(
        filename=filename,
        version=int(filename.split("-")[0]),
        sql=sql,
        checksum=checksum(sql),
        repeatable=is_repeatable(sql),
        statements=tuple(split_statements(sql)),
    )


class TestSplitStatements:
    def test_splits_on_semicolons(self):
        assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]

    def test_drops_the_trailing_comment_after_the_last_statement(self):
        # Sending this to the server is an ERROR, not a no-op, and every one of
        # these files ends with a comment block.
        assert split_statements("SELECT 1;\n-- done\n") == ["SELECT 1"]

    def test_keeps_a_dollar_quoted_body_whole(self):
        sql = """
        CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$
          SELECT 1;
          SELECT 2;
        $$;
        """
        statements = split_statements(sql)
        assert len(statements) == 1
        assert "SELECT 1;" in statements[0]
        assert "SELECT 2;" in statements[0]

    def test_keeps_a_TAGGED_dollar_quote_whole(self):
        # The bug in the line-based splitter this replaces: it only knew `$$`, so a
        # body using another tag was split at its first internal semicolon.
        sql = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $func$ SELECT 1; SELECT 2; $func$;"
        assert len(split_statements(sql)) == 1

    def test_two_functions_are_two_statements(self):
        sql = "CREATE FUNCTION a() RETURNS int AS $$ SELECT 1; $$;\n" \
              "CREATE FUNCTION b() RETURNS int AS $$ SELECT 2; $$;"
        assert len(split_statements(sql)) == 2

    def test_a_semicolon_inside_a_string_is_not_a_boundary(self):
        sql = "COMMENT ON TABLE t IS 'first; second';"
        assert split_statements(sql) == ["COMMENT ON TABLE t IS 'first; second'"]

    def test_an_escaped_quote_does_not_end_the_string(self):
        sql = "COMMENT ON TABLE t IS 'it''s fine; really';"
        assert len(split_statements(sql)) == 1

    def test_a_semicolon_in_a_line_comment_is_not_a_boundary(self):
        assert split_statements("SELECT 1 -- ; not here\n;") == ["SELECT 1 -- ; not here"]

    def test_a_semicolon_in_a_block_comment_is_not_a_boundary(self):
        assert len(split_statements("SELECT /* ; */ 1;")) == 1

    def test_nested_block_comments_are_tracked(self):
        # Postgres nests /* */, unlike most dialects. Getting this wrong ends the
        # comment early and the rest of the comment becomes SQL.
        assert len(split_statements("SELECT /* a /* b */ ; c */ 1;")) == 1

    def test_a_dollar_placeholder_is_not_a_quote(self):
        # `$1` must not open a dollar-quoted block, or everything after it is
        # swallowed into one statement.
        assert len(split_statements("SELECT $1; SELECT $2;")) == 2

    def test_a_statement_without_a_trailing_semicolon_still_counts(self):
        assert split_statements("SELECT 1") == ["SELECT 1"]

    def test_an_empty_script_produces_nothing(self):
        assert split_statements("") == []
        assert split_statements("\n-- only a comment\n") == []


class TestRepeatableDirective:
    def test_detects_the_directive(self):
        assert is_repeatable("-- migration: repeatable\nCREATE TABLE t ();")

    def test_is_case_and_space_insensitive(self):
        assert is_repeatable("--   Migration:  REPEATABLE  ")

    def test_absent_means_run_once(self):
        # The SAFE default: a file that cannot survive a second application must
        # not be re-applied just because someone forgot to say so.
        assert not is_repeatable("CREATE TABLE t ();")

    def test_the_words_in_prose_do_not_count(self):
        assert not is_repeatable("-- this migration is repeatable in spirit\nSELECT 1;")


class TestPlan:
    def test_a_never_applied_migration_is_pending(self):
        result = plan([migration("001-a.sql", "SELECT 1;")], {})
        assert [p.migration.filename for p in result.pending] == ["001-a.sql"]
        assert [p.reason for p in result.pending] == ["new"]

    def test_an_unchanged_migration_is_left_alone(self):
        one = migration("001-a.sql", "SELECT 1;")
        result = plan([one], {"001-a.sql": one.checksum})
        assert result.is_empty
        assert result.unchanged == ("001-a.sql",)

    def test_an_edited_repeatable_migration_is_reapplied(self):
        one = migration("001-a.sql", "-- migration: repeatable\nSELECT 2;")
        result = plan([one], {"001-a.sql": checksum("-- migration: repeatable\nSELECT 1;")})
        assert [p.reason for p in result.pending] == ["changed"]
        assert not result.drifted

    def test_an_edited_RUN_ONCE_migration_is_drift_and_is_not_applied(self):
        # The case this whole module exists for. Re-running it would double-apply;
        # ignoring it would leave the database silently unlike the repository.
        one = migration("002-b.sql", "ALTER TABLE t ADD COLUMN c int;")
        result = plan([one], {"002-b.sql": checksum("ALTER TABLE t ADD COLUMN b int;")})
        assert result.is_empty
        assert [d.filename for d in result.drifted] == ["002-b.sql"]

    def test_drift_advice_names_both_checksums_and_the_remedy(self):
        one = migration("002-b.sql", "SELECT 2;")
        result = plan([one], {"002-b.sql": checksum("SELECT 1;")})
        advice = result.drifted[0].advice
        assert "002-b.sql" in advice
        assert "NEW numbered file" in advice

    def test_a_recorded_migration_that_no_longer_exists_is_ignored(self):
        # A deleted file is not drift: the database is ahead of the repository,
        # which is a different (and usually deliberate) situation.
        result = plan([migration("001-a.sql", "SELECT 1;")], {"000-gone.sql": "abc"})
        assert [p.migration.filename for p in result.pending] == ["001-a.sql"]
        assert not result.drifted

    def test_summary_counts_all_three_buckets(self):
        applied = migration("001-a.sql", "SELECT 1;")
        drifted = migration("002-b.sql", "SELECT 2;")
        pending = migration("003-c.sql", "SELECT 3;")
        result = plan(
            [applied, drifted, pending],
            {"001-a.sql": applied.checksum, "002-b.sql": "stale"},
        )
        assert result.summary() == "1 to apply, 1 unchanged, 1 drifted"


class TestLoadMigrations:
    def test_orders_by_version_not_by_string(self, tmp_path):
        # '10' sorts before '2' as text. A migration applied out of order is a
        # schema built in the wrong sequence.
        for name in ("002-b.sql", "010-c.sql", "001-a.sql"):
            (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
        assert [m.filename for m in load_migrations(tmp_path)] == [
            "001-a.sql",
            "002-b.sql",
            "010-c.sql",
        ]

    def test_rejects_a_file_that_is_not_a_migration(self, tmp_path):
        # Loudly, rather than skipping it. A migration that silently does not run
        # is the worst outcome available here.
        (tmp_path / "helper.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(ValueError, match="does not look like a migration"):
            load_migrations(tmp_path)

    def test_rejects_two_migrations_with_the_same_version(self, tmp_path):
        (tmp_path / "002-a.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "002-b.sql").write_text("SELECT 2;", encoding="utf-8")
        with pytest.raises(ValueError, match="share version 2"):
            load_migrations(tmp_path)

    def test_does_not_descend_into_subdirectories(self, tmp_path):
        # sql/checks/ holds diagnostic queries. Applying them as migrations would
        # be both wrong and confusing.
        (tmp_path / "001-a.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "checks").mkdir()
        (tmp_path / "checks" / "landmarks.sql").write_text("SELECT 2;", encoding="utf-8")
        assert [m.filename for m in load_migrations(tmp_path)] == ["001-a.sql"]

    def test_a_missing_directory_says_so(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_migrations(tmp_path / "nope")


class TestTheRealSchema:
    """Against sql/, because that is the file the splitter actually has to handle."""

    def test_the_shipped_schema_loads(self):
        migrations = load_migrations(sql_dir())
        assert migrations, "sql/ has no migrations - has the directory moved?"
        assert migrations[0].filename == "001-init.sql"

    def test_001_is_declared_repeatable(self):
        # docs/SPATIAL-DB.md tells people to edit this file in place and re-run it.
        # Without the directive the runner would call the second run drift and
        # refuse, making the documented workflow an error.
        first = load_migrations(sql_dir())[0]
        assert first.repeatable, "001-init.sql lost its `-- migration: repeatable` line"

    def test_every_function_body_survives_splitting(self):
        # Every migration, not just the first: 002 carries a dollar-quoted block
        # too, and the splitter's failure mode does not care which file it is in.
        for migration_file in load_migrations(sql_dir()):
            for statement in migration_file.statements:
                # A body cut in half leaves an unbalanced $$, which is the exact
                # signature of the bug this splitter replaces.
                assert statement.count("$$") % 2 == 0, (
                    f"unbalanced dollar quote in {migration_file.filename}: {statement[:80]}"
                )

    def test_every_create_lands_in_its_own_statement(self):
        first = load_migrations(sql_dir())[0]
        # Matched at line starts, not with startswith: a statement legitimately
        # carries the comment block that precedes it, so the CREATE is rarely the
        # first thing in the string.
        creates = re.compile(r"^\s*CREATE\b", re.MULTILINE | re.IGNORECASE)
        counts = [len(creates.findall(s)) for s in first.statements]
        # 3 tables + 3 indexes + 4 functions + 1 view + 1 extension = 12. A FLOOR
        # rather than the number, so adding to the schema does not fail a test about
        # splitting.
        assert sum(counts) >= 12, f"only {sum(counts)} CREATE statements found"
        # The substance: no statement carries two of them. That is what a missed
        # boundary looks like.
        assert max(counts) <= 1, "two CREATEs in one statement - a boundary was missed"

    def test_no_statement_is_only_a_comment(self):
        for migration_file in load_migrations(sql_dir()):
            for statement in migration_file.statements:
                without_comments = "\n".join(
                    line for line in statement.splitlines() if not line.strip().startswith("--")
                )
                assert without_comments.strip(), "a comment-only statement would error"
