"""Migration report renderer tests.

WHY: this is what an operator reads to find out whether a schema change happened,
and its EXIT CODE is what CI reads for the same thing. Both matter more than they
look - a renderer that prints drift and exits 0 turns a blocking problem into a
line of green log, and one that crashes on an unexpected payload hides the report
behind a traceback at precisely the wrong moment.

Same lesson as tests/test_dlq_format.py, which is why this renderer is a file
rather than a heredoc.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from render_migration import render  # noqa: E402 - after the path insert, necessarily

APPLIED = {
    "database": "corridoreventhub",
    "migrationDir": "/var/task/sql",
    "dryRun": False,
    "tls": "encrypted, certificate NOT verified (set SPATIAL_DB_CA_BUNDLE to verify)",
    "discovered": [
        {"filename": "001-init.sql", "kind": "repeatable", "statements": 16},
        {"filename": "002-corridor-real.sql", "kind": "run-once", "statements": 25},
    ],
    "unchanged": ["001-init.sql"],
    "drifted": [],
    "pending": [{"filename": "002-corridor-real.sql", "reason": "new"}],
    "applied": [
        {
            "filename": "002-corridor-real.sql",
            "reason": "new",
            "kind": "run-once",
            "statements": 25,
            "ms": 4120,
        }
    ],
}

DRIFTED = {
    **APPLIED,
    "applied": [],
    "pending": [],
    "drifted": [
        {
            "filename": "002-corridor-real.sql",
            "appliedChecksum": "aaaaaaaaaaaa",
            "fileChecksum": "bbbbbbbbbbbb",
            "advice": "put the change in a NEW numbered file",
        }
    ],
}

ERROR_ENVELOPE = {
    "errorType": "RuntimeError",
    "errorMessage": "002-corridor-real.sql failed and was rolled back: syntax error",
    "stackTrace": ["  File ...\n", "  File ...\n"],
}


def rendered(payload) -> tuple[str, int]:
    lines, code = render(json.dumps(payload))
    return "\n".join(lines), code


class TestAppliedRun:
    def test_names_the_file_and_what_it_cost(self):
        text, code = rendered(APPLIED)
        assert "002-corridor-real.sql" in text
        assert "25 statements" in text
        assert "4120ms" in text
        assert code == 0

    def test_separates_applied_from_unchanged(self):
        text, _ = rendered(APPLIED)
        assert "APPLIED" in text
        assert "UNCHANGED" in text
        assert "001-init.sql" in text.split("UNCHANGED")[1]

    def test_points_at_the_invariant_check_afterwards(self):
        # Applying a schema change and not verifying it is the mistake this line
        # exists to prevent.
        text, _ = rendered(APPLIED)
        assert "npm run db" in text

    def test_says_so_plainly_when_there_was_nothing_to_do(self):
        text, code = rendered({**APPLIED, "applied": [], "pending": []})
        assert "NOTHING TO APPLY" in text
        assert code == 0


class TestPlan:
    def test_says_would_apply_rather_than_applied(self):
        # The distinction the operator is relying on.
        text, code = rendered({**APPLIED, "dryRun": True, "applied": []})
        assert "WOULD APPLY" in text
        assert "APPLIED\n" not in text
        assert "nothing applied" in text
        assert code == 0


class TestDrift:
    def test_fails_the_run(self):
        text, code = rendered(DRIFTED)
        assert code == 1
        assert "DRIFT" in text

    def test_fails_a_PLAN_too(self):
        # A plan that prints drift and exits 0 turns a blocking problem into green
        # CI output.
        _, code = rendered({**DRIFTED, "dryRun": True})
        assert code == 1

    def test_shows_the_remedy_not_just_the_complaint(self):
        text, _ = rendered(DRIFTED)
        assert "NEW numbered file" in text


class TestFailureEnvelope:
    def test_shows_the_handler_error_message(self):
        text, code = rendered(ERROR_ENVELOPE)
        assert code == 1
        assert "rolled back" in text
        assert "FAILED" in text

    def test_mentions_the_traceback_without_printing_it(self):
        text, _ = rendered(ERROR_ENVELOPE)
        assert "2 frames" in text
        assert "File ..." not in text

    def test_renders_a_multiline_error_readably(self):
        payload = {**ERROR_ENVELOPE, "errorMessage": "refusing to migrate:\n  one\n  two"}
        text, code = rendered(payload)
        assert code == 1
        assert "one" in text and "two" in text


class TestMalformedOutput:
    def test_unparseable_output_is_shown_rather_than_crashing(self):
        lines, code = render("<html>502 Bad Gateway</html>")
        assert code == 1
        assert "502" in "\n".join(lines)

    def test_empty_output_says_so(self):
        lines, code = render("")
        assert code == 1
        assert "no output" in "\n".join(lines)

    def test_a_json_scalar_is_not_treated_as_a_report(self):
        lines, code = render('"null"')
        assert code == 1
        assert lines
