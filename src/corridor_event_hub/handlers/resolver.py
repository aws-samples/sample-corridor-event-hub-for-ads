"""Resolver - candidate events in, versioned events out.

The stage the architecture calls RESOLVE, and the one the pipeline was missing: adapters produced candidates and nothing turned them into events. Triggered by
``CandidateEventProduced`` from the normalizer, one candidate per invocation.

THIS HANDLER IS DELIBERATELY THIN. Every decision - match, merge, field
precedence, the lifecycle chain - is in ``core/resolution.py``, which touches no
AWS API and no clock it was not handed. What is left here is exactly the I/O:
read the store, ask the policy, write the store, announce it. That split is why the
cross-agency merge case is tested without an AWS account, and it is the same seam
reasoning as ``core/lrs.py``.

WHAT MAKES THIS SAFE TO RUN ON A POLLED FEED. The collector re-fetches every 60
seconds, so this function sees the same source record over and over:

  - A per-source-record POINTER maps (sourceId, nativeId) -> eventId, so the
    hundredth delivery of one work zone updates one event rather than creating a
    hundred.
  - A CONTENT HASH on that pointer short-circuits the unchanged case, which is most
    of them. Without it every poll would write a version nobody asked for and
    "how many times did this event change" would be unanswerable.
  - The hash deliberately EXCLUDES retrieved_at and raw_ref, because those change
    on every fetch by construction. Hashing them would make every poll look like a
    change, which is the same bug as having no hash at all but harder to see.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from ..core.awsclients import client as aws_client
from ..core.eventstore import (
    ConcurrentModification,
    DynamoEventStore,
    EventStore,
    SourcePointer,
    VersionAlreadyExists,
)
from ..core.ids import new_event_id
from ..core.matcher import NEAR_MISS_DECAY_MILES
from ..core.resolution import (
    IllegalTransition,
    Resolution,
    resolve_new,
    resolve_update,
    seconds_until_ttl,
)
from ..core.serde import candidate_from_dict, confidence_from_dict, dumps, to_jsonable
from ..core.timeutil import now_utc, parse_iso
from ..core.types import CandidateEvent

_events = aws_client("events")
_sfn = aws_client("stepfunctions")

#: Built once per container, like the normalizer's conflator: a client is not free
#: and the store holds no per-invocation state.
_store: EventStore | None = None

# EventBridge caps PutEvents at 10 entries per call.
_PUT_EVENTS_BATCH = 10

#: How far either side of a candidate to look for events it might be.
#:
#: NOT an arbitrary radius. Beyond ``NEAR_MISS_DECAY_MILES`` the matcher's spatial
#: component is zero, which fails the spatial gate, which caps the score below the
#: review band - so an event further away than this CANNOT merge and cannot even
#: reach review. Reading more of the corridor would cost money to reach the same
#: verdict. Tying it to the matcher's own constant means the two cannot drift.
SEARCH_PAD_MILES = NEAR_MISS_DECAY_MILES


def store() -> EventStore:
    global _store
    if _store is None:
        _store = DynamoEventStore()
    return _store


def content_hash(candidate: CandidateEvent) -> str:
    """A stable fingerprint of what the SOURCE said, ignoring when we asked.

    ``retrieved_at`` and ``raw_ref`` are excluded: both change on every fetch, so
    including them would defeat the idempotency the hash exists to provide.
    ``source_updated_at`` is kept - that one changing IS the source telling us the
    record changed.
    """
    payload = to_jsonable(candidate)
    source = dict(payload.get("source") or {})
    source.pop("retrieved_at", None)
    source.pop("raw_ref", None)
    payload["source"] = source
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def ingest_latency_ms(candidate: CandidateEvent, now: Any) -> int | None:
    """Milliseconds from FETCH to QUERYABLE.

    "p95 ingest-to-queryable latency for incidents is at or under 90 s under 10x
    load" (acceptance criterion 9), and this is the only place in the pipeline where
    both ends of that interval are known:

      the START is ``source.retrieved_at`` - when the collector fetched the bytes -
      which travels with the candidate all the way here.

      the END is now, because this function is what makes the event queryable. The
      moment the version lands in the store, the query API can return it.

    NOT the per-invocation `latencyMs`, which times this handler alone and would
    report 40ms for a payload that spent two minutes waiting on a retry. That number
    is useful for tuning the function; this one is what a consumer actually waits.

    ``retrieved_at`` is set by our own collector rather than by an agency, so it is
    OUR clock at both ends and the subtraction is meaningful. A negative result
    would mean clock skew inside our own account, so it clamps at zero rather than
    reporting a negative latency that would poison a p95.
    """
    fetched = parse_iso(candidate.source.retrieved_at)
    if fetched is None:
        return None
    return max(0, int((now - fetched).total_seconds() * 1000))


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Triggered by the ``CandidateEventProduced`` EventBridge rule."""
    started = time.monotonic()
    detail = event["detail"]
    candidate = candidate_from_dict(detail["candidate"])
    confidence = confidence_from_dict(detail.get("provisionalConfidence") or {})
    payload_ref = detail.get("rawRef")
    source_id = candidate.source.source_id
    fingerprint = content_hash(candidate)

    # One retry, in process, and only for the case where retrying is the right
    # answer. A concurrent write means the decision was made against a version that
    # is no longer current, so the fix is to re-read and decide again - not to
    # retry the same write. Anything still conflicting after that goes to the DLQ,
    # where a human can see that two payloads are fighting over one event.
    attempts = 2
    for attempt in range(attempts):
        try:
            resolution = _resolve(candidate, confidence, fingerprint, payload_ref)
            break
        except ConcurrentModification as exc:
            if attempt == attempts - 1:
                raise
            print(
                dumps(
                    {
                        "msg": "resolver_retry",
                        "sourceId": source_id,
                        "reason": str(exc),
                    }
                )
            )
        except VersionAlreadyExists as exc:
            # This exact version is already stored, so the work is done. A
            # duplicate delivery is a normal event on an at-least-once bus, not a
            # failure, and raising here would send a correct outcome to the DLQ.
            print(dumps({"msg": "resolver_duplicate_delivery", "sourceId": source_id,
                         "reason": str(exc)}))
            return {"action": "unchanged", "eventId": None, "versions": 0}
        except IllegalTransition as exc:
            # Rejected and alarmed, never coerced. Raising puts the payload in
            # the DLQ with the reason attached, which is what "alarmed" means here -
            # see the onFailure destination in lib/ingest-stack.ts.
            print(dumps({"msg": "illegal_transition", "sourceId": source_id,
                         "reason": str(exc)}))
            raise

    _announce(resolution, source_id, payload_ref)

    latency_ms = int((time.monotonic() - started) * 1000)
    final = resolution.changes[-1].final if resolution.changes else None
    # THE LOG FORMAT IS A CONTRACT. Every metric filter in
    # lib/observability-stack.ts reads these names - see tests/test_handlers.py on
    # why renaming one blanks a dashboard silently rather than failing a deploy.
    print(
        dumps(
            {
                "msg": "resolved",
                "sourceId": source_id,
                "action": resolution.action,
                # A DIMENSION on the latency metric, not decoration: the 90s
                # budget is per class, and IngestLatencyMs is dimensioned by this
                # field. Taken from the candidate rather than
                # from `final` so it is present even when nothing was written.
                "eventClass": candidate.event_class,
                "eventId": resolution.subject_event_id,
                "matchedEventId": resolution.matched_event_id,
                "matchValue": resolution.match.value if resolution.match else None,
                "lifecycleState": final.lifecycle_state if final else None,
                "confidence": final.confidence.value if final else None,
                "versions": sum(len(change.steps) for change in resolution.changes),
                "reviews": len(resolution.reviews),
                "latencyMs": latency_ms,
                # The number the budget is stated against. See
                # ingest_latency_ms - this is the ONLY place both ends are known.
                "ingestLatencyMs": ingest_latency_ms(candidate, now_utc()),
                "explanation": resolution.explanation,
            }
        )
    )

    return {
        "action": resolution.action,
        "eventId": resolution.subject_event_id,
        "matchedEventId": resolution.matched_event_id,
        "versions": sum(len(change.steps) for change in resolution.changes),
        "reviews": len(resolution.reviews),
    }


def _resolve(
    candidate: CandidateEvent,
    confidence: Any,
    fingerprint: str,
    payload_ref: str | None,
) -> Resolution:
    """Read the store, ask the policy, write the store."""
    import math

    events = store()
    now = now_utc()
    source_id = candidate.source.source_id
    native_id = candidate.source.native_id

    pointer = events.get_pointer(source_id, native_id)
    known = events.get_current(pointer.event_id) if pointer else None

    if pointer is not None and known is not None:
        resolution = resolve_update(
            candidate,
            confidence,
            known,
            now,
            payload_ref=payload_ref,
            content_unchanged=pointer.content_hash == fingerprint,
        )
    else:
        # A pointer with no event is a store that lost the event but kept the
        # pointer. Treated as new rather than as an error: recreating the event is
        # recoverable, and refusing to would drop the record entirely.
        nearby = (
            []
            if math.isnan(candidate.extent.begin_measure)
            else events.events_overlapping(
                candidate.extent.route,
                min(candidate.extent.begin_measure, candidate.extent.end_measure)
                - SEARCH_PAD_MILES,
                max(candidate.extent.begin_measure, candidate.extent.end_measure)
                + SEARCH_PAD_MILES,
            )
        )
        resolution = resolve_new(
            candidate,
            confidence,
            new_event_id(now),
            nearby,
            now,
            payload_ref=payload_ref,
        )

    for change in resolution.changes:
        events.write_change(change)

    if resolution.reviews:
        events.queue_reviews(resolution.reviews)

    # Arm the TTL timer for every event this created. Only on creation - an
    # update does not need a second timer, because the running execution reads the
    # event fresh on every tick and picks up the reset TTL by itself.
    for change in resolution.changes:
        if change.created:
            _arm_lifecycle_timer(change.final, now)

    # The pointer moves LAST, and only after the versions are stored. If this
    # function dies before this line, the next delivery re-resolves from unchanged
    # state; if it died after, the work is already done. Writing the pointer first
    # would create the one unrecoverable order: a record marked as handled whose
    # event was never written.
    if resolution.action != "unchanged":
        target = resolution.subject_event_id
        if target:
            events.put_pointer(
                SourcePointer(
                    source_id=source_id,
                    native_id=native_id,
                    event_id=target,
                    content_hash=fingerprint,
                    source_updated_at=candidate.source.source_updated_at,
                )
            )
    return resolution


def _arm_lifecycle_timer(event: Any, now: Any) -> None:
    """Start the per-event TTL execution.

    THE EXECUTION NAME IS THE EVENT ID, which is the whole idempotency story: Step
    Functions rejects a duplicate name for 90 days, so a redelivered payload cannot
    start a second timer for one event. No condition to check and no state to keep.

    FAILING TO ARM MUST NOT FAIL THE RESOLUTION. The event is already stored and
    queryable; losing its timer means it will not auto-expire, which the query API
    still surfaces as `lifecycle.ttl_expired`. Raising here would instead discard a
    correctly resolved event to the DLQ over a timer, which is the worse trade. The
    log line is what an alarm reads.
    """
    arn = os.environ.get("LIFECYCLE_STATE_MACHINE_ARN")
    if not arn:
        # Not deployed, or deliberately off. Said once per invocation rather than
        # silently, so "nothing ever expires" is never a mystery.
        print(dumps({"msg": "lifecycle_timer_unconfigured", "eventId": event.event_id}))
        return

    wait_seconds = seconds_until_ttl(event, now)
    if wait_seconds is None:
        # A terminal state needs no timer. A candidate that failed validation is
        # created and cleared in one invocation and lands here.
        return

    try:
        _sfn.start_execution(
            stateMachineArn=arn,
            name=event.event_id,
            input=json.dumps(
                {"eventId": event.event_id, "waitSeconds": wait_seconds, "tick": 0}
            ),
        )
    except _sfn.exceptions.ExecutionAlreadyExists:
        pass
    except Exception as exc:  # noqa: BLE001 - a timer failure must not lose the event
        print(
            dumps(
                {
                    "msg": "lifecycle_timer_failed",
                    "eventId": event.event_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )


def _announce(resolution: Resolution, source_id: str, payload_ref: str | None) -> None:
    """Publish what changed.

    The table's DynamoDB stream is the ordered, at-least-once change feed; these
    events are the human-legible companion to it - what
    the dashboards, the review console, and anything that should not have to parse
    a stream record subscribe to.
    """
    if resolution.action == "unchanged":
        return

    entries: list[dict[str, Any]] = []
    for change in resolution.changes:
        final = change.final
        entries.append(
            {
                "EventBusName": os.environ["EVENT_BUS"],
                "Source": "corridor-event-hub.resolver",
                "DetailType": "EventResolved",
                "Detail": dumps(
                    {
                        "action": resolution.action,
                        "eventId": final.event_id,
                        "created": change.created,
                        "version": final.version,
                        "lifecycleState": final.lifecycle_state,
                        "eventClass": final.event_class,
                        "beginMeasure": final.extent.begin_measure,
                        "endMeasure": final.extent.end_measure,
                        "confidence": final.confidence.value,
                        "agencies": sorted({s.agency for s in final.sources}),
                        "matchedEventId": resolution.matched_event_id,
                        "matchValue": resolution.match.value if resolution.match else None,
                        # The explanation travels with the announcement, so a
                        # consumer never has to ask a second question to find out why.
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
                        "sourceId": source_id,
                        "rawRef": payload_ref,
                        "policyVersion": resolution.policy_version,
                    }
                ),
            }
        )

    for review in resolution.reviews:
        # An ambiguous pair is an output, not a log line. Both
        # events stay separately published; this is what puts the pair in front of
        # a human.
        entries.append(
            {
                "EventBusName": os.environ["EVENT_BUS"],
                "Source": "corridor-event-hub.resolver",
                "DetailType": "MatchReviewQueued",
                "Detail": dumps(review),
            }
        )

    for start in range(0, len(entries), _PUT_EVENTS_BATCH):
        _events.put_events(Entries=entries[start : start + _PUT_EVENTS_BATCH])
