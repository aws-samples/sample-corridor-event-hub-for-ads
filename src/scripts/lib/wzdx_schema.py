"""Validate a projected feed against the OFFICIAL WZDx JSON Schema.

`corridor_event_hub.core.wzdx.validate_feed` encodes the required fields and closed
enumerations by hand: it is fast, it runs in unit tests, and its messages name the
projection field that is wrong. This module is the authority it is checked against —
the schema USDOT publishes, unmodified, in `reference/wzdx/4.2/`.

Both, because they fail differently. The hand-rolled validator says
`features[3].properties.core_details.direction 'EB' is not one of ...`; the schema says
`'EB' is not one of [...]` at a JSON pointer. The first is better to debug. The second
is the one a consumer's parser actually agrees with, and it catches the constraints
nobody thought to hand-encode — the two real defects it found on its first run were
`end_date: null` (required as a string, and our code carried a comment asserting the
spec allowed null) and a `related_road_events[].type` of `"related"`, which is not in
the enum at all. Neither was reachable by the hand-rolled check, because both were
written from the same wrong reading of the spec.

WHY THIS LIVES IN scripts/lib AND NOT IN corridor_event_hub/. `jsonschema` is a dev
dependency, and `scripts/build-lambda.sh` copies the whole `corridor_event_hub`
package into the deployment bundle. A module in there importing `jsonschema` ships a
file that raises ImportError the moment anything touches it, and the bundle is
asserted to be pure Python besides. Nothing the Lambda runs needs schema validation:
the projection is validated in CI, before it can be deployed at all.

Used by `scripts/check-wzdx.sh` (over the real captured payloads) and
`tests/test_wzdx_schema.py` (over synthetic events). `tests/test_render_migration.py`
sets up sys.path for `scripts/lib` the same way.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The vendored schema directory. Resolved from this file rather than the working
#: directory: `npm run lint:wzdx` and pytest run from different places.
SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "reference" / "wzdx" / "4.2"

#: The entry point schema. WZDx 4.2 renamed WZDxFeed to WorkZoneFeed.
FEED_SCHEMA = "WorkZoneFeed.json"


class SchemaUnavailable(RuntimeError):
    """The vendored schemas or `jsonschema` are missing.

    RAISED, never returned as "no errors". A conformance check that quietly
    degrades to a pass when its schema is absent is worse than no check: it reports
    success for a feed nobody validated.
    """


@lru_cache(maxsize=1)
def _validator() -> Any:
    try:
        from jsonschema import Draft7Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT7
    except ImportError as exc:  # pragma: no cover - the message IS the behaviour
        raise SchemaUnavailable(
            f"{exc}. Install the dev extras: pip install -e '.[dev]', or npm run setup"
        ) from exc

    format_checker = FormatChecker()
    if "date-time" not in format_checker.checkers:
        # THE TRAP THIS EXISTS TO CATCH. jsonschema treats `format` as an annotation
        # unless a FormatChecker is passed, and even then it can only check
        # `date-time` when rfc3339-validator is installed. Miss either and every
        # timestamp in the feed validates unread - the check goes green having
        # verified nothing about the one field type WZDx specifies most precisely.
        raise SchemaUnavailable(
            "the date-time format checker is not installed, so timestamps would not be "
            "validated and this check would pass without reading them. "
            "Install rfc3339-validator (it is in the dev extras)."
        )

    if not SCHEMA_DIR.is_dir():
        raise SchemaUnavailable(f"vendored WZDx schemas not found at {SCHEMA_DIR}")

    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        identifier = document.get("$id")
        if not identifier:
            raise SchemaUnavailable(f"{path.name} has no $id, so nothing can $ref it")
        resources.append(
            (identifier, Resource.from_contents(document, default_specification=DRAFT7))
        )

    # No `retrieve` callback, deliberately: an unresolvable $ref then raises instead
    # of being fetched over the network. A missing vendored file has to be a loud
    # failure, not a silent download that makes CI depend on github.com being up.
    registry = Registry().with_resources(resources)

    feed_schema = json.loads((SCHEMA_DIR / FEED_SCHEMA).read_text(encoding="utf-8"))
    return Draft7Validator(feed_schema, registry=registry, format_checker=format_checker)


def schema_version() -> str:
    """The WZDx version these schemas are, read from the directory name."""
    return SCHEMA_DIR.name


def schema_errors(feed: dict[str, Any]) -> list[str]:
    """Every schema violation in this feed, as `<json pointer>: <message>` strings.

    ALL of them, sorted by location, for the same reason `validate_feed` returns a
    list: one CI run should show one mapping change's four consequences, not the
    first of four.

    `oneOf` needs the special case. WZDx models a road event as "work zone OR detour
    OR restriction", so a broken work zone reports as "does not match any branch"
    with the real cause buried in sub-errors - including the sub-errors from the
    branches it was never trying to be. Reporting the raw message means printing an
    entire feature and none of the reason. This keeps the sub-errors and drops the
    `const` failures on `event_type`, which are just the branches saying "not me".
    """
    errors: list[str] = []
    for error in _validator().iter_errors(feed):
        for flat in _flatten(error):
            errors.append(flat)
    return sorted(set(errors))


def _flatten(error: Any) -> list[str]:
    if error.validator == "oneOf" and error.context:
        out = []
        for sub in error.context:
            if sub.validator == "const" and list(sub.absolute_path)[-1:] == ["event_type"]:
                continue
            out.extend(_flatten(sub))
        if out:
            return out
    pointer = "/".join(str(part) for part in error.absolute_path) or "<feed>"
    return [f"{pointer}: {error.message}"]
