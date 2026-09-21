"""Resolution policy tests - what gets WRITTEN, and why.

core/matcher.py is tested on whether two candidates are the same event. This is
tested on the consequences: how many events exist afterwards, what state each is
in, whose value won which field, and whether the merge can be undone.

THE CASES THAT MATTER ARE THE ONES WITH A SILENT FAILURE MODE:

  - A merge that dissolves the child cannot be un-merged, and nothing about
    the parent looks wrong afterwards. So the child's continued existence in
    `merged` is asserted directly.
  - A polled feed re-delivering an unchanged record must not write a version. The
    failure is not an error, it is a version history so noisy that "when did this
    actually change" becomes unanswerable.
  - A late update that overwrites a newer one leaves a plausible event holding
    stale values. Nothing raises. So the ordering rule is asserted on the value,
    not just on the action name.
  - Discarding a losing value on a conflict is unrecoverable and invisible
   , so the alternates are asserted rather than assumed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core import resolution
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.matcher import MERGE_THRESHOLD
from corridor_event_hub.core.resolution import (
    IllegalTransition,
    is_stale,
    resolve_new,
    resolve_update,
)
from corridor_event_hub.core.types import LaneImpact


def resolve_first(candidate, nearby=(), now=None, event_id=None):
    at = now or NOW
    return resolve_new(
        candidate,
        score(candidate, at),
        event_id or new_event_id(at),
        list(nearby),
        at,
        payload_ref=candidate.source.raw_ref,
    )


def final_of(resolution_result, event_id=None):
    for change in resolution_result.changes:
        if event_id is None or change.event_id == event_id:
            return change.final
    raise AssertionError(f"no change for {event_id}")


class TestNewEvent:
    """A candidate nothing matches"""

    def test_creates_one_event_and_walks_it_to_active(self):
        result = resolve_first(make_candidate())

        assert result.action == "created"
        assert len(result.changes) == 1
        change = result.changes[0]
        assert change.created is True
        # reported -> validated -> active, as three versions.
        assert [step.event.lifecycle_state for step in change.steps] == [
            "reported",
            "validated",
            "active",
        ]
        assert change.final.version == 3

    def test_starts_in_reported_even_though_it_ends_active(self):
        # The chain is the point. An event that appeared already-active would have
        # no recorded moment of having been validated, and the audit trail carries
        # the trigger and actor behind every state it held.
        change = resolve_first(make_candidate()).changes[0]
        first = change.steps[0]
        assert first.event.lifecycle_state == "reported"
        assert first.audit.from_state is None
        assert first.audit.actor == "adapter"

    def test_every_version_has_an_audit_record_with_a_matching_sequence(self):
        # The invariant the whole store design rests on: v#n and audit#n describe
        # the same moment, so reconstruction needs no join.
        change = resolve_first(make_candidate()).changes[0]
        for step in change.steps:
            assert step.audit.sequence == step.event.version
            assert step.audit.event_id == step.event.event_id
            assert step.audit.rule_version == resolution.RESOLVER_POLICY_VERSION

    def test_records_the_payload_that_caused_each_transition(self):
        # An audit record has to point at the bytes behind it.
        change = resolve_first(make_candidate()).changes[0]
        assert all(s.audit.trigger_payload_ref == "s3://amzn-s3-demo-rawzone/raw/one.json" for s in change.steps)

    def test_a_scheduled_future_event_is_validated_but_not_active(self):
        # A work zone spends most of its life scheduled-but-inactive.
        # Publishing it as active would tell a truck a lane is closed next month.
        future = make_candidate(
            event_class="work_zone",
            start_time="2026-09-01T00:00:00.000Z",
            end_time="2026-09-30T00:00:00.000Z",
        )
        change = resolve_first(future).changes[0]
        assert change.final.lifecycle_state == "validated"

    def test_an_event_that_never_geolocated_is_closed_without_publishing(self):
        # The existing `reported -> cleared` edge, used for the case its rationale
        # describes. Publishing an event with no location would put a hazard at the
        # corridor's western terminus by default.
        import math

        unresolved = make_candidate(
            begin=math.nan, end=math.nan, states=[], conflation_method="unresolved"
        )
        change = resolve_first(unresolved).changes[0]
        assert change.final.lifecycle_state == "cleared"
        assert "never geolocated" in change.steps[-1].audit.reason
        assert change.steps[-1].audit.trigger == "validation_fail"

    def test_derives_severity_and_duration_with_versioned_logic(self):
        # A derived field carries the version of the logic that produced it.
        closed = make_candidate(
            event_class="closure",
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)],
            agency_severity="major",
        )
        event = final_of(resolve_first(closed))
        assert event.severity.function_version
        assert event.severity.score > 70  # closure baseline plus a closed lane
        # The agency's own word survives alongside the computed one.
        assert event.severity.agency_asserted == "major"
        assert event.expected_duration.basis == "agency_stated"

    def test_records_which_fields_the_source_contributed(self):
        # Provenance, and the distinction that decides merges later: a source that
        # said nothing about lanes has not contributed an empty list.
        silent = make_candidate(lane_impacts=[])
        event = final_of(resolve_first(silent))
        assert "lane_impacts" not in event.sources[0].contributed_fields
        assert "extent" in event.sources[0].contributed_fields


class TestCrossAgencyMerge:
    """Two agencies, one crash - acceptance criterion 2"""

    def setup_method(self):
        first = make_candidate(source_id="az511-events", begin=100.0, end=100.4)
        self.parent = final_of(resolve_first(first))
        self.second = make_candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.6)
        self.result = resolve_first(self.second, nearby=[self.parent])

    def test_merges_into_the_existing_event(self):
        assert self.result.action == "merged"
        assert self.result.matched_event_id == self.parent.event_id
        assert self.result.match.value >= MERGE_THRESHOLD

    def test_writes_both_events_and_keeps_the_child_identifiable(self):
        # Un-merge must restore the child's own identity and history, which
        # is impossible if the merge dissolved it. TWO changes, not one.
        assert len(self.result.changes) == 2
        child = final_of(self.result, self.result.subject_event_id)
        parent = final_of(self.result, self.parent.event_id)
        assert child.lifecycle_state == "merged"
        assert parent.lifecycle_state == "active"
        assert parent.event_id in child.related_event_ids
        assert child.event_id in parent.related_event_ids

    def test_the_child_is_validated_before_it_is_merged(self):
        # Through `validated`, not straight to `merged`: the table has no
        # reported -> merged edge, and the child having been independently
        # validated is what makes the merge evidence rather than an assumption.
        child_change = next(
            c for c in self.result.changes if c.event_id == self.result.subject_event_id
        )
        assert [s.event.lifecycle_state for s in child_change.steps] == [
            "reported",
            "validated",
            "merged",
        ]

    def test_provenance_survives_the_merge(self):
        # The merged event retains the full source list of all its parents.
        parent = final_of(self.result, self.parent.event_id)
        assert {s.source_id for s in parent.sources} == {"az511-events", "ok-odot-wzdx"}
        assert {s.agency for s in parent.sources} == {"az511-events", "ok-odot-wzdx"}

    def test_confidence_rises_through_corroboration_and_only_through_it(self):
        # The component that SHOULD move on a merge is corroboration. If recency
        # moved too, the score would be flattering for the wrong reason - the exact
        # bug the strip export's comments record having been caught before.
        parent = final_of(self.result, self.parent.event_id)
        assert parent.confidence.breakdown.corroboration > (
            self.parent.confidence.breakdown.corroboration
        )
        assert parent.confidence.value > self.parent.confidence.value

    def test_the_merge_is_explained_in_the_result(self):
        # A merge that cannot say why it merged is indistinguishable from a
        # bug.
        text = " ".join(self.result.explanation)
        assert self.parent.event_id in text
        assert "measure-range overlap" in text
        assert "reversible" in text

    def test_the_audit_record_names_the_decision_and_its_score(self):
        parent_change = next(c for c in self.result.changes if c.event_id == self.parent.event_id)
        audit = parent_change.steps[-1].audit
        assert audit.trigger == "dedup_decision"
        assert audit.actor == "rule"
        assert self.result.subject_event_id in audit.reason
        # A merge into the parent is not a state transition - it stays active.
        assert audit.from_state == audit.to_state == "active"


class TestSameSourceIsNotCorroboration:
    """One agency reporting twice is a duplicate, not two witnesses"""

    def test_a_second_record_from_the_same_source_does_not_merge(self):
        first = final_of(resolve_first(make_candidate(source_id="ok-odot-wzdx", native_id="a")))
        again = make_candidate(source_id="ok-odot-wzdx", native_id="b", begin=100.1, end=100.5)
        result = resolve_first(again, nearby=[first])

        # Created separately: this is one agency's own duplicate, which is a
        # different problem from cross-agency corroboration and must not inflate
        # confidence by pretending to be one.
        assert result.action == "created"


class TestReviewBand:
    """Ambiguity is queued, and both events stay published"""

    def test_an_ambiguous_pair_is_queued_without_merging(self):
        # Far enough apart to fall out of the merge band but inside the near-miss
        # decay, which is exactly the "cannot tell" region.
        first = final_of(resolve_first(make_candidate(source_id="az511-events", begin=100.0)))
        other = make_candidate(source_id="ok-odot-wzdx", begin=103.0, end=103.2)
        result = resolve_first(other, nearby=[first])

        assert result.action == "created"  # NOT merged
        assert len(result.reviews) == 1
        review = result.reviews[0]
        assert review.other_event_id == first.event_id
        assert review.explanation  # the score's own reasoning travels with it
        assert "review" in " ".join(result.explanation).lower()

    def test_a_clear_match_resolves_an_ambiguity_rather_than_queueing_it(self):
        # If a candidate is ambiguous against one event and a clear match to
        # another, the clear match is the answer. Queueing anyway would put a
        # decided question in front of a human.
        ambiguous = final_of(resolve_first(make_candidate(source_id="az511-events", begin=103.0)))
        exact = final_of(
            resolve_first(make_candidate(source_id="nm-dot-wzdx", begin=100.0, end=100.4))
        )
        result = resolve_first(
            make_candidate(source_id="ok-odot-wzdx", begin=100.1, end=100.5),
            nearby=[ambiguous, exact],
        )
        assert result.action == "merged"
        assert result.reviews == []


class TestStaleEventsAreNotMatchable:
    def test_a_stale_event_does_not_absorb_a_fresh_report(self):
        # Without this, a crash whose TTL expired hours ago silently absorbs a new
        # crash at the same milepost, and the new one inherits the old one's
        # history. The `active` TTL for an incident is 2h.
        old = final_of(resolve_first(make_candidate(source_id="az511-events")))
        much_later = NOW + timedelta(hours=6)
        assert is_stale(old, much_later)

        result = resolve_new(
            make_candidate(source_id="ok-odot-wzdx", begin=100.1),
            score(make_candidate(source_id="ok-odot-wzdx"), much_later),
            new_event_id(much_later),
            [old],
            much_later,
        )
        assert result.action == "created"

    def test_a_work_zone_is_not_stale_after_an_hour(self):
        # Profiles are per class. A shared TTL would expire a three-year
        # work zone on an incident's two-hour clock.
        zone = final_of(
            resolve_first(
                make_candidate(
                    event_class="work_zone",
                    start_time="2026-08-01T00:00:00.000Z",
                    end_time="2026-12-01T00:00:00.000Z",
                )
            )
        )
        assert not is_stale(zone, NOW + timedelta(hours=1))


class TestUpdates:
    """Idempotency and ordering"""

    def setup_method(self):
        self.candidate = make_candidate()
        self.existing = final_of(resolve_first(self.candidate))

    def test_identical_content_writes_nothing(self):
        result = resolve_update(
            self.candidate,
            score(self.candidate),
            self.existing,
            NOW,
            content_unchanged=True,
        )
        assert result.action == "unchanged"
        assert result.changes == []

    def test_a_changed_record_becomes_a_new_version(self):
        changed = make_candidate(
            source_updated_at="2026-08-10T12:30:00.000Z",
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)],
        )
        result = resolve_update(changed, score(changed), self.existing, NOW)

        assert result.action == "updated"
        event = final_of(result)
        assert event.version == self.existing.version + 1
        assert len(event.lane_impacts) == 1
        # One source still, so corroboration must NOT have moved.
        assert len(event.sources) == 1

    def test_a_late_update_is_recorded_but_does_not_regress_the_values(self):
        # The rule is explicit: recorded, but published state does not regress. An overwrite here leaves a plausible event holding stale values
        # and nothing raises.
        fresh = make_candidate(
            source_updated_at="2026-08-10T13:00:00.000Z",
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)],
        )
        updated = final_of(resolve_update(fresh, score(fresh), self.existing, NOW))

        stale = make_candidate(source_updated_at="2026-08-10T11:00:00.000Z", lane_impacts=[])
        result = resolve_update(stale, score(stale), updated, NOW)

        assert result.action == "late_ignored"
        event = final_of(result)
        # A version WAS written - the late arrival is part of the record - but the
        # lane impact from the newer report survives.
        assert event.version == updated.version + 1
        assert len(event.lane_impacts) == 1
        # And the audit record says what happened, so the late arrival is not
        # merely absent from the values - it is recorded as having been rejected.
        audit = result.changes[0].steps[0].audit
        assert "late update" in audit.reason
        assert "not applied" in audit.reason

    def test_a_validated_event_activates_when_its_start_time_arrives(self):
        scheduled = make_candidate(
            event_class="work_zone",
            start_time="2026-08-10T18:00:00.000Z",
            end_time="2026-08-20T00:00:00.000Z",
        )
        validated = final_of(resolve_first(scheduled))
        assert validated.lifecycle_state == "validated"

        later = NOW + timedelta(hours=7)
        moved = make_candidate(
            event_class="work_zone",
            start_time="2026-08-10T18:00:00.000Z",
            end_time="2026-08-20T00:00:00.000Z",
            source_updated_at="2026-08-10T19:00:00.000Z",
        )
        result = resolve_update(moved, score(moved, later), validated, later)
        assert final_of(result).lifecycle_state == "active"


class TestFieldPrecedence:
    """Documented precedence, and the losing values are kept"""

    def test_the_more_specific_extent_wins_over_the_vaguer_one(self):
        # A county-sized polygon must not overwrite a milepost. Reliability alone
        # would let it: nws-alerts is 0.95 against ok-odot-wzdx's 0.8.
        precise = make_candidate(
            source_id="ok-odot-wzdx",
            event_class="weather",
            conflation_method="milepost",
            positional_accuracy_meters=160,
            begin=100.0,
            end=100.2,
        )
        parent = final_of(resolve_first(precise))
        coarse = make_candidate(
            source_id="nws-alerts",
            event_class="weather",
            conflation_method="polygon_intersect",
            positional_accuracy_meters=5000,
            begin=99.0,
            end=101.0,
        )
        result = resolve_first(coarse, nearby=[parent])
        assert result.action == "merged"

        merged = final_of(result, parent.event_id)
        assert merged.extent.conflation_method == "milepost"
        assert merged.extensions["field_provenance"]["extent"] == "ok-odot-wzdx"

    def test_losing_values_are_retained_as_alternates_not_discarded(self):
        stated = make_candidate(
            source_id="ok-odot-wzdx", event_subtype="crash", agency_severity="major"
        )
        parent = final_of(resolve_first(stated))
        disagrees = make_candidate(
            source_id="nm-dot-wzdx", event_subtype="crash", agency_severity="minor",
            begin=100.1, end=100.5,
        )
        result = resolve_first(disagrees, nearby=[parent])
        merged = final_of(result, parent.event_id)

        alternates = merged.extensions["alternates"]
        assert "agency_severity" in alternates
        kept = alternates["agency_severity"][0]
        assert kept["value"] == "minor"
        assert kept["source_id"] == "nm-dot-wzdx"
        # And the disagreement is visible in the explanation, not just in the data.
        assert any("agency_severity" in line for line in result.explanation)

    def test_a_conflict_lowers_internal_consistency(self):
        # An unresolved contradiction across sources is a quality signal, so
        # a merge that had to pick a winner should not score as cleanly as one that
        # did not.
        agree = make_candidate(source_id="ok-odot-wzdx", agency_severity="major")
        parent = final_of(resolve_first(agree))
        conflicting = make_candidate(
            source_id="nm-dot-wzdx", agency_severity="minor", begin=100.1, end=100.5
        )
        merged = final_of(resolve_first(conflicting, nearby=[parent]), parent.event_id)
        assert merged.confidence.breakdown.internal_consistency < 1.0

    def test_an_observed_time_beats_an_estimated_one_from_a_better_source(self):
        # A timestamp string cannot say how firm it is, so time_confidence is
        # its specificity. Without that this reduces to reliability and a national
        # feed's estimate overwrites a state feed's observation.
        observed = make_candidate(
            source_id="ok-odot-wzdx",  # 0.8
            event_class="weather",
            time_confidence="observed",
            start_time="2026-08-10T11:00:00.000Z",
        )
        parent = final_of(resolve_first(observed))
        estimated = make_candidate(
            source_id="nws-alerts",  # 0.95 - the MORE reliable source
            event_class="weather",
            time_confidence="estimated",
            start_time="2026-08-10T11:20:00.000Z",
            begin=100.1,
            end=100.5,
        )
        merged = final_of(resolve_first(estimated, nearby=[parent]), parent.event_id)
        assert merged.start_time == "2026-08-10T11:00:00.000Z"
        assert merged.extensions["field_provenance"]["start_time"] == "ok-odot-wzdx"

    def test_a_source_withdrawing_a_detail_takes_effect(self):
        # THE CASE WITH NO ERROR AT ALL. An agency reports a closed lane, then
        # reopens it - the new record has NO lane impacts. If the source's own
        # earlier claim competes with its new one, the empty claim loses and the lane
        # stays closed forever, attributed to an agency that already said otherwise.
        closed = make_candidate(
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)]
        )
        existing = final_of(resolve_first(closed))
        assert len(existing.lane_impacts) == 1

        reopened = make_candidate(
            lane_impacts=[], source_updated_at="2026-08-10T12:30:00.000Z"
        )
        updated = final_of(resolve_update(reopened, score(reopened), existing, NOW))
        assert updated.lane_impacts == []

    def test_another_agency_still_holds_its_ground_when_one_withdraws(self):
        # The other half: superseding applies ONLY to the source that is speaking.
        # A withdrawal by one agency must not erase what a different agency reported.
        first = make_candidate(source_id="ok-odot-wzdx", agency_severity="major")
        parent = final_of(resolve_first(first))
        second = make_candidate(
            source_id="nm-dot-wzdx", agency_severity="minor", begin=100.1, end=100.5
        )
        merged = final_of(resolve_first(second, nearby=[parent]), parent.event_id)
        assert merged.severity.agency_asserted == "major"  # ok-odot-wzdx won

        # Now ok-odot-wzdx withdraws its severity. nm-dot-wzdx's retained alternate
        # is the only remaining claim, so it becomes the value.
        withdrawn = make_candidate(
            source_id="ok-odot-wzdx",
            agency_severity=None,
            source_updated_at="2026-08-10T13:00:00.000Z",
        )
        after = final_of(resolve_update(withdrawn, score(withdrawn), merged, NOW))
        assert after.severity.agency_asserted == "minor"

    def test_said_nothing_never_beats_said_something(self):
        # An empty claim losing on specificity would be fragile; they are dropped
        # before ranking, so a silent high-reliability source cannot blank a field.
        with_lanes = make_candidate(
            source_id="nm-dot-wzdx",  # 0.7, the LOWER reliability
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)],
        )
        parent = final_of(resolve_first(with_lanes))
        silent = make_candidate(source_id="nws-alerts", lane_impacts=[], begin=100.1, end=100.5)

        merged = final_of(resolve_first(silent, nearby=[parent]), parent.event_id)
        assert len(merged.lane_impacts) == 1


class TestIllegalTransitions:
    """Rejected and alarmed, never coerced"""

    def test_advancing_along_an_edge_the_table_does_not_have_raises(self):
        event = final_of(resolve_first(make_candidate()))
        with pytest.raises(IllegalTransition, match="transition table"):
            resolution._advance(event, "archived", "source_update", "rule", "nope", NOW, None)

    def test_the_lifecycle_table_is_the_only_authority(self):
        # Guards against the resolver growing its own opinion about legality. Every
        # transition it emits has to be in the published table.
        from corridor_event_hub.core.lifecycle import is_legal_transition

        result = resolve_first(make_candidate())
        for change in result.changes:
            for step in change.steps:
                if step.audit.from_state and step.audit.from_state != step.audit.to_state:
                    assert is_legal_transition(
                        step.audit.from_state, step.audit.to_state, step.audit.trigger
                    ), f"{step.audit.from_state} -> {step.audit.to_state}"
