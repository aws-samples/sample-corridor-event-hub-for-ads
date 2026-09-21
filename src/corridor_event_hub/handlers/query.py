"""Query API.

Behind API Gateway, one function:

    GET /health
    GET /events                    filtered, with full provenance
    GET /events/{eventId}          one event and WHY it looks like that: audit
                                   trail, field provenance, retained alternates,
                                   merge explanation
    GET /events/{eventId}/history  every version and transition, `as_of`
    GET /ahead                     what is ahead of me
    GET /review                    the ambiguous-match queue
    GET /wzdx                      WZDx v4.2 feed - NOT enveloped, see
                                   wzdx_feed() on why it is the one exception

snake_case, NOT the camelCase of the strip document. Deliberate, and the reason is
in core/serde.py: the canonical field names are snake_case,
so a wire format that matches them field for field means an adopter can read the
model and grep the payload. ``strip_export.py`` translates to camelCase
because its consumer is a browser; this API's consumer is an integrator, and for
them the canonical names are the feature.

MOST OF THIS FILE IS FILTERING, AND ALMOST NONE OF IT IS SPATIAL. Look-ahead is a
measure comparison; a corridor window is a range scan on a GSI sort key; dedup
already happened upstream. The one genuinely 2D input is a bbox, and even that is
answered by asking the corridor which of its vertices the box contains and turning
that into a measure range - which is why this function does not need a spatial
database and only loads corridor geometry when a bbox or a lat/lon actually arrives.
That is the claim of ADR 0002 § The split, and why,
on the serve side.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ..core import lrs
from ..core.confidence import CONFIDENCE_MODEL_VERSION, explain_confidence
from ..core.confidence import WEIGHTS as CONFIDENCE_WEIGHTS
from ..core.eventstore import LIVE_STATES, DynamoEventStore, EventStore
from ..core.lifecycle import profile_for
from ..core.matcher import MATCH_MODEL_VERSION, MERGE_THRESHOLD, REVIEW_THRESHOLD
from ..core.resolution import RESOLVER_POLICY_VERSION, is_stale, ttl_expires_at
from ..core.serde import to_jsonable
from ..core.timeutil import iso_utc, now_utc, parse_iso
from ..core.types import Event
from ..core.wzdx import PUBLISHABLE_STATES, to_wzdx_feed

_store: EventStore | None = None

#: Result cap. Declared rather than implicit, and REPORTED in the response when it
#: bites: a query that silently returned the first 200 of 900 events would read as
#: "that is all there is", and a consumer deciding what is ahead of it on the road
#: cannot tell the difference between an empty corridor and a truncated answer.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

#: Default look-ahead distance. 50 miles is roughly 45 minutes at highway speed -
#: long enough to matter to a routing decision, short enough that the answer is
#: still true when it arrives.
DEFAULT_LOOK_AHEAD_MILES = 50.0

#: The advisory notice travels in every response rather than living only in a
#: document. A field in the payload is the only place a downstream integrator cannot
#: fail to see it, and it has to be stated plainly.
ADVISORY_NOTICE = (
    "Corridor Event Hub is advisory decision-support. Records are corroborated, scored, and "
    "explained, but are NOT validated for safety-critical control. Confidence is a "
    "published breakdown, not a guarantee - see the confidence_model block."
)


def store() -> EventStore:
    global _store
    if _store is None:
        _store = DynamoEventStore()
    return _store


def _route() -> str:
    """Which corridor this function serves.

    Same contract as the normalizer's: configuration, not a literal, because a
    route name in corridor_event_hub/handlers is a portability break that
    scripts/check-portability.sh fails on.
    """
    configured = os.environ.get("CEH_ROUTE")
    if configured:
        return configured
    return lrs.active_corridor().route


def _corridor(route: str) -> lrs.CorridorConfig:
    """Corridor geometry, loaded ONLY when a request actually needs it.

    Lazily and behind a branch, because most requests do not. A measure-based
    look-ahead, a class filter, an event by id - none of them need geometry, and
    loading an 11,873-vertex centerline from Postgres to answer them would put a
    database round trip in front of the latency budget for no reason. A bbox
    or a lat/lon is the only thing that needs it.

    The presence of SPATIAL_DB_SECRET_ARN chooses the source, matching
    handlers/normalizer.py: one variable, set by CDK when it grants the database,
    rather than a flag that can disagree with reality.
    """
    if os.environ.get("SPATIAL_DB_SECRET_ARN") and lrs._corridor_source is None:
        from ..core import postgis

        lrs.set_corridor_source(postgis.load_corridor)
    return lrs.corridor_for(route)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _milepost(measure: float, corridor: lrs.CorridorConfig | None) -> dict[str, Any] | None:
    """Corridor measure rendered back into a state's own reference.

    Included because a corridor measure is an internal coordinate and a state
    milepost is what an agency, a dispatcher, and a driver all recognize. Omitting
    it would make every consumer reimplement the offset table.
    """
    if math.isnan(measure):
        return None
    resolved = corridor.milepost_for(measure) if corridor else None
    if resolved is None:
        return None
    state, milepost = resolved
    return {"state": state, "milepost": round(milepost, 2)}


def _lifecycle_block(event: Event, now: Any) -> dict[str, Any]:
    """Where this event is in its life, and how much of that to trust.

    ``ttl_expired`` is a RACE INDICATOR, not a second state machine. The lifecycle
    timers (handlers/lifecycle.py) move a stale event down the ladder, but a tick
    fires on a schedule and a read can land in the gap between expiry and the tick
    that acts on it. This flag closes that gap for the consumer: an event reported
    as `active` with `ttl_expired: true` has passed its deadline and has not yet
    been moved.

    A PERSISTENTLY expired event is a finding, not a race - it means that event's
    timer chain died, which is what the CorridorEventHub-lifecycle-executions-failed alarm
    watches for.
    """
    profile = profile_for(event.event_class)
    expired = is_stale(event, now)
    return {
        "state": event.lifecycle_state,
        "entered_at": event.updated_at,
        "ttl_seconds": profile.ttl_seconds.get(event.lifecycle_state),
        "ttl_expires_at": ttl_expires_at(event),
        "ttl_expired": expired,
        "ttl_expired_is_advisory": True,
        "reopen_window_seconds": profile.reopen_window_seconds,
        "confidence_half_life_seconds": profile.confidence_half_life_seconds,
    }


def _event_out(
    event: Event, now: Any, corridor: lrs.CorridorConfig | None = None
) -> dict[str, Any]:
    """One event as the API returns it: the canonical record plus what explains it."""
    out = to_jsonable(event)
    out["milepost_begin"] = _milepost(event.extent.begin_measure, corridor)
    out["milepost_end"] = _milepost(event.extent.end_measure, corridor)
    out["lifecycle"] = _lifecycle_block(event, now)
    # The score never travels without the breakdown that produced
    # it. This is the API-side half of the promise core/confidence.py makes.
    out["confidence"]["explanation"] = explain_confidence(event.confidence)
    out["agencies"] = sorted({s.agency for s in event.sources})
    # Two feeds in one independence group are not two witnesses. A consumer
    # counting agencies would otherwise read mirrored feeds as corroboration.
    out["independent_source_count"] = len(
        {_independence_group(s.source_id) for s in event.sources}
    )
    return out


def _independence_group(source_id: str) -> str:
    from ..core.confidence import INDEPENDENCE_GROUPS

    return INDEPENDENCE_GROUPS.get(source_id, source_id)


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """What every response carries regardless of route.

    The model versions are here because an integrator needs them: a consumer that
    has calibrated a trust threshold against one scoring model has to be able to
    notice when the model changed, and a score whose model is unnamed
    cannot support that.
    """
    return {
        "advisory": ADVISORY_NOTICE,
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
        **payload,
    }


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _csv(params: dict[str, str], name: str) -> list[str]:
    raw = params.get(name)
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


def _number(params: dict[str, str], name: str) -> float | None:
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise BadRequest(f"{name} must be a number, got {raw!r}") from exc


def _limit(params: dict[str, str]) -> int:
    """The result cap, with zero treated as a request rather than as absence.

    NOT `_number(...) or DEFAULT_LIMIT`. Zero is falsy, so that form silently reads
    `limit=0` as "no limit given" and returns 200 events to a caller who asked for
    none - a filter ignored without complaint, which is the failure mode this whole
    module is written against.
    """
    raw = _number(params, "limit")
    if raw is None:
        return DEFAULT_LIMIT
    if raw < 1:
        raise BadRequest(f"limit must be at least 1, got {raw:g}")
    return min(int(raw), MAX_LIMIT)


def _bbox_to_measures(params: dict[str, str], route: str) -> tuple[float, float] | None:
    """A bbox -> a corridor measure range.

    THE ONLY 2D OPERATION ON THE SERVE PATH, and it is answered by intersecting the
    box with the corridor's own vertices rather than with the events: an event is
    inside the box if it is on the stretch of corridor the box contains. That keeps
    the query numeric after this line, and it means the answer is expressed in the
    same units as everything else.

    The resolution floor is the centerline's vertex spacing. For a box smaller than
    that spacing the range collapses to the nearest vertices, which over-selects
    slightly. Over-selecting is the right direction to be wrong for a query about
    hazards ahead of a vehicle.
    """
    raw = params.get("bbox")
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise BadRequest("bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise BadRequest(f"bbox has a non-numeric value: {raw!r}") from exc

    corridor = _corridor(route)
    measures = corridor.measures
    hits = [
        (measures[index] if measures else None)
        for index, (lon, lat) in enumerate(corridor.centerline)
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
    ]
    inside = [m for m in hits if m is not None]
    if not hits:
        # The box does not touch the corridor at all. An empty measure range, which
        # correctly returns nothing - as opposed to no filter, which would return
        # everything and look like the box had been ignored.
        return (math.inf, -math.inf)
    if not inside:
        raise BadRequest(
            "bbox filtering needs a calibrated corridor (centerline measures). "
            "Query by begin_measure/end_measure instead, or rebuild the corridor."
        )
    return (min(inside), max(inside))


def _overlaps_window(event: Event, start: Any, end: Any) -> bool:
    """A time window, on the same open-ended-means-still-running rule the
    matcher uses. An event with no end time has not ended.
    """
    event_start = parse_iso(event.start_time)
    event_end = parse_iso(event.end_time) if event.end_time else None
    if event_start is None:
        return False
    if end is not None and event_start > end:
        return False
    return not (start is not None and event_end is not None and event_end < start)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class BadRequest(Exception):
    """A client error: reported as 400 with the reason, never as an empty 200.

    An unparseable filter returning "no events" is the worst available answer -
    indistinguishable from a clear road.
    """


class NotFound(Exception):
    pass


def list_events(params: dict[str, str]) -> dict[str, Any]:
    """Events by bbox / LRS range / class / state / lifecycle state /
    min-confidence / time window, with full provenance included.
    """
    now = now_utc()
    route = params.get("route") or _route()
    lifecycle_states = _csv(params, "lifecycle_state") or list(LIVE_STATES)
    classes = set(_csv(params, "event_class"))
    states = {s.upper() for s in _csv(params, "state")}
    min_confidence = _number(params, "min_confidence")
    limit = _limit(params)

    begin = _number(params, "begin_measure")
    end = _number(params, "end_measure")
    bbox_range = _bbox_to_measures(params, route)
    if bbox_range is not None:
        # Both given: the intersection, because two filters should narrow rather
        # than one silently winning.
        begin = bbox_range[0] if begin is None else max(begin, bbox_range[0])
        end = bbox_range[1] if end is None else min(end, bbox_range[1])

    window_start = parse_iso(params.get("start"))
    window_end = parse_iso(params.get("end"))
    if params.get("start") and window_start is None:
        raise BadRequest(f"start is not a parseable timestamp: {params['start']!r}")
    if params.get("end") and window_end is None:
        raise BadRequest(f"end is not a parseable timestamp: {params['end']!r}")

    corridor = _safe_corridor(route)
    lo = begin if begin is not None else 0.0
    hi = end if end is not None else (corridor.total_miles if corridor else math.inf)
    if hi < lo:
        matched: list[Event] = []
    else:
        matched = store().events_overlapping(route, lo, hi, states=lifecycle_states)

    # The remaining predicates run HERE rather than as DynamoDB filter expressions.
    # Deliberate at corridor volume: a filter expression is applied after the read
    # is metered, so it saves bandwidth and not cost, and doing it in process keeps
    # the filter semantics in one readable place instead of split across an
    # expression language. Revisit if a corridor ever holds tens of thousands of
    # live events.
    filtered = [
        event
        for event in matched
        if (not classes or event.event_class in classes)
        and (not states or states.intersection({s.upper() for s in event.extent.states}))
        and (min_confidence is None or event.confidence.value >= min_confidence)
        and _overlaps_window(event, window_start, window_end)
    ]

    truncated = len(filtered) > limit
    return _envelope(
        {
            "query": {
                "route": route,
                "lifecycle_state": lifecycle_states,
                "event_class": sorted(classes) or None,
                "state": sorted(states) or None,
                "begin_measure": begin,
                "end_measure": end,
                "bbox_measure_range": list(bbox_range) if bbox_range else None,
                "min_confidence": min_confidence,
                "start": params.get("start"),
                "end": params.get("end"),
                "limit": limit,
            },
            "count": min(len(filtered), limit),
            "matched_before_limit": len(filtered),
            "truncated": truncated,
            "events": [_event_out(e, now, corridor) for e in filtered[:limit]],
        }
    )


def _safe_corridor(route: str) -> lrs.CorridorConfig | None:
    """The corridor if it is available, None if it is not.

    Milepost rendering is a nicety and the corridor may be unreachable - the
    database not deployed, the route not loaded. Degrading to measures-only is far
    better than a 500 on every query, so the failure is swallowed HERE, where the
    consequence is one missing convenience field, and nowhere else.
    """
    try:
        return _corridor(route)
    except Exception as exc:  # noqa: BLE001 - a missing corridor must not fail a query
        print(json.dumps({"msg": "corridor_unavailable", "route": route, "error": str(exc)}))
        return None


def get_event(event_id: str) -> dict[str, Any]:
    """One event, with everything that explains it."""
    now = now_utc()
    events = store()
    event = events.get_current(event_id)
    if event is None:
        raise NotFound(f"no event {event_id}")

    history = events.history(event_id)
    corridor = _safe_corridor(event.extent.route)
    out = _event_out(event, now, corridor)

    dedup_audit = [a for a in history.audit if a.trigger == "dedup_decision"]
    out["explanation"] = {
        # Which source won each field, and what the losers said. Both halves
        # are required - "source A won" without A's and B's values is not an
        # explanation, it is an assertion.
        "field_provenance": event.extensions.get("field_provenance") or {},
        "alternates": event.extensions.get("alternates") or {},
        "merge_decisions": [
            {
                "sequence": a.sequence,
                "from_state": a.from_state,
                "to_state": a.to_state,
                "reason": a.reason,
                "recorded_at": a.recorded_at,
                "rule_version": a.rule_version,
            }
            for a in dedup_audit
        ],
        "related_event_ids": event.related_event_ids,
        "confidence": explain_confidence(event.confidence),
    }
    out["audit"] = [to_jsonable(a) for a in history.audit]
    out["version_count"] = len(history.versions)
    return _envelope({"event": out})


def get_history(event_id: str, params: dict[str, str]) -> dict[str, Any]:
    """The full state history, reconstructable for any past instant.

    ``as_of`` is what makes this bitemporal rather than merely a version list.
    Passing a system time returns the event AS IT WAS BELIEVED TO BE then - which
    is the question an incident review actually asks, and one an overwrite-in-place
    store cannot answer at all.

    TIES RESOLVE TO THE LAST VERSION AT THAT INSTANT, and the reason is worth
    knowing before reading the output. One resolver invocation can write several
    versions - `reported`, `validated`, `active` is three - and they all carry the
    same ``recorded_at``, because they genuinely happened in the same instant; what
    orders them is the sequence number, not the clock. So "as of the moment this was
    first reported" returns the state at the END of that moment, which was `active`.
    Inventing sub-millisecond offsets to separate them would make the timestamps
    look more precise than the events were.
    """
    events = store()
    history = events.history(event_id)
    if not history.versions and events.get_current(event_id) is None:
        raise NotFound(f"no event {event_id}")

    as_of_raw = params.get("as_of")
    as_of = parse_iso(as_of_raw)
    if as_of_raw and as_of is None:
        raise BadRequest(f"as_of is not a parseable timestamp: {as_of_raw!r}")

    versions = history.versions
    audit = history.audit
    as_of_version = None
    if as_of is not None:
        # System time, not event time: `recorded_at` is when we wrote it down, and
        # "what did we believe at 14:00" is a question about our records.
        visible = [
            v
            for v, a in zip(versions, audit)
            if (parse_iso(a.recorded_at) or parse_iso(v.updated_at)) is not None
            and (parse_iso(a.recorded_at) or parse_iso(v.updated_at)) <= as_of
        ]
        as_of_version = to_jsonable(visible[-1]) if visible else None
        versions = visible
        audit = [a for a in audit if (parse_iso(a.recorded_at) or as_of) <= as_of]

    return _envelope(
        {
            "event_id": event_id,
            "as_of": as_of_raw,
            "as_of_version": as_of_version,
            "version_count": len(versions),
            "versions": [to_jsonable(v) for v in versions],
            "audit": [to_jsonable(a) for a in audit],
        }
    )


def look_ahead(params: dict[str, str]) -> dict[str, Any]:
    """Given position, heading, and a look-ahead distance, events ordered
    by distance with lane-level detail.

    THE CONSUMER IS AN AUTOMATED TRUCK, and two things follow from that:

      Ordered by distance, not by severity or confidence. The vehicle needs the
      next thing first; ranking by anything else makes the caller re-sort.

      `distance_miles` is signed from the vehicle's own position and direction of
      travel, so a westbound query gets decreasing measures as increasing
      distances without the caller having to know which way measures run.
    """
    now = now_utc()
    route = params.get("route") or _route()
    direction = (params.get("direction") or "").upper()
    heading = _number(params, "heading")
    if not direction and heading is not None:
        # Heading -> travel direction. The corridor's direction vocabulary is
        # EB/WB (types.DIRECTIONS), so an eastward heading component means EB.
        # `direction` is the unambiguous parameter and wins when both are given.
        direction = "EB" if 0.0 <= heading % 360.0 < 180.0 else "WB"
    if direction not in ("EB", "WB", "BOTH"):
        raise BadRequest("direction must be EB, WB or BOTH (or pass heading in degrees)")

    look_ahead_miles = _number(params, "distance") or DEFAULT_LOOK_AHEAD_MILES
    if look_ahead_miles <= 0:
        raise BadRequest("distance must be positive")

    position = _number(params, "position")
    corridor = _safe_corridor(route)
    if position is None:
        lat = _number(params, "lat")
        lon = _number(params, "lon")
        if lat is None or lon is None:
            raise BadRequest("pass position (corridor measure) or both lat and lon")
        if corridor is None:
            raise BadRequest(
                "lat/lon look-ahead needs corridor geometry, which is unavailable. "
                "Pass position as a corridor measure instead."
            )
        conflated = lrs.LocalConflator(corridor=corridor).conflate(
            lrs.CoordinateInput(lon=lon, lat=lat)
        )
        if math.isnan(conflated.begin_measure):
            raise BadRequest(f"({lat}, {lon}) does not conflate onto {route}")
        position = conflated.begin_measure

    # Read a window rather than the corridor: the answer can only contain events
    # within the look-ahead distance, so that is what to pay for.
    lo = position - look_ahead_miles if direction != "EB" else position
    hi = position + look_ahead_miles if direction != "WB" else position
    nearby = store().events_overlapping(route, lo, hi, states=LIVE_STATES)

    min_confidence = _number(params, "min_confidence")
    ahead = []
    for event in nearby:
        if not lrs.is_ahead_of(
            (event.extent.begin_measure, event.extent.end_measure, event.extent.direction),
            (position, direction),
            look_ahead_miles,
        ):
            continue
        if min_confidence is not None and event.confidence.value < min_confidence:
            continue
        near_edge = (
            min(event.extent.begin_measure, event.extent.end_measure)
            if direction != "WB"
            else max(event.extent.begin_measure, event.extent.end_measure)
        )
        distance = abs(near_edge - position)
        rendered = _event_out(event, now, corridor)
        rendered["distance_miles"] = round(distance, 2)
        # No field whose meaning depends on reading prose. The lane detail
        # is already structured on the event; this is the summary a vehicle acts on
        # without interpreting anything.
        rendered["lane_summary"] = {
            "lanes_described": len(event.lane_impacts),
            "general_lanes_closed": sum(
                1
                for lane in event.lane_impacts
                if lane.type == "general" and lane.status == "closed"
            ),
            "any_inferred": any(lane.inferred for lane in event.lane_impacts),
        }
        ahead.append((distance, rendered))

    ahead.sort(key=lambda pair: pair[0])
    return _envelope(
        {
            "query": {
                "route": route,
                "position": position,
                "position_milepost": _milepost(position, corridor),
                "direction": direction,
                "heading": heading,
                "distance": look_ahead_miles,
                "min_confidence": min_confidence,
            },
            "count": len(ahead),
            "events": [rendered for _distance, rendered in ahead],
        }
    )


def review_queue(params: dict[str, str]) -> dict[str, Any]:
    """The ambiguous pairs, with both events still published.

    Worth being explicit about what this queue IS: not a list of errors, but of
    decisions the matcher declined to make. A visible queue is an honest answer to
    an ambiguous score; a forced merge is not.
    """
    reviews = store().open_reviews(_limit(params))
    return _envelope(
        {
            "count": len(reviews),
            "band": {"review_at_or_above": REVIEW_THRESHOLD, "merge_at_or_above": MERGE_THRESHOLD},
            "reviews": [to_jsonable(r) for r in reviews],
        }
    )


def wzdx_feed(params: dict[str, str]) -> dict[str, Any]:
    """Spec-conformant work-zone GeoJSON at a stable URL.

    NOT wrapped in ``_envelope``. Every other route here returns a Corridor Event Hub
    document with an advisory notice and model versions attached; this one returns a
    WZDx v4.2 feed and nothing else, because a consumer pointing a conformance
    validator at this URL must get a document the spec recognizes. Our own metadata
    would be an unrecognized top-level key - see `feed_info.publisher` and the
    per-feature description for where the Corridor Event Hub provenance actually goes.

    BUILT LIVE FROM THE STORE rather than served from a cached artifact. At corridor
    volume the projection is cheap, and a cache would introduce the one failure this
    feed cannot afford: serving a work zone that has already cleared, with a stale
    `update_date` claiming otherwise.
    """
    route = params.get("route") or _route()
    corridor = _safe_corridor(route)
    # Work zones only, and only while they still affect traffic - the projection
    # filters again, but reading only what can be published keeps the query narrow.
    events = store().events_overlapping(
        route,
        0.0,
        corridor.total_miles if corridor else math.inf,
        states=PUBLISHABLE_STATES,
    )
    projection = to_wzdx_feed(
        events,
        publisher=os.environ.get("WZDX_PUBLISHER", "Corridor Event Hub for ADS"),
        update_date=iso_utc(now_utc()),
        corridor=corridor,
        contact_name=os.environ.get("WZDX_CONTACT_NAME") or None,
        contact_email=os.environ.get("WZDX_CONTACT_EMAIL") or None,
        license_url=os.environ.get("WZDX_LICENSE") or None,
        update_frequency_seconds=int(os.environ.get("WZDX_UPDATE_FREQUENCY", "300")),
    )

    # The record-rather-than-drop rule on the output side: an event that should
    # have been published and could not be is reported, not silently absent. It cannot go in
    # the feed without breaking conformance, so it goes in the log.
    if projection.excluded:
        print(
            json.dumps(
                {
                    "msg": "wzdx_excluded",
                    "count": len(projection.excluded),
                    "eventIds": [e.event_id for e in projection.excluded[:20]],
                    "reasons": sorted({e.reason for e in projection.excluded}),
                }
            )
        )
    return projection.feed


# ---------------------------------------------------------------------------
# API Gateway plumbing
# ---------------------------------------------------------------------------


def route_request(method: str, path: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Method + path + query -> (status, body).

    Separated from the Lambda envelope so the whole API is callable from a test,
    and from a local dev server, without constructing an API Gateway event. Same
    reasoning as core/resolution.py being pure: an HTTP shape is not a good place
    to keep logic.
    """
    if method != "GET":
        return 405, {"error": f"{method} not allowed; this API is read-only"}

    trimmed = "/" + path.strip("/")
    try:
        if trimmed in ("/", "/health"):
            return 200, {"status": "ok", "route": _route()}
        if trimmed == "/events":
            return 200, list_events(params)
        if trimmed == "/ahead":
            return 200, look_ahead(params)
        if trimmed == "/review":
            return 200, review_queue(params)
        if trimmed == "/wzdx":
            return 200, wzdx_feed(params)
        if trimmed.startswith("/events/"):
            rest = trimmed[len("/events/") :]
            if rest.endswith("/history"):
                return 200, get_history(rest[: -len("/history")], params)
            if "/" not in rest:
                return 200, get_event(rest)
        return 404, {"error": f"no route for {trimmed}"}
    except BadRequest as exc:
        return 400, {"error": str(exc)}
    except NotFound as exc:
        return 404, {"error": str(exc)}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """API Gateway HTTP API (payload format 2.0)."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    path = http.get("path") or event.get("rawPath") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1 :]
    params = {k: v for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}

    status, body = route_request(method, path, params)

    print(
        json.dumps(
            {
                "msg": "queried",
                "path": path,
                "status": status,
                # A WZDx feed has `features` rather than `count`; reporting whichever
                # exists keeps one log shape for every route.
                "count": body.get("count", len(body.get("features", []) or []) or None),
                "truncated": body.get("truncated"),
            }
        )
    )
    return {
        "statusCode": status,
        "headers": {
            # The WZDx feed is GeoJSON and says so, because a consumer content-type
            # sniffing for a standards feed should not have to guess.
            "content-type": (
                "application/geo+json"
                if status == 200 and body.get("type") == "FeatureCollection"
                else "application/json"
            ),
            # A consumer must be able to tell a cached answer from a live one when
            # the answer is about a hazard on the road.
            "cache-control": "no-store",
        },
        # allow_nan=False for the same reason core/serde.dumps has it: an unresolved
        # measure must have become null upstream, not a bare NaN token that no
        # strict JSON parser will read.
        "body": json.dumps(to_jsonable(body), allow_nan=False),
    }
