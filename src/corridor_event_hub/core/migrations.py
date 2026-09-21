"""Schema migrations - what needs applying, in what order, and what has drifted.

PURE. This module reads .sql files and compares them against a record of what has
already been applied. It never opens a database connection. That split is the
point: the statement splitter and the drift rules are where the bugs live, and
neither needs Postgres to be wrong. The runner that uses this is
``handlers/db_migrate.py``.

TWO CLASSES OF FILE, because the project already depends on both:

  run-once (the default)
      A numbered file applied exactly once, then frozen. Editing it afterwards is
      DRIFT: the database no longer matches the file, and re-running it would
      either fail or double-apply. Drift is REPORTED, never silently re-applied.

  repeatable  (``-- migration: repeatable`` anywhere in the file)
      Re-applied whenever its contents change. ``sql/001-init.sql`` is one: it is
      written entirely as ``CREATE ... IF NOT EXISTS`` / ``CREATE OR REPLACE``,
      and docs/SPATIAL-DB.md tells people to edit it in place for anything
      additive. Treating it as run-once would make the documented workflow an
      error, so the distinction is declared in the file rather than inferred.

WHY A CHECKSUM RATHER THAN JUST A FILENAME: a filename-only record cannot tell
"already applied" from "applied, then edited". The second is the case that
silently leaves a cluster and a repository disagreeing about the schema, which is
exactly what a migration tracker is for.

The checksum covers the whole file, comments included. A comment-only edit
therefore counts as a change: harmless on a repeatable file (it re-applies
idempotently), and reported as drift on a run-once one. Deliberate - the
alternative is a normaliser deciding which edits "don't count", which is a worse
thing to be wrong about.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# `NNN-name.sql`. The number orders the file; the name is for humans. Anything
# else in sql/ is a mistake worth failing on rather than skipping - a migration
# that silently does not run is the worst outcome available here.
_FILENAME = re.compile(r"^(\d+)-([A-Za-z0-9][A-Za-z0-9._-]*)\.sql$")

# Declared in the file, not guessed from its contents. "Does this file re-run?"
# is a decision the author makes; sniffing for IF NOT EXISTS would get it right
# most of the time, which is the wrong reliability for this question.
_REPEATABLE = re.compile(r"^\s*--\s*migration:\s*repeatable\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class Migration:
    """One .sql file on disk."""

    filename: str
    version: int
    sql: str
    checksum: str
    repeatable: bool
    statements: tuple[str, ...]

    @property
    def kind(self) -> str:
        return "repeatable" if self.repeatable else "run-once"


@dataclass(frozen=True)
class Pending:
    """A migration that should be applied, and why."""

    migration: Migration
    #: ``new`` - never applied. ``changed`` - repeatable, and its contents moved.
    reason: str


@dataclass(frozen=True)
class Drift:
    """A run-once migration that was edited after being applied.

    Not applied. Not ignored. The remedy is in ``advice``, because the operator
    who hits this at 2am should not have to reason it out from first principles.
    """

    filename: str
    applied_checksum: str
    file_checksum: str

    @property
    def advice(self) -> str:
        return (
            f"{self.filename} was applied as {self.applied_checksum[:12]} and is now "
            f"{self.file_checksum[:12]}. A run-once migration is history and editing it "
            "does not change the database. Either put the change in a NEW numbered file, "
            "or - if the edit really is cosmetic and the schema already matches - update "
            "the checksum in schema_migration by hand and say so in the commit."
        )


@dataclass(frozen=True)
class Plan:
    pending: tuple[Pending, ...]
    unchanged: tuple[str, ...]
    drifted: tuple[Drift, ...]

    @property
    def is_empty(self) -> bool:
        return not self.pending

    def summary(self) -> str:
        return (
            f"{len(self.pending)} to apply, {len(self.unchanged)} unchanged, "
            f"{len(self.drifted)} drifted"
        )


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def is_repeatable(sql: str) -> bool:
    return _REPEATABLE.search(sql) is not None


def load_migrations(directory: Path) -> list[Migration]:
    """Read every migration in ``directory``, ordered by version.

    NON-RECURSIVE, on purpose: ``sql/checks/`` holds diagnostic queries, not
    migrations, and a recursive walk would apply them.

    Raises on a filename that does not match ``NNN-name.sql`` and on a duplicated
    version number. Both are cases where guessing produces a plausible order that
    is not the author's.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"migration directory not found: {directory}")

    migrations: list[Migration] = []
    seen: dict[int, str] = {}

    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        match = _FILENAME.match(path.name)
        if not match:
            raise ValueError(
                f"{path.name} does not look like a migration. Expected NNN-name.sql "
                f"(for example 002-add-tmc-segments.sql). Diagnostic queries belong in "
                f"{directory.name}/checks/, which is not scanned."
            )

        version = int(match.group(1))
        if version in seen:
            raise ValueError(
                f"two migrations share version {version}: {seen[version]} and {path.name}. "
                "Their order would depend on the filesystem, so renumber one."
            )
        seen[version] = path.name

        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                filename=path.name,
                version=version,
                sql=sql,
                checksum=checksum(sql),
                repeatable=is_repeatable(sql),
                statements=tuple(split_statements(sql)),
            )
        )

    migrations.sort(key=lambda m: m.version)
    return migrations


def plan(migrations: list[Migration], applied: dict[str, str]) -> Plan:
    """Decide what to apply, given ``{filename: checksum}`` already recorded."""
    pending: list[Pending] = []
    unchanged: list[str] = []
    drifted: list[Drift] = []

    for migration in migrations:
        recorded = applied.get(migration.filename)
        if recorded is None:
            pending.append(Pending(migration, "new"))
        elif recorded == migration.checksum:
            unchanged.append(migration.filename)
        elif migration.repeatable:
            pending.append(Pending(migration, "changed"))
        else:
            drifted.append(
                Drift(
                    filename=migration.filename,
                    applied_checksum=recorded,
                    file_checksum=migration.checksum,
                )
            )

    return Plan(tuple(pending), tuple(unchanged), tuple(drifted))


def split_statements(sql: str) -> list[str]:
    """Split a script into statements the way Postgres would read it.

    NEEDED because both consumers - the Data API in scripts/db.sh and the
    migration Lambda - take ONE statement per call. A naive ``sql.split(';')``
    destroys any function body, and this schema is largely functions.

    Character-level rather than line-level, which buys three things a line scan
    gets wrong:

      - ``$tag$`` bodies, not just ``$$``. The line-based version this replaces
        only tracked ``$$``, so a function using ``$func$`` was silently cut in
        half - and docs/SPATIAL-DB.md had to carry a "stick to $$" warning that is
        now unnecessary.
      - a semicolon inside a string literal (``COMMENT ON ... IS 'a; b'``) is
        text, not a statement boundary.
      - a trailing ``;`` in a ``--`` comment ends nothing.

    Comments are KEPT in the statement they precede. Postgres does not care, and
    the reasoning in this schema's comments is worth having in the server log when
    a statement fails.
    """
    statements: list[str] = []
    buf: list[str] = []
    dollar_tag: str | None = None
    in_string = False
    in_line_comment = False
    in_block_comment = 0  # block comments nest in Postgres

    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]
        pair = sql[index : index + 2]

        if in_line_comment:
            buf.append(char)
            if char == "\n":
                in_line_comment = False
            index += 1
            continue

        if in_block_comment:
            if pair == "/*":
                in_block_comment += 1
                buf.append(pair)
                index += 2
                continue
            if pair == "*/":
                in_block_comment -= 1
                buf.append(pair)
                index += 2
                continue
            buf.append(char)
            index += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                buf.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(char)
                index += 1
            continue

        if in_string:
            # '' is an escaped quote, not the end of the string.
            if pair == "''":
                buf.append(pair)
                index += 2
                continue
            buf.append(char)
            if char == "'":
                in_string = False
            index += 1
            continue

        # --- ordinary SQL ---
        if pair == "--":
            in_line_comment = True
            buf.append(pair)
            index += 2
            continue
        if pair == "/*":
            in_block_comment = 1
            buf.append(pair)
            index += 2
            continue
        if char == "'":
            in_string = True
            buf.append(char)
            index += 1
            continue

        tag = _dollar_tag_at(sql, index)
        if tag:
            dollar_tag = tag
            buf.append(tag)
            index += len(tag)
            continue

        if char == ";":
            statements.append("".join(buf))
            buf = []
            index += 1
            continue

        buf.append(char)
        index += 1

    statements.append("".join(buf))
    return [s.strip() for s in statements if not _is_only_comments(s)]


_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _dollar_tag_at(sql: str, index: int) -> str | None:
    """``$$`` or ``$name$`` starting at ``index``, if there is one.

    ``$1`` is a parameter placeholder, not a quote, and does not match: the tag
    body cannot start with a digit and the closing ``$`` is required.
    """
    match = _DOLLAR_TAG.match(sql, index)
    return match.group(0) if match else None


def _is_only_comments(chunk: str) -> bool:
    """True for a chunk with no executable SQL in it.

    Every script ends with one of these - the text after the final ``;`` - and
    sending it to the server is an error rather than a no-op.
    """
    stripped = re.sub(r"/\*.*?\*/", " ", chunk, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", " ", stripped)
    return not stripped.strip()
