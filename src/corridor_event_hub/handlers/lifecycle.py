"""Lifecycle timer - the TTL half of the state machine.

THE FAILURE THIS EXISTS TO FIX is the well-known one in existing 511 feeds:
a condition that is no longer being reported stays published as live indefinitely,
because nothing ever decides it has gone quiet. Every state has a TTL; when it
lapses the event moves down the ladder toward `cleared` with a `stale_no_updates`
reason. Without this the pipeline reproduces the exact behaviour it criticises, and
does so invisibly - a stale event looks identical to a fresh one.

ONE STEP FUNCTIONS EXECUTION PER EVENT, by design. This handler is the
`Tick` state inside it: wake, read the event, decide, and say how long to sleep
next. The machine is Wait -> Tick -> Choice -> Wait, which means:

  IT READS THE EVENT EVERY TICK RATHER THAN TRUSTING ITS INPUT. A source update
  between ticks resets the TTL and may have moved the event already. Acting on the
  state carried in the execution input would expire an event that had just been
  confirmed - so the input carries only an id and a sleep duration, and everything
  else is read fresh. That also means no execution has to be cancelled or restarted
  when an event changes, which is what makes per-event executions survivable.

  A LOST RACE IS NOT AN ERROR. If the resolver writes a version between this
  handler's read and its write, the store's conditional write refuses and
  the tick simply re-arms. The resolver's update is the better information.

WHY NOT A SCHEDULED SWEEPER over "all events where ttl < now". It is the obvious
alternative and it needs a query this table cannot answer cheaply: expiry time is
derived from `updated_at` plus a per-class TTL, so it is not a key and a sweep is a
scan of every live event every minute. Per-event timers put the cost on the events
that actually have deadlines, and the execution history is a second audit trail for
free. The trade is more executions; at corridor volume that is cheap.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..core.awsclients import client as aws_client
from ..core.eventstore import (
    ConcurrentModification,
    DynamoEventStore,
    EventStore,
    VersionAlreadyExists,
)
from ..core.resolution import expire, seconds_until_ttl
from ..core.serde import dumps
from ..core.timeutil import now_utc

_events = aws_client("events")
_sfn = aws_client("stepfunctions")

_store: EventStore | None = None

#: Never sleep less than this. A TTL that has already lapsed reports 0 seconds
#: remaining, and a Wait of 0 in a loop is a busy spin against DynamoDB and the
#: Step Functions state-transition bill.
MIN_WAIT_SECONDS = 30

#: Slack added to every wait, so the tick lands just AFTER expiry rather than on it.
#: Waking exactly at the deadline means a clock skew of milliseconds reads the event
#: as not-yet-stale, the tick re-arms for 30 seconds, and every event pays two ticks
#: for every one it needs.
WAIT_SLACK_SECONDS = 5

#: Ticks per execution before handing off to a fresh one.
#:
#: Standard executions are capped at 25,000 history events, and every iteration of
#: the loop writes several. An event that keeps being confirmed - a work zone under
#: daily updates, a congestion event cycling active/clearing - can tick for months,
#: and an execution that hits the history limit FAILS, silently ending the only
#: thing that would ever have expired that event. So a bounded execution hands off
#: to a successor and the chain continues. 200 is far under the limit with room for
#: the retries and catch states the machine adds around this handler.
MAX_TICKS_PER_EXECUTION = 200


def store() -> EventStore:
    global _store
    if _store is None:
        _store = DynamoEventStore()
    return _store


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """One tick. Returns the next instruction for the state machine.

    ``status`` is what the machine's Choice state reads:
      wait  - sleep ``waitSeconds`` and tick again
      done  - this event needs no more timers; the execution ends
    """
    event_id = event["eventId"]
    tick = int(event.get("tick") or 0) + 1
    now = now_utc()

    current = store().get_current(event_id)
    if current is None:
        # The event is gone. Not an error worth failing on: the store is
        # authoritative and an execution outliving its event should stop, not retry
        # forever against nothing.
        print(dumps({"msg": "lifecycle_tick", "eventId": event_id, "action": "missing"}))
        return {"eventId": event_id, "status": "done", "action": "missing", "tick": tick}

    resolution = expire(current, now, payload_ref=f"timer:{event_id}#{tick}")
    action = resolution.action

    if action == "expired":
        try:
            for change in resolution.changes:
                store().write_change(change)
            _announce(resolution, current.event_class)
        except (ConcurrentModification, VersionAlreadyExists) as exc:
            # The resolver got there first with better information. Re-arm against
            # whatever it wrote rather than retrying our own stale decision.
            print(
                dumps(
                    {
                        "msg": "lifecycle_tick",
                        "eventId": event_id,
                        "action": "superseded",
                        "reason": str(exc),
                        "tick": tick,
                    }
                )
            )
            action = "superseded"

    final = resolution.changes[-1].final if resolution.changes and action == "expired" else current
    remaining = seconds_until_ttl(final, now)

    if action == "terminal" or remaining is None:
        print(
            dumps(
                {
                    "msg": "lifecycle_tick",
                    "eventId": event_id,
                    "action": "terminal",
                    "lifecycleState": final.lifecycle_state,
                    "tick": tick,
                }
            )
        )
        return {
            "eventId": event_id,
            "status": "done",
            "action": "terminal",
            "lifecycleState": final.lifecycle_state,
            "tick": tick,
        }

    wait_seconds = max(MIN_WAIT_SECONDS, remaining + WAIT_SLACK_SECONDS)

    print(
        dumps(
            {
                "msg": "lifecycle_tick",
                "eventId": event_id,
                "action": action,
                "lifecycleState": final.lifecycle_state,
                "waitSeconds": wait_seconds,
                "tick": tick,
                "explanation": resolution.explanation,
            }
        )
    )

    if tick >= MAX_TICKS_PER_EXECUTION:
        # Hand off before the execution history limit ends this chain silently.
        started = _start_successor(event_id, tick, wait_seconds)
        return {
            "eventId": event_id,
            "status": "done",
            "action": "handed_off" if started else "handoff_failed",
            "tick": tick,
        }

    return {
        "eventId": event_id,
        "status": "wait",
        "action": action,
        "waitSeconds": wait_seconds,
        "tick": tick,
    }


def _start_successor(event_id: str, tick: int, wait_seconds: int) -> bool:
    """Continue the timer chain in a fresh execution.

    The state machine ARN comes from the environment rather than from a construct
    reference, and the name is derived from the event id and the tick count so a
    retry of this handler cannot start two successors: Step Functions rejects a
    duplicate execution name, which is the idempotency rather than a
    condition this code has to check.
    """
    arn = os.environ.get("LIFECYCLE_STATE_MACHINE_ARN")
    if not arn:
        print(dumps({"msg": "lifecycle_handoff_unconfigured", "eventId": event_id}))
        return False
    try:
        _sfn.start_execution(
            stateMachineArn=arn,
            name=f"{event_id}-{tick}"[:80],
            input=json.dumps({"eventId": event_id, "waitSeconds": wait_seconds, "tick": 0}),
        )
        print(dumps({"msg": "lifecycle_handoff", "eventId": event_id, "afterTick": tick}))
        return True
    except _sfn.exceptions.ExecutionAlreadyExists:
        return True
    except Exception as exc:  # noqa: BLE001 - reported, and the tick still succeeded
        # Raising here would fail the execution AND lose the handoff. Reporting it
        # leaves a log line an alarm can find, which is better than both.
        print(dumps({"msg": "lifecycle_handoff_failed", "eventId": event_id,
                     "error": f"{type(exc).__name__}: {exc}"}))
        return False


def _announce(resolution: Any, event_class: str) -> None:
    """A timer-driven transition is a change like any other.

    Emitted with the same DetailType the resolver uses, deliberately: a consumer
    tracking an event should not need to know whether a human, a feed, or a clock
    moved it - only that it moved, and why. The trigger in the transition block says
    which.
    """
    for change in resolution.changes:
        final = change.final
        _events.put_events(
            Entries=[
                {
                    "EventBusName": os.environ["EVENT_BUS"],
                    "Source": "corridor-event-hub.lifecycle",
                    "DetailType": "EventResolved",
                    "Detail": dumps(
                        {
                            "action": "expired",
                            "eventId": final.event_id,
                            "created": False,
                            "version": final.version,
                            "lifecycleState": final.lifecycle_state,
                            "eventClass": event_class,
                            "beginMeasure": final.extent.begin_measure,
                            "endMeasure": final.extent.end_measure,
                            "confidence": final.confidence.value,
                            "agencies": sorted({s.agency for s in final.sources}),
                            "explanation": resolution.explanation,
                            "transitions": [
                                {
                                    "fromState": step.audit.from_state,
                                    "toState": step.audit.to_state,
                                    "trigger": step.audit.trigger,
                                    "reason": step.audit.reason,
                                }
                                for step in change.steps
                            ],
                            "policyVersion": resolution.policy_version,
                        }
                    ),
                }
            ]
        )
