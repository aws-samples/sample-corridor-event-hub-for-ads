"""Lifecycle tests.

The important ones here are the NEGATIVE cases. Illegal transitions have to be
rejected and alarmed rather than coerced. A state machine that only proves its happy
path proves very little.
"""

from __future__ import annotations

import pytest

from corridor_event_hub.core.lifecycle import (
    LIFECYCLE_PROFILES,
    TRANSITIONS,
    is_legal_transition,
    legal_targets_from,
    profile_for,
    target_for_source_absent,
)
from corridor_event_hub.core.types import EVENT_CLASSES, LIFECYCLE_STATES


class TestLegalTransitions:
    """legal transitions"""

    def test_walks_the_full_happy_path_reported_to_archived(self):
        assert is_legal_transition("reported", "validated", "validation_pass")
        assert is_legal_transition("validated", "active", "source_update")
        assert is_legal_transition("active", "clearing", "source_update")
        assert is_legal_transition("clearing", "cleared", "source_update")
        assert is_legal_transition("cleared", "archived", "retention_rule")

    def test_rejects_skipping_validation(self):
        # A report must never publish without passing checks.
        assert not is_legal_transition("reported", "active", "source_update")

    def test_rejects_resurrecting_an_archived_event(self):
        # Archived records are immutable.
        for to_state in LIFECYCLE_STATES:
            if to_state == "archived":
                continue
            assert not is_legal_transition("archived", to_state, "source_update")

    def test_rejects_a_right_trigger_on_the_wrong_edge(self):
        assert not is_legal_transition("reported", "merged", "dedup_decision")
        assert not is_legal_transition("cleared", "clearing", "source_update")

    def test_rejects_a_valid_edge_with_the_wrong_trigger(self):
        # clearing -> cleared is legal, but not because of a dedup decision.
        assert not is_legal_transition("clearing", "cleared", "dedup_decision")

    def test_allows_an_operator_to_override(self):
        # An operator can force any edge, but it is audited with a reason.
        assert is_legal_transition("reported", "active", "operator_override")


class TestReopenAndUnmerge:
    """re-open and un-merge"""

    def test_allows_cleared_to_active_for_a_recurrence(self):
        # Secondary crashes and re-closures at the same location are common.
        assert is_legal_transition("cleared", "active", "source_update")

    def test_allows_merged_to_active_to_undo_a_wrong_merge(self):
        assert is_legal_transition("merged", "active", "dedup_decision")
        assert is_legal_transition("merged", "active", "operator_override")

    def test_allows_clearing_to_active_when_impacts_increase_again(self):
        # Congestion cycles this edge constantly.
        assert is_legal_transition("clearing", "active", "derived_signal")


class TestTheSnapshotAmbiguity:
    """THE SNAPSHOT AMBIGUITY (Open Question 2)"""

    def test_routes_an_absent_record_to_clearing_when_semantics_unconfirmed(self):
        # The conservative failure. Lingering slightly too long is recoverable;
        # clearing a live hazard in front of an automated truck is not.
        to_state, reason = target_for_source_absent("UNKNOWN")
        assert to_state == "clearing"
        assert "unconfirmed" in reason

    def test_routes_to_cleared_only_when_a_source_explicitly_confirms(self):
        # NWS is the contrast case: leaving the active list really means over.
        to_state, _reason = target_for_source_absent("cleared")
        assert to_state == "cleared"

    @pytest.mark.parametrize(
        "value", ["", "unknown", "maybe", "CLEARED_probably", "null", "Cleared", "CLEARED"]
    )
    def test_treats_any_unrecognized_value_as_unconfirmed(self, value):
        # Defaulting to `cleared` on a typo, a missing catalog field, or a
        # case-variant would be a silent, corridor-wide correctness bug. Fail safe.
        assert target_for_source_absent(value)[0] == "clearing"

    def test_source_absent_never_reaches_cleared_directly_in_the_table(self):
        direct = [
            t for t in TRANSITIONS if t.to_state == "cleared" and "source_absent" in t.triggers
        ]
        assert direct == []


class TestTtlProfiles:
    """TTL profiles"""

    @pytest.mark.parametrize("event_class", EVENT_CLASSES)
    def test_defines_a_profile_for_every_event_class(self, event_class):
        assert event_class in LIFECYCLE_PROFILES

    def test_gives_congestion_a_far_shorter_active_ttl_than_a_work_zone(self):
        # Per-class profiles. A three-year work zone and a probe-derived
        # queue cannot share a timeout.
        congestion = profile_for("congestion").ttl_seconds["active"]
        work_zone = profile_for("work_zone").ttl_seconds["active"]
        assert congestion < work_zone / 100

    @pytest.mark.parametrize("event_class", EVENT_CLASSES)
    def test_gives_every_class_an_active_ttl_so_nothing_lives_forever(self, event_class):
        assert profile_for(event_class).ttl_seconds.get("active", 0) > 0

    def test_decays_road_surface_faster_than_the_weather_that_caused_it(self):
        # The reason classes 5 and 6 are split at all.
        assert (
            profile_for("road_surface").confidence_half_life_seconds
            < profile_for("weather").confidence_half_life_seconds
        )

    def test_falls_back_to_the_incident_profile_for_an_unknown_class(self):
        assert profile_for("does_not_exist").event_class == "incident"


class TestTransitionTableIntegrity:
    @pytest.mark.parametrize("state", [s for s in LIFECYCLE_STATES if s != "archived"])
    def test_every_state_except_archived_has_an_outbound_edge(self, state):
        assert legal_targets_from(state), f"{state} is a dead end"

    def test_every_state_is_reachable_from_reported(self):
        seen = {"reported"}
        grew = True
        while grew:
            grew = False
            for transition in TRANSITIONS:
                if transition.from_state in seen and transition.to_state not in seen:
                    seen.add(transition.to_state)
                    grew = True
        for state in LIFECYCLE_STATES:
            assert state in seen, f"{state} is unreachable"

    def test_every_transition_documents_its_rationale(self):
        # Adopters need the why, not just the what.
        for transition in TRANSITIONS:
            assert len(transition.rationale) > 20, (
                f"{transition.from_state}->{transition.to_state} has no rationale"
            )

    def test_has_no_duplicate_from_to_pairs(self):
        pairs = [f"{t.from_state}->{t.to_state}" for t in TRANSITIONS]
        assert len(set(pairs)) == len(pairs)

    def test_every_transition_names_only_known_states_and_triggers(self):
        # The table is DATA, so a typo in it is a data error that no type
        # checker catches. This is the check that catches it.
        from corridor_event_hub.core.lifecycle import TRANSITION_TRIGGERS

        for transition in TRANSITIONS:
            assert transition.from_state in LIFECYCLE_STATES
            assert transition.to_state in LIFECYCLE_STATES
            assert transition.triggers, "a transition with no trigger is unreachable"
            for trigger in transition.triggers:
                assert trigger in TRANSITION_TRIGGERS
