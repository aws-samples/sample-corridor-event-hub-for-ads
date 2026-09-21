"""Local dev API for the record lifecycle tracker, reading the DEPLOYED stack.

    npm run trace-ui     # this server + the tracker's Vite dev server on :5174
    npm run trace        # just this server, on :8788

WHAT THIS IS, AND HOW IT DIFFERS FROM strip_server. There are two local UIs and
they answer two different questions:

    strip_server  :8787  ->  ui/        WHAT IS ON THE CORRIDOR RIGHT NOW.
                                        Runs the adapters live, one snapshot, no
                                        history - it says so in its own payload
                                        (`historyAvailable: false`).

    trace_server  :8788  ->  ui-trace/  WHAT HAPPENED TO THIS RECORD.
                                        Reads the deployed event store: the
                                        append-only version chain, the audit record
                                        behind every transition, and the S3 payload
                                        that caused each one.

The second one needs the cloud, because the history only exists there. A local
simulation of a lifecycle would be a demo of the state machine, not a view of the
system - and the difference matters most in exactly the situation you would open
this tool for.

READ-ONLY, LOCALHOST-ONLY, UNAUTHENTICATED - in that order of importance. It binds
to 127.0.0.1 and every AWS call underneath it is a read (see core/cloud.py), so the
worst a request can do is spend a few read units. It is NOT a deployment target:
the real query API belongs behind API Gateway with IAM auth, which is already
deployed and whose URL this server reports in `/api/meta`.

snake_case wire format, matching handlers/query.py rather than the strip document,
because these routes are local stand-ins for that API's `/events`,
`/events/{id}` and `/events/{id}/history`. Pointing ui-trace at the deployed API is
then a base-URL change plus SigV4, not a re-model.

Stdlib only, same reasoning as strip_server: nothing here may enter the Lambda
bundle's dependency closure.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import cloud, lrs
from .core.confidence import CONFIDENCE_MODEL_VERSION, INDEPENDENCE_GROUPS
from .core.confidence import WEIGHTS as CONFIDENCE_WEIGHTS
from .core.lifecycle import LIFECYCLE_PROFILES, TRANSITIONS
from .core.matcher import MATCH_MODEL_VERSION, MERGE_THRESHOLD, REVIEW_THRESHOLD
from .core.resolution import RESOLVER_POLICY_VERSION, TTL_LADDER
from .core.serde import to_jsonable
from .core.timeutil import now_iso, now_utc
from .core.trace import STAGES, TRIGGER_STAGE, build_trace, record_summary
from .core.types import EVENT_CLASSES, LIFECYCLE_STATES

DEFAULT_PORT = 8788

#: Minimum age before a listing is re-read from DynamoDB. Unlike strip_server's
#: floor - which exists because AZ511 throttles at 10 requests/60s - this one is
#: about money and latency: listing the corridor is one indexed query per lifecycle
#: state, and a browser polling every few seconds would pay for the same answer
#: dozens of times a minute.
LIST_CACHE_SECONDS = 20.0

#: Traces are cached far more briefly. A trace is what someone stares at while a
#: record is moving, so being 20 seconds stale is worse here than a few extra reads.
TRACE_CACHE_SECONDS = 5.0

#: Cap on how many records one listing returns, and it is REPORTED when it bites.
#: A truncated list that looks complete is the failure this whole codebase is
#: written against.
DEFAULT_LIMIT = 400
MAX_LIMIT = 2000

#: How many of a record's most recent steps one trace reads. Live data has a record
#: with 1,564 versions - one per 60-second poll over a day - so an unbounded read is
#: several megabytes for a view that can render a few hundred rows. The trace document
#: reports the window and the true totals, so a bounded read never looks complete.
DEFAULT_TRACE_WINDOW = 200
MAX_TRACE_WINDOW = 2000


class Cache:
    """Keyed, time-bounded, one in-flight read per key.

    The lock is per key rather than global: a trace being read must not block the
    listing behind it, and two tabs opening the same trace should produce one read.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 64) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._entries: dict[str, tuple[float, Any]] = {}

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str, build: Any, force: bool = False) -> tuple[Any, dict[str, Any]]:
        with self._key_lock(key):
            entry = self._entries.get(key)
            age = None if entry is None else time.monotonic() - entry[0]
            fresh = entry is not None and age is not None and age < self._ttl and not force
            if fresh:
                return entry[1], {
                    "served_from_cache": True,
                    "age_seconds": round(age or 0.0, 1),
                    "ttl_seconds": self._ttl,
                }
            value = build()
            with self._lock:
                if len(self._entries) >= self._max:
                    # Oldest out. A dev tool holding every trace someone clicked
                    # would grow without bound over a long session.
                    oldest = min(self._entries, key=lambda k: self._entries[k][0])
                    self._entries.pop(oldest, None)
                self._entries[key] = (time.monotonic(), value)
            return value, {
                "served_from_cache": False,
                "age_seconds": 0.0,
                "ttl_seconds": self._ttl,
            }


class Api:
    """The reads, separated from the HTTP plumbing so each one is callable directly.

    Deliberate: `python -c "from corridor_event_hub.trace_server import Api; Api().records()"`
    is how you check a change to a read without a browser in the loop.
    """

    def __init__(self) -> None:
        self._lists = Cache(LIST_CACHE_SECONDS)
        self._traces = Cache(TRACE_CACHE_SECONDS)
        self._pipeline = Cache(LIST_CACHE_SECONDS)

    # --- shared -----------------------------------------------------------

    def _route(self) -> str:
        return lrs.active_corridor().route

    def _milepost(self) -> Any:
        """A measure -> ``{state, milepost}`` renderer, or None if unavailable.

        Same degradation as handlers/query.py: a corridor measure is an internal
        coordinate and a state milepost is what an agency recognizes, but its
        absence must cost one convenience field rather than the whole response.
        """
        try:
            corridor = lrs.active_corridor()
        except Exception:  # noqa: BLE001 - no geometry is a degraded mode, not a failure
            return None

        def render(measure: float) -> dict[str, Any] | None:
            resolved = corridor.milepost_for(measure)
            if resolved is None:
                return None
            state, milepost = resolved
            return {"state": state, "milepost": round(milepost, 2)}

        return render

    def _cloud_block(self) -> dict[str, Any]:
        found = cloud.resources()
        return {
            "account": found.account,
            "region": found.region,
            "profile": found.profile,
            "caller_arn": found.caller_arn,
            "event_table": found.event_table,
            "source_catalog_table": found.source_catalog_table,
            "raw_bucket": found.raw_bucket,
            "stack": found.stack,
            "discovered_via": found.discovered_via,
            "query_api_url": found.query_api_url,
            "dashboard_url": found.dashboard_url,
            "lifecycle_state_machine_arn": found.lifecycle_state_machine_arn,
            "scheduled_sources": found.scheduled_sources,
        }

    # --- routes -----------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """Everything the UI needs to render labels and legends, as DATA.

        The stage definitions, the transition table, the TTL ladder and the profiles
        are SERVED rather than duplicated in TypeScript, for the reason
        strip_export.py gives about the same table: a hardcoded copy in the front end
        is a second source of truth that drifts the first time an edge changes, and
        drifts silently because nothing compares the two.
        """
        return {
            "generated_at": now_iso(),
            "cloud": self._cloud_block(),
            "route": self._route(),
            "lifecycle_states": list(LIFECYCLE_STATES),
            "event_classes": list(EVENT_CLASSES),
            "stages": [dict(s) for s in STAGES],
            "trigger_stage": dict(TRIGGER_STAGE),
            "ttl_ladder": dict(TTL_LADDER),
            "transitions": [
                {
                    "from_state": rule.from_state,
                    "to_state": rule.to_state,
                    "triggers": list(rule.triggers),
                    "rationale": rule.rationale,
                }
                for rule in TRANSITIONS
            ],
            "lifecycle_profiles": {
                name: {
                    "ttl_seconds": dict(profile.ttl_seconds),
                    "reopen_window_seconds": profile.reopen_window_seconds,
                    "confidence_half_life_seconds": profile.confidence_half_life_seconds,
                }
                for name, profile in LIFECYCLE_PROFILES.items()
            },
            "independence_groups": dict(INDEPENDENCE_GROUPS),
            "confidence_model": {
                "version": CONFIDENCE_MODEL_VERSION,
                "weights": dict(CONFIDENCE_WEIGHTS),
            },
            "match_model": {
                "version": MATCH_MODEL_VERSION,
                "merge_threshold": MERGE_THRESHOLD,
                "review_threshold": REVIEW_THRESHOLD,
            },
            "resolver_policy_version": RESOLVER_POLICY_VERSION,
        }

    def records(
        self,
        states: list[str] | None = None,
        event_classes: list[str] | None = None,
        source_ids: list[str] | None = None,
        query: str | None = None,
        limit: int = DEFAULT_LIMIT,
        force: bool = False,
    ) -> dict[str, Any]:
        """The record list: one row per current record, newest activity first.

        The cache key is the state set and the limit ONLY. Class, source and text
        filters are applied after the read because they narrow a result already in
        memory, and making them part of the key would mean a fresh set of DynamoDB
        queries every time someone types a character into the search box.
        """
        route = self._route()
        wanted = states or ["reported", "validated", "active", "clearing"]
        limit = max(1, min(limit, MAX_LIMIT))
        key = f"{route}|{','.join(sorted(wanted))}|{limit}"

        def build() -> dict[str, Any]:
            events, fetched, truncated = cloud.list_current(route, wanted, limit=limit)
            now = now_utc()
            milepost = self._milepost()
            return {
                "fetched_at": now_iso(),
                "records": [record_summary(e, now, milepost=milepost) for e in events],
                "fetched_counts": fetched,
                "truncated": truncated,
            }

        payload, cache_meta = self._lists.get(key, build, force=force)
        rows = payload["records"]

        if event_classes:
            rows = [r for r in rows if r["event_class"] in set(event_classes)]
        if source_ids:
            wanted_sources = set(source_ids)
            rows = [r for r in rows if wanted_sources.intersection(r["source_ids"])]
        if query:
            needle = query.strip().lower()
            rows = [r for r in rows if _matches(r, needle)]

        # Most recently touched first: the tracker is opened to see what is moving.
        rows = sorted(rows, key=lambda r: r["updated_at"] or "", reverse=True)

        return {
            "generated_at": payload["fetched_at"],
            "route": route,
            "query": {
                "lifecycle_state": wanted,
                "event_class": event_classes or None,
                "source_id": source_ids or None,
                "q": query or None,
                "limit": limit,
            },
            "count": len(rows),
            "fetched_count": len(payload["records"]),
            "fetched_counts_by_state": payload["fetched_counts"],
            "truncated": payload["truncated"],
            "truncation_note": (
                f"the listing stopped at {limit} records, so records beyond it are NOT shown - "
                f"narrow the state filter or raise limit"
                if payload["truncated"]
                else None
            ),
            "records": rows,
            "cache": cache_meta,
        }

    def trace(
        self, event_id: str, window: int = DEFAULT_TRACE_WINDOW, force: bool = False
    ) -> dict[str, Any]:
        """One record's whole life. The reason this tool exists.

        Related events are resolved to summaries rather than left as bare ids: a
        merge is only explicable if you can see what it merged into, and an id
        alone makes the reader do a second lookup by hand.

        ``window`` bounds the read at the most recent N steps, because live data
        contains records with over 1,500 versions. The document says when it is
        windowed and what the true totals are.
        """
        window = max(1, min(window, MAX_TRACE_WINDOW))

        def build() -> dict[str, Any]:
            current = cloud.current_event(event_id)
            if current is None:
                return {"error": "not_found", "event_id": event_id}
            history = cloud.history_window(event_id, window=window)
            now = now_utc()
            milepost = self._milepost()
            document = build_trace(
                current,
                history.versions,
                history.audit,
                now,
                milepost=milepost,
                totals=history.totals(),
            )
            related = []
            for other_id in current.related_event_ids[:20]:
                other = cloud.current_event(other_id)
                related.append(
                    record_summary(other, now, milepost=milepost)
                    if other is not None
                    else {"event_id": other_id, "missing": True}
                )
            document["related"] = related
            document["generated_at"] = now_iso()
            document["cloud"] = self._cloud_block()
            return document

        payload, cache_meta = self._traces.get(f"{event_id}|{window}", build, force=force)
        if payload.get("error") == "not_found":
            return payload
        return {**payload, "cache": cache_meta}

    def lookup(self, source_id: str, native_id: str) -> dict[str, Any]:
        """Agency record id -> our event id (the idempotency pointer, used here for
        navigation).

        The question someone arrives with: they are looking at a record on a state
        511 site and want to know what this system did with it.
        """
        event_id = cloud.event_id_for_source_record(source_id, native_id)
        return {
            "source_id": source_id,
            "native_id": native_id,
            "event_id": event_id,
            "found": event_id is not None,
            "note": None
            if event_id
            else (
                "no pointer for that source record. Either it was never ingested, or the "
                "source_id/native_id pair does not match what the adapter recorded - the "
                "native id is the AGENCY's own id, exactly as it appears in the feed."
            ),
        }

    def raw(self, ref: str, native_id: str | None = None) -> dict[str, Any]:
        return cloud.read_raw(ref, native_id=native_id)

    def pipeline(self, force: bool = False) -> dict[str, Any]:
        """The pipeline around the records: is anything arriving, is anything stuck.

        On the same screen as the records on purpose. An empty record list means
        something completely different depending on whether the feeds are being
        fetched and the dead-letter queues are empty.
        """

        def build() -> dict[str, Any]:
            return {
                "generated_at": now_iso(),
                "cloud": self._cloud_block(),
                "route": self._route(),
                "state_counts": cloud.state_counts(self._route()),
                "sources": cloud.source_ingest_status(),
                "dlqs": cloud.dlq_depths(),
                "timers": cloud.timer_health(),
            }

        payload, cache_meta = self._pipeline.get("pipeline", build, force=force)
        return {**payload, "cache": cache_meta}


def _matches(row: dict[str, Any], needle: str) -> bool:
    """Free-text match over the fields someone would actually paste in.

    Event id, agency record id, class, subtype, agency, state - the last of which is
    why this is a substring match rather than a prefix one: "TX" should find records
    on the Texas segment.
    """
    haystack = [
        row["event_id"],
        row["event_class"],
        row["event_subtype"],
        row["lifecycle_state"],
        *row["native_ids"],
        *row["agencies"],
        *row["source_ids"],
        *row["states"],
    ]
    return any(needle in str(value).lower() for value in haystack)


def _handler_factory(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CorridorEventHubTraceDev/0.1"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(to_jsonable(payload), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _params(self, query: str) -> dict[str, str]:
            return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            path, _, raw_query = self.path.partition("?")
            params = self._params(raw_query)
            force = params.get("refresh") == "1" or params.get("force") == "1"

            try:
                if path == "/api/health":
                    self._send_json(200, {"ok": True})
                    return
                if path == "/api/meta":
                    self._send_json(200, api.meta())
                    return
                if path == "/api/records":
                    self._send_json(
                        200,
                        api.records(
                            states=_csv(params.get("state")),
                            event_classes=_csv(params.get("event_class")),
                            source_ids=_csv(params.get("source_id")),
                            query=params.get("q"),
                            limit=int(params.get("limit") or DEFAULT_LIMIT),
                            force=force,
                        ),
                    )
                    return
                if path.startswith("/api/records/"):
                    event_id = urllib.parse.unquote(path[len("/api/records/") :]).strip("/")
                    if not event_id:
                        self._send_json(400, {"error": "no event id in path"})
                        return
                    document = api.trace(
                        event_id,
                        window=int(params.get("window") or DEFAULT_TRACE_WINDOW),
                        force=force,
                    )
                    self._send_json(404 if document.get("error") else 200, document)
                    return
                if path == "/api/lookup":
                    source_id = params.get("source_id") or ""
                    native_id = params.get("native_id") or ""
                    if not source_id or not native_id:
                        self._send_json(400, {"error": "both source_id and native_id are required"})
                        return
                    self._send_json(200, api.lookup(source_id, native_id))
                    return
                if path == "/api/raw":
                    ref = params.get("ref") or ""
                    if not ref:
                        self._send_json(400, {"error": "ref is required (an s3:// pointer)"})
                        return
                    self._send_json(200, api.raw(ref, native_id=params.get("native_id")))
                    return
                if path == "/api/pipeline":
                    self._send_json(200, api.pipeline(force=force))
                    return

                self._send_json(
                    404,
                    {
                        "error": "not found",
                        "available": [
                            "/api/meta",
                            "/api/records",
                            "/api/records/{eventId}",
                            "/api/lookup?source_id=&native_id=",
                            "/api/raw?ref=&native_id=",
                            "/api/pipeline",
                            "/api/health",
                        ],
                    },
                )
            except cloud.CloudUnavailable as exc:
                # 503 rather than 500: the code is fine, the cloud is not reachable,
                # and the message carries the fix. The UI renders it verbatim - a
                # generic "failed to fetch" would send someone debugging the browser.
                self._send_json(503, {"error": "cloud_unavailable", "detail": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": "bad_request", "detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 - report, do not kill the server
                traceback.print_exc()
                self._send_json(
                    500,
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "hint": "see the server log for the traceback",
                    },
                )

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[trace-api] {fmt % args}\n")

    return Handler


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return parts or None


def serve(port: int) -> int:
    api = Api()

    # Discover UP FRONT rather than on the first request. The whole tool depends on
    # reaching one account, and "which account am I about to read" is the first thing
    # an operator should see - not something they infer from the data looking wrong.
    try:
        found = cloud.resources()
        print(
            f"reading account {found.account} in {found.region}"
            f" (profile {found.profile or '<default>'}, via {found.discovered_via})"
        )
        print(f"  event store : {found.event_table}")
        print(f"  raw zone    : {found.raw_bucket or '<not found>'}")
    except cloud.CloudUnavailable as exc:
        # Not fatal: the server still starts and every request returns this message,
        # so the UI can display it. Exiting here would make `npm run trace-ui` fail
        # with a dead front end and the reason scrolled off in another process.
        print(f"WARNING  cannot reach the deployed stack yet:\n  {exc}", file=sys.stderr)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(api))
    print(f"trace API on http://127.0.0.1:{port}/api/records  [READ-ONLY]")
    print(f"  listings cached {LIST_CACHE_SECONDS:.0f}s, traces {TRACE_CACHE_SECONDS:.0f}s")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local read-only API over the deployed event store, for the lifecycle tracker."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    return serve(port=args.port)


if __name__ == "__main__":
    sys.exit(main())
