"""WZDx projection and conformance tests.

CONFORMANCE IS CHECKED IN CI, not at runtime: published WZDx output validates
against the spec, and a deliberately malformed record fails the build. So the
malformed cases below are the check itself, not defensive extras - if `validate_feed` cannot be made to fail, it is not
checking anything and the conformance claim is decoration.

THE PROJECTION IS LOSSY AND THE INTERESTING TESTS ARE THE LOSSES. A superset model
narrowing into a closed vocabulary has to decide what happens to values the target
has no word for, and each of those decisions can be wrong in a way that still
validates. Publishing `all-lanes-open` for "we do not know about lanes" validates
perfectly and invents reassurance the source never gave; putting a both-directions
work zone on one carriageway validates and closes a road that is open.
"""

from __future__ import annotations

import copy

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.resolution import resolve_new
from corridor_event_hub.core.types import LaneImpact
from corridor_event_hub.core.wzdx import (
    WZDX_VERSION,
    corridor_slice,
    to_wzdx_feed,
    validate_feed,
)

PUBLISHER = "Corridor Event Hub test"
UPDATE_DATE = "2026-08-10T12:00:00.000Z"


def work_zone(**kwargs):
    """An active work zone, resolved the way the pipeline would."""
    defaults = {
        "event_class": "work_zone",
        "event_subtype": "lane_closure",
        "start_time": "2026-08-01T00:00:00.000Z",
        "end_time": "2026-12-01T00:00:00.000Z",
    }
    candidate = make_candidate(**{**defaults, **kwargs})
    result = resolve_new(candidate, score(candidate), new_event_id(NOW), [], NOW)
    return result.changes[0].final


def project(events, corridor=None):
    return to_wzdx_feed(
        events, publisher=PUBLISHER, update_date=UPDATE_DATE, corridor=corridor
    )


@pytest.fixture(scope="module")
def corridor():
    """The real offline corridor, so geometry derivation is tested against real
    vertices and a real measures array rather than a hand-made line."""
    from corridor_event_hub.core.lrs import active_corridor

    return active_corridor()


def on_corridor(corridor, **kwargs):
    """A work zone on the REAL corridor, so its measures resolve to real geometry."""
    begin = kwargs.pop("begin", corridor.states[0].corridor_offset + 10.0)
    return work_zone(route=corridor.route, begin=begin, end=begin + 2.0, **kwargs)


class TestConformance:
    def test_a_projected_feed_validates(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        assert validate_feed(feed) == []

    def test_the_feed_declares_the_version_it_targets(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        assert feed["feed_info"]["version"] == WZDX_VERSION
        assert feed["type"] == "FeatureCollection"

    def test_an_empty_corridor_still_produces_a_valid_feed(self):
        # A feed with no work zones is a legitimate answer and must not be malformed.
        # An empty `features` list is also how a consumer learns the corridor is
        # clear, which is different from the feed being broken.
        feed = project([]).feed
        assert feed["features"] == []
        assert validate_feed(feed) == []


class TestMalformedRecordsFailTheBuild:
    """The conformance gate, stated as tests"""

    def test_a_missing_required_property_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        del feed["features"][0]["properties"]["vehicle_impact"]
        errors = validate_feed(feed)
        assert any("vehicle_impact is required" in e for e in errors)

    def test_a_value_outside_a_closed_enumeration_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["properties"]["vehicle_impact"] = "mostly-fine"
        errors = validate_feed(feed)
        assert any("mostly-fine" in e for e in errors)

    def test_a_bad_direction_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["properties"]["core_details"]["direction"] = "EB"
        # Our own vocabulary is EB/WB; emitting it raw would be the obvious bug.
        assert any("direction 'EB'" in e for e in validate_feed(feed))

    def test_missing_geometry_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["geometry"] = None
        assert any("geometry is required" in e for e in validate_feed(feed))

    def test_a_swapped_lon_lat_is_caught(self, corridor):
        # Validates as GeoJSON, puts the corridor in the Indian Ocean, and has no
        # other tripwire anywhere in the system.
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["geometry"]["coordinates"] = [[35.4, -97.5], [35.5, -97.6]]
        assert any("longitude, latitude" in e for e in validate_feed(feed))

    def test_a_one_point_linestring_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["geometry"]["coordinates"] = [[-97.5, 35.4]]
        assert any("at least 2 positions" in e for e in validate_feed(feed))

    def test_an_end_date_before_the_start_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["properties"]["end_date"] = "2020-01-01T00:00:00.000Z"
        assert any("precedes start_date" in e for e in validate_feed(feed))

    def test_a_duplicate_feature_id_is_caught(self, corridor):
        # What a dedup bug upstream would produce, and it makes a consumer's upsert
        # ambiguous.
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"].append(copy.deepcopy(feed["features"][0]))
        assert any("duplicated" in e for e in validate_feed(feed))

    def test_a_feature_naming_an_undeclared_data_source_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        feed["features"][0]["properties"]["core_details"]["data_source_id"] = "ghost"
        assert any("not in feed_info.data_sources" in e for e in validate_feed(feed))

    def test_a_missing_feed_info_is_caught(self, corridor):
        feed = project([on_corridor(corridor)], corridor).feed
        del feed["feed_info"]
        assert any("feed_info is missing" in e for e in validate_feed(feed))

    def test_every_error_is_reported_not_just_the_first(self, corridor):
        # A mapping change that breaks four fields should show four errors in one CI
        # run rather than four runs.
        feed = project([on_corridor(corridor)], corridor).feed
        del feed["features"][0]["properties"]["vehicle_impact"]
        del feed["features"][0]["properties"]["location_method"]
        feed["features"][0]["properties"]["core_details"]["direction"] = "sideways"
        assert len(validate_feed(feed)) >= 3


class TestWhatIsPublished:
    def test_only_work_zones_are_published(self, corridor):
        # WZDx is a WORK ZONE exchange. Publishing a crash as a `work-zone` road
        # event is a conformance lie that validates cleanly, which is the worst kind.
        zone = on_corridor(corridor)
        crash = work_zone(
            route=corridor.route,
            event_class="incident",
            begin=zone.extent.begin_measure,
            end=zone.extent.end_measure,
            native_id="crash",
        )
        projection = project([zone, crash], corridor)
        assert projection.published == 1
        assert projection.feed["features"][0]["id"] == zone.event_id

    def test_a_clearing_work_zone_is_still_published(self, corridor):
        # A work zone being packed up still affects traffic; dropping it early sends
        # a truck into a lane that is not open yet.
        from corridor_event_hub.core.resolution import _revise

        zone = _revise(on_corridor(corridor), NOW, lifecycle_state="clearing")
        assert project([zone], corridor).published == 1

    def test_a_cleared_work_zone_is_not_published(self, corridor):
        from corridor_event_hub.core.resolution import _revise

        zone = _revise(on_corridor(corridor), NOW, lifecycle_state="cleared")
        assert project([zone], corridor).published == 0


class TestRedistributionIsCheckedBeforePublishing:
    """A licence obligation, enforced at the point it becomes a published document.

    FOUND IN A DEPLOYED FEED, which is why these are here rather than trusted to
    review: `/wzdx` was serving 6 of 24 features attributed to
    `aws-location-traffic` - HERE content licensed via AWS, `redistributable: false`,
    attribution mandatory. Nothing was wrong with the class filter. An Amazon
    Location `traffic_incidents` feature with `kind: construction` maps to
    `work_zone` legitimately, and `work_zone` is publishable legitimately. The
    licence was simply never consulted on the way out.
    """

    def test_a_source_whose_licence_forbids_republication_is_excluded(self, corridor):
        blocked = on_corridor(corridor, source_id="aws-location-traffic")
        projection = project([blocked], corridor)
        assert projection.published == 0
        assert "licence forbids republication" in projection.excluded[0].reason
        assert "aws-location-traffic" in projection.excluded[0].reason

    def test_a_source_with_unconfirmed_terms_is_also_excluded(self, corridor):
        # UNKNOWN IS NOT PERMISSION. Same rule ADR 0003 applies to a source that goes
        # silent: an unconfirmed term is not a term in our favour.
        projection = project([on_corridor(corridor, source_id="az511-events")], corridor)
        assert projection.published == 0
        assert "terms unconfirmed" in projection.excluded[0].reason

    def test_refused_and_unconfirmed_are_reported_as_different_problems(self, corridor):
        # Both block, and they are not the same finding: `false` will never publish,
        # `unknown` publishes the day somebody asks the agency. An operator reading a
        # single flat count cannot tell which of the two they are looking at.
        refused = project([on_corridor(corridor, source_id="aws-location-traffic")], corridor)
        unconfirmed = project([on_corridor(corridor, source_id="az511-events")], corridor)
        assert refused.excluded[0].reason != unconfirmed.excluded[0].reason

    def test_an_uncatalogued_source_is_excluded_rather_than_assumed_open(self, corridor):
        # A source nobody catalogued is a source nobody checked the terms of.
        projection = project([on_corridor(corridor, source_id="brand-new-feed")], corridor)
        assert projection.published == 0
        assert "unconfirmed" in projection.excluded[0].reason

    def test_a_redistributable_source_still_publishes(self, corridor):
        # The guard has to be a filter, not a wall - or the honest fix for a quiet
        # feed becomes deleting the check.
        assert project([on_corridor(corridor, source_id="ok-odot-wzdx")], corridor).published == 1
        assert project([on_corridor(corridor, source_id="tx-dot-wzdx")], corridor).published == 1

    def test_one_blocked_contributor_disqualifies_a_merged_event(self, corridor):
        """THE CASE A PER-SOURCE CHECK WOULD MISS.

        A merged record carries fields from every source that contributed. If a
        licensed source won even one field, republishing the merged event
        republishes that field - and attributes it to whichever agency happens to be
        `sources[0]`, which is worse than publishing it plainly.
        """
        first = on_corridor(corridor, source_id="tx-dot-wzdx")
        candidate = make_candidate(
            source_id="aws-location-traffic",
            event_class="work_zone",
            event_subtype="lane_closure",
            route=corridor.route,
            begin=first.extent.begin_measure,
            end=first.extent.end_measure,
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-12-01T00:00:00.000Z",
        )
        merged = resolve_new(candidate, score(candidate), new_event_id(NOW), [first], NOW)
        assert merged.action == "merged"
        parent = next(c.final for c in merged.changes if c.event_id == first.event_id)
        assert {s.source_id for s in parent.sources} == {
            "tx-dot-wzdx",
            "aws-location-traffic",
        }, "the merge did not actually combine the two sources"

        projection = project([parent], corridor)
        assert projection.published == 0
        assert "aws-location-traffic" in projection.excluded[0].reason

    def test_an_excluded_record_is_counted_and_explained_never_silent(self, corridor):
        # The output side of the record-rather-than-drop rule. A work zone missing
        # from a standards feed with no record of why is the same quiet loss
        # forbidden on the way in.
        projection = project([on_corridor(corridor, source_id="aws-location-traffic")], corridor)
        assert len(projection.excluded) == 1
        assert projection.excluded[0].event_id
        assert projection.excluded[0].reason


class TestLossyMappings:
    """Where a superset meets a closed vocabulary"""

    def test_unknown_lane_impacts_are_published_as_unknown_not_as_all_open(self, corridor):
        # THE LIVE OKLAHOMA CASE. `lanes: []` means "we do not know", and
        # `all-lanes-open` would invent reassurance the source never gave.
        zone = on_corridor(corridor, lane_impacts=[])
        feature = project([zone], corridor).feed["features"][0]
        assert feature["properties"]["vehicle_impact"] == "unknown"
        assert validate_feed(project([zone], corridor).feed) == []

    def test_a_partial_closure_maps_to_some_lanes_closed(self, corridor):
        zone = on_corridor(
            corridor,
            lane_impacts=[
                LaneImpact(ordinal=1, type="general", status="closed", inferred=False),
                LaneImpact(ordinal=2, type="general", status="open", inferred=False),
            ],
        )
        feature = project([zone], corridor).feed["features"][0]
        assert feature["properties"]["vehicle_impact"] == "some-lanes-closed"
        assert [lane["status"] for lane in feature["properties"]["lanes"]] == ["closed", "open"]

    def test_a_full_closure_maps_to_all_lanes_closed(self, corridor):
        zone = on_corridor(
            corridor,
            lane_impacts=[
                LaneImpact(ordinal=1, type="general", status="closed", inferred=False)
            ],
        )
        assert (
            project([zone], corridor).feed["features"][0]["properties"]["vehicle_impact"]
            == "all-lanes-closed"
        )

    def test_a_both_directions_event_becomes_undefined_not_a_guess(self, corridor):
        # WZDx has no "both". Picking a side would publish a closure on a
        # carriageway that is open.
        zone = on_corridor(corridor, direction="BOTH")
        feature = project([zone], corridor).feed["features"][0]
        assert feature["properties"]["core_details"]["direction"] == "undefined"
        assert validate_feed(project([zone], corridor).feed) == []

    def test_an_intermittent_lane_maps_to_the_closest_defined_value(self, corridor):
        zone = on_corridor(
            corridor,
            lane_impacts=[
                LaneImpact(ordinal=1, type="general", status="intermittent", inferred=False)
            ],
        )
        feature = project([zone], corridor).feed["features"][0]
        assert feature["properties"]["lanes"][0]["status"] == "alternating-flow"
        assert validate_feed(project([zone], corridor).feed) == []

    def test_an_hov_lane_does_not_leak_our_vocabulary(self, corridor):
        # WZDx has no HOV lane type. Emitting "HOV" would fail a consumer's
        # parser on a value that looks harmless in our own model.
        zone = on_corridor(
            corridor,
            lane_impacts=[LaneImpact(ordinal=1, type="HOV", status="open", inferred=False)],
        )
        assert validate_feed(project([zone], corridor).feed) == []

    def test_an_open_ended_event_is_excluded_rather_than_given_an_invented_end(
        self, corridor
    ):
        # WZDx REQUIRES end_date, as a timestamp - null is not a value it takes. A
        # work zone we cannot date the end of therefore cannot be published, and the
        # alternatives are worse than excluding it: an end derived from a
        # class-average duration is a number no agency said, and a consumer
        # scheduling around it cannot tell it from a real one.
        #
        # Counted and explained, like a missing geometry. The count is the argument
        # for asking these DOTs for real end dates.
        projection = project([on_corridor(corridor, end_time=None)], corridor)
        assert projection.published == 0
        assert [e.reason for e in projection.excluded] == [
            "no end_date: WZDx requires one and the source gave none we trust, so "
            "publishing would mean inventing an end time for a live work zone"
        ]
        assert validate_feed(projection.feed) == []

    def test_only_a_native_lrs_position_claims_to_be_verified(self, corridor):
        # A conflated position is a projection onto a centerline, not a
        # survey. Claiming otherwise overstates our own accuracy in a published feed.
        projected = on_corridor(corridor, conflation_method="coordinate")
        native = on_corridor(corridor, conflation_method="native_lrs", native_id="n")
        assert (
            project([projected], corridor).feed["features"][0]["properties"][
                "is_start_position_verified"
            ]
            is False
        )
        assert (
            project([native], corridor).feed["features"][0]["properties"][
                "is_start_position_verified"
            ]
            is True
        )


class TestGeometryDerivation:
    def test_a_milepost_extent_becomes_a_linestring_from_the_corridor(self, corridor):
        # Most extents arrive as mileposts with no geometry of their own, and WZDx
        # requires geometry. This is the conversion that makes them publishable.
        zone = on_corridor(corridor, conflation_method="milepost")
        assert zone.extent.geometry is None

        feature = project([zone], corridor).feed["features"][0]
        assert feature["geometry"]["type"] == "LineString"
        assert len(feature["geometry"]["coordinates"]) >= 2
        assert validate_feed(project([zone], corridor).feed) == []

    def test_the_derived_line_lies_within_the_requested_measures(self, corridor):
        # The slice must describe the stretch the event claims, not the whole route.
        begin = corridor.states[0].corridor_offset + 20.0
        coordinates = corridor_slice(corridor, begin, begin + 5.0)
        whole = corridor_slice(corridor, 0.0, corridor.total_miles)
        assert 2 <= len(coordinates) < len(whole)

    def test_a_point_event_still_produces_a_two_position_line(self, corridor):
        # A LineString needs two distinct positions; a degenerate slice would be an
        # invalid geometry rather than an obviously wrong one.
        begin = corridor.states[0].corridor_offset + 30.0
        coordinates = corridor_slice(corridor, begin, begin)
        assert len(coordinates) >= 2

    def test_an_event_whose_geometry_cannot_be_derived_is_excluded_with_a_reason(
        self, corridor
    ):
        # The record-rather-than-drop rule on the output side. Publishing an
        # invented point would be
        # worse; dropping it silently would be worse still.
        #
        # THE REALISTIC PATH, and it is not an unlocatable event - one of those fails
        # validation and is `cleared`, so it never reaches the projection at all. It
        # is a perfectly good milepost-conflated work zone published while the
        # CORRIDOR is unavailable: the spatial database is down, or this route is not
        # loaded. Then there is nothing to derive a LineString from.
        zone = on_corridor(corridor, conflation_method="milepost")
        assert zone.extent.geometry is None
        assert zone.lifecycle_state == "active"

        projection = project([zone], corridor=None)
        assert projection.published == 0
        assert len(projection.excluded) == 1
        assert projection.excluded[0].event_id == zone.event_id
        assert "no geometry" in projection.excluded[0].reason
        # And the feed it did produce is still conformant, so a corridor outage
        # degrades the feed's COVERAGE rather than its validity.
        assert validate_feed(projection.feed) == []

    def test_an_events_own_geometry_wins_over_a_derived_one(self, corridor):
        from corridor_event_hub.core.resolution import _revise
        from corridor_event_hub.core.types import GeoJsonGeometry

        own = GeoJsonGeometry(type="LineString", coordinates=[[-97.5, 35.4], [-97.4, 35.4]])
        zone = on_corridor(corridor)
        with_geometry = _revise(
            zone, NOW, extent=type(zone.extent)(**{**vars(zone.extent), "geometry": own})
        )
        feature = project([with_geometry], corridor).feed["features"][0]
        assert feature["geometry"]["coordinates"] == [[-97.5, 35.4], [-97.4, 35.4]]


class TestProvenanceSurvivesTheProjection:
    def test_every_contributing_agency_appears_in_data_sources(self, corridor):
        # WZDx has no richer place for provenance, so feed_info is where
        # a merged event's sources have to land or they are lost on publication.
        first = on_corridor(corridor, source_id="ok-odot-wzdx")
        merged = resolve_new(
            make_candidate(
                source_id="tx-dot-wzdx",
                event_class="work_zone",
                route=corridor.route,
                begin=first.extent.begin_measure,
                end=first.extent.end_measure,
                start_time="2026-08-01T00:00:00.000Z",
                end_time="2026-12-01T00:00:00.000Z",
            ),
            score(make_candidate(source_id="tx-dot-wzdx")),
            new_event_id(NOW),
            [first],
            NOW,
        )
        assert merged.action == "merged"
        parent = next(c.final for c in merged.changes if c.event_id == first.event_id)

        feed = project([parent], corridor).feed
        ids = {source["data_source_id"] for source in feed["feed_info"]["data_sources"]}
        assert ids == {"ok-odot-wzdx", "tx-dot-wzdx"}
        assert all(source["organization_name"] for source in feed["feed_info"]["data_sources"])
        assert validate_feed(feed) == []

    def test_data_sources_are_ordered_so_two_runs_diff_cleanly(self, corridor):
        zone = on_corridor(corridor)
        first = project([zone], corridor).feed
        second = project([zone], corridor).feed
        assert first == second

    def test_the_publisher_is_configuration_not_a_literal(self, corridor):
        # An adopting DOT publishes under its own name.
        feed = to_wzdx_feed(
            [on_corridor(corridor)],
            publisher="Some Other DOT",
            update_date=UPDATE_DATE,
            corridor=corridor,
        ).feed
        assert feed["feed_info"]["publisher"] == "Some Other DOT"
        assert validate_feed(feed) == []

    def test_an_empty_contact_is_omitted_rather_than_published_blank(self, corridor):
        # An empty contact_email validates and is worse than none: it tells a
        # consumer there is someone to contact.
        feed = to_wzdx_feed(
            [on_corridor(corridor)],
            publisher=PUBLISHER,
            update_date=UPDATE_DATE,
            corridor=corridor,
            contact_email=None,
        ).feed
        assert "contact_email" not in feed["feed_info"]
