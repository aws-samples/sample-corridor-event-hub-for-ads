"""Lifecycle trace tests - what the tracker CLAIMS about a record's life.

The trace is a diagnostic view, which makes a wrong trace worse than no trace: it
is read precisely when someone is deciding whether the pipeline misbehaved, so a
view that invents a transition or hides a gap sends that decision the wrong way.

THE CASES HERE ARE THE ONES WITH A SILENT FAILURE MODE:

  - A confirmation (`active -> active` on a re-report) is NOT a transition. Treating
    it as one made every routine update render as an illegal transition against live
    data, which would bury the one that genuinely was.
  - A diff that reports `raw_ref` moving the same way it reports a lane closing is
    unreadable, and unreadable is indistinguishable from empty after the tenth row.
  - A windowed read of a 1,500-version record must not report an audit gap, and must
    not present 200 of 1,500 steps as the whole history.
  - The findings are the product. Each one is asserted on a store built to have that
    defect, because a finding that never fires is decoration.

These build REAL histories through core/resolution.py and InMemoryEventStore rather
than hand-assembling audit records: a hand-written trail can be internally
consistent in ways the pipeline never produces, and then the test passes while the
view is wrong about live data.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.eventstore import InMemoryEventStore
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.lifecycle import AuditRecord
from corridor_event_hub.core.resolution import expire, resolve_new, resolve_update
from corridor_event_hub.core.trace import (
    STAGES,
    build_stages,
    build_steps,
    build_trace,
    diff_versions,
    findings,
    is_transition,
    record_summary,
    state_durations,
)

# ---------------------------------------------------------------------------
# Helpers: a record with a real history, built the way the pipeline builds one
# ---------------------------------------------------------------------------


def write(store: InMemoryEventStore, resolution) -> None:
    for change in resolution.changes:
        store.write_change(change)


def new_record(store: InMemoryEventStore, candidate=None, now=NOW, nearby=()):
    candidate = candidate or make_candidate()
    resolution = resolve_new(
        candidate,
        score(candidate, now),
        new_event_id(now),
        list(nearby),
        now,
        payload_ref=candidate.source.raw_ref,
    )
    write(store, resolution)
    return resolution.changes[0].final


def update(store: InMemoryEventStore, event, candidate, now, content_unchanged=False):
    resolution = resolve_update(
        candidate,
        score(candidate, now),
        event,
        now,
        payload_ref=candidate.source.raw_ref,
        content_unchanged=content_unchanged,
    )
    write(store, resolution)
    return store.get_current(event.event_id)


def trace_of(store: InMemoryEventStore, event, now=NOW, totals=None):
    history = store.history(event.event_id)
    current = store.get_current(event.event_id)
    return build_trace(current, history.versions, history.audit, now, totals=totals)


# ---------------------------------------------------------------------------


class TestSteps:
    def test_first_report_is_not_reported_as_a_transition_from_nowhere(self):
        store = InMemoryEventStore()
        event = new_record(store)
        history = store.history(event.event_id)

        first = build_steps(history.versions, history.audit)[0]
        assert first["from_state"] is None
        assert first["to_state"] == "reported"
        # No edge exists into `reported` from nothing, so legality must not be
        # asserted against the table here - the alternative reports every record's
        # birth as illegal.
        assert first["transition"] is False
        assert first["legal"] is True

    def test_a_re_report_at_the_same_state_is_a_confirmation_not_a_transition(self):
        """The bug this test exists for.

        Live data: an Oklahoma work zone with 1,564 `active -> active` steps, every
        one of which read as an illegal transition because the table has no
        active->active edge - correctly, since a confirmation is not a transition.
        """
        store = InMemoryEventStore()
        event = new_record(store)
        later = NOW + timedelta(minutes=5)
        event = update(
            store,
            event,
            make_candidate(source_updated_at="2026-08-10T11:55:00.000Z"),
            later,
        )

        steps = build_steps(*_history(store, event))
        confirmations = [s for s in steps if not s["transition"]]
        assert confirmations, "expected at least one same-state step"
        assert all(s["legal"] for s in confirmations)
        assert not [f for f in _codes(store, event) if f == "illegal_transition_stored"]

    def test_a_step_carries_the_payload_that_caused_it(self):
        """This pointer is what makes the trace reach back to ingestion."""
        store = InMemoryEventStore()
        event = new_record(
            store, make_candidate(raw_ref="s3://amzn-s3-demo-rawzone/source=ok/2026-08-10T12:00:00Z-abc.json")
        )
        steps = build_steps(*_history(store, event))
        assert steps[0]["payload_ref"] == "s3://amzn-s3-demo-rawzone/source=ok/2026-08-10T12:00:00Z-abc.json"

    def test_both_clocks_are_kept_and_their_gap_is_computed(self):
        """Occurred_at and recorded_at are different clocks."""
        store = InMemoryEventStore()
        event = new_record(store)
        step = build_steps(*_history(store, event))[0]
        assert step["occurred_at"]
        assert step["recorded_at"]
        assert step["lag_seconds"] is not None


class TestDiffs:
    def test_refetch_bookkeeping_is_marked_and_a_real_change_is_not(self):
        store = InMemoryEventStore()
        event = new_record(store, make_candidate(raw_ref="s3://amzn-s3-demo-rawzone/one.json"))
        later = NOW + timedelta(minutes=1)
        event = update(
            store,
            event,
            make_candidate(
                raw_ref="s3://amzn-s3-demo-rawzone/two.json",
                retrieved_at="2026-08-10T12:01:00.000Z",
                source_updated_at="2026-08-10T11:59:00.000Z",
                end_time="2026-08-10T15:00:00.000Z",
            ),
            later,
        )
        versions = store.history(event.event_id).versions
        changes = diff_versions(versions[-2], versions[-1])
        by_path = {c["path"]: c for c in changes}

        ref = next(c for p, c in by_path.items() if p.endswith(".raw_ref"))
        assert ref["bookkeeping"] is True
        assert ref["notable"] is False

        assert by_path["end_time"]["bookkeeping"] is False
        assert by_path["end_time"]["notable"] is True

    def test_confidence_moving_on_its_own_counts_as_bookkeeping(self):
        """Recency decays against the clock, so the score moves with no news."""
        store = InMemoryEventStore()
        event = new_record(store)
        event = update(
            store,
            event,
            make_candidate(source_updated_at="2026-08-10T11:50:00.000Z"),
            NOW + timedelta(minutes=30),
        )
        versions = store.history(event.event_id).versions
        confidence_changes = [
            c
            for c in diff_versions(versions[-2], versions[-1])
            if c["path"].startswith("confidence")
        ]
        assert confidence_changes, "expected the score to move"
        assert all(c["bookkeeping"] for c in confidence_changes)

    def test_a_source_joining_is_reported_as_added_not_as_every_field_changing(self):
        """Positional list comparison reports a reordered list as total change.

        The candidate pair is the one TestCrossAgencyMerge uses, so this asserts on a
        merge the resolver genuinely performs rather than on a hoped-for one.
        """
        store = InMemoryEventStore()
        event = new_record(
            store, make_candidate(source_id="az511-events", native_id="az-1", begin=100.0, end=100.4)
        )
        merging = make_candidate(
            source_id="ok-odot-wzdx", native_id="ok-1", begin=100.2, end=100.6
        )
        resolution = resolve_new(
            merging,
            score(merging, NOW),
            new_event_id(NOW),
            [event],
            NOW,
            payload_ref=merging.source.raw_ref,
        )
        assert resolution.action == "merged", "the pair must merge for this to test anything"
        write(store, resolution)

        versions = store.history(event.event_id).versions
        added = [
            c
            for c in diff_versions(versions[-2], versions[-1])
            if c["path"].startswith("sources[") and c["to"] == "added"
        ]
        assert added, "a joining source must appear as one `added` line"
        assert len(added) == 1, f"one join, one line - got {[c['path'] for c in added]}"

    def test_an_unresolved_measure_does_not_diff_against_itself(self):
        """NaN != NaN, so a naive diff reports every unresolved extent as changing."""
        store = InMemoryEventStore()
        candidate = make_candidate(
            begin=float("nan"), end=float("nan"), conflation_method="unresolved"
        )
        event = new_record(store, candidate)
        event = update(
            store,
            event,
            make_candidate(
                begin=float("nan"),
                end=float("nan"),
                conflation_method="unresolved",
                source_updated_at="2026-08-10T11:50:00.000Z",
            ),
            NOW + timedelta(minutes=2),
        )
        versions = store.history(event.event_id).versions
        measures = [c for c in diff_versions(versions[-2], versions[-1]) if "measure" in c["path"]]
        assert measures == []


class TestStateDurations:
    def test_consecutive_confirmations_collapse_into_one_span_with_a_count(self):
        store = InMemoryEventStore()
        event = new_record(store)
        for minute in (1, 2, 3):
            event = update(
                store,
                event,
                make_candidate(
                    source_updated_at=f"2026-08-10T11:5{minute}:00.000Z",
                    raw_ref=f"s3://amzn-s3-demo-rawzone/{minute}.json",
                ),
                NOW + timedelta(minutes=minute),
            )
        spans = state_durations(
            store.history(event.event_id).audit, event, NOW + timedelta(hours=1)
        )
        assert len(spans) < 4, "confirmations must not each become a band"
        assert any(s["updates"] > 0 for s in spans)
        assert spans[-1]["current"] is True

    def test_a_current_state_the_audit_trail_does_not_explain_is_shown_as_unaudited(self):
        store = InMemoryEventStore()
        event = new_record(store)
        audit = store.history(event.event_id).audit
        # A `current` pointer nobody audited into place: exactly the disagreement the
        # span list must expose rather than silently reconcile.
        moved = _with_state(event, "archived")
        spans = state_durations(audit, moved, NOW + timedelta(hours=1))
        assert spans[-1]["state"] == "archived"
        assert spans[-1].get("unaudited") is True


class TestStages:
    def test_every_declared_stage_is_present_and_in_order(self):
        store = InMemoryEventStore()
        event = new_record(store)
        versions, audit = _history(store, event)
        stages = build_stages(event, versions, audit, NOW)
        assert [s["stage"] for s in stages] == [s["stage"] for s in STAGES]
        assert all(s.get("what") for s in stages)

    def test_the_normalize_stage_does_not_invent_a_mapping_issue_count(self):
        """Mapping issues are metered, not persisted per event. Absence must be stated."""
        store = InMemoryEventStore()
        event = new_record(store)
        versions, audit = _history(store, event)
        normalize = next(
            s for s in build_stages(event, versions, audit, NOW) if s["stage"] == "normalize"
        )
        assert "mapping_issues_note" in normalize["evidence"]
        assert "mapping_issue_count" not in normalize["evidence"]

    def test_a_windowed_read_reports_the_true_totals_not_what_it_read(self):
        store = InMemoryEventStore()
        event = new_record(store)
        versions, audit = _history(store, event)
        stages = build_stages(
            event, versions, audit, NOW, totals={"versions": 1564, "audit": 1564, "windowed": True}
        )
        resolve = next(s for s in stages if s["stage"] == "resolve")
        assert resolve["evidence"]["version_count"] == 1564

    def test_creation_time_survives_a_windowed_read(self):
        """`created_at` is carried forward on every version for exactly this reason."""
        store = InMemoryEventStore()
        event = new_record(store)
        event = update(
            store, event, make_candidate(source_updated_at="2026-08-10T11:50:00.000Z"), NOW
        )
        versions = store.history(event.event_id).versions
        audit = store.history(event.event_id).audit
        # Only the tail was read, as history_window() does.
        stages = build_stages(
            event,
            versions[-1:],
            audit[-1:],
            NOW,
            totals={"versions": len(versions), "audit": len(audit), "windowed": True},
        )
        resolve = next(s for s in stages if s["stage"] == "resolve")
        assert resolve["at"] == event.created_at


class TestFindings:
    def test_a_healthy_record_raises_nothing_worse_than_info(self):
        store = InMemoryEventStore()
        event = new_record(store)
        raised = findings(event, *_history(store, event), NOW)
        assert [f for f in raised if f["severity"] == "error"] == []

    def test_an_audit_gap_is_an_error(self):
        store = InMemoryEventStore()
        event = new_record(store)
        versions, audit = _history(store, event)
        holed = audit + [
            AuditRecord(
                event_id=event.event_id,
                sequence=3,  # 2 is missing
                from_state="reported",
                to_state="validated",
                trigger="validation_pass",
                actor="rule",
                trigger_payload_ref=None,
                rule_version="test",
                reason="gap",
                occurred_at=event.updated_at,
                recorded_at=event.updated_at,
            )
        ]
        codes = {f["code"] for f in findings(event, versions, holed, NOW)}
        assert "audit_gap" in codes

    def test_a_windowed_read_is_not_mistaken_for_an_audit_gap(self):
        store = InMemoryEventStore()
        event = new_record(store)
        for minute in range(1, 5):
            event = update(
                store,
                event,
                make_candidate(source_updated_at=f"2026-08-10T11:5{minute}:00.000Z"),
                NOW + timedelta(minutes=minute),
            )
        versions, audit = _history(store, event)
        raised = findings(
            event,
            versions[-2:],
            audit[-2:],
            NOW,
            totals={"versions": len(versions), "audit": len(audit), "windowed": True},
        )
        assert "audit_gap" not in {f["code"] for f in raised}
        assert "no_audit_trail" not in {f["code"] for f in raised}

    def test_an_expired_ttl_that_has_not_moved_is_raised_against_the_ladder(self):
        """Briefly a race; persistently a dead timer chain."""
        store = InMemoryEventStore()
        event = new_record(store)
        much_later = NOW + timedelta(days=3)
        raised = {f["code"]: f for f in findings(event, *_history(store, event), much_later)}
        assert "ttl_expired_not_moved" in raised
        assert raised["ttl_expired_not_moved"]["severity"] == "error"

    def test_an_unresolved_extent_says_it_is_invisible_to_corridor_queries(self):
        store = InMemoryEventStore()
        event = new_record(
            store,
            make_candidate(begin=float("nan"), end=float("nan"), conflation_method="unresolved"),
        )
        raised = {f["code"] for f in findings(event, *_history(store, event), NOW)}
        assert "unresolved_extent" in raised

    def test_merged_with_no_parent_is_an_error(self):
        """An un-merge has nothing to restore from."""
        store = InMemoryEventStore()
        event = new_record(store)
        orphan = _with_state(event, "merged")
        orphan.related_event_ids = []
        raised = {f["code"] for f in findings(orphan, *_history(store, event), NOW)}
        assert "merged_without_parent" in raised

    def test_a_single_witness_is_reported_as_info_not_as_a_fault(self):
        """One independent source is a limit on the claim, not a defect."""
        store = InMemoryEventStore()
        event = new_record(store)
        active = _with_state(event, "active")
        raised = {f["code"]: f for f in findings(active, *_history(store, event), NOW)}
        assert raised["single_witness"]["severity"] == "info"

    def test_clock_skew_between_our_fetch_and_the_agency_is_raised(self):
        store = InMemoryEventStore()
        event = new_record(
            store,
            make_candidate(
                retrieved_at="2026-08-10T12:00:00.000Z",
                source_updated_at="2026-08-10T13:00:00.000Z",  # after we fetched
            ),
        )
        raised = {f["code"] for f in findings(event, *_history(store, event), NOW)}
        assert "clock_skew" in raised

    def test_version_churn_is_raised_when_every_step_is_bookkeeping(self):
        """The defect this tool found on its first run against live data.

        ODOT regenerates `end_date` per request (see
        adapters/ok_odot_wzdx.regenerated_end_date_minutes), so the content hash
        differs on every poll, idempotency cannot fire, and a version lands every 60
        seconds while the canonical record never changes.
        """
        store = InMemoryEventStore()
        event = new_record(store)
        for minute in range(1, 15):
            event = update(
                store,
                event,
                # Only the fetch bookkeeping moves: same source_updated_at, new bytes.
                make_candidate(raw_ref=f"s3://amzn-s3-demo-rawzone/{minute}.json", retrieved_at=_at(minute)),
                NOW + timedelta(minutes=minute),
            )
        versions, audit = _history(store, event)
        steps = build_steps(versions, audit)
        raised = {f["code"] for f in findings(event, versions, audit, NOW, steps=steps)}
        assert "version_churn" in raised

    def test_version_churn_is_not_raised_when_records_genuinely_change(self):
        store = InMemoryEventStore()
        event = new_record(store)
        for minute in range(1, 15):
            event = update(
                store,
                event,
                make_candidate(
                    raw_ref=f"s3://amzn-s3-demo-rawzone/{minute}.json",
                    retrieved_at=_at(minute),
                    source_updated_at=_at(minute),
                    end_time=f"2026-08-10T1{4 + (minute % 5)}:00:00.000Z",
                ),
                NOW + timedelta(minutes=minute),
            )
        versions, audit = _history(store, event)
        steps = build_steps(versions, audit)
        raised = {f["code"] for f in findings(event, versions, audit, NOW, steps=steps)}
        assert "version_churn" not in raised


class TestSummary:
    def test_the_row_carries_the_ttl_position_and_the_agency_ids(self):
        store = InMemoryEventStore()
        event = new_record(store, make_candidate(source_id="ok-odot-wzdx", native_id="250182-1"))
        row = record_summary(event, NOW)
        assert row["native_ids"] == ["250182-1"]
        assert row["source_ids"] == ["ok-odot-wzdx"]
        assert row["ttl_expires_at"]
        assert row["ttl_expired"] is False
        assert row["terminal"] is False

    def test_a_nan_measure_becomes_null_rather_than_a_json_breaking_token(self):
        """allow_nan=False in the server's encoder; a bare NaN would 500 the request."""
        store = InMemoryEventStore()
        event = new_record(
            store,
            make_candidate(begin=float("nan"), end=float("nan"), conflation_method="unresolved"),
        )
        row = record_summary(event, NOW)
        assert row["begin_measure"] is None
        assert row["unresolved_extent"] is True

    def test_a_terminal_record_is_labelled_terminal(self):
        store = InMemoryEventStore()
        event = new_record(store)
        assert record_summary(_with_state(event, "cleared"), NOW)["terminal"] is True


class TestTraceDocument:
    def test_the_document_is_json_encodable_with_nan_rejected(self):
        """The server encodes with allow_nan=False, so this is the real constraint."""
        import json

        from corridor_event_hub.core.serde import to_jsonable

        store = InMemoryEventStore()
        event = new_record(
            store,
            make_candidate(begin=float("nan"), end=float("nan"), conflation_method="unresolved"),
        )
        document = trace_of(store, event)
        json.dumps(to_jsonable(document), allow_nan=False)

    def test_a_full_read_reports_itself_as_not_windowed(self):
        store = InMemoryEventStore()
        event = new_record(store)
        document = trace_of(store, event)
        assert document["window"]["windowed"] is False
        assert document["window"]["note"] is None

    def test_a_windowed_read_says_so_in_the_document(self):
        store = InMemoryEventStore()
        event = new_record(store)
        document = trace_of(
            store, event, totals={"versions": 1564, "audit": 1564, "windowed": True, "window": 200}
        )
        assert document["window"]["windowed"] is True
        assert "1564" in document["window"]["note"]
        assert document["counts"]["versions"] == 1564
        assert document["counts"]["versions_read"] == len(store.history(event.event_id).versions)

    def test_a_ttl_expiry_appears_as_a_timer_step_on_the_lifecycle_stage(self):
        """The end of life, arrived at by silence rather than by an agency clear."""
        store = InMemoryEventStore()
        event = new_record(store)
        much_later = NOW + timedelta(days=9)
        resolution = expire(event, much_later, payload_ref=None)
        write(store, resolution)
        current = store.get_current(event.event_id)
        document = trace_of(store, current, now=much_later)

        timer_steps = [s for s in document["steps"] if s["trigger"] == "timer_ttl"]
        assert timer_steps, "the expiry must appear as a step"
        assert timer_steps[-1]["stage"] == "lifecycle"
        assert timer_steps[-1]["actor"] == "timer"
        assert current.lifecycle_state != event.lifecycle_state


# ---------------------------------------------------------------------------


def _history(store: InMemoryEventStore, event):
    history = store.history(event.event_id)
    return history.versions, history.audit


def _codes(store: InMemoryEventStore, event, now=NOW):
    return [f["code"] for f in findings(event, *_history(store, event), now)]


def _with_state(event, state: str):
    """A shallow copy of an event in a different state.

    Used to build the disagreements the findings exist to catch. Deliberately does
    NOT go through the resolver: the resolver refuses to make these, which is why
    they have to be constructed to be tested against.
    """
    import copy

    moved = copy.deepcopy(event)
    moved.lifecycle_state = state
    return moved


def _at(minute: int) -> str:
    return f"2026-08-10T12:{minute:02d}:00.000Z"


def test_is_transition_distinguishes_confirmation_from_movement():
    def record(from_state, to_state):
        return AuditRecord(
            event_id="e",
            sequence=1,
            from_state=from_state,
            to_state=to_state,
            trigger="source_update",
            actor="adapter",
            trigger_payload_ref=None,
            rule_version="test",
            reason="",
            occurred_at="",
            recorded_at="",
        )

    assert is_transition(record("active", "clearing")) is True
    assert is_transition(record("active", "active")) is False
    assert is_transition(record(None, "reported")) is False
