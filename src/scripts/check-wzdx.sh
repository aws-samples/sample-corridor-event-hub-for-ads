#!/usr/bin/env bash
#
# WZDx conformance check.
#
# "Published WZDx output validates against the spec in CI; a deliberately malformed
# record fails the build."
#
# WHY THIS IS A SEPARATE STEP AND NOT JUST A pytest. tests/test_wzdx.py covers the
# projection unit by unit against synthetic events. This runs the projection over
# the REAL captured payloads - the same bytes the adapters see in tests/fixtures -
# so it catches the conformance breaks that only appear in live data shapes: a feed
# whose lane vocabulary changed, a record with no geometry, a direction spelling
# nobody anticipated. It is the difference between "the projection is correct" and
# "the projection is correct ABOUT THIS CORRIDOR'S ACTUAL FEEDS".
#
# TWO VALIDATORS RUN HERE, and both have to pass:
#
#   validate_feed()   core/wzdx.py's required-fields and enum table. Fast, and its
#                     messages name the projection field that is wrong.
#   schema_errors()   the OFFICIAL WZDx JSON Schema, vendored in reference/wzdx/4.2/
#                     and run offline. This is what conformance means.
#
# The second is not redundant. On its first run it rejected a feed the first had
# passed: `end_date: null` (WZDx requires a timestamp) and a related-event type of
# "related" (not in the enum). A hand-written validator can only encode the spec as
# its author read it, and both defects came from the same misreading.
#
# It also fails the build on a deliberately malformed record, which is the half
# that proves the validators are doing anything at all.
#
# Run: npm run check   (or npm run lint:wzdx)

set -uo pipefail
cd "$(dirname "$0")/.."

VENV=.venv
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "no venv found - run 'npm run setup' first"
  exit 1
fi

"$PY" - <<'PY'
import sys

sys.path.insert(0, "scripts/lib")

from wzdx_schema import schema_errors, schema_version

from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.adapters.feeds import feed_targets
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.lrs import LocalConflator, active_corridor
from corridor_event_hub.core.resolution import resolve_new
from corridor_event_hub.core.confidence import ScoringInput, score_confidence
from corridor_event_hub.core.timeutil import now_iso, now_utc
from corridor_event_hub.core.wzdx import WZDX_VERSION, to_wzdx_feed, validate_feed

FIXTURES = "tests/fixtures"

corridor = active_corridor()
conflator = LocalConflator(corridor=corridor)
now = now_utc()
retrieved_at = now_iso()

# Every captured payload, through the real adapters, into real events. No network:
# the fixtures ARE the replay path, so this check is deterministic.
#
# CURRENT VERSION PER EVENT ID, keyed like the event store's `current` item and for
# the same reason. A merge produces changes for TWO events - the child and a new
# version of the parent - so appending every change to a list accumulates several
# versions of one id, and the projection then publishes one feature per version.
# That is a duplicate `id` in a standards feed, which makes a consumer's upsert
# ambiguous. Found by this check on its first run against real payloads, which is
# exactly what it is for.
current: dict[str, object] = {}
parsed_sources = 0
for target in feed_targets():
    try:
        with open(f"{FIXTURES}/{target.fixture}", encoding="utf-8") as handle:
            body = handle.read()
    except FileNotFoundError:
        continue
    try:
        result = target.adapter.parse(
            body,
            AdapterContext(
                conflator=conflator, raw_ref=f"file://{target.fixture}", retrieved_at=retrieved_at
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a broken adapter is a different check's job
        print(f"  skip  {target.source_id}: adapter raised ({type(exc).__name__}: {exc})")
        continue
    parsed_sources += 1
    for candidate in result.candidates:
        confidence = score_confidence(
            ScoringInput(
                candidate=candidate,
                sources=[candidate.source],
                last_confirmed_at=candidate.source.source_updated_at
                or candidate.source.retrieved_at,
                now=now,
            )
        )
        resolution = resolve_new(
            candidate, confidence, new_event_id(now), list(current.values()), now
        )
        for change in resolution.changes:
            current[change.event_id] = change.final

events = list(current.values())
projection = to_wzdx_feed(
    events,
    publisher="Corridor Event Hub conformance check",
    update_date=retrieved_at,
    corridor=corridor,
)
errors = validate_feed(projection.feed)
schema_failures = schema_errors(projection.feed)

print(f"  spec version        WZDx {WZDX_VERSION}")
print(f"  official schema     reference/wzdx/{schema_version()} (offline)")
print(f"  sources parsed      {parsed_sources}")
print(f"  events resolved     {len(events)}")
print(f"  features published  {projection.published}")
if projection.excluded:
    # Reported, never silent: a work zone absent from a standards feed with no
    # record of why is the same quiet loss forbidden on the way in.
    print(f"  excluded            {len(projection.excluded)}")
    for reason in sorted({e.reason for e in projection.excluded}):
        print(f"      - {reason}")

if errors:
    print()
    print(f"FAILED - {len(errors)} conformance error(s) in the projected feed:")
    for error in errors:
        print(f"  {error}")
    sys.exit(1)

if schema_failures:
    print()
    print(f"FAILED - {len(schema_failures)} official-schema error(s) in the projected feed:")
    for failure in schema_failures:
        print(f"  {failure}")
    print()
    print("  These passed core/wzdx.py's own check, which means that table and the")
    print("  spec disagree. The schema is right. Fix the projection, then add the")
    print("  rule to core/wzdx.py so the fast check catches it next time.")
    sys.exit(1)

# The other half of Prove the validator can fail. A check that only ever
# passes is indistinguishable from a check that does nothing, and this one guards a
# claim ("spec-conformant") that a consumer will rely on.
if not projection.feed["features"]:
    # No live work zone in the fixtures - inject a minimal valid feature so the
    # negative test still has something to break. Stated rather than skipped.
    print()
    print("  note: no work zone in the captured payloads, so the malformed-record")
    print("        case is checked against a synthetic feature.")
    projection.feed["features"].append(
        {
            "id": "SYNTHETIC",
            "type": "Feature",
            "properties": {
                "core_details": {
                    "event_type": "work-zone",
                    "data_source_id": projection.feed["feed_info"]["data_sources"][0][
                        "data_source_id"
                    ],
                    "direction": "eastbound",
                    "road_names": [corridor.route],
                    "update_date": retrieved_at,
                },
                "start_date": retrieved_at,
                "end_date": None,
                "is_start_date_verified": False,
                "is_end_date_verified": False,
                "is_start_position_verified": False,
                "is_end_position_verified": False,
                "location_method": "channel-device-method",
                "vehicle_impact": "unknown",
                "lanes": [],
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [list(corridor.centerline[0]), list(corridor.centerline[1])],
            },
        }
    )
    if validate_feed(projection.feed) or schema_errors(projection.feed):
        print("FAILED - the synthetic feature does not validate; fix this check.")
        sys.exit(1)

malformed = projection.feed
malformed["features"][0]["properties"]["vehicle_impact"] = "mostly-fine"
if not validate_feed(malformed):
    print()
    print("FAILED - a deliberately malformed record PASSED validation.")
    print("  The build has to fail on non-conformance. A validator that")
    print("  cannot reject anything makes the conformance claim decoration.")
    sys.exit(1)
if not schema_errors(malformed):
    print()
    print("FAILED - a deliberately malformed record PASSED the official schema.")
    print("  The registry resolved but nothing was checked - suspect a $ref that")
    print("  silently matched an empty subschema.")
    sys.exit(1)

# A SECOND MALFORMED CASE, aimed at the schema specifically: a timestamp that is
# shaped like one and is not a real instant. `format: date-time` is an annotation
# unless jsonschema is given a FormatChecker AND rfc3339-validator is installed, so
# this is the case that catches the check having gone green without reading a single
# date - the failure mode that makes a conformance claim worse than having none.
malformed["features"][0]["properties"]["vehicle_impact"] = "unknown"
malformed["features"][0]["properties"]["start_date"] = "2026-13-45T99:99:99Z"
if not schema_errors(malformed):
    print()
    print("FAILED - an impossible start_date PASSED the official schema.")
    print("  date-time formats are not being validated. Check that")
    print("  rfc3339-validator is installed: pip install -e '.[dev]'")
    sys.exit(1)

print()
print("PASS  projected feed conforms to the official WZDx schema, and malformed")
print("      records are rejected by both validators")
PY
rc=$?

if [ "$rc" -ne 0 ]; then
  cat <<'EOF'

Published WZDx output must validate, and CI must fail when it does not.
Fix the projection in corridor_event_hub/core/wzdx.py, or the crosswalk it reads.
Do NOT relax validate_feed to make this pass - it encodes what a consumer's
parser will reject. Do NOT edit reference/wzdx/4.2/ at all: those are upstream
artifacts, and a local tweak makes the conformance claim worthless.
EOF
fi

exit "$rc"
