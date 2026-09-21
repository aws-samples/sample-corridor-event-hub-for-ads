"""The event store.

THE TABLE DESIGN IS ALREADY DECLARED, in lib/ingest-stack.ts, and this module is
the other half of that contract:

    PK = eventId,  SK = 'current' | 'v#<n>' | 'audit#<n>'
    GSI 'by-state-measure' = (gsiLifecycleRoute, gsiBeginMeasure)

APPEND-ONLY, AND THE CONDITION EXPRESSIONS ARE WHAT MAKE IT TRUE. Nothing
overwrites a version or an audit record: every write of one carries
``attribute_not_exists(sk)``, so a replayed payload cannot rewrite history
even by accident. Only the ``current`` pointer is updated in place, and that
update is conditional on the version it thinks it is replacing, which is the
optimistic concurrency needed when two agencies' payloads land on one event in the
same second.

ONE TRANSACTION PER VERSION, and this is the decision worth explaining. Each step
writes three items - the version, its audit record, and the moved ``current``
pointer - and they go in a single ``transact_write_items``. Writing them
separately would be cheaper and would occasionally leave a version with no audit
record, or a ``current`` pointing at a version that is not there. An audit trail
with holes is not an audit trail, and there is no "mostly" version of that.

That is also why this module uses the LOW-LEVEL client and its own serializer
rather than ``boto3.resource('dynamodb')`` the way handlers/collector.py does: the
resource API has no transactional write. One serialization strategy for the whole
module, stated once here, rather than two that agree until they do not.

WHAT IS NOT HERE. No spatial index, because there is nothing spatial left: once
conflation has produced a measure range, "events between mile 300 and 350" is a
numeric range query on a GSI sort key. That is the central claim of
ADR 0002 § The split, and why, and this file is
where it is either true or false.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .lifecycle import AuditRecord
from .resolution import EventChange, ReviewItem
from .serde import event_from_dict, to_jsonable
from .types import Event

#: Sort-key prefixes. Zero-padded so lexicographic order IS numeric order - `v#10`
#: must not sort before `v#9`, which is exactly what an unpadded key would do and
#: would silently corrupt "the latest version" for any event that lives past nine
#: versions. A polled feed reaches nine versions in ten minutes.
_VERSION_WIDTH = 10
CURRENT_SK = "current"


def version_sk(version: int) -> str:
    return f"v#{version:0{_VERSION_WIDTH}d}"


def audit_sk(sequence: int) -> str:
    return f"audit#{sequence:0{_VERSION_WIDTH}d}"


#: Source-record pointers and the review queue live in their own partitions of the
#: same table. A prefix rather than a second table: they are read on the same code
#: path as the events, they are tiny, and a second table would mean a second set of
#: grants and a second thing to remember to deploy.
POINTER_PREFIX = "src#"
REVIEW_PARTITION = "review"


def pointer_pk(source_id: str, native_id: str) -> str:
    return f"{POINTER_PREFIX}{source_id}#{native_id}"


#: How far WEST of a query's lower bound to start scanning, in corridor miles.
#:
#: THE INTERVAL-QUERY PROBLEM, stated plainly because it is a real limitation and
#: not a rounding detail. The GSI is sorted by `begin_measure`, so a range query
#: naturally finds events that START inside the window. An event that starts west of
#: the window and RUNS INTO it - a 200-mile weather alert, a work zone spanning a
#: state line - would be missed. Widening the lower bound by this much and filtering
#: on the true overlap afterwards catches every event shorter than this constant.
#:
#: An event longer than this is still missed. That is the honest limit of one sort
#: key: the fix is an interval index, which is a PostGIS range type or a second GSI
#: keyed on end_measure. That remains an open design decision rather than something
#: to guess at here. 250 miles comfortably exceeds the longest extent observed in the
#: live feeds (a multi-county NWS alert, ~90 miles of corridor).
RANGE_QUERY_LOOKBACK_MILES = 250.0

#: Which lifecycle states a corridor query reads by default: everything a consumer
#: would consider live. `merged` is excluded because a merged event is represented
#: by its parent, and `cleared`/`archived` because they are history - both are
#: reachable explicitly by naming them.
LIVE_STATES = ("reported", "validated", "active", "clearing")


@dataclass
class SourcePointer:
    """What we already know about one source's record.

    ``content_hash`` is what makes idempotency cheap: the collector re-fetches
    every 60 seconds and most records are unchanged, so comparing a hash is the
    difference between one read and a write of a version nobody needed.
    """

    source_id: str
    native_id: str
    event_id: str
    content_hash: str
    source_updated_at: str | None


@dataclass
class History:
    """The full state history, both clocks."""

    versions: list[Event]
    audit: list[AuditRecord]


class EventStore:
    """The seam. ``DynamoEventStore`` and ``InMemoryEventStore`` both satisfy it.

    Same reasoning as ``core/lrs.Conflator``: the interesting logic is in
    core/resolution.py, and it should be testable - and runnable in the local UI
    API - without an AWS account. A store behind an interface also means DynamoDB
    versus Postgres for the event store stays an arguable choice rather than a
    rewrite.
    """

    def get_current(self, event_id: str) -> Event | None:
        raise NotImplementedError

    def get_pointer(self, source_id: str, native_id: str) -> SourcePointer | None:
        raise NotImplementedError

    def write_change(self, change: EventChange) -> None:
        raise NotImplementedError

    def put_pointer(self, pointer: SourcePointer) -> None:
        raise NotImplementedError

    def events_overlapping(
        self,
        route: str,
        begin_measure: float,
        end_measure: float,
        states: Iterable[str] = LIVE_STATES,
    ) -> list[Event]:
        raise NotImplementedError

    def history(self, event_id: str) -> History:
        raise NotImplementedError

    def queue_reviews(self, items: Iterable[ReviewItem]) -> None:
        raise NotImplementedError

    def open_reviews(self, limit: int = 100) -> list[ReviewItem]:
        raise NotImplementedError


class ConcurrentModification(Exception):
    """Another writer moved this event while we were deciding what to write.

    NOT a bug and NOT something to paper over with a retry inside the store: the
    resolver's decision was made against a version that is no longer current, so
    the decision itself has to be remade against fresh state. The handler lets the
    invocation fail and Lambda redelivers, which re-reads. See handlers/resolver.py.
    """


class VersionAlreadyExists(Exception):
    """A version or audit record with this sequence is already stored.

    The append-only guarantee firing. In normal operation this means the same
    payload was delivered twice and the first delivery won, which is idempotency
    working rather than failing - the handler treats it as a no-op.
    """


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
#
# DynamoDB has no float type: numbers are decimal, arbitrary precision. Handing
# boto3 a Python float raises, and handing it `Decimal(0.1)` stores the binary
# artifact 0.1000000000000000055511151231257827. Both are avoided by going through
# `str`, which is what `repr` of a float gives to the shortest round-tripping
# representation.
#
# NaN never reaches here: `to_jsonable` turns a non-finite measure into None
#, and DynamoDB would reject Decimal('NaN') anyway.


def _to_item(value: Any) -> Any:
    value = to_jsonable(value)
    return _decimalize(value)


def _decimalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def _from_item(value: Any) -> Any:
    if isinstance(value, Decimal):
        # An integral Decimal comes back as int so `version` and `ordinal` are ints
        # rather than 3.0 - which would serialize into the API as `3.0` and read as
        # a float to any consumer with a typed schema.
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: _from_item(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_item(v) for v in value]
    return value


def audit_from_dict(data: dict[str, Any]) -> AuditRecord:
    """Public because core/cloud.py reads audit items directly.

    The lifecycle tracker reads the most recent slice of a long history with its own
    descending query - an event polled every 60 seconds for a day has 1,500 versions,
    and ``history`` deliberately returns ALL of them. One parser for both paths, so a
    field added to an audit record cannot be understood by one reader and not the
    other.
    """
    return AuditRecord(
        event_id=data["event_id"],
        sequence=int(data["sequence"]),
        from_state=data.get("from_state"),
        to_state=data["to_state"],
        trigger=data["trigger"],
        actor=data["actor"],
        trigger_payload_ref=data.get("trigger_payload_ref"),
        rule_version=data.get("rule_version") or "unknown",
        reason=data.get("reason") or "",
        occurred_at=data.get("occurred_at") or "",
        recorded_at=data.get("recorded_at") or "",
        operator_id=data.get("operator_id"),
    )


def _review_from_dict(data: dict[str, Any]) -> ReviewItem:
    return ReviewItem(
        event_id=data["event_id"],
        other_event_id=data["other_event_id"],
        score=float(data["score"]),
        explanation=list(data.get("explanation") or []),
        queued_at=data.get("queued_at") or "",
        reason=data.get("reason") or "match_score_in_ambiguous_band",
    )


def gsi_partition(lifecycle_state: str, route: str) -> str:
    """The GSI partition key: lifecycle state and corridor together.

    Both, because a query is always "live events on THIS corridor" - a partition
    keyed on route alone would put every cleared event of the corridor's history in
    the same partition as its handful of active ones, and every query would read
    and discard them.
    """
    return f"{lifecycle_state}#{route}"


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------


class DynamoEventStore(EventStore):
    def __init__(self, table_name: str | None = None, client: Any = None) -> None:
        if client is None:
            # Imported here rather than at module scope for the same reason boto3 was:
            # this module is imported by local tools, and awsclients pulls in boto3.
            from .awsclients import client as aws_client

            client = aws_client("dynamodb")
        self._client = client
        self._table = table_name or os.environ["EVENT_TABLE"]
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # --- plumbing ---------------------------------------------------------

    def _serialize(self, item: dict[str, Any]) -> dict[str, Any]:
        return {k: self._serializer.serialize(v) for k, v in item.items() if v is not None}

    def _deserialize(self, item: dict[str, Any]) -> dict[str, Any]:
        return _from_item({k: self._deserializer.deserialize(v) for k, v in item.items()})

    def _event_item(self, event: Event, sk: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "eventId": event.event_id,
            "sk": sk,
            "version": event.version,
            "event": _to_item(event),
        }
        if sk == CURRENT_SK:
            # GSI keys live only on `current`. A version snapshot carrying them
            # would put every historical version of every event into the corridor
            # query's result set.
            item["gsiLifecycleRoute"] = gsi_partition(
                event.lifecycle_state, event.extent.route
            )
            begin = _to_item(event.extent.begin_measure)
            if begin is not None:
                item["gsiBeginMeasure"] = begin
            else:
                # An unresolved extent has no measure, so it cannot be in the
                # corridor index at all. It is still stored and still queryable by
                # id - it is kept rather than dropped - it just does not
                # appear in a range scan, because there is no honest answer to
                # "where is it".
                item.pop("gsiBeginMeasure", None)
                item["gsiLifecycleRoute"] = None
                item = {k: v for k, v in item.items() if v is not None}
        return item

    # --- reads ------------------------------------------------------------

    def get_current(self, event_id: str) -> Event | None:
        response = self._client.get_item(
            TableName=self._table,
            Key=self._serialize({"eventId": event_id, "sk": CURRENT_SK}),
            # Strongly consistent: the resolver decides what to write based on this,
            # and an eventually-consistent read here means deciding against a
            # version that has already been superseded.
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return event_from_dict(self._deserialize(item)["event"])

    def get_pointer(self, source_id: str, native_id: str) -> SourcePointer | None:
        response = self._client.get_item(
            TableName=self._table,
            Key=self._serialize({"eventId": pointer_pk(source_id, native_id), "sk": "pointer"}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        data = self._deserialize(item)
        return SourcePointer(
            source_id=source_id,
            native_id=native_id,
            event_id=data["targetEventId"],
            content_hash=data.get("contentHash") or "",
            source_updated_at=data.get("sourceUpdatedAt"),
        )

    def events_overlapping(
        self,
        route: str,
        begin_measure: float,
        end_measure: float,
        states: Iterable[str] = LIVE_STATES,
    ) -> list[Event]:
        lo = min(begin_measure, end_measure)
        hi = max(begin_measure, end_measure)
        found: dict[str, Event] = {}
        for state in states:
            for item in self._query_all(
                IndexName="by-state-measure",
                KeyConditionExpression=(
                    "gsiLifecycleRoute = :partition AND gsiBeginMeasure BETWEEN :lo AND :hi"
                ),
                ExpressionAttributeValues=self._serialize(
                    {
                        ":partition": gsi_partition(state, route),
                        ":lo": _to_item(lo - RANGE_QUERY_LOOKBACK_MILES),
                        ":hi": _to_item(hi),
                    }
                ),
            ):
                event = event_from_dict(self._deserialize(item)["event"])
                # The lookback above over-reads on purpose; this is where the
                # over-read is discarded on TRUE overlap rather than on start alone.
                if max(event.extent.begin_measure, event.extent.end_measure) >= lo:
                    found[event.event_id] = event
        return sorted(found.values(), key=lambda e: e.extent.begin_measure)

    def history(self, event_id: str) -> History:
        items = self._query_all(
            KeyConditionExpression="eventId = :id",
            ExpressionAttributeValues=self._serialize({":id": event_id}),
            ConsistentRead=True,
        )
        versions: list[Event] = []
        audit: list[AuditRecord] = []
        for raw in items:
            data = self._deserialize(raw)
            sk = data["sk"]
            if sk.startswith("v#"):
                versions.append(event_from_dict(data["event"]))
            elif sk.startswith("audit#"):
                audit.append(audit_from_dict(data["audit"]))
        return History(
            versions=sorted(versions, key=lambda e: e.version),
            audit=sorted(audit, key=lambda a: a.sequence),
        )

    def open_reviews(self, limit: int = 100) -> list[ReviewItem]:
        items = self._query_all(
            KeyConditionExpression="eventId = :id",
            ExpressionAttributeValues=self._serialize({":id": REVIEW_PARTITION}),
            # Newest first: a review queue read by a human wants the most recent
            # ambiguity at the top.
            ScanIndexForward=False,
            limit=limit,
        )
        return [_review_from_dict(self._deserialize(item)["review"]) for item in items]

    def _query_all(self, limit: int | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        """Query, following pagination.

        Paginating rather than trusting one page: DynamoDB caps a query response at
        1MB, and an event with a long version history plus its audit trail passes
        that. A store that silently returned the first page would make the full
        state history quietly partial.
        """
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            page = self._client.query(
                TableName=self._table,
                **({"ExclusiveStartKey": start_key} if start_key else {}),
                **kwargs,
            )
            items.extend(page.get("Items", []))
            start_key = page.get("LastEvaluatedKey")
            if not start_key or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    # --- writes -----------------------------------------------------------

    def write_change(self, change: EventChange) -> None:
        """Persist one event's version chain, one transaction per version.

        Per version rather than one transaction for the whole chain, because a chain
        can exceed the 100-item transaction limit only in theory but exceeds the
        4MB transaction payload limit in practice - three versions of an event
        carrying a county-sized NWS polygon is enough. Each version is individually
        atomic, and a chain that stops halfway leaves a consistent event in an
        earlier state, which the next delivery moves forward.
        """
        for index, step in enumerate(change.steps):
            event = step.event
            creating = change.created and index == 0
            transaction = [
                # 1. the immutable version
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": self._serialize(self._event_item(event, version_sk(event.version))),
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
                # 2. its audit record, keyed by the same sequence
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": self._serialize(
                            {
                                "eventId": event.event_id,
                                "sk": audit_sk(step.audit.sequence),
                                "audit": _to_item(step.audit),
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
                # 3. the current pointer
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": self._serialize(self._event_item(event, CURRENT_SK)),
                        # Creating: nothing may exist. Advancing: the version we are
                        # replacing must still be the one we read. Either way an
                        # unexpected state fails the whole transaction rather than
                        # overwriting a decision made from fresher information.
                        "ConditionExpression": (
                            "attribute_not_exists(eventId)"
                            if creating
                            else "version = :expected"
                        ),
                        **(
                            {}
                            if creating
                            else {
                                "ExpressionAttributeValues": self._serialize(
                                    {":expected": event.version - 1}
                                )
                            }
                        ),
                    }
                },
            ]
            try:
                self._client.transact_write_items(TransactItems=transaction)
            except Exception as exc:  # noqa: BLE001 - re-raised as a typed failure below
                self._reraise(exc, event)

    def _reraise(self, exc: Exception, event: Event) -> None:
        """Turn a TransactionCanceledException into something the caller can act on.

        boto3 reports every cancelled transaction as one exception whose reasons are
        in a list, so the two cases that matter here - "already written" and "someone
        else moved it" - are indistinguishable without reading them.
        """
        name = type(exc).__name__
        reasons = ""
        response = getattr(exc, "response", None) or {}
        if isinstance(response, dict):
            reasons = str(
                [r.get("Code") for r in response.get("CancellationReasons", []) or []]
            )
        if "TransactionCanceled" not in name and "ConditionalCheckFailed" not in name:
            raise exc
        if reasons.count("ConditionalCheckFailed") and "attribute_not_exists" not in reasons:
            # The version item already existed -> this exact write already landed.
            raise VersionAlreadyExists(
                f"{event.event_id} v{event.version} is already stored: {reasons}"
            ) from exc
        raise ConcurrentModification(
            f"{event.event_id} moved while v{event.version} was being written: {reasons}"
        ) from exc

    def put_pointer(self, pointer: SourcePointer) -> None:
        self._client.put_item(
            TableName=self._table,
            Item=self._serialize(
                {
                    "eventId": pointer_pk(pointer.source_id, pointer.native_id),
                    "sk": "pointer",
                    "targetEventId": pointer.event_id,
                    "contentHash": pointer.content_hash,
                    "sourceUpdatedAt": pointer.source_updated_at,
                }
            ),
        )

    def queue_reviews(self, items: Iterable[ReviewItem]) -> None:
        for item in items:
            self._client.put_item(
                TableName=self._table,
                Item=self._serialize(
                    {
                        "eventId": REVIEW_PARTITION,
                        "sk": f"{item.queued_at}#{item.event_id}#{item.other_event_id}",
                        "review": _to_item(item),
                    }
                ),
            )


# ---------------------------------------------------------------------------
# In memory
# ---------------------------------------------------------------------------


class InMemoryEventStore(EventStore):
    """The same store, in a dict.

    Not a mock: it enforces the same append-only and optimistic-concurrency rules,
    so a test that passes against it is testing the resolver's behaviour rather
    than a stub's willingness to accept anything. That is the difference between
    this and a MagicMock, and it is why the interesting resolver tests do not need
    moto or an AWS account.
    """

    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self.versions: dict[str, list[Event]] = {}
        self.audit: dict[str, list[AuditRecord]] = {}
        self.pointers: dict[str, SourcePointer] = {}
        self.reviews: list[ReviewItem] = []

    def get_current(self, event_id: str) -> Event | None:
        return self.events.get(event_id)

    def get_pointer(self, source_id: str, native_id: str) -> SourcePointer | None:
        return self.pointers.get(pointer_pk(source_id, native_id))

    def put_pointer(self, pointer: SourcePointer) -> None:
        self.pointers[pointer_pk(pointer.source_id, pointer.native_id)] = pointer

    def write_change(self, change: EventChange) -> None:
        for index, step in enumerate(change.steps):
            event = step.event
            existing = self.events.get(event.event_id)
            if change.created and index == 0:
                if existing is not None:
                    raise ConcurrentModification(f"{event.event_id} already exists")
            elif existing is None or existing.version != event.version - 1:
                held = existing.version if existing else None
                raise ConcurrentModification(
                    f"{event.event_id}: expected version {event.version - 1}, store holds {held}"
                )
            history = self.versions.setdefault(event.event_id, [])
            if any(v.version == event.version for v in history):
                raise VersionAlreadyExists(f"{event.event_id} v{event.version}")
            history.append(event)
            self.audit.setdefault(event.event_id, []).append(step.audit)
            self.events[event.event_id] = event

    def events_overlapping(
        self,
        route: str,
        begin_measure: float,
        end_measure: float,
        states: Iterable[str] = LIVE_STATES,
    ) -> list[Event]:
        import math

        lo = min(begin_measure, end_measure)
        hi = max(begin_measure, end_measure)
        wanted = set(states)
        out = [
            event
            for event in self.events.values()
            if event.extent.route == route
            and event.lifecycle_state in wanted
            and not math.isnan(event.extent.begin_measure)
            and min(event.extent.begin_measure, event.extent.end_measure) <= hi
            and max(event.extent.begin_measure, event.extent.end_measure) >= lo
        ]
        return sorted(out, key=lambda e: e.extent.begin_measure)

    def history(self, event_id: str) -> History:
        return History(
            versions=sorted(self.versions.get(event_id, []), key=lambda e: e.version),
            audit=sorted(self.audit.get(event_id, []), key=lambda a: a.sequence),
        )

    def queue_reviews(self, items: Iterable[ReviewItem]) -> None:
        self.reviews.extend(items)

    def open_reviews(self, limit: int = 100) -> list[ReviewItem]:
        return sorted(self.reviews, key=lambda r: r.queued_at, reverse=True)[:limit]
