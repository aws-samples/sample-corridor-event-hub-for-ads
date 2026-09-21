"""The projection against the OFFICIAL WZDx JSON Schema.

`test_wzdx.py` checks the projection against `core/wzdx.py`'s own table of required
fields and enums. This checks it against the schema USDOT publishes, vendored in
`reference/wzdx/4.2/` - the difference between "the projection matches what we think
the spec says" and "the projection matches the spec".

That difference was real. The first run of this check rejected a feed the hand-rolled
validator had passed: `end_date: null`, where WZDx requires a timestamp, and a
related-event type of `"related"`, which is not in the enum. Both came from the same
misreading of the spec, which is exactly why a validator written from that reading
could not catch either.

`scripts/check-wzdx.sh` runs the same schema over the REAL captured payloads. These
tests run it over synthetic events, so a projection bug that no current fixture
happens to trigger still fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.lrs import active_corridor
from corridor_event_hub.core.resolution import resolve_new
from corridor_event_hub.core.types import LaneImpact
from corridor_event_hub.core.wzdx import WZDX_VERSION, to_wzdx_feed, validate_feed

# scripts/lib is not a package and is not importable as one - the schema validator
# lives there so `jsonschema` cannot reach the Lambda bundle. Same approach as
# tests/test_render_migration.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from wzdx_schema import SCHEMA_DIR, schema_errors, schema_version  # noqa: E402

PUBLISHER = "Corridor Event Hub test"
UPDATE_DATE = "2026-08-10T12:00:00.000Z"


@pytest.fixture(scope="module")
def corridor():
    return active_corridor()


def work_zone(corridor, **kwargs):
    begin = kwargs.pop("begin", corridor.states[0].corridor_offset + 10.0)
    defaults = {
        "event_class": "work_zone",
        "event_subtype": "lane_closure",
        "start_time": "2026-08-01T00:00:00.000Z",
        "end_time": "2026-12-01T00:00:00.000Z",
        "route": corridor.route,
        "begin": begin,
        "end": begin + 2.0,
    }
    candidate = make_candidate(**{**defaults, **kwargs})
    result = resolve_new(candidate, score(candidate), new_event_id(NOW), [], NOW)
    return result.changes[0].final


def project(events, corridor):
    return to_wzdx_feed(events, publisher=PUBLISHER, update_date=UPDATE_DATE, corridor=corridor)


class TestTheCheckItself:
    """A conformance check that cannot fail is decoration. These are the tests that
    say this one can, and that it is reading what it claims to read."""

    def test_the_vendored_schema_matches_the_version_we_publish(self):
        assert schema_version() == WZDX_VERSION
        assert (SCHEMA_DIR / "WorkZoneFeed.json").is_file()

    def test_an_enum_violation_is_rejected(self, corridor):
        feed = project([work_zone(corridor)], corridor).feed
        feed["features"][0]["properties"]["vehicle_impact"] = "mostly-fine"
        assert any("mostly-fine" in error for error in schema_errors(feed))

    def test_an_impossible_timestamp_is_rejected(self, corridor):
        """The one that proves `format: date-time` is actually being checked.

        jsonschema treats `format` as an annotation unless it is given a
        FormatChecker, and can only check date-time when rfc3339-validator is
        installed. Without this test the suite passes green having validated no
        timestamp in the feed - a worse position than not checking at all, because
        the check is then evidence for a claim it never tested.
        """
        feed = project([work_zone(corridor)], corridor).feed
        feed["features"][0]["properties"]["start_date"] = "2026-13-45T99:99:99Z"
        assert any("date-time" in error for error in schema_errors(feed))

    def test_a_missing_required_field_is_rejected(self, corridor):
        feed = project([work_zone(corridor)], corridor).feed
        del feed["features"][0]["properties"]["vehicle_impact"]
        assert any("vehicle_impact" in error for error in schema_errors(feed))

    def test_a_swapped_coordinate_passes_the_schema_and_our_own_check_catches_it(self, corridor):
        """THE CASE THAT ARGUES FOR KEEPING BOTH VALIDATORS.

        GeoJSON's own schema constrains a position to two-or-more numbers and
        nothing else - no ranges - so a lon/lat swap is valid GeoJSON, valid WZDx,
        and puts this corridor in the Indian Ocean. The official schema is the
        authority on the spec; it is not the whole of what a consumer needs to be
        true, and this is the shape of what it cannot see.
        """
        feed = project([work_zone(corridor)], corridor).feed
        feed["features"][0]["geometry"]["coordinates"] = [[35.2, -101.8], [35.3, -101.7]]
        assert schema_errors(feed) == []
        assert any("longitude, latitude" in error for error in validate_feed(feed))


class TestTheProjectionConforms:
    def test_a_work_zone_validates_against_the_official_schema(self, corridor):
        feed = project([work_zone(corridor)], corridor).feed
        assert schema_errors(feed) == []

    def test_an_empty_feed_validates(self, corridor):
        """ "No work zones anywhere" is a legitimate and frequent answer, and it is
        how a consumer learns the road is clear. It cannot be the one case that
        publishes an invalid document."""
        feed = project([], corridor).feed
        assert schema_errors(feed) == []

    def test_a_merged_event_validates(self, corridor):
        """A merge publishes `related_road_events`, whose `type` is a closed enum -
        the field the schema caught the projection getting wrong."""
        first = work_zone(corridor, source_id="ok-odot-wzdx")
        candidate = make_candidate(
            source_id="tx-dot-wzdx",
            event_class="work_zone",
            event_subtype="lane_closure",
            route=corridor.route,
            begin=first.extent.begin_measure,
            end=first.extent.end_measure,
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-12-01T00:00:00.000Z",
        )
        resolution = resolve_new(candidate, score(candidate), new_event_id(NOW), [first], NOW)
        assert resolution.action == "merged"
        parent = next(c.final for c in resolution.changes if c.event_id == first.event_id)
        assert parent.related_event_ids, "nothing to validate if the merge left no relation"

        feed = project([parent], corridor).feed
        related = feed["features"][0]["properties"]["core_details"]["related_road_events"]
        assert related and all(r["type"] == "related-work-zone" for r in related)
        assert schema_errors(feed) == []

    def test_every_lane_status_and_type_we_can_emit_validates(self, corridor):
        """Every crosswalk output, not one sample: a lane vocabulary entry mapping
        to a value outside WZDx's enum is a one-line mistake that only shows up when
        a source happens to report that lane."""
        from corridor_event_hub.core.wzdx import _LANE_STATUS, _LANE_TYPE

        lanes = [
            LaneImpact(ordinal=index + 1, type=lane_type, status=status, inferred=False)
            for index, (status, lane_type) in enumerate(
                zip(sorted(_LANE_STATUS), sorted(_LANE_TYPE) * 3)
            )
        ]
        feed = project([work_zone(corridor, lane_impacts=lanes)], corridor).feed
        assert schema_errors(feed) == []
        assert validate_feed(feed) == []

    def test_a_both_directions_work_zone_validates(self, corridor):
        """WZDx has no "both", so the projection emits `undefined`. It has to be a
        value the schema accepts, not just one we chose."""
        feed = project([work_zone(corridor, direction="BOTH")], corridor).feed
        assert schema_errors(feed) == []


class TestTheTwoValidatorsAgree:
    """Where they disagree, the schema is right and core/wzdx.py is the bug. These
    catch the drift in the direction that matters: the fast check passing something
    the spec rejects."""

    def test_an_event_with_no_end_date_is_excluded_not_published(self, corridor):
        """WZDx requires `end_date`. Our `end_time` is null whenever a source gave
        none or gave one we rejected - Oklahoma regenerates its `end_date` per
        request, so the adapter throws those away. Publishing would mean inventing
        an end time for a live work zone, so the event is excluded WITH A REASON.
        """
        projection = project([work_zone(corridor, end_time=None)], corridor)
        assert projection.published == 0
        assert len(projection.excluded) == 1
        assert "end_date" in projection.excluded[0].reason
        assert schema_errors(projection.feed) == []
        assert validate_feed(projection.feed) == []

    def test_a_null_end_date_forced_into_the_feed_fails_both(self, corridor):
        """The projection excludes it; this is what happens if that check is ever
        removed. Both validators have to reject it, or the exclusion is the only
        thing standing between us and a non-conformant feed."""
        feed = project([work_zone(corridor)], corridor).feed
        feed["features"][0]["properties"]["end_date"] = None
        assert schema_errors(feed)
        assert validate_feed(feed)

    def test_a_bad_related_event_type_fails_both(self, corridor):
        feed = project([work_zone(corridor)], corridor).feed
        feed["features"][0]["properties"]["core_details"]["related_road_events"] = [
            {"type": "related", "id": "CEH-1"}
        ]
        assert schema_errors(feed)
        assert validate_feed(feed)

    def test_a_non_cc0_license_fails_both(self, corridor):
        """WZDx names one licence URL, as an enum of one. `to_wzdx_feed` refuses to
        build a feed with another; this is the check for a feed that got one anyway."""
        feed = project([work_zone(corridor)], corridor).feed
        feed["feed_info"]["license"] = "https://opensource.org/licenses/MIT"
        assert schema_errors(feed)
        assert validate_feed(feed)

    def test_the_projection_refuses_a_non_cc0_license(self, corridor):
        with pytest.raises(ValueError, match="publicdomain/zero"):
            to_wzdx_feed(
                [work_zone(corridor)],
                publisher=PUBLISHER,
                update_date=UPDATE_DATE,
                corridor=corridor,
                license_url="https://opensource.org/licenses/MIT",
            )
