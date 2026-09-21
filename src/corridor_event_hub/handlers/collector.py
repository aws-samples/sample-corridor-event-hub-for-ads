"""Collector - fetch one source, store raw bytes immutably, announce it.

This function does NOT parse. That separation is Raw payloads are
persisted BEFORE any transformation, with fetch metadata, so every normalized
record links back to the exact bytes it came from and replay is possible
without contacting sources again.

Runs in the VPC, so its outbound feed calls go through NAT - see network-stack.ts
on why that is the dominant cost line in this stack.

HTTP is ``urllib`` from the standard library rather than ``requests`` or
``httpx``. The Lambda bundle then carries no HTTP dependency at all, which keeps
the deployment package to shapely plus this package - and a collector whose only
job is "GET a URL, keep the bytes" does not need a session abstraction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..core.awsclients import client as aws_client
from ..core.awsclients import resource as aws_resource
from ..core.config import load_json
from ..core.timeutil import iso_utc

# Built through core.awsclients so every call carries the AWS Solutions user agent.
_s3 = aws_client("s3")
_events = aws_client("events")
_dynamodb = aws_resource("dynamodb")
_secrets = aws_client("secretsmanager")

# Secrets are cached per container. A key fetch per collection run would add
# latency and cost for a value that effectively never changes.
_secret_cache: dict[str, str] = {}

_SOURCE_CATALOG = load_json("sources.json")

FETCH_TIMEOUT_SECONDS = 45

# The query parameter a feed spells its credential with, when the catalog does not
# say. TxDOT and AZ511 both use `key`; Oklahoma uses `access_token`, which is why
# the name is a catalog field at all (see `authQueryParam` in sources.json).
DEFAULT_AUTH_QUERY_PARAM = "key"

# NO CREDENTIAL LITERAL LIVES IN THIS FILE, and the absence is the point.
#
# There used to be a `_PUBLIC_TOKEN_FALLBACK` here: the Oklahoma feed's 64-character
# token, hardcoded as the value used when the environment variable was unset. It was
# defensible on its own terms - the token is published in the federal ITS WorkZone
# Feed Registry, so it is not a secret - and it was still the wrong thing to ship
# A reference architecture teaches by example, and a reader who has
# not read ADR 0004 learns "tokens go in source" from one glance at this file.
#
# Every source now resolves its credential the same way, from Secrets Manager, and
# there is no branch here where a literal could live.


def _get_secret(name: str) -> str:
    cached = _secret_cache.get(name)
    if cached:
        return cached
    response = _secrets.get_secret_value(SecretId=name)
    value = response.get("SecretString")
    if not value:
        raise ValueError(f"secret {name} has no string value")
    _secret_cache[name] = value
    return value


def _catalog_entry(source_id: str) -> dict[str, Any] | None:
    for source in _SOURCE_CATALOG.get("sources", []):
        if source.get("sourceId") == source_id:
            return source
    return None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """EventBridge Scheduler invokes this with ``{"sourceId": "..."}``."""
    source_id = event["sourceId"]
    source = _catalog_entry(source_id)
    if source is None:
        raise ValueError(f"unknown sourceId: {source_id} - not in catalog")

    started_at = datetime.now(timezone.utc)

    # THE PREPARE STEP IS PART OF THE FETCH, for observability purposes.
    #
    # `_fetch` is careful never to raise - a 503 from an agency is an observation,
    # not a bug (see its docstring) - and everything downstream of here is built on
    # that: the `fetch_failed` log line, the `CorridorEventHub/FetchFailures` metric filter
    # over it, the per-source fetch-failure alarms, and the source-health record.
    #
    # `_build_url` was NOT inside that contract, and it does two things that raise:
    # it validates the endpoint scheme, and it resolves the credential from Secrets
    # Manager. A raise there ends the invocation before the failure branch below,
    # so the failure existed ONLY as a Lambda error - no `fetch_failed` line, no
    # `FetchFailures` data point, no per-source alarm, and no `lastError` in the
    # catalog table. THIS HAPPENED: 2026-08-17 21:40-21:48Z, 23 invocations of the
    # three `api_key_secret` sources failed with
    #
    #   ResourceNotFoundException: Secrets Manager can't find the specified secret
    #
    # while the "Fetch failures" widget stayed empty, every
    # `CorridorEventHub-fetch-failures-*` alarm stayed OK, and the catalog still showed the
    # sources as never having failed. Only the generic `CorridorEventHub-lambda-errors`
    # alarm fired, and it names no source. The staleness alarms could not help
    # either: their window floor is 30 minutes and the gap was nine.
    #
    # So a credential we cannot resolve is reported the same way as a feed that
    # will not answer - status 0 with the exception in `error` - because from the
    # operator's side both are "this source produced nothing this poll", and the
    # per-source signal names which source.
    try:
        url = _build_url(source)
        headers = _build_headers(source)
    except Exception as exc:  # noqa: BLE001 - bad scheme, missing/denied secret
        url, headers = source["endpoint"], {}
        status, body, error_message = 0, "", f"{type(exc).__name__}: {exc}"
    else:
        if source.get("authMethod") == "aws_sigv4":
            # A tiled source is not one URL GET: it is N SigV4-signed requests whose
            # addresses come from the corridor geometry. Its endpoint in the catalog is
            # a {Z}/{X}/{Y} TEMPLATE, so GETting it as a literal URL would fetch
            # nothing and report a 403 that looks like an auth problem.
            status, body, error_message = _fetch_tiles(source)
        else:
            status, body, error_message = _fetch(url, headers)

    finished_at = datetime.now(timezone.utc)
    latency_ms = int((finished_at - started_at).total_seconds() * 1000)

    # Record freshness/health regardless of outcome. A failed fetch is
    # data, not just an error - it degrades the confidence of derived records.
    _record_source_health(
        source,
        status=status,
        latency_ms=latency_ms,
        num_bytes=len(body),
        error=error_message,
        at=iso_utc(finished_at),
    )

    if error_message or status != 200 or not body:
        # Do not raise: a single feed being down must not look like a system
        # failure, and must not stop the other sources (isolation NFR).
        print(
            json.dumps(
                {
                    "msg": "fetch_failed",
                    "sourceId": source_id,
                    "status": status,
                    "error": error_message,
                }
            )
        )
        return {
            "sourceId": source_id,
            "status": status,
            "bytes": 0,
            "rawRef": None,
            "unchanged": False,
        }

    raw_bucket = os.environ["RAW_BUCKET"]

    # The checksum makes a duplicate IDENTIFIABLE; it does not deduplicate. The key
    # embeds `iso_utc(started_at)`, so identical bytes fetched a minute apart are two
    # objects and two RawPayloadStored events. Measured against the live raw zone
    # 2026-08-17: 123 checksums stored under more than one key (117 under two, 6 under
    # three) - 122 nm-dot-weathershare, which we poll at 300s and which refreshes every
    # ~600s, plus one tx-dot-wzdx. It was 107 on 2026-08-12; the count grows until this
    # is fixed. Recoverable volume is 521 MB of 19.83 GB, so the reason to fix it is the
    # duplicate downstream events, not the storage.
    #
    # What this layout actually buys is REPLAY determinism - the bytes behind
    # any record are immutable and re-parse identically - not write suppression.
    # Suppressing the duplicate write needs a last-checksum read before put_object;
    # `unchanged` in the return value is the placeholder for that, still hardcoded
    # False. Do not describe this as fetch idempotency until it is.
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    key = _raw_key(source_id, started_at, checksum)
    raw_ref = f"s3://{raw_bucket}/{key}"

    _s3.put_object(
        Bucket=raw_bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
        ChecksumAlgorithm="SHA256",
        # Fetch metadata travels WITH the bytes.
        Metadata={
            "source-id": source_id,
            "fetch-url": url.split("?")[0],  # never persist a token in metadata
            "http-status": str(status),
            "retrieved-at": iso_utc(started_at),
            "latency-ms": str(latency_ms),
            "sha256": checksum,
        },
    )

    _events.put_events(
        Entries=[
            {
                "EventBusName": os.environ["EVENT_BUS"],
                "Source": "corridor-event-hub.collector",
                "DetailType": "RawPayloadStored",
                "Detail": json.dumps(
                    {
                        "sourceId": source_id,
                        "agency": source["agency"],
                        "rawRef": raw_ref,
                        "bucket": raw_bucket,
                        "key": key,
                        "checksum": checksum,
                        "retrievedAt": iso_utc(started_at),
                        "bytes": len(body),
                    }
                ),
            }
        ]
    )

    print(
        json.dumps(
            {
                "msg": "collected",
                "sourceId": source_id,
                "bytes": len(body),
                "latencyMs": latency_ms,
                "rawRef": raw_ref,
            }
        )
    )

    return {
        "sourceId": source_id,
        "status": status,
        "bytes": len(body),
        "rawRef": raw_ref,
        "unchanged": False,
    }


def _fetch(url: str, headers: dict[str, str]) -> tuple[int, str, str | None]:
    """GET a feed. Returns ``(status, body, error)`` and never raises.

    A non-200 is returned rather than raised because an agency feed returning 503
    is an observation about the feed, not a bug in the collector.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(  # nosec B310 # https, enforced by _build_url
            request, timeout=FETCH_TIMEOUT_SECONDS
        ) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, response.read().decode(charset, errors="replace"), None
    except urllib.error.HTTPError as exc:
        # An HTTP error still carries a body, and that body is often the agency's
        # explanation. Keep it: it is what someone will need at 2am.
        body = ""
        with contextlib.suppress(Exception):  # a body we cannot read is not fatal
            body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS, connection reset
        return 0, "", f"{type(exc).__name__}: {exc}"


def _use_database_corridor() -> None:
    """Point the corridor loader at Postgres, if this function was given a database.

    WHY THE COLLECTOR NEEDS A CORRIDOR AT ALL: it does not, for five of six sources.
    A tiled source is the exception - it has no single URL, so the tile addresses
    have to be derived from the centerline BEFORE anything is fetched.

    THE BUG THIS FIXES: when the corridor moved out of the Lambda bundle and into
    Postgres, this handler kept falling back to ``_load_corridor_from_json`` and the
    JSON was no longer there. Every ``aws-location-traffic`` poll failed with "no
    offline corridor found" while the URL-fetched sources carried on, because they
    do not touch geometry until normalization. Needing the corridor at FETCH time is
    a different shape from needing it at PARSE time.

    Left as a no-op when SPATIAL_DB_SECRET_ARN is unset so ``npm run probe`` and the
    tests keep resolving the corridor from JSON with no AWS account, and so a
    deployment without the spatial stack fails loudly at the database rather than
    quietly against a corridor from somewhere else.

    Guarded on ``_corridor_source`` being unset rather than assigned every call,
    because ``set_corridor_source`` clears the per-route cache - re-installing it on
    a warm container would reload an 11,873-vertex centerline on every invocation.
    Same pattern as handlers/query.py.
    """
    if not os.environ.get("SPATIAL_DB_SECRET_ARN"):
        return

    from ..core import lrs

    if lrs._corridor_source is None:
        from ..core import postgis

        lrs.set_corridor_source(postgis.load_corridor)

    # WITHOUT THIS the loader is asked for route "" - `corridor` resolves through
    # active_corridor(), which falls back to "whatever the JSON file declares" and
    # there is no file. The route is configuration, never a literal here.
    route = os.environ.get("CEH_ROUTE")
    if route:
        lrs.set_active_route(route)


def _fetch_tiles(source: dict[str, Any]) -> tuple[int, str, str | None]:
    """Fetch a tiled source and wrap the tiles in one JSON envelope.

    Returns the same ``(status, body, error)`` shape as ``_fetch`` so the rest of
    the handler - raw persistence, health recording, the EventBridge announcement -
    treats this source like any other.

    Reuses ``adapters.feeds.fetch_traffic_tiles`` rather than reimplementing the
    tile walk: two copies of "which tiles cover the corridor" is exactly how the
    deployed pipeline and the local probe end up disagreeing about what a source
    produced, which is the drift this project keeps designing against.

    The credential is the function's own execution role - no key, no secret, so
    nothing here to rotate (ADR 0004). The role needs ``geo-maps:GetTile``.
    """
    from ..adapters.feeds import catalog_entry, fetch_traffic_tiles

    _use_database_corridor()

    zoom = int(source.get("tileZoom") or catalog_entry(source["sourceId"]).get("tileZoom") or 8)
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    try:
        status, body = fetch_traffic_tiles(region, zoom)
    except Exception as exc:  # noqa: BLE001 - same expected outcome as a down feed
        return 0, "", f"{type(exc).__name__}: {exc}"

    if status != 200:
        # An empty envelope is a FAILED fetch, not a clear corridor.
        return status, body, f"no tiles fetched (status {status})"
    return status, body, None


def _build_url(source: dict[str, Any]) -> str:
    """Secrets belong in Secrets Manager, never in the catalog or in code.

    THE SCHEME IS CHECKED HERE, at the one place where a catalog value becomes a URL
    and a credential is appended to it. ``sources.json`` is DATA, so the
    endpoint is editable without a code review, while ``urlopen`` will open
    ``http://`` - putting the key below on the wire in cleartext - and ``file://``
    just as readily. Refusing anything but https is what makes the B310 suppression
    at the fetch call honest, and it fails at build-url time rather than mid-fetch,
    so the error names the source instead of surfacing as a timeout.
    """
    auth_method = source.get("authMethod")
    endpoint = source["endpoint"]
    if urllib.parse.urlsplit(endpoint).scheme != "https":
        raise ValueError(
            f"{source['sourceId']}: endpoint must be https, got {endpoint!r}"
        )

    if auth_method == "api_key_secret":
        # Credential: Secrets Manager only. Never the catalog, never code.
        #
        # THIS IS NOW THE ONLY CREDENTIALED BRANCH. `query_token_public` used to sit
        # above it, resolving a token from the catalog or from the literal in this
        # file, and it existed for exactly one source whose token is published in a
        # federal registry. Both the branch and the literal are gone:
        # one credential path is one place to audit, and the distinction the branch
        # name was drawing - "this one is public, relax" - is a judgment that does not
        # travel with the code to whoever forks it.
        #
        # An adopter with a genuinely public token and no wish to pay for a secret has
        # a simpler option that needs no branch: put it in the `endpoint` and use
        # `authMethod: "none"`. That is honest about what is happening rather than
        # dressing a URL up as a credential mechanism.
        secret_id = source.get("secretId")
        if not secret_id:
            raise ValueError(
                f"{source['sourceId']} has authMethod=api_key_secret but no secretId"
            )
        # The env override is what makes a feed reachable from a laptop with no AWS
        # credentials, and it is per-source rather than global.
        env_key = "TOKEN_" + source["sourceId"].upper().replace("-", "_")
        credential = os.environ.get(env_key) or _get_secret(secret_id)
        param = source.get("authQueryParam") or DEFAULT_AUTH_QUERY_PARAM
        return f"{endpoint}?{param}={urllib.parse.quote(credential, safe='')}"

    return endpoint


def _build_headers(source: dict[str, Any]) -> dict[str, str]:
    """Headers for a feed request.

    A User-Agent is sent on EVERY request, not only where a source demands one.
    Two independent reasons, and the second was found the hard way:

    1. NWS policy requires an identifying User-Agent with contact info; requests
       without one may be blocked outright.

    2. ``urllib`` defaults to ``Python-urllib/3.x``, and Cloudflare-fronted agency
       endpoints reject that outright - oktraffic.org returns 403 with body
       ``error code: 1010`` for it while serving the identical URL to curl. Nothing
       about the URL, the token, or the code is wrong; the default UA alone is
       enough to make an entire state's feed disappear behind a 403 that reads like
       an auth failure.

    Identifying ourselves is also simply the courteous thing to do to an agency
    whose feed we poll every 60 seconds.
    """
    return {
        "Accept": "application/geo+json, application/json",
        "User-Agent": user_agent(),
    }


def user_agent() -> str:
    return os.environ.get(
        "CEH_USER_AGENT", "Corridor Event Hub (contact: set CEH_USER_AGENT)"
    )


def _raw_key(source_id: str, at: datetime, checksum: str) -> str:
    """Partitioned for replay-by-window and Athena scanning."""
    at = at.astimezone(timezone.utc)
    return (
        f"raw/source={source_id}"
        f"/year={at.year:04d}/month={at.month:02d}/day={at.day:02d}/hour={at.hour:02d}"
        f"/{iso_utc(at)}-{checksum[:12]}.json"
    )


def _record_source_health(
    source: dict[str, Any],
    *,
    status: int,
    latency_ms: int,
    num_bytes: int,
    error: str | None,
    at: str,
) -> None:
    healthy = status == 200 and num_bytes > 0
    try:
        table = _dynamodb.Table(os.environ["CATALOG_TABLE"])
        clauses = [
            "lastAttemptAt = :at",
            "lastStatus = :status",
            "lastLatencyMs = :latency",
            "lastBytes = :bytes",
            "lastError = :error",
            "agency = :agency",
            "endpoint = :endpoint",
            "lastSuccessAt = :at" if healthy else "lastFailureAt = :at",
        ]
        table.update_item(
            Key={"sourceId": source["sourceId"]},
            UpdateExpression="SET " + ", ".join(clauses),
            ExpressionAttributeValues={
                ":at": at,
                ":status": status,
                ":latency": latency_ms,
                ":bytes": num_bytes,
                ":error": error,
                ":agency": source["agency"],
                ":endpoint": source["endpoint"],
            },
        )
    except Exception as exc:  # noqa: BLE001 - health recording must never break collection
        print(json.dumps({"msg": "health_record_failed", "error": str(exc)}))
