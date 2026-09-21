"""Builds the corridor strip DATA document.

    npm run strip              # fetch live where possible, write docs/strip/data.json

``build()`` is the shared producer: the React app gets this same document from
``strip_server`` over ``/api/strip``, and this entry point writes it to a file for
scripting, diffing, and archiving a snapshot.

It is deliberately close in shape to what the query API must return, so
pointing the app at the real API later is a base-URL change rather than a rewrite.

NOTE: this used to also generate a self-contained ``strip.html``. That viewer has
been replaced by the React app under ``ui/`` - one renderer, so the two cannot
drift. ``npm run ui`` is the way to look at this data.

The strip axis is corridor MEASURE, not longitude. That is only honest while the LRS
is honest, so the export always states which case it is in: ``corridor.verified``
plus a ``corridor.warning`` that is non-null only when the geometry is a
placeholder. The UI renders the warning when there is one, because an unlabelled
strip is a claim about accuracy.

``config/corridor.json`` now carries ``verified: true`` - real state LRS geometry
with calibrated measures, so the warning is absent and the axis means what it says.
The mechanism stays because the flag can go back to false: regenerating from a bad
fetch writes it false, and the warning returns with it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from .adapters.adapter import AdapterContext
from .adapters.feeds import FeedTarget, catalog_entry, feed_targets
from .core.confidence import (
    CONFIDENCE_MODEL_VERSION,
    WEIGHTS,
    ScoringInput,
    explain_confidence,
    score_confidence,
)
from .core.lifecycle import TRANSITIONS, profile_for, target_for_source_absent
from .core.lrs import (
    CORRIDOR_TOTAL_MILES,
    LocalConflator,
    corridor,
    measure_to_state_milepost,
)
from .core.matcher import MATCH_MODEL_VERSION, MatchPair, cluster_candidates
from .core.serde import to_jsonable
from .core.timeutil import iso_utc, now_iso, parse_iso
from .core.types import CandidateEvent, Confidence

# The checkout root, which is the directory CONTAINING the package: src/. Two
# .parent hops, not three - this file is src/corridor_event_hub/strip_export.py.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = _CHECKOUT_ROOT / "docs" / "strip"
FIXTURE_DIR = _CHECKOUT_ROOT / "tests" / "fixtures"

FETCH_TIMEOUT_SECONDS = 30

# Classes with no LIVE FEED behind them, so the viewer shows absence honestly.
# Absence is a finding, so it is DATA rather than a footnote. See DATA-SOURCES.md
# for how each of these was probed.
#
# "No live feed" is not the same as "no data", which is why
# dimensional_restriction is no longer in this list: 1,543 NBI structures are
# loaded and conflated. Listing a class that HAS data as unsourced understates
# coverage, which is the same category of dishonesty as the reverse.
UNSOURCED_CLASSES = (
    {
        "eventClass": "truck_parking",
        "reason": "nothing found on this corridor; inventory must be built",
    },
    {
        "eventClass": "road_surface",
        "reason": "weather-derived only; real sensing needs RWIS",
    },
)


def _measure_label(measure: float) -> str:
    state_mp = measure_to_state_milepost(measure)
    if state_mp:
        state, milepost = state_mp
        return f"{state} MP {milepost:.1f}"
    return f"measure {measure:.1f}"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _confidence_out(confidence: Confidence) -> dict[str, Any]:
    """Render a confidence for the viewer.

    The breakdown keys are camelCased here, unlike everywhere else in the codebase.
    This file emits a WIRE FORMAT for a browser, and every other key in that document
    is camelCase (``beginMeasure``, ``eventClass``) because that is the idiom the
    viewer's JavaScript reads. A single snake_case island inside it is the kind of
    inconsistency that costs someone an afternoon.

    The internal model stays snake_case, matching the canonical field names in
    the canonical model - see core/serde.py. This is the one boundary that translates.
    """
    return {
        "value": confidence.value,
        "breakdown": {
            _camel(name): value
            for name, value in to_jsonable(confidence.breakdown).items()
        },
        "explanation": explain_confidence(confidence),
    }


class Fetched:
    def __init__(
        self,
        body: str | None,
        mode: str,
        http_status: int | None,
        latency_ms: int | None,
        note: str | None,
    ) -> None:
        self.body = body
        self.mode = mode
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.note = note


def load(target: FeedTarget, fixtures_only: bool) -> Fetched:
    """Live fetch with fixture fallback.

    Falling back is REPORTED, never silent - a strip built from captured bytes that
    claims to be live would be the single most misleading thing this tool could do.
    """

    def fixture() -> str:
        return (FIXTURE_DIR / target.fixture).read_text(encoding="utf-8")

    if fixtures_only:
        return Fetched(fixture(), "fixture", None, None, "captured payload (--fixtures)")

    if not target.live:
        if target.fetcher is not None and not target.env_var and not target.secret_id:
            # IAM-authenticated: there is no key to set, so naming one would send
            # someone hunting for a credential that does not exist.
            reason = "no AWS credentials (IAM-authenticated source); using captured payload"
        elif target.key_error:
            # The specific cause, from resolve_key. Far more useful than the generic
            # "set the env var or grant read" advice, which names the two fixes least
            # likely to be the real problem - the usual cause is that `aws` is not on
            # PATH for this process at all.
            reason = f"{target.key_error} Using captured payload."
        else:
            reason = (
                f"no key available (set {target.env_var} or grant read on"
                f" {target.secret_id}); using captured payload"
            )
        return Fetched(fixture(), "fixture", None, None, reason)

    started = time.monotonic()

    if target.fetcher is not None:
        # Not a single URL GET - see FeedTarget.fetcher.
        try:
            status, body = target.fetcher()
        except Exception as exc:  # noqa: BLE001 - same expected outcome as a down feed
            return Fetched(
                fixture(),
                "fixture",
                None,
                int((time.monotonic() - started) * 1000),
                f"live fetch failed ({type(exc).__name__}: {exc}); using captured payload",
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if status != 200:
            return Fetched(
                fixture(),
                "fixture",
                status,
                latency_ms,
                f"live fetch returned status {status}; using captured payload",
            )
        return Fetched(body, "live", status, latency_ms, None)

    request = urllib.request.Request(target.url, headers=target.headers or {}, method="GET")
    try:
        with urllib.request.urlopen(  # nosec B310 # https, enforced by FeedTarget
            request, timeout=FETCH_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            return Fetched(body, "live", response.status, latency_ms, None)
    except urllib.error.HTTPError as exc:
        return Fetched(
            fixture(),
            "fixture",
            exc.code,
            int((time.monotonic() - started) * 1000),
            f"live fetch returned HTTP {exc.code}; using captured payload",
        )
    except Exception as exc:  # noqa: BLE001 - a down feed is an expected outcome
        return Fetched(
            fixture(),
            "fixture",
            None,
            int((time.monotonic() - started) * 1000),
            f"live fetch failed ({type(exc).__name__}: {exc}); using captured payload",
        )


def _pair_out(pair: MatchPair) -> dict[str, Any]:
    return {
        "from": pair.from_index,
        "to": pair.to_index,
        "value": pair.score.value,
        "explanation": pair.score.explanation,
    }


def _transitions_from(state: str) -> list[dict[str, Any]]:
    """The legal edges out of a state, with their triggers and rationale.

    The transition table is machine-readable DATA precisely so it can be published
    rather than re-typed, so the viewer renders THIS. A hardcoded copy
    in TypeScript would be a second source of truth that drifts the first time an
    edge is added - and drifts silently, because nothing compares the two.
    """
    return [
        {
            "toState": rule.to_state,
            "triggers": list(rule.triggers),
            "rationale": rule.rationale,
        }
        for rule in TRANSITIONS
        if rule.from_state == state
    ]


# Why the timeline says "first observed" and not "created": this exporter builds ONE
# snapshot and holds no state between builds, so it has no history to show. Every
# event is first-seen on every build and the TTL countdown restarts with it. The
# deployed resolver keeps the append-only audit trail and a timeline reading
# real transitions is a query-API change, not a UI change.
#
# This is stated in the document rather than only in the UI because the export is
# also read by scripts and archived as a snapshot: a consumer computing "how long has
# this been active" from `enteredAt` would get a wrong answer with no warning.
NO_HISTORY_NOTE = (
    "Single snapshot, no persisted lifecycle history: this exporter holds no state "
    "between builds, so every event is first observed on this build and its TTL "
    "countdown restarts with it. Elapsed time in a state is NOT derivable from this "
    "document. The deployed resolver keeps the append-only audit trail."
)


def build(fixtures_only: bool) -> dict[str, Any]:
    conflator = LocalConflator()
    retrieved_at = now_iso()

    sources: list[dict[str, Any]] = []
    flat: list[CandidateEvent] = []

    for target in feed_targets():
        got = load(target, fixtures_only)
        candidate_count = 0
        off_corridor = 0
        issues: list[dict[str, Any]] = []
        payload_bytes: int | None = None

        if got.body is not None:
            payload_bytes = len(got.body)
            try:
                raw_ref = (
                    f"s3://local-strip/{target.source_id}/not-persisted"
                    if got.mode == "live"
                    else f"file://tests/fixtures/{target.fixture}"
                )
                result = target.adapter.parse(
                    got.body,
                    AdapterContext(
                        conflator=conflator,
                        raw_ref=raw_ref,
                        retrieved_at=retrieved_at,
                    ),
                )
                candidate_count = len(result.candidates)
                off_corridor = result.off_corridor
                flat.extend(result.candidates)

                # Group rather than truncate. The counts ARE the review queue.
                grouped: dict[tuple, dict[str, Any]] = {}
                for mapping_issue in result.issues:
                    key = (mapping_issue.reason, mapping_issue.field)
                    if key in grouped:
                        grouped[key]["count"] += 1
                    else:
                        grouped[key] = {"count": 1, "example": mapping_issue.detail}
                issues = [
                    {
                        "reason": reason,
                        "field": field_name,
                        "count": value["count"],
                        "example": value["example"],
                    }
                    for (reason, field_name), value in grouped.items()
                ]
            except Exception as exc:  # noqa: BLE001 - one bad adapter must not kill the export
                got.note = f"adapter threw: {type(exc).__name__}: {exc}"
                got.mode = "failed"

        catalog = catalog_entry(target.source_id)
        sources.append(
            {
                "sourceId": target.source_id,
                "agency": target.adapter.agency,
                "label": target.label,
                # 'live' | 'fixture' | 'failed' - the viewer must be able to say which.
                "mode": got.mode,
                "httpStatus": got.http_status,
                "latencyMs": got.latency_ms,
                "payloadBytes": payload_bytes,
                "candidateCount": candidate_count,
                "offCorridor": off_corridor,
                "issues": issues,
                "note": got.note,
                # Licence terms travel WITH the data into the published artifact.
                # The strip is the one output someone might screenshot into a deck,
                # so "not redistributable" has to be visible there rather than only
                # in the catalog. `redistributable: null` means UNKNOWN, which is
                # not the same as permitted.
                "redistributable": catalog.get("redistributable"),
                "licenseShort": catalog.get("licenseShort"),
                "attribution": catalog.get("attribution"),
                # Catalog facts the event timeline grades trust against. They travel
                # with the data for the same reason the licence does: a grade whose
                # inputs are not visible is another opaque number.
                #
                # `independenceGroup` is the one that changes a conclusion. Two
                # agencies in the SAME group are not independent corroboration
                #, so an event labelled "2 agencies" can still be
                # single-source evidence, and only this field can say so.
                "independenceGroup": catalog.get("independenceGroup"),
                # UNKNOWN is why `source_absent` routes to `clearing` rather than
                # `cleared` for this source - see core/lifecycle.py, OPEN QUESTION 2.
                "snapshotSemantics": catalog.get("snapshotSemantics"),
                # What "stale" means for THIS feed. A record unchanged for an hour is
                # nothing for a work-zone feed that publishes daily and a fault for
                # one that publishes every 60s, so the threshold cannot be global.
                "publishCadenceSeconds": catalog.get("publishCadenceSeconds"),
                "freshnessSloSeconds": catalog.get("freshnessSloSeconds"),
            }
        )

    # Confidence at the candidate level: single source, uncorroborated. The
    # cluster-level score below is the one that reflects corroboration - both are
    # legitimate and the distinction is deliberate (docs/OPERATING.md).
    candidates = [
        {
            # Index in the flat candidate array; the viewer joins clusters by this.
            "id": index,
            "sourceId": c.source.source_id,
            "agency": c.source.agency,
            "nativeId": c.source.native_id,
            "eventClass": c.event_class,
            "eventSubtype": c.event_subtype,
            "beginMeasure": c.extent.begin_measure,
            "endMeasure": c.extent.end_measure,
            "direction": c.extent.direction,
            "states": c.extent.states,
            # Per-state milepost, since that is what a DOT actually recognizes.
            "beginLabel": _measure_label(c.extent.begin_measure),
            "endLabel": _measure_label(c.extent.end_measure),
            "conflationMethod": c.extent.conflation_method,
            "positionalAccuracyMeters": c.extent.positional_accuracy_meters,
            "startTime": c.start_time,
            "endTime": c.end_time,
            "timeConfidence": c.time_confidence,
            # Event time and system time are different clocks, so both are
            # exported and neither stands in for the other. `sourceUpdatedAt` is when
            # the AGENCY last changed the record - the basis confidence decays from
            #; `retrievedAt` is when WE fetched, which is always ~now.
            #
            # Conflating the two is not hypothetical. It scored a three-week-old
            # Oklahoma work zone 0.76 here against the pipeline's 0.58 from the same
            # bytes (TestAgreesWithTheDeployedPipeline). The timeline draws both marks
            # so the gap between "the agency last said so" and "we asked" is visible
            # rather than something a reader has to know to look for.
            "sourceUpdatedAt": c.source.source_updated_at,
            "retrievedAt": c.source.retrieved_at,
            "laneImpacts": [
                {
                    "ordinal": lane.ordinal,
                    "type": lane.type,
                    "status": lane.status,
                    "inferred": lane.inferred,
                }
                for lane in c.lane_impacts
            ],
            "agencySeverity": c.agency_severity,
            "confidence": _confidence_out(
                score_confidence(
                    ScoringInput(
                        candidate=c,
                        sources=[c.source],
                        # MUST match handlers/normalizer.py. `retrieved_at` alone is
                        # when WE fetched, which is always "just now" and so always
                        # scores recency ~1.0; `source_updated_at` is when the AGENCY
                        # last changed the record, which is the recency signal.
                        #
                        # Using the wrong one made the strip disagree with the
                        # deployed pipeline about the same event: an Oklahoma work
                        # zone unchanged since 2026-07-22 scored 0.76 here and 0.58
                        # in the normalizer. The strip is the artifact people trust
                        # on demo day, and it was the flattering one.
                        last_confirmed_at=c.source.source_updated_at or c.source.retrieved_at,
                    )
                )
            ),
            # Where the exact bytes live.
            "rawRef": c.source.raw_ref,
            "issueCount": len(c.mapping_issues),
        }
        for index, c in enumerate(flat)
    ]

    # Snapshot semantics per source, taken from the rows just built rather than
    # re-read from the catalog, so the UI's source table and the lifecycle routing it
    # explains cannot disagree about the same feed.
    semantics_by_source = {s["sourceId"]: s["snapshotSemantics"] for s in sources}

    clusters = []
    for cluster_id, cluster in enumerate(cluster_candidates(flat)):
        members = [flat[i] for i in cluster.members]
        primary = members[0]

        # Corroboration counts only INDEPENDENT sources, which is exactly what
        # a cluster of >1 agency provides. This is the score that should differ from
        # the per-candidate one above - through CORROBORATION, not through a
        # different recency basis.
        #
        # The most recent agency update across the cluster, falling back per source.
        # The freshest confirming report is what recency should decay from, so
        # one stale corroborator must not drag down an otherwise fresh event.
        last_confirmed_at = max(
            m.source.source_updated_at or m.source.retrieved_at for m in members
        )
        confidence = score_confidence(
            ScoringInput(
                candidate=primary,
                sources=[m.source for m in members],
                last_confirmed_at=last_confirmed_at,
            )
        )
        profile = profile_for(primary.event_class)

        # A first sighting is `reported` (see lifecycleState below), so the TTL and the
        # legal edges are the ones out of that state.
        lifecycle_state = "reported"
        ttl_seconds = profile.ttl_seconds.get(lifecycle_state)
        entered_at = retrieved_at
        entered = parse_iso(entered_at)
        ttl_expires_at = (
            iso_utc(entered + timedelta(seconds=ttl_seconds))
            if ttl_seconds is not None and entered is not None
            else None
        )

        source_absent = []
        for source_id in sorted({m.source.source_id for m in members}):
            # `or "UNKNOWN"`: a catalog entry with no snapshotSemantics is an unanswered
            # question, not permission to clear. target_for_source_absent treats
            # anything but the literal "cleared" conservatively, and passing the
            # missing case through it explicitly keeps that decision in one place.
            semantics = semantics_by_source.get(source_id) or "UNKNOWN"
            to_state, reason = target_for_source_absent(semantics)
            source_absent.append(
                {
                    "sourceId": source_id,
                    "snapshotSemantics": semantics,
                    "toState": to_state,
                    "reason": reason,
                }
            )

        clusters.append(
            {
                "clusterId": cluster_id,
                "members": cluster.members,
                "beginMeasure": min(m.extent.begin_measure for m in members),
                "endMeasure": max(m.extent.end_measure for m in members),
                "eventClass": primary.event_class,
                "direction": primary.extent.direction,
                # Agencies contributing - length > 1 is a demonstrated cross-agency
                # merge.
                "agencies": sorted({m.source.agency for m in members}),
                "confidence": _confidence_out(confidence),
                "joins": [_pair_out(p) for p in cluster.joins],
                "reviewPairs": [_pair_out(p) for p in cluster.review_pairs],
                # A first sighting is `reported`, never `active` - promotion is a
                # resolver decision with an audit record, not something an
                # exporter asserts.
                "lifecycleState": lifecycle_state,
                "ttlSeconds": ttl_seconds,
                # Everything the timeline needs to draw WHERE THIS EVENT IS IN ITS
                # LIFE and where it goes next. `state` and `ttlSeconds` deliberately
                # are NOT repeated in here: two fields carrying one fact is two fields
                # to keep in step.
                "lifecycle": {
                    "enteredAt": entered_at,
                    "ttlExpiresAt": ttl_expires_at,
                    # The recency basis the score above actually used - the same value
                    # by construction, so the mark on the timeline and the number in
                    # the breakdown cannot tell different stories.
                    "lastConfirmedAt": last_confirmed_at,
                    "reopenWindowSeconds": profile.reopen_window_seconds,
                    # Decay is continuous on a class half-life, which is what
                    # lets the viewer project confidence forward instead of implying
                    # today's number holds indefinitely.
                    "confidenceHalfLifeSeconds": profile.confidence_half_life_seconds,
                    "transitions": _transitions_from(lifecycle_state),
                    # Where a disappearance from each contributing feed would send
                    # this event, and why. The conservative default is the point: an
                    # event whose source has UNKNOWN snapshot semantics must not look
                    # like one that can be trusted to clear itself.
                    "sourceAbsent": source_absent,
                    "historyAvailable": False,
                    "note": NO_HISTORY_NOTE,
                },
            }
        )

    warning = (
        None
        if corridor.verified
        else (
            "Placeholder centerline and approximate state mileages (corridor.json "
            "verified=false). Positions are accurate to +/- several miles. Fine to "
            "demonstrate the pipeline, NOT to publish."
        )
    )

    return {
        "generatedAt": retrieved_at,
        "corridor": {
            "route": corridor.route,
            "totalMiles": round(CORRIDOR_TOTAL_MILES, 1),
            "verified": corridor.verified,
            "warning": warning,
            "states": [
                {
                    "state": s.state,
                    "beginMeasure": s.corridor_offset,
                    "endMeasure": s.corridor_offset + s.length_miles,
                }
                for s in corridor.states
            ],
        },
        "matchModelVersion": MATCH_MODEL_VERSION,
        # An integrator sets a trust threshold against these numbers, so the
        # weights that produced them are part of the contract rather than an internal
        # detail. Publishing them is also what lets the viewer PROJECT confidence
        # forward honestly: recency is the only component that moves with the clock
        #, so a projection needs its weight to say anything about the total.
        # Without this the UI would have to hardcode 0.2 and silently disagree with
        # the scorer the first time the model was retuned.
        "confidenceModel": {
            "version": CONFIDENCE_MODEL_VERSION,
            "weights": {_camel(name): weight for name, weight in WEIGHTS.items()},
        },
        "sources": sources,
        "candidates": candidates,
        "clusters": clusters,
        "unsourcedClasses": list(UNSOURCED_CLASSES),
    }


def write(data: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # allow_nan=False is the tripwire: an unresolved measure must have become null in
    # to_jsonable, not a bare NaN token that no strict JSON parser will read.
    payload = json.dumps(to_jsonable(data), indent=2, allow_nan=False)

    out = OUT_DIR / "data.json"
    out.write_text(payload + "\n", encoding="utf-8")

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the corridor strip data file.")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="use captured payloads only, no network",
    )
    args = parser.parse_args(argv)

    data = build(fixtures_only=args.fixtures)
    out = write(data)

    sources = data["sources"]
    live = sum(1 for s in sources if s["mode"] == "live")
    clusters = data["clusters"]
    print(f"strip data written: {out}")
    print(
        f"  {len(sources)} source(s) ({live} live, {len(sources) - live} from fixtures)  "
        f"{len(data['candidates'])} candidate(s)  {len(clusters)} cluster(s)"
    )

    merged = [c for c in clusters if len(c["members"]) > 1]
    if merged:
        detail = ", ".join(
            f"#{c['clusterId']} ({' + '.join(c['agencies'])})" for c in merged
        )
        print(f"  {len(merged)} cross-source merge(s): {detail}")
    else:
        print(
            "  no merges in this snapshot - the work-zone feeds cover disjoint corridor\n"
            "           segments, so no two agencies describe the same event. Not a matcher\n"
            "           failure; see the matcher tests for the merge cases."
        )

    reviews = sum(len(c["reviewPairs"]) for c in clusters)
    if reviews:
        print(f"  {reviews} ambiguous pair(s) routed to review")
    if not corridor.verified:
        print("  WARNING  corridor.json verified=false - positions are +/- several miles")

    print("\nRun `npm run ui` to view this in the browser (React app + live API).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
