"""Resolver handler tests - the I/O contract around the policy.

core/resolution.py is where the decisions are tested. What is left for the handler
is everything that only goes wrong once it is wired to real infrastructure:

1. THE POLLED-FEED CASE. The collector re-fetches every 60 seconds, so this handler
   sees the same record over and over. Getting that wrong does not raise - it
   quietly writes a version per poll, or worse, an EVENT per poll. Both look like
   the pipeline working.

2. THE LOG FORMAT IS A CONTRACT, same as for the collector and normalizer: the
   observability stack reads `$.msg`, `$.action`, `$.eventId`, `$.latencyMs`.

3. WHAT CROSSES THE BUS. EventBridge rejects a bare NaN token, and an unresolved
   extent is exactly the path that could carry one.

The store is the real ``InMemoryEventStore`` rather than a mock: it enforces the
same append-only and concurrency rules as the DynamoDB one, so these tests exercise
the handler against a store that can refuse it.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.eventstore import ConcurrentModification, InMemoryEventStore
from corridor_event_hub.core.serde import to_jsonable


class _StubEvents:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries):  # noqa: N803 - boto3's own parameter name
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("EVENT_BUS", "test-bus")
    monkeypatch.setenv("EVENT_TABLE", "test-events")
    module = importlib.import_module("corridor_event_hub.handlers.resolver")
    importlib.reload(module)
    monkeypatch.setattr(module, "_events", _StubEvents())
    monkeypatch.setattr(module, "_store", InMemoryEventStore())
    # A fixed clock. Confidence decays on wall time and lifecycle depends on
    # "is now inside the active window", so a real clock makes these tests pass or
    # fail depending on the date they are run.
    monkeypatch.setattr(module, "now_utc", lambda: NOW)
    return module


def candidate_event(candidate=None, **kwargs):
    """The EventBridge detail the normalizer publishes."""
    subject = candidate or make_candidate(**kwargs)
    return {
        "detail": {
            "sourceId": subject.source.source_id,
            "rawRef": subject.source.raw_ref,
            "candidate": to_jsonable(subject),
            "provisionalConfidence": to_jsonable(score(subject)),
        }
    }


def _logged(capsys) -> list[dict[str, Any]]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _details(resolver, detail_type: str) -> list[dict[str, Any]]:
    return [
        json.loads(e["Detail"])
        for e in resolver._events.entries
        if e["DetailType"] == detail_type
    ]


class TestCreate:
    def test_resolves_a_candidate_into_a_stored_event(self, resolver):
        result = resolver.handler(candidate_event())

        assert result["action"] == "created"
        stored = resolver._store.get_current(result["eventId"])
        assert stored is not None
        assert stored.lifecycle_state == "active"

    def test_announces_what_it_did_with_the_reasoning_attached(self, resolver):
        # The explanation travels with the announcement, so a consumer never
        # has to ask a second question to find out why.
        resolver.handler(candidate_event())
        announced = _details(resolver, "EventResolved")
        assert len(announced) == 1
        assert announced[0]["action"] == "created"
        assert announced[0]["explanation"]
        assert [t["toState"] for t in announced[0]["transitions"]] == [
            "reported",
            "validated",
            "active",
        ]

    def test_writes_the_source_pointer_so_the_next_poll_finds_the_event(self, resolver):
        result = resolver.handler(candidate_event())
        pointer = resolver._store.get_pointer("ok-odot-wzdx", "ok-odot-wzdx-1")
        assert pointer is not None
        assert pointer.event_id == result["eventId"]
        assert pointer.content_hash

    def test_emits_the_log_fields_the_dashboards_filter_on(self, resolver, capsys):
        resolver.handler(candidate_event())
        line = next(entry for entry in _logged(capsys) if entry["msg"] == "resolved")
        for required in (
            "sourceId",
            "action",
            "eventId",
            "versions",
            "latencyMs",
            # Dimensions and values the observability stack's metric filters read.
            # A missing one does not fail a deploy - it blanks a widget silently,
            # which is why this list is a test and not a comment.
            "eventClass",
            "ingestLatencyMs",
            "reviews",
        ):
            assert required in line, f"metric filter reads $.{required}"

    def test_reports_the_end_to_end_ingest_latency_not_its_own_runtime(self, resolver, capsys):
        # `latencyMs` times this handler; a payload that
        # spent two minutes on a retry would report ~40ms there and look healthy. The
        # measured interval has to start when the COLLECTOR fetched the bytes.
        candidate = make_candidate(retrieved_at="2026-08-10T11:59:00.000Z")  # 60s before NOW
        resolver.handler(candidate_event(candidate))

        line = next(entry for entry in _logged(capsys) if entry["msg"] == "resolved")
        assert line["ingestLatencyMs"] == 60_000
        assert line["latencyMs"] < 1_000  # the handler itself is fast
        assert line["ingestLatencyMs"] > line["latencyMs"]

    def test_an_unparseable_retrieved_at_reports_no_latency_rather_than_a_wrong_one(
        self, resolver, capsys
    ):
        # A guessed zero would silently improve the p95 the pipeline is judged on.
        resolver.handler(candidate_event(make_candidate(retrieved_at="whenever")))
        line = next(entry for entry in _logged(capsys) if entry["msg"] == "resolved")
        assert line["ingestLatencyMs"] is None

    def test_the_latency_never_goes_negative_on_clock_skew(self, resolver):
        # Both ends are our own clock, so a negative value means skew inside our
        # account - and a negative sample would poison the p95 rather than show up.
        future = make_candidate(retrieved_at="2026-08-10T12:05:00.000Z")
        assert resolver.ingest_latency_ms(future, NOW) == 0

    def test_emits_valid_json_even_when_an_extent_is_unresolved(self, resolver):
        # EventBridge rejects a bare NaN token at runtime. This is the path that
        # could carry one: an unlocatable record is kept rather than dropped.
        import math

        def reject(value):
            raise AssertionError(f"invalid JSON constant: {value}")

        resolver.handler(
            candidate_event(begin=math.nan, end=math.nan, states=[],
                            conflation_method="unresolved")
        )
        for entry in resolver._events.entries:
            json.loads(entry["Detail"], parse_constant=reject)

    def test_an_unlocatable_record_is_closed_rather_than_published(self, resolver):
        import math

        result = resolver.handler(
            candidate_event(begin=math.nan, end=math.nan, states=[],
                            conflation_method="unresolved")
        )
        assert resolver._store.get_current(result["eventId"]).lifecycle_state == "cleared"


class TestPolledFeedIdempotency:
    """Idempotency - the case a polled corridor hits every 60 seconds"""

    def test_the_same_payload_twice_creates_one_event_and_one_version_chain(self, resolver):
        first = resolver.handler(candidate_event())
        second = resolver.handler(candidate_event())

        assert second["action"] == "unchanged"
        assert second["versions"] == 0
        assert len(resolver._store.events) == 1
        history = resolver._store.history(first["eventId"])
        assert len(history.versions) == 3  # reported, validated, active - and no more

    def test_an_unchanged_redelivery_announces_nothing(self, resolver):
        resolver.handler(candidate_event())
        before = len(resolver._events.entries)
        resolver.handler(candidate_event())
        # A change feed that republished every poll would be useless to anything
        # trying to react to actual changes.
        assert len(resolver._events.entries) == before

    def test_a_refetch_at_a_different_time_is_still_unchanged(self, resolver):
        # retrieved_at and raw_ref change on EVERY fetch by construction. Hashing
        # them would make every poll look like a change - the same bug as having no
        # hash, but harder to see.
        resolver.handler(candidate_event())
        later = resolver.handler(
            candidate_event(
                retrieved_at="2026-08-10T12:05:00.000Z",
                raw_ref="s3://amzn-s3-demo-rawzone/raw/two.json",
            )
        )
        assert later["action"] == "unchanged"

    def test_a_real_change_from_the_source_does_write_a_version(self, resolver):
        from corridor_event_hub.core.types import LaneImpact

        first = resolver.handler(candidate_event())
        changed = resolver.handler(
            candidate_event(
                source_updated_at="2026-08-10T12:30:00.000Z",
                lane_impacts=[
                    LaneImpact(ordinal=1, type="general", status="closed", inferred=False)
                ],
            )
        )
        assert changed["action"] == "updated"
        assert changed["eventId"] == first["eventId"]
        stored = resolver._store.get_current(first["eventId"])
        assert len(stored.lane_impacts) == 1

    def test_the_content_hash_ignores_only_the_volatile_fields(self, resolver):
        base = make_candidate()
        same_bytes_later = make_candidate(
            retrieved_at="2026-08-11T09:00:00.000Z", raw_ref="s3://amzn-s3-demo-other-rawzone/key.json"
        )
        genuinely_changed = make_candidate(source_updated_at="2026-08-10T13:00:00.000Z")

        assert resolver.content_hash(base) == resolver.content_hash(same_bytes_later)
        # source_updated_at changing IS the source saying the record changed.
        assert resolver.content_hash(base) != resolver.content_hash(genuinely_changed)


class TestCrossAgencyMergeEndToEnd:
    """Acceptance criterion 2, through the handler"""

    def setup_resolver(self, resolver):
        first = resolver.handler(
            candidate_event(make_candidate(source_id="az511-events", begin=100.0, end=100.4))
        )
        second = resolver.handler(
            candidate_event(make_candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.6))
        )
        return first, second

    def test_two_agencies_reporting_one_crash_resolve_to_one_active_event(self, resolver):
        first, second = self.setup_resolver(resolver)

        assert second["action"] == "merged"
        assert second["matchedEventId"] == first["eventId"]

        parent = resolver._store.get_current(first["eventId"])
        child = resolver._store.get_current(second["eventId"])
        assert parent.lifecycle_state == "active"
        assert child.lifecycle_state == "merged"
        assert {s.agency for s in parent.sources} == {"az511-events", "ok-odot-wzdx"}

    def test_both_events_are_announced(self, resolver):
        # The parent gained provenance and the child changed state. A consumer
        # tracking either id has to hear about it.
        self.setup_resolver(resolver)
        merged = [d for d in _details(resolver, "EventResolved") if d["action"] == "merged"]
        assert len(merged) == 2
        assert {d["lifecycleState"] for d in merged} == {"active", "merged"}

    def test_the_third_poll_of_each_feed_changes_nothing(self, resolver):
        # The case that would quietly re-merge forever: after a merge, each source's
        # pointer must resolve to its own event, not to a fresh match attempt.
        self.setup_resolver(resolver)
        events_before = len(resolver._store.events)
        again = resolver.handler(
            candidate_event(make_candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.6))
        )
        assert again["action"] == "unchanged"
        assert len(resolver._store.events) == events_before


class TestReviewQueue:
    def test_an_ambiguous_pair_is_queued_and_announced(self, resolver):
        # An ambiguity is an output, not a log line.
        resolver.handler(
            candidate_event(make_candidate(source_id="az511-events", begin=100.0, end=100.2))
        )
        resolver.handler(
            candidate_event(make_candidate(source_id="ok-odot-wzdx", begin=103.0, end=103.2))
        )

        assert len(resolver._store.reviews) == 1
        queued = _details(resolver, "MatchReviewQueued")
        assert len(queued) == 1
        assert queued[0]["explanation"]
        # Both events stay separately published.
        assert len(resolver._store.events) == 2


class TestFailureHandling:
    def test_a_concurrent_write_is_retried_once_against_fresh_state(self, resolver, capsys):
        # The right response to losing a race is to re-read and decide again, not to
        # retry the same write. One retry in process; anything still conflicting
        # goes to the DLQ where a human can see two payloads fighting.
        real_write = resolver._store.write_change
        calls = {"n": 0}

        def fail_once(change):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConcurrentModification("simulated race")
            return real_write(change)

        resolver._store.write_change = fail_once
        result = resolver.handler(candidate_event())

        assert result["action"] == "created"
        assert any(line["msg"] == "resolver_retry" for line in _logged(capsys))

    def test_a_persistent_conflict_is_raised_so_the_dlq_sees_it(self, resolver):
        def always_fail(change):
            raise ConcurrentModification("permanent")

        resolver._store.write_change = always_fail
        with pytest.raises(ConcurrentModification):
            resolver.handler(candidate_event())

    def test_a_duplicate_delivery_is_a_no_op_not_a_failure(self, resolver, capsys):
        # At-least-once delivery is normal on an event bus. Raising here would send
        # a CORRECT outcome to the dead-letter queue.
        from corridor_event_hub.core.eventstore import VersionAlreadyExists

        def already_there(change):
            raise VersionAlreadyExists("v1 exists")

        resolver._store.write_change = already_there
        result = resolver.handler(candidate_event())

        assert result["action"] == "unchanged"
        assert any(
            line["msg"] == "resolver_duplicate_delivery" for line in _logged(capsys)
        )

    def test_an_illegal_transition_is_raised_rather_than_coerced(self, resolver, capsys):
        # Rejected and alarmed. Raising is what puts the payload in the DLQ
        # with its reason, which is the "alarmed" half.
        from corridor_event_hub.core.resolution import IllegalTransition

        def illegal(*args, **kwargs):
            raise IllegalTransition("active -> archived on source_update")

        resolver._store.write_change = illegal
        with pytest.raises(IllegalTransition):
            resolver.handler(candidate_event())
        assert any(line["msg"] == "illegal_transition" for line in _logged(capsys))
