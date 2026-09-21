"""Normalizer - run the right adapter over a stored raw payload.

Reads bytes from S3 (never re-fetches the source: that is what makes replay work),
selects the adapter by ``sourceId``, and emits candidate events plus
whatever could not be mapped.

This function scores nothing and decides no lifecycle state. It hands
candidates to the resolver. Keeping that line clean is what keeps the architecture
portable - see README.md § Not built yet.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from ..adapters.adapter import AdapterContext
from ..adapters.registry import adapter_for
from ..core.awsclients import client as aws_client
from ..core.confidence import ScoringInput, score_confidence
from ..core.lrs import Conflator, LocalConflator, set_active_route, set_corridor_source
from ..core.serde import to_jsonable

_s3 = aws_client("s3")
_events = aws_client("events")

#: Built on first use, once per container: loading the corridor is not free and the
#: conflator is stateless, so it is safe to share across invocations.
_conflator_cache: Conflator | None = None

#: Which corridor this function serves, from the environment. NO DEFAULT, and the
#: portability check is why: a literal route name here would be a corridor
#: identifier in core code, which is exactly what must live in configuration
#: instead. scripts/check-portability.sh caught this being written the lazy way.
#:
#: Unset is resolved rather than guessed - see _route().
ROUTE_ENV = "CEH_ROUTE"


def _route(available: Callable[[], list[str]] | None = None) -> str:
    """Which corridor to serve.

    Explicit environment variable wins. Failing that, if the source describes
    exactly ONE corridor, that is unambiguous and is used. More than one and no
    variable set is a real ambiguity, so it raises rather than picking - silently
    conflating a payload against the wrong corridor would place events hundreds of
    miles from where they are.
    """
    configured = os.environ.get(ROUTE_ENV)
    if configured:
        return configured

    routes = available() if available else []
    if len(routes) == 1:
        return routes[0]
    if not routes:
        # No source to ask: the offline corridor names itself.
        from ..core.lrs import active_corridor

        return active_corridor().route
    raise RuntimeError(
        f"{len(routes)} corridors are available ({', '.join(routes)}) and {ROUTE_ENV} "
        "is not set, so there is no way to know which one this function serves. "
        "Set it in lib/ingest-stack.ts."
    )


def _conflator() -> Conflator:
    """The conflator, chosen by what this deployment actually has.

    WITH A DATABASE: the corridor comes from Postgres and polygon conflation runs
    there, while per-record point conflation stays in process. See HybridConflator
    for why that split rather than all-or-nothing.

    WITHOUT ONE: everything is in process from a JSON corridor. This is what keeps
    `npm run probe`, the UI API and the whole test suite running with no AWS account,
    and it is the fallback if the spatial stack is not deployed.

    The choice is made by the presence of SPATIAL_DB_SECRET_ARN rather than by a
    flag, because that variable is what CDK sets when it grants this function the
    database. One thing to configure, not two that can disagree.
    """
    global _conflator_cache
    if _conflator_cache is not None:
        return _conflator_cache

    if os.environ.get("SPATIAL_DB_SECRET_ARN"):
        from ..core import postgis

        set_corridor_source(postgis.load_corridor)
        route = _route(postgis.available_routes)
        set_active_route(route)
        corridor = postgis.load_corridor(route)
        _conflator_cache = postgis.HybridConflator(
            local=LocalConflator(corridor=corridor),
            remote=postgis.PostgisConflator(route),
        )
        print(
            json.dumps(
                {
                    "msg": "conflator_ready",
                    "route": route,
                    "corridorSource": "postgres",
                    "vertices": len(corridor.centerline),
                    "calibrated": corridor.measures is not None,
                    "polygonPath": "postgis",
                }
            )
        )
    else:
        _conflator_cache = LocalConflator()
        print(
            json.dumps(
                {
                    "msg": "conflator_ready",
                    "route": _route(),
                    "corridorSource": "json",
                    "polygonPath": "sampled",
                }
            )
        )
    return _conflator_cache

# EventBridge caps PutEvents at 10 entries per call.
_PUT_EVENTS_BATCH = 10


def handler(event: dict[str, Any], context: Any = None) -> dict[str, int]:
    """Triggered by the ``RawPayloadStored`` EventBridge rule."""
    detail = event["detail"]
    source_id = detail["sourceId"]
    adapter = adapter_for(source_id)

    if adapter is None:
        # Never silently drop. An unregistered source is a real problem.
        print(json.dumps({"msg": "no_adapter_registered", "sourceId": source_id}))
        _announce_quarantine(detail, f"no adapter registered for {source_id}")
        return {"candidates": 0, "issues": 1, "offCorridor": 0}

    obj = _s3.get_object(Bucket=detail["bucket"], Key=detail["key"])
    body = obj["Body"].read().decode("utf-8")

    result = adapter.parse(
        body,
        AdapterContext(
            conflator=_conflator(),
            raw_ref=detail["rawRef"],
            retrieved_at=detail["retrievedAt"],
        ),
    )

    # Score each candidate now so latency stays inside the ingest budget - scoring
    # is in the real-time path, not a batch job.
    #
    # At this stage `sources` is just the one that reported it, so corroboration
    # scores as single-source. The resolver re-scores after matching, when it knows
    # who else is reporting the same event. Both scores are legitimate; this one
    # answers "how much do we trust this report" and the resolver's answers "how
    # much do we trust this event".
    scored = [
        (
            candidate,
            score_confidence(
                ScoringInput(
                    candidate=candidate,
                    sources=[candidate.source],
                    last_confirmed_at=(
                        candidate.source.source_updated_at or detail["retrievedAt"]
                    ),
                )
            ),
        )
        for candidate in result.candidates
    ]

    entries = [
        {
            "EventBusName": os.environ["EVENT_BUS"],
            "Source": "corridor-event-hub.normalizer",
            "DetailType": "CandidateEventProduced",
            "Detail": json.dumps(
                {
                    "sourceId": source_id,
                    "rawRef": detail["rawRef"],
                    "candidate": to_jsonable(candidate),
                    "provisionalConfidence": to_jsonable(confidence),
                },
                allow_nan=False,
            ),
        }
        for candidate, confidence in scored
    ]

    for start in range(0, len(entries), _PUT_EVENTS_BATCH):
        _events.put_events(Entries=entries[start : start + _PUT_EVENTS_BATCH])

    # Mapping issues go to the review queue as first-class output.
    if result.issues:
        _events.put_events(
            Entries=[
                {
                    "EventBusName": os.environ["EVENT_BUS"],
                    "Source": "corridor-event-hub.normalizer",
                    "DetailType": "MappingIssuesFound",
                    "Detail": json.dumps(
                        {
                            "sourceId": source_id,
                            "rawRef": detail["rawRef"],
                            "adapterSchemaVersion": adapter.expected_schema_version,
                            "issues": to_jsonable(result.issues[:100]),
                            "totalIssues": len(result.issues),
                        },
                        allow_nan=False,
                    ),
                }
            ]
        )

    values: list[float] = [confidence.value for _, confidence in scored]
    print(
        json.dumps(
            {
                "msg": "normalized",
                "sourceId": source_id,
                "candidates": len(result.candidates),
                "offCorridor": result.off_corridor,
                "issues": len(result.issues),
                # Surfacing the confidence range makes the quality signal visible in
                # logs from day one rather than only in the API.
                "confidenceRange": [min(values), max(values)] if values else None,
            }
        )
    )

    return {
        "candidates": len(result.candidates),
        "issues": len(result.issues),
        "offCorridor": result.off_corridor,
    }


def _announce_quarantine(detail: dict[str, Any], reason: str) -> None:
    _events.put_events(
        Entries=[
            {
                "EventBusName": os.environ["EVENT_BUS"],
                "Source": "corridor-event-hub.normalizer",
                "DetailType": "PayloadQuarantined",
                "Detail": json.dumps({**detail, "reason": reason}),
            }
        ]
    )
