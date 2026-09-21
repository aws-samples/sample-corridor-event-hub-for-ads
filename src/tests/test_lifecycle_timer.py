"""TTL expiry tests - the mechanism whose absence is invisible.

WHY THIS FILE MATTERS MORE THAN ITS SIZE SUGGESTS. Every other failure in this
pipeline announces itself: a dead letter, a failed transition, an empty query. A
missing TTL announces nothing at all. The corridor keeps serving an event that
cleared three hours ago, every dashboard stays green, and the only way to notice is
to already know. That is the failure mode of existing 511 feeds
and the one the TTL ladder exists to close, so these tests pin the ladder itself:

    reported  --ttl-->  cleared     never corroborated, never published
    validated --ttl-->  active      its window arrived
    active    --ttl-->  clearing    NOT cleared: silence is not confirmation
    clearing  --ttl-->  cleared

The `active -> clearing` step is the load-bearing one, and the fact that it does
NOT go straight to `cleared` is the ADR 0003 decision: lingering slightly too long
is recoverable, clearing a live hazard in front of an automated truck is not.
"""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from typing import Any

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.eventstore import ConcurrentModification, InMemoryEventStore
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.lifecycle import is_legal_transition
from corridor_event_hub.core.resolution import (
    TTL_LADDER,
    expire,
    resolve_new,
    seconds_until_ttl,
)


def seeded(store=None, **kwargs):
    """One resolved event, optionally written into a store."""
    candidate = make_candidate(**kwargs)
    result = resolve_new(candidate, score(candidate), new_event_id(NOW), [], NOW)
    if store is not None:
        for change in result.changes:
            store.write_change(change)
    return result.changes[0].final


class TestTheLadder:
    def test_every_ladder_edge_is_legal_in_the_published_table(self):
        # The ladder is which of the LEGAL edges a timer takes, never a second
        # opinion about legality. If these ever disagree, the table wins.
        for from_state, to_state in TTL_LADDER.items():
            assert is_legal_transition(from_state, to_state, "timer_ttl"), (
                f"{from_state} -> {to_state} on timer_ttl is not in core/lifecycle.py"
            )

    def test_an_active_event_gone_quiet_moves_to_clearing_not_cleared(self):
        # ADR 0003. Going straight to `cleared` on silence would clear a live hazard
        # on the strength of a feed hiccup.
        event = seeded()
        assert event.lifecycle_state == "active"

        result = expire(event, NOW + timedelta(hours=3))
        assert result.action == "expired"
        moved = result.changes[0].final
        assert moved.lifecycle_state == "clearing"
        assert "stale_no_updates" in result.changes[0].steps[0].audit.reason

    def test_the_full_ladder_runs_to_cleared(self):
        event = seeded()
        at = NOW
        states = [event.lifecycle_state]
        for _ in range(4):
            at = at + timedelta(hours=3)
            result = expire(event, at)
            if result.action != "expired":
                break
            event = result.changes[0].final
            states.append(event.lifecycle_state)
        assert states == ["active", "clearing", "cleared"]

    def test_a_cleared_event_has_no_further_timers(self):
        # The ladder stops at `cleared`: archiving is a retention decision on a
        # different clock (`retention_rule`), not a consequence of silence.
        event = seeded()
        clearing = expire(event, NOW + timedelta(hours=3)).changes[0].final
        cleared = expire(clearing, NOW + timedelta(hours=4)).changes[0].final

        assert cleared.lifecycle_state == "cleared"
        assert expire(cleared, NOW + timedelta(days=30)).action == "terminal"
        assert seconds_until_ttl(cleared, NOW) is None

    def test_nothing_expires_before_its_ttl(self):
        event = seeded()
        result = expire(event, NOW + timedelta(minutes=30))
        assert result.action == "waiting"
        assert result.changes == []

    def test_the_reason_records_why_and_the_audit_names_the_timer(self):
        # The reason is recorded explicitly, and so is the actor. A transition
        # nobody can attribute is not an audit trail.
        event = seeded()
        step = expire(event, NOW + timedelta(hours=3)).changes[0].steps[0]
        audit = step.audit
        assert audit.trigger == "timer_ttl"
        assert audit.actor == "timer"
        assert "stale_no_updates" in audit.reason
        assert audit.from_state == "active"
        assert audit.to_state == "clearing"
        # The store's invariant: v#n and audit#n describe the same moment, so
        # reconstruction needs no join. A timer transition is no exception.
        assert audit.sequence == step.event.version == event.version + 1
        # Which version of the rules decided this.
        assert audit.rule_version


class TestPerClassProfiles:
    """A congestion event and a three-year work zone cannot share a timeout"""

    def test_a_work_zone_outlives_an_incident_by_days(self):
        incident = seeded(event_class="incident")
        zone = seeded(
            event_class="work_zone",
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-12-01T00:00:00.000Z",
        )
        assert seconds_until_ttl(incident, NOW) == 7200  # 2h
        assert seconds_until_ttl(zone, NOW) == 604800  # 7d

        at = NOW + timedelta(hours=3)
        assert expire(incident, at).action == "expired"
        assert expire(zone, at).action == "waiting"

    def test_congestion_expires_in_minutes(self):
        congestion = seeded(event_class="congestion", end_time=None)
        assert seconds_until_ttl(congestion, NOW) == 900
        assert expire(congestion, NOW + timedelta(minutes=20)).action == "expired"


class TestScheduledEventsAreNotPublishedEarly:
    def test_a_future_work_zone_whose_ttl_lapsed_is_not_activated(self):
        # `validated -> active` is a PUBLISHING decision, not a decay one. Firing it
        # on a future-dated work zone would tell a truck a lane is closed next month.
        zone = seeded(
            event_class="work_zone",
            start_time="2026-09-01T00:00:00.000Z",
            end_time="2026-09-30T00:00:00.000Z",
        )
        assert zone.lifecycle_state == "validated"

        # validated TTL for a work zone is 24h; go well past it, still before Sept.
        result = expire(zone, NOW + timedelta(days=3))
        assert result.action == "not_yet_due"
        assert result.changes == []
        assert "still in the future" in result.explanation[0]

    def test_it_activates_once_its_start_time_has_passed(self):
        zone = seeded(
            event_class="work_zone",
            start_time="2026-08-12T00:00:00.000Z",
            end_time="2026-09-30T00:00:00.000Z",
        )
        assert zone.lifecycle_state == "validated"
        result = expire(zone, NOW + timedelta(days=3))
        assert result.action == "expired"
        assert result.changes[0].final.lifecycle_state == "active"


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


class _StubEvents:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries):  # noqa: N803 - boto3's own parameter name
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class _ExecutionAlreadyExists(Exception):
    pass


class _StubSfn:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions namespace
        ExecutionAlreadyExists = _ExecutionAlreadyExists

    def __init__(self, fail: Exception | None = None) -> None:
        self.started: list[dict[str, Any]] = []
        self.fail = fail

    def start_execution(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.started.append(kwargs)
        return {"executionArn": "arn:aws:states:::execution/x"}


@pytest.fixture
def timer(monkeypatch):
    monkeypatch.setenv("EVENT_BUS", "test-bus")
    monkeypatch.setenv("EVENT_TABLE", "test-events")
    monkeypatch.setenv("LIFECYCLE_STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:lifecycle")
    module = importlib.import_module("corridor_event_hub.handlers.lifecycle")
    importlib.reload(module)
    monkeypatch.setattr(module, "_events", _StubEvents())
    monkeypatch.setattr(module, "_sfn", _StubSfn())
    monkeypatch.setattr(module, "_store", InMemoryEventStore())
    return module


def _logged(capsys) -> list[dict[str, Any]]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class TestTickHandler:
    def test_re_arms_when_nothing_is_due(self, timer, monkeypatch):
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW)

        result = timer.handler({"eventId": event.event_id, "waitSeconds": 10})
        assert result["status"] == "wait"
        assert result["action"] == "waiting"
        # Slept to just after the deadline rather than exactly on it.
        assert result["waitSeconds"] == 7200 + timer.WAIT_SLACK_SECONDS
        assert timer._store.get_current(event.event_id).lifecycle_state == "active"

    def test_expires_a_stale_event_and_re_arms_for_the_next_rung(self, timer, monkeypatch):
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=3))

        result = timer.handler({"eventId": event.event_id, "waitSeconds": 7200})
        assert result["action"] == "expired"
        assert result["status"] == "wait"
        assert timer._store.get_current(event.event_id).lifecycle_state == "clearing"

    def test_stops_when_the_event_reaches_a_terminal_state(self, timer, monkeypatch):
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=3))
        timer.handler({"eventId": event.event_id, "waitSeconds": 1})
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=6))
        timer.handler({"eventId": event.event_id, "waitSeconds": 1})

        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=9))
        result = timer.handler({"eventId": event.event_id, "waitSeconds": 1})
        assert result["status"] == "done"
        assert result["action"] == "terminal"
        assert timer._store.get_current(event.event_id).lifecycle_state == "cleared"

    def test_reads_the_event_fresh_rather_than_trusting_its_input(self, timer, monkeypatch):
        # A source update between ticks resets the TTL. Acting on state carried in the
        # execution input would expire an event that had just been confirmed.
        from corridor_event_hub.core.resolution import resolve_update

        event = seeded(timer._store)
        refreshed = make_candidate(
            source_updated_at="2026-08-10T14:30:00.000Z",
            end_time="2026-08-10T20:00:00.000Z",
        )
        later = NOW + timedelta(hours=2, minutes=50)
        update = resolve_update(refreshed, score(refreshed, later), event, later)
        for change in update.changes:
            timer._store.write_change(change)

        # The tick wakes at the ORIGINAL deadline, but the event moved.
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=3))
        result = timer.handler({"eventId": event.event_id, "waitSeconds": 7200})
        assert result["action"] == "waiting"
        assert timer._store.get_current(event.event_id).lifecycle_state == "active"

    def test_a_lost_race_re_arms_instead_of_failing(self, timer, monkeypatch, capsys):
        # The resolver writing between this handler's read and its write is normal.
        # Its update is the better information, so the tick yields.
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=3))

        def conflict(change):
            raise ConcurrentModification("resolver got there first")

        timer._store.write_change = conflict
        result = timer.handler({"eventId": event.event_id, "waitSeconds": 1})

        assert result["status"] == "wait"
        assert result["action"] == "superseded"
        assert any(line.get("action") == "superseded" for line in _logged(capsys))

    def test_a_missing_event_ends_the_execution(self, timer, monkeypatch):
        monkeypatch.setattr(timer, "now_utc", lambda: NOW)
        result = timer.handler({"eventId": "GONE", "waitSeconds": 1})
        assert result["status"] == "done"
        assert result["action"] == "missing"

    def test_announces_a_timer_transition_like_any_other_change(self, timer, monkeypatch):
        # A consumer should not need to know whether a feed, a human, or a
        # clock moved an event - only that it moved, and why.
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(hours=3))
        timer.handler({"eventId": event.event_id, "waitSeconds": 1})

        detail = json.loads(timer._events.entries[0]["Detail"])
        assert timer._events.entries[0]["DetailType"] == "EventResolved"
        assert detail["action"] == "expired"
        assert detail["transitions"][0]["trigger"] == "timer_ttl"
        assert detail["lifecycleState"] == "clearing"

    def test_never_sleeps_for_zero_seconds(self, timer, monkeypatch):
        # A Wait of 0 in a loop is a busy spin against DynamoDB and the state
        # transition bill.
        #
        # THE CASE THAT WOULD SPIN is `not_yet_due`: a future-dated work zone whose
        # validated TTL has lapsed writes NOTHING, so `updated_at` never moves and
        # `remaining` stays 0 on every tick, forever. Every other outcome writes a
        # version and resets the clock. The floor is what makes the re-arm cheap.
        event = seeded(
            timer._store,
            event_class="work_zone",
            start_time="2026-09-01T00:00:00.000Z",
            end_time="2026-09-30T00:00:00.000Z",
        )
        assert event.lifecycle_state == "validated"

        monkeypatch.setattr(timer, "now_utc", lambda: NOW + timedelta(days=3))
        result = timer.handler({"eventId": event.event_id, "waitSeconds": 1})

        assert result["action"] == "not_yet_due"
        assert result["status"] == "wait"
        assert result["waitSeconds"] == timer.MIN_WAIT_SECONDS
        # Nothing was written, which is the point - and why the floor is load-bearing.
        assert timer._store.get_current(event.event_id).version == event.version


class TestExecutionHandoff:
    """The history limit that would otherwise end a timer chain silently"""

    def test_hands_off_to_a_successor_at_the_tick_cap(self, timer, monkeypatch):
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW)

        result = timer.handler(
            {
                "eventId": event.event_id,
                "waitSeconds": 1,
                "tick": timer.MAX_TICKS_PER_EXECUTION - 1,
            }
        )
        assert result["status"] == "done"
        assert result["action"] == "handed_off"

        started = timer._sfn.started[0]
        assert started["name"].startswith(event.event_id)
        payload = json.loads(started["input"])
        assert payload["eventId"] == event.event_id
        assert payload["tick"] == 0  # the successor starts its own count

    def test_a_duplicate_successor_is_not_an_error(self, timer, monkeypatch):
        # A retry of this handler must not start two chains. Step Functions rejects
        # the duplicate name, which IS the idempotency.
        monkeypatch.setattr(timer, "_sfn", _StubSfn(fail=_ExecutionAlreadyExists()))
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW)

        result = timer.handler(
            {"eventId": event.event_id, "waitSeconds": 1, "tick": timer.MAX_TICKS_PER_EXECUTION}
        )
        assert result["action"] == "handed_off"

    def test_a_failed_handoff_is_reported_rather_than_hidden(self, timer, monkeypatch, capsys):
        monkeypatch.setattr(timer, "_sfn", _StubSfn(fail=RuntimeError("throttled")))
        event = seeded(timer._store)
        monkeypatch.setattr(timer, "now_utc", lambda: NOW)

        result = timer.handler(
            {"eventId": event.event_id, "waitSeconds": 1, "tick": timer.MAX_TICKS_PER_EXECUTION}
        )
        assert result["action"] == "handoff_failed"
        assert any(line["msg"] == "lifecycle_handoff_failed" for line in _logged(capsys))


class TestResolverArmsTheTimer:
    def test_creating_an_event_starts_exactly_one_execution(self, monkeypatch):
        monkeypatch.setenv("EVENT_BUS", "test-bus")
        monkeypatch.setenv("EVENT_TABLE", "test-events")
        monkeypatch.setenv("LIFECYCLE_STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:l")
        module = importlib.import_module("corridor_event_hub.handlers.resolver")
        importlib.reload(module)
        monkeypatch.setattr(module, "_events", _StubEvents())
        monkeypatch.setattr(module, "_sfn", _StubSfn())
        monkeypatch.setattr(module, "_store", InMemoryEventStore())
        monkeypatch.setattr(module, "now_utc", lambda: NOW)

        from corridor_event_hub.core.serde import to_jsonable

        candidate = make_candidate()
        detail = {
            "detail": {
                "sourceId": candidate.source.source_id,
                "rawRef": candidate.source.raw_ref,
                "candidate": to_jsonable(candidate),
                "provisionalConfidence": to_jsonable(score(candidate)),
            }
        }
        first = module.handler(detail)

        assert len(module._sfn.started) == 1
        # The execution NAME is the event id, which is the whole idempotency story:
        # Step Functions rejects a duplicate for 90 days.
        assert module._sfn.started[0]["name"] == first["eventId"]

        # An update must not arm a second timer - the running execution re-reads.
        module.handler(detail)
        assert len(module._sfn.started) == 1

    def test_a_timer_failure_does_not_lose_the_event(self, monkeypatch, capsys):
        # The event is already stored and queryable. Raising would discard a
        # correctly resolved event to the DLQ over a timer.
        monkeypatch.setenv("EVENT_BUS", "test-bus")
        monkeypatch.setenv("EVENT_TABLE", "test-events")
        monkeypatch.setenv("LIFECYCLE_STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:l")
        module = importlib.import_module("corridor_event_hub.handlers.resolver")
        importlib.reload(module)
        monkeypatch.setattr(module, "_events", _StubEvents())
        monkeypatch.setattr(module, "_sfn", _StubSfn(fail=RuntimeError("throttled")))
        monkeypatch.setattr(module, "_store", InMemoryEventStore())
        monkeypatch.setattr(module, "now_utc", lambda: NOW)

        from corridor_event_hub.core.serde import to_jsonable

        candidate = make_candidate()
        result = module.handler(
            {
                "detail": {
                    "sourceId": candidate.source.source_id,
                    "rawRef": candidate.source.raw_ref,
                    "candidate": to_jsonable(candidate),
                    "provisionalConfidence": to_jsonable(score(candidate)),
                }
            }
        )
        assert result["action"] == "created"
        assert module._store.get_current(result["eventId"]) is not None
        assert any(line["msg"] == "lifecycle_timer_failed" for line in _logged(capsys))
