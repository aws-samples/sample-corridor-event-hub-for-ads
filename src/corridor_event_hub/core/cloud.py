"""Read-only reads against the DEPLOYED stack, for the local lifecycle tracker.

    npm run trace         # the API alone on :8788
    npm run trace-ui      # that API plus the React app on :5174

WHY THIS EXISTS: ``strip_server`` runs the adapters and shows one snapshot, and it
says so - it holds no state between builds, so ``historyAvailable`` is false and
elapsed time in a state is not derivable from it. The lifecycle history a record
actually has lives in the deployed event store: an append-only version chain, an
audit record per transition, and a pointer to the exact S3 bytes that caused each
one. This module reads that, so the tracker shows the real thing rather than a
simulation of it.

EVERY ARN AND NAME IS DISCOVERED, NEVER HARDCODED - the same rule scripts/db.sh
follows, for the same reason: literal ARNs from one account silently keep
"working" against the wrong account, and the failure looks like an empty database
rather than a misconfiguration. Discovery reads the CloudFormation outputs the
stacks already publish. Environment variables override, which is what makes a
second deployment or a renamed stack a flag rather than an edit.

READ-ONLY BY CONSTRUCTION, and that is a design constraint rather than a habit.
Every call in this file is a Get/Query/Describe/List. There is no write path, no
operator-override endpoint, and no client with a mutating call on it - so pointing
this tool at production cannot change production. Adding an operator override later
means adding a deliberate, audited, authenticated write path, not relaxing
something here.

boto3 is a DEV dependency (the Lambda runtime provides it), so it is imported
lazily and its absence is reported as the actionable thing it is rather than as an
ImportError from inside a request handler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .eventstore import CURRENT_SK, DynamoEventStore, gsi_partition, pointer_pk
from .serde import event_from_dict
from .types import LIFECYCLE_STATES, Event

#: The stacks that publish what this tool reads. Names, not ARNs, and overridable:
#: a second deployment in the same account is a prefix change.
INGEST_STACK = os.environ.get("CEH_INGEST_STACK", "CorridorEventHubIngest")

#: Upper bound for the corridor range scan that lists events. Finite on purpose -
#: DynamoDB rejects Decimal('Infinity'), and the GSI sort key is a measure in miles,
#: so any number larger than a planet works and this one is obviously not a real
#: milepost.
_MEASURE_CEILING = 1_000_000.0

#: How many bytes of a raw payload are SHOWN. A raw NWS fetch is ~250 KB and an NM
#: WeatherShare fetch is 4 MB; nobody reads 4 MB of JSON in a drawer, and the point of
#: showing raw bytes is to prove the record came from somewhere, which a slice does.
MAX_RAW_BYTES = 512_000

#: How many bytes are READ when a specific record is being extracted, which is more
#: than are shown. Extraction is the reason the panel exists - "this record, as the
#: agency published it" - and the 4 MB WeatherShare payload holds thousands of records
#: with no ordering, so the one being traced can be anywhere in it. Reading 8 MB to
#: find it and displaying 512 KB is the right trade; reading only what is displayed
#: made every WeatherShare record report "not found in this payload", which is a
#: wrong answer rather than a partial one.
MAX_EXTRACT_BYTES = 8_000_000


class CloudUnavailable(Exception):
    """The deployed stack could not be read, with the reason and the fix.

    Raised rather than degraded to an empty result: a tracker that shows no records
    because it could not authenticate looks exactly like a tracker showing a healthy
    empty corridor, and those are opposite conclusions.
    """


@dataclass
class CloudResources:
    """What was discovered, and how - so the UI can name the account it is reading."""

    region: str
    account: str | None
    caller_arn: str | None
    profile: str | None
    event_table: str
    source_catalog_table: str | None = None
    raw_bucket: str | None = None
    event_bus: str | None = None
    lifecycle_state_machine_arn: str | None = None
    dlq_urls: dict[str, str] = field(default_factory=dict)
    query_api_url: str | None = None
    dashboard_url: str | None = None
    scheduled_sources: list[str] = field(default_factory=list)
    discovered_via: str = "cloudformation"
    stack: str = INGEST_STACK


def _boto3() -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise CloudUnavailable(
            "boto3 is not installed. It is a dev dependency here because the Lambda "
            "runtime provides it: run `npm run install-py` (or `pip install -e .[dev]`)."
        ) from exc
    return boto3


def _client(service: str) -> Any:
    """Every client this module builds, carrying the AWS Solutions user agent.

    ``_boto3()`` is still called first so a missing boto3 is reported as the actionable
    thing it is (see its docstring) rather than as an ImportError from inside
    ``awsclients``.
    """
    _boto3()
    from .awsclients import client as aws_client

    return aws_client(service)


#: Discovery is cached for the process: the outputs of a deployed stack do not
#: change while a dev server is running, and a describe-stacks per request would add
#: a round trip to every read for a value that is constant.
_resources: CloudResources | None = None


def resources(refresh: bool = False) -> CloudResources:
    global _resources
    if _resources is not None and not refresh:
        return _resources

    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or _session_region()
    )
    identity = _identity()

    env_table = os.environ.get("EVENT_TABLE")
    outputs: dict[str, str] = {}
    if env_table:
        discovered_via = "environment"
    else:
        outputs = _stack_outputs(INGEST_STACK, region, identity)
        discovered_via = "cloudformation"

    found = CloudResources(
        region=region or "<unset>",
        account=identity.get("account"),
        caller_arn=identity.get("arn"),
        profile=os.environ.get("AWS_PROFILE"),
        event_table=env_table or outputs["EventTableName"],
        source_catalog_table=os.environ.get("SOURCE_CATALOG_TABLE")
        or outputs.get("SourceCatalogTableName"),
        raw_bucket=os.environ.get("RAW_BUCKET") or outputs.get("RawBucketName"),
        event_bus=outputs.get("EventBusName"),
        lifecycle_state_machine_arn=outputs.get("LifecycleStateMachineArn"),
        dlq_urls={
            name.replace("DlqUrl", "").lower(): value
            for name, value in outputs.items()
            if name.endswith("DlqUrl")
        },
        query_api_url=outputs.get("QueryApiUrl"),
        dashboard_url=outputs.get("DashboardUrl"),
        scheduled_sources=[
            s.strip() for s in (outputs.get("ScheduledSources") or "").split(",") if s.strip()
        ],
        discovered_via=discovered_via,
    )

    # The catalog table is not a CloudFormation output, so it is found by its
    # logical-id prefix. Listing tables is cheaper than adding an output that
    # requires a deploy to take effect, and the tracker degrades to "no ingest
    # status" rather than failing if it is absent.
    if found.source_catalog_table is None:
        found.source_catalog_table = _find_table("SourceCatalog")

    _resources = found
    return found


def _session_region() -> str | None:
    try:
        from .awsclients import session

        return session().region_name
    except Exception:  # noqa: BLE001 - absence is the answer, not an error
        return None


def _identity() -> dict[str, Any]:
    """Who we are, asked before anything else.

    First call in the chain on purpose: an expired SSO session, a missing profile
    and a stack that was never deployed produce three different messages, and this
    is the call that distinguishes the first two from the third.
    """
    try:
        got = _client("sts").get_caller_identity()
        return {"account": got.get("Account"), "arn": got.get("Arn")}
    except CloudUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with the fix, not re-raised raw
        raise CloudUnavailable(
            f"no usable AWS credentials ({type(exc).__name__}: {exc}). "
            f"AWS_PROFILE={os.environ.get('AWS_PROFILE') or '<unset>'}, "
            f"region={os.environ.get('AWS_DEFAULT_REGION') or os.environ.get('AWS_REGION') or '<unset>'}. "
            "Set AWS_PROFILE (and refresh SSO if it expired), or point the tracker at a "
            "table directly with EVENT_TABLE=<name>."
        ) from exc


def _stack_outputs(stack: str, region: str | None, identity: dict[str, Any]) -> dict[str, str]:
    try:
        described = _client("cloudformation").describe_stacks(StackName=stack)
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        raise CloudUnavailable(
            f"stack {stack} was not readable in account {identity.get('account')} "
            f"({region}): {type(exc).__name__}: {exc}. Either it is not deployed here, or "
            f"the shell points at a different account. Deploy with `npm run deploy`, set "
            f"CEH_INGEST_STACK=<name> for a differently-named stack, or set "
            f"EVENT_TABLE=<name> to skip discovery."
        ) from exc

    stacks = described.get("Stacks") or []
    outputs = {
        output["OutputKey"]: output["OutputValue"]
        for output in (stacks[0].get("Outputs") or [] if stacks else [])
    }
    if "EventTableName" not in outputs:
        raise CloudUnavailable(
            f"stack {stack} has no EventTableName output, so this is not a Corridor Event Hub "
            f"ingest stack. Found outputs: {', '.join(sorted(outputs)) or '<none>'}."
        )
    return outputs


def _find_table(fragment: str) -> str | None:
    try:
        paginator = _client("dynamodb").get_paginator("list_tables")
        for page in paginator.paginate():
            for name in page.get("TableNames", []):
                if fragment in name and name.startswith(INGEST_STACK):
                    return name
    except Exception:  # noqa: BLE001 - a missing catalog costs one panel, not the tool
        return None
    return None


def store() -> DynamoEventStore:
    """The same event store the deployed handlers use, pointed at the same table.

    Reusing ``DynamoEventStore`` rather than hand-rolling reads is the point: its
    ``history`` already paginates (an event with 49 versions and a county-sized NWS
    polygon passes DynamoDB's 1 MB response cap), and a second implementation would
    be a second thing to keep correct.
    """
    return DynamoEventStore(table_name=resources().event_table)


# ---------------------------------------------------------------------------
# Listing current records
# ---------------------------------------------------------------------------


def list_current(
    route: str,
    states: list[str] | None = None,
    limit: int = 500,
) -> tuple[list[Event], dict[str, int], bool]:
    """Every current record on a corridor, by lifecycle state.

    NO SCAN. The GSI partition is ``state#route`` (core/eventstore.gsi_partition), so
    one query per requested state returns exactly the records in it, sorted by
    corridor measure. That is why this can list 800 cleared events without reading
    the 47,000 items in the table - the versions and audit records are in the same
    partitions but not in this index.

    THE ONE BLIND SPOT, stated because the count in the UI would otherwise be
    silently wrong: a record whose extent never conflated has no measure, so
    ``DynamoEventStore`` deliberately leaves it out of the index (there is no honest
    answer to "where is it"). Such records are invisible here and reachable by id.
    ``unresolved_count`` in the pipeline summary is where that number comes from.

    Returns ``(events, counts_by_state, truncated)``.
    """
    wanted = [s for s in (states or list(LIFECYCLE_STATES)) if s in LIFECYCLE_STATES]
    client = _client("dynamodb")
    table = resources().event_table
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()

    events: list[Event] = []
    counts: dict[str, int] = {}

    # Read ONE MORE than asked for. That extra record is what distinguishes "there
    # are exactly `limit` records" from "there are more and you are seeing the first
    # `limit` of them" - and reporting the second as the first is how a listing
    # quietly becomes a wrong answer.
    ceiling = limit + 1

    for state in wanted:
        found = 0
        start_key: dict[str, Any] | None = None
        while len(events) < ceiling:
            page = client.query(
                TableName=table,
                IndexName="by-state-measure",
                KeyConditionExpression=(
                    "gsiLifecycleRoute = :partition AND gsiBeginMeasure BETWEEN :lo AND :hi"
                ),
                ExpressionAttributeValues={
                    ":partition": {"S": gsi_partition(state, route)},
                    ":lo": {"N": str(-_MEASURE_CEILING)},
                    ":hi": {"N": str(_MEASURE_CEILING)},
                },
                **({"ExclusiveStartKey": start_key} if start_key else {}),
            )
            for item in page.get("Items", []):
                row = {k: deserializer.deserialize(v) for k, v in item.items()}
                events.append(event_from_dict(_plain(row["event"])))
                found += 1
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                break
        counts[state] = found

    return events[:limit], counts, len(events) > limit


def _plain(value: Any) -> Any:
    """Decimal -> int/float, everywhere in a deserialized item.

    DynamoDB has no float type, so every number comes back as a Decimal and
    ``event_from_dict`` would carry Decimals into a document that gets JSON-encoded.
    Mirrors ``eventstore._from_item``; called here because this module queries the
    index directly rather than through the store's own read path.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def state_counts(route: str) -> dict[str, int]:
    """How many current records are in each lifecycle state, without reading them.

    ``Select=COUNT`` so the answer costs the index read and no bandwidth - the
    difference between a header that refreshes cheaply and one that pulls every
    cleared event on the corridor to print a number.
    """
    client = _client("dynamodb")
    table = resources().event_table
    counts: dict[str, int] = {}
    for state in LIFECYCLE_STATES:
        total = 0
        start_key: dict[str, Any] | None = None
        while True:
            page = client.query(
                TableName=table,
                IndexName="by-state-measure",
                Select="COUNT",
                KeyConditionExpression="gsiLifecycleRoute = :partition",
                ExpressionAttributeValues={":partition": {"S": gsi_partition(state, route)}},
                **({"ExclusiveStartKey": start_key} if start_key else {}),
            )
            total += page.get("Count", 0)
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                break
        counts[state] = total
    return counts


@dataclass
class HistorySlice:
    """The most recent slice of one record's history, with the true totals.

    WHY A SLICE AND NOT THE WHOLE THING. ``EventStore.history`` returns every version
    and every audit record, correctly and with pagination - and against live data
    that is 1,564 versions of one Oklahoma work zone, several megabytes, for a view
    that can only usefully render the last few hundred steps. So the tracker reads
    the tail and carries the totals alongside it, and the trace document says
    explicitly that it is windowed. A view that showed 200 of 1,564 steps without
    saying which is the failure this codebase keeps refusing to ship.
    """

    versions: list[Event]
    audit: list[Any]
    version_total: int
    audit_total: int
    window: int

    @property
    def windowed(self) -> bool:
        return self.audit_total > len(self.audit) or self.version_total > len(self.versions)

    def totals(self) -> dict[str, Any]:
        return {
            "versions": self.version_total,
            "audit": self.audit_total,
            "windowed": self.windowed,
            "window": self.window,
        }


#: Sort-key bounds for the two item families under one event id. The version keys are
#: zero-padded (`v#0000000001`), so digits sort below 'z' and these bounds cover any
#: number of versions without knowing how many there are.
_VERSION_RANGE = ("v#", "v#z")
_AUDIT_RANGE = ("audit#", "audit#z")


def history_window(event_id: str, window: int = 200) -> HistorySlice:
    """The last ``window`` versions and audit records of one event, newest read first.

    ``window + 1`` versions are read on purpose: the oldest step shown needs its
    PREDECESSOR to have a diff at all, and without it the first row in the view would
    silently show no changes rather than showing what changed.
    """
    client = _client("dynamodb")
    table = resources().event_table
    from boto3.dynamodb.types import TypeDeserializer

    from .eventstore import audit_from_dict

    deserializer = TypeDeserializer()

    def read(bounds: tuple[str, str], limit: int) -> list[dict[str, Any]]:
        page = client.query(
            TableName=table,
            KeyConditionExpression="eventId = :id AND sk BETWEEN :lo AND :hi",
            ExpressionAttributeValues={
                ":id": {"S": event_id},
                ":lo": {"S": bounds[0]},
                ":hi": {"S": bounds[1]},
            },
            # Descending: the tail is what a window wants, and asking DynamoDB for it
            # backwards costs one query rather than reading everything and slicing.
            ScanIndexForward=False,
            Limit=limit,
        )
        return [
            _plain({k: deserializer.deserialize(v) for k, v in item.items()})
            for item in page.get("Items", [])
        ]

    def count(bounds: tuple[str, str]) -> int:
        total = 0
        start_key: dict[str, Any] | None = None
        while True:
            page = client.query(
                TableName=table,
                Select="COUNT",
                KeyConditionExpression="eventId = :id AND sk BETWEEN :lo AND :hi",
                ExpressionAttributeValues={
                    ":id": {"S": event_id},
                    ":lo": {"S": bounds[0]},
                    ":hi": {"S": bounds[1]},
                },
                **({"ExclusiveStartKey": start_key} if start_key else {}),
            )
            total += page.get("Count", 0)
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return total

    versions = [event_from_dict(row["event"]) for row in read(_VERSION_RANGE, window + 1)]
    audit = [audit_from_dict(row["audit"]) for row in read(_AUDIT_RANGE, window)]
    return HistorySlice(
        versions=sorted(versions, key=lambda v: v.version),
        audit=sorted(audit, key=lambda a: a.sequence),
        version_total=count(_VERSION_RANGE),
        audit_total=count(_AUDIT_RANGE),
        window=window,
    )


def event_id_for_source_record(source_id: str, native_id: str) -> str | None:
    """The agency's own record id -> our event id, by GetItem rather than by search.

    The pointer key is deterministic (``src#<source>#<native>``), which is what makes
    "paste the id from the 511 site and find the record" a single read instead of a
    scan. It is the idempotency pointer, used here for navigation.
    """
    client = _client("dynamodb")
    got = client.get_item(
        TableName=resources().event_table,
        Key={"eventId": {"S": pointer_pk(source_id, native_id)}, "sk": {"S": "pointer"}},
    )
    item = got.get("Item")
    return item["targetEventId"]["S"] if item and "targetEventId" in item else None


def current_event(event_id: str) -> Event | None:
    client = _client("dynamodb")
    got = client.get_item(
        TableName=resources().event_table,
        Key={"eventId": {"S": event_id}, "sk": {"S": CURRENT_SK}},
    )
    item = got.get("Item")
    if not item:
        return None
    from boto3.dynamodb.types import TypeDeserializer

    row = {k: TypeDeserializer().deserialize(v) for k, v in item.items()}
    return event_from_dict(_plain(row["event"]))


# ---------------------------------------------------------------------------
# The ingestion end: raw bytes, and the collector's own status
# ---------------------------------------------------------------------------


def read_raw(ref: str, native_id: str | None = None) -> dict[str, Any]:
    """The exact bytes a record came from, by ``s3://`` reference.

    THIS IS WHAT MAKES THE TRACE REACH INGESTION. Every audit record carries the
    payload pointer that caused it, and being able to open that payload is the
    difference between "the pipeline says a source said this" and seeing the source
    say it.

    ``native_id`` narrows a multi-megabyte fetch to the one record inside it when the
    payload is JSON - a 4 MB WeatherShare document holds thousands of records and
    only one of them is this event's.
    """
    if not ref.startswith("s3://"):
        raise ValueError(f"not an s3 reference: {ref!r}")
    bucket, _, key = ref[len("s3://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 reference: {ref!r}")

    allowed = resources().raw_bucket
    if allowed and bucket != allowed:
        # The reference comes from stored data, and this endpoint turns it into a
        # GetObject. Pinning it to the discovered raw zone keeps a poisoned or
        # mistyped ref from making the dev server a general-purpose S3 reader.
        raise ValueError(f"refusing to read outside the raw zone ({allowed}): {bucket}")

    # Read enough to FIND the record; show only enough to READ. The two limits differ
    # because they serve different purposes - see MAX_EXTRACT_BYTES.
    read_limit = MAX_EXTRACT_BYTES if native_id else MAX_RAW_BYTES
    client = _client("s3")
    got = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{read_limit - 1}")
    body = got["Body"].read()
    total = _content_range_total(got.get("ContentRange")) or got.get("ContentLength")
    text = body.decode("utf-8", errors="replace")
    # Whether the OBJECT was fully read, which is what decides whether "record not
    # found" is a conclusion or an artefact of how much was read.
    complete = not (total and total > len(body))
    shown = text[:MAX_RAW_BYTES]

    out: dict[str, Any] = {
        "ref": ref,
        "bucket": bucket,
        "key": key,
        "bytes_read": len(body),
        "bytes_returned": len(shown.encode("utf-8", errors="replace")),
        "bytes_total": total,
        # About the DISPLAYED body, which is what the reader is looking at.
        "truncated": len(shown) < len(text) or not complete,
        "last_modified": got.get("LastModified").isoformat() if got.get("LastModified") else None,
        "body": shown,
        "record": None,
        "record_note": None,
    }

    if native_id:
        found = _find_record(text, native_id)
        out["record"] = found
        if found is not None:
            if len(shown) < len(text):
                out["record_note"] = (
                    f"found by searching the first {len(body)} bytes of a {total}-byte payload. "
                    f"The slice shown below is smaller than what was searched, so the record "
                    f"above may not appear in it."
                )
        elif not complete:
            # The one case where absence proves nothing, and saying "not found" would
            # be a wrong answer rather than a partial one.
            out["record_note"] = (
                f"payload is {total} bytes and only the first {len(body)} were read, so the "
                f"record could not be located. It is stored whole - read the object directly "
                f"if you need the rest."
            )
        else:
            # Absence here is usually correct and usually not a fault. The commonest
            # cause is that the id was never in the payload to begin with: two feeds
            # publish records with NO per-record id, so the adapter synthesizes one
            # from the record's own content (nm_dot_weathershare._synthesize_native_id).
            # Saying only "not found" would imply the payload is missing something it
            # never had.
            out["record_note"] = (
                f"no object in this payload carries the id {native_id!r}. Usually this means "
                f"the id was SYNTHESIZED by the adapter because the feed publishes no "
                f"per-record id at all, so there is nothing to match on and the whole payload "
                f"below is the evidence. Also expected when the payload is not JSON (a vector "
                f"tile, say), or when the agency reissued the record under a new id."
            )
    return out


def _content_range_total(header: str | None) -> int | None:
    if not header or "/" not in header:
        return None
    try:
        return int(header.rsplit("/", 1)[1])
    except ValueError:
        return None


#: Where an agency record's own id tends to live. Checked in order; the first hit
#: wins. WZDx uses `id`, NWS uses `id` on the feature, AZ511 uses `ID`.
_ID_KEYS = ("id", "ID", "Id", "identifier", "nativeId", "native_id", "RecordId")


def _find_record(text: str, native_id: str) -> Any:
    """Pull one record out of a raw payload by its native id.

    A recursive walk rather than a per-source path: the six feeds nest their records
    differently (``features[]``, ``rows[]``, a bare array), and a walk is one
    implementation instead of six that each drift when a vendor reshapes a response.
    """
    import json

    try:
        payload = json.loads(text)
    except ValueError:
        return None

    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in _ID_KEYS:
                if str(node.get(key)) == native_id:
                    return node
            # NWS puts the id on the feature and the fields under `properties`.
            props = node.get("properties")
            if isinstance(props, dict):
                for key in _ID_KEYS:
                    if str(props.get(key)) == native_id:
                        return node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def source_ingest_status() -> list[dict[str, Any]]:
    """The collector's own record of every fetch: the ingest stage, per feed.

    The catalog table is written by the collector on every attempt, so this answers
    "is anything arriving at all" - the question that has to be asked before any
    per-record question makes sense. A feed whose last attempt failed explains an
    entire corridor going quiet far faster than reading records does.
    """
    table = resources().source_catalog_table
    if not table:
        return []
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    client = _client("dynamodb")
    rows: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    while True:
        # A scan, and the right call: this table has one item per source (six), so
        # a scan is one read unit and a query would need a key nobody has.
        page = client.scan(
            TableName=table, **({"ExclusiveStartKey": start_key} if start_key else {})
        )
        for item in page.get("Items", []):
            rows.append(_plain({k: deserializer.deserialize(v) for k, v in item.items()}))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break
    return sorted(rows, key=lambda r: str(r.get("sourceId")))


# ---------------------------------------------------------------------------
# Pipeline health: the parts of "from ingestion to the end" that are not per-record
# ---------------------------------------------------------------------------


def dlq_depths() -> list[dict[str, Any]]:
    """Depth of every dead-letter queue. Should be zero, and says so when it is not.

    A record that never appears in the tracker did not necessarily fail to exist -
    it may be sitting in a DLQ. Which is why this is on the same screen: an empty
    record list with a non-empty DLQ is a completely different situation from an
    empty record list with empty queues.
    """
    client = _client("sqs")
    out: list[dict[str, Any]] = []
    for name, url in sorted(resources().dlq_urls.items()):
        try:
            attributes = client.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            out.append(
                {
                    "queue": name,
                    "url": url,
                    "depth": int(attributes.get("ApproximateNumberOfMessages", 0)),
                    "in_flight": int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - one unreadable queue is not fatal
            out.append(
                {"queue": name, "url": url, "depth": None, "in_flight": None, "error": str(exc)}
            )
    return out


#: Timer executions to sample. One RUNNING execution per live event is normal
#: (the stack output says so), so this is a sample for the counts rather
#: than a listing - and it is capped, and the cap is reported.
_EXECUTION_SAMPLE = 100


def timer_health() -> dict[str, Any]:
    """The TTL timers, as the tracker needs them: how many, and are any dead.

    A FAILED execution means one event will never expire, which is exactly the
    ``ttl_expired_not_moved`` finding on a record seen from the other end. Both
    views are here because the record-level finding tells you a record is stuck and
    this tells you whether it is one record or all of them.
    """
    arn = resources().lifecycle_state_machine_arn
    if not arn:
        return {"state_machine_arn": None, "note": "no lifecycle state machine output found"}
    client = _client("stepfunctions")
    out: dict[str, Any] = {
        "state_machine_arn": arn,
        "sampled": _EXECUTION_SAMPLE,
        "counts": {},
        "failed": [],
        "note": (
            f"counts are over the most recent {_EXECUTION_SAMPLE} executions per status, not "
            f"the full history - a RUNNING execution per live event is normal."
        ),
    }
    for status in ("RUNNING", "FAILED", "TIMED_OUT", "ABORTED", "SUCCEEDED"):
        try:
            page = client.list_executions(
                stateMachineArn=arn, statusFilter=status, maxResults=_EXECUTION_SAMPLE
            )
        except Exception as exc:  # noqa: BLE001 - report, do not fail the panel
            out["counts"][status] = None
            out.setdefault("errors", {})[status] = str(exc)
            continue
        executions = page.get("executions", [])
        out["counts"][status] = len(executions)
        out["counts_capped"] = out.get("counts_capped") or len(executions) >= _EXECUTION_SAMPLE
        if status in ("FAILED", "TIMED_OUT", "ABORTED"):
            out["failed"].extend(
                {
                    "name": e.get("name"),
                    "status": status,
                    "started_at": e.get("startDate").isoformat() if e.get("startDate") else None,
                    "stopped_at": e.get("stopDate").isoformat() if e.get("stopDate") else None,
                }
                for e in executions[:20]
            )
    return out
