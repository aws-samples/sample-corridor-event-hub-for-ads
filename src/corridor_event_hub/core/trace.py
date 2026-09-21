"""Record lifecycle trace assembly.

WHAT A TRACE IS: one record's whole life, from the bytes that first mentioned it to
whatever ended it, expressed as data. The event store already holds every ingredient
- an append-only version chain, an audit record per transition, and a payload
pointer on each one - but it holds them as 50 sibling items under one partition key.
This module turns those items into the thing an operator actually asks for: what
happened to this record, in order, why, and what is about to happen next.

PURE ON PURPOSE. Nothing here touches AWS. It takes an ``Event`` and a ``History``
and returns a document, so every interesting case - a merge, an un-merge, a TTL
ladder, an audit trail with a hole in it - is reachable in a test with
``InMemoryEventStore`` and no account. ``core/cloud.py`` is the half that reads the
cloud; keeping the two apart is what makes this one testable at all.

snake_case, NOT the camelCase of the strip document. Deliberate, and the reason is
handlers/query.py's: this document is the integrator-facing shape, field for field
with the canonical names, so ``ui-trace`` can later be pointed
at the deployed query API's ``/events/{id}`` + ``/history`` with a base-URL change
instead of a rewrite. The strip document translates to camelCase because its
producer is a snapshot builder for a browser; this one keeps the canonical names.

THE FIVE STAGES ARE A CLAIM ABOUT THE PIPELINE, and the claim is checked rather
than asserted: every stage states the evidence it read, and says so plainly when
there is none. `normalize` is the honest example - mapping issues are metered and
logged by the normalizer but are NOT persisted per event, so the stage reports what
conflation produced and links out for the rest rather than inventing a count.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .confidence import INDEPENDENCE_GROUPS
from .config import load_json
from .lifecycle import (
    TRANSITION_TRIGGERS,
    AuditRecord,
    is_legal_transition,
    legal_targets_from,
    profile_for,
)
from .resolution import TTL_LADDER, is_stale, seconds_until_ttl, ttl_expires_at
from .serde import to_jsonable
from .timeutil import parse_iso
from .types import LIFECYCLE_STATES, Event

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

#: The pipeline as a record experiences it. Published as data, with a `what` line
#: each, because the UI's spine is generated from this rather than hardcoded - the
#: same reason the transition table is served rather than re-typed in TypeScript.
STAGES: tuple[dict[str, str], ...] = (
    {
        "stage": "ingest",
        "label": "Ingested",
        "what": (
            "The collector fetched the agency payload and wrote the exact bytes to the "
            "raw zone. Every step below carries the payload pointer that caused it."
        ),
    },
    {
        "stage": "normalize",
        "label": "Normalized",
        "what": (
            "An adapter mapped those bytes to the canonical model and conflated the "
            "location onto the corridor. Class, subtype, extent and lane impacts "
            "are set here; lifecycle state and confidence are NOT."
        ),
    },
    {
        "stage": "resolve",
        "label": "Resolved",
        "what": (
            "The resolver assigned this record its stable event id, reconciled it against "
            "what was already known, and decided whether it duplicated another event. "
            "Each decision appended an immutable version."
        ),
    },
    {
        "stage": "lifecycle",
        "label": "Lifecycle",
        "what": (
            "State transitions, each with a trigger, an actor and a reason. TTL "
            "timers move a record that has gone quiet down the ladder rather than leaving "
            "it published forever."
        ),
    },
    {
        "stage": "terminal",
        "label": "End of life",
        "what": (
            "Cleared, merged into another event, or archived as an immutable historical "
            "record. Provenance survives all three."
        ),
    },
)

#: Which stage a transition belongs to, by what triggered it. A `source_update` is
#: an ingest-driven step and carries the payload that drove it; a `timer_ttl` is the
#: pipeline acting on silence and carries no new bytes at all. That difference is
#: the single most useful thing to know when reading a trace, so it is explicit.
TRIGGER_STAGE: dict[str, str] = {
    "source_update": "ingest",
    "source_absent": "lifecycle",
    "timer_ttl": "lifecycle",
    "derived_signal": "lifecycle",
    "dedup_decision": "resolve",
    "validation_pass": "resolve",
    "validation_fail": "resolve",
    "operator_override": "lifecycle",
    "retention_rule": "terminal",
}

#: States that end a record's life. `merged` is terminal for the CHILD only: the
#: parent carries it forward, and an un-merge takes it back to `active`.
TERMINAL_STATES = ("cleared", "archived", "merged")

# ---------------------------------------------------------------------------
# Version diffing
# ---------------------------------------------------------------------------

#: Paths that change on every version and say nothing. `updated_at` and `version`
#: are the version itself; `confidence.computed_at` moves whenever the score is
#: recomputed, which is every time. Excluding them is what makes a diff readable -
#: a diff where three of four lines are always present trains a reader to skip it.
DIFF_IGNORED = frozenset(
    {
        "version",
        "updated_at",
        "confidence.computed_at",
        "confidence.model_version",
        "severity.function_version",
    }
)

#: Fields where a diff is worth flagging even when tiny. Everything else is
#: reported as-is; these get `notable: true` so the UI can lead with them.
DIFF_NOTABLE_PREFIXES = (
    "lifecycle_state",
    "extent.begin_measure",
    "extent.end_measure",
    "extent.direction",
    "lane_impacts",
    "end_time",
    "start_time",
    "related_event_ids",
    "severity.computed",
)

#: Changes that mean "we fetched again", not "the record changed".
#:
#: THIS DISTINCTION IS THE POINT OF THE DIFF. A record polled every 60 seconds
#: accumulates a version per poll, and if `raw_ref` moving looks the same as a lane
#: closing, the version chain is unreadable. `confidence` is in here because the
#: recency component decays continuously against the clock, so the score
#: moves on its own with no new information at all.
DIFF_BOOKKEEPING_SUFFIXES = (
    ".raw_ref",
    ".retrieved_at",
)
DIFF_BOOKKEEPING_PREFIXES = ("confidence.",)

#: How long a value may be before a diff truncates it. An NWS polygon is tens of
#: thousands of coordinates and would otherwise make one diff line larger than the
#: rest of the document put together.
_MAX_DIFF_VALUE_CHARS = 240


def _short(value: Any) -> Any:
    """A value fit for a diff line: scalars verbatim, structures summarized."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > _MAX_DIFF_VALUE_CHARS:
            return value[:_MAX_DIFF_VALUE_CHARS] + f"... ({len(value)} chars)"
        if isinstance(value, float) and not math.isfinite(value):
            # NaN is how an unresolved measure travels. It must not become
            # the string "nan" in a JSON document a browser reads.
            return None
        return value
    if isinstance(value, list):
        return f"[{len(value)} item(s)]"
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(value)[:6]) + "}"
    return str(value)


def _diff_value(path: str, before: Any, after: Any, out: list[dict[str, Any]]) -> None:
    if path in DIFF_IGNORED:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _diff_value(f"{path}.{key}" if path else key, before.get(key), after.get(key), out)
        return
    if _same(before, after):
        return
    bookkeeping = path.endswith(DIFF_BOOKKEEPING_SUFFIXES) or path.startswith(
        DIFF_BOOKKEEPING_PREFIXES
    )
    out.append(
        {
            "path": path,
            "from": _short(before),
            "to": _short(after),
            "bookkeeping": bookkeeping,
            "notable": path.startswith(DIFF_NOTABLE_PREFIXES) and not bookkeeping,
        }
    )


def _same(before: Any, after: Any) -> bool:
    """Equality that treats two NaN measures as unchanged.

    ``float('nan') != float('nan')``, so a plain comparison reports every
    unresolved extent as changing on every single version - a diff line that is
    always there and never means anything.
    """
    if isinstance(before, float) and isinstance(after, float) and math.isnan(before):
        return math.isnan(after)
    return before == after


def _keyed(items: list[dict[str, Any]], *keys: str) -> dict[tuple, dict[str, Any]]:
    return {tuple(item.get(k) for k in keys): item for item in items}


def diff_versions(before: Event | None, after: Event) -> list[dict[str, Any]]:
    """What changed between two consecutive versions of one event.

    LISTS ARE COMPARED BY IDENTITY, not by position: sources by
    ``(source_id, native_id)`` and lane impacts by ordinal. Positional comparison
    reports a reordered source list as every field changing, which is exactly the
    kind of noise that makes a diff useless the first time a second agency joins an
    event.
    """
    if before is None:
        return []

    old = to_jsonable(before)
    new = to_jsonable(after)
    out: list[dict[str, Any]] = []

    for name, keys in (("sources", ("source_id", "native_id")), ("lane_impacts", ("ordinal",))):
        old_items = _keyed(old.pop(name, []) or [], *keys)
        new_items = _keyed(new.pop(name, []) or [], *keys)
        for key in sorted(set(old_items) | set(new_items), key=lambda k: [str(p) for p in k]):
            label = "/".join(str(part) for part in key if part is not None)
            if key not in old_items:
                out.append(
                    {
                        "path": f"{name}[{label}]",
                        "from": None,
                        "to": "added",
                        "notable": True,
                        "bookkeeping": False,
                    }
                )
            elif key not in new_items:
                out.append(
                    {
                        "path": f"{name}[{label}]",
                        "from": "present",
                        "to": "removed",
                        "notable": True,
                        "bookkeeping": False,
                    }
                )
            else:
                _diff_value(f"{name}[{label}]", old_items[key], new_items[key], out)

    # extensions carry vendor field names verbatim, so they are diffed as a
    # flat map rather than descended into: `nws_headline` changing is one line.
    old_ext = old.pop("extensions", {}) or {}
    new_ext = new.pop("extensions", {}) or {}
    for key in sorted(set(old_ext) | set(new_ext)):
        _diff_value(f"extensions.{key}", old_ext.get(key), new_ext.get(key), out)

    _diff_value("", old, new, out)
    return out


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def _age_seconds(value: str | None, now: datetime) -> float | None:
    moment = parse_iso(value)
    if moment is None:
        return None
    return round((now - moment).total_seconds(), 1)


def _independent_groups(event: Event) -> list[str]:
    """Two feeds in one independence group are ONE witness, not two."""
    return sorted({INDEPENDENCE_GROUPS.get(s.source_id, s.source_id) for s in event.sources})


def _measure(value: float) -> float | None:
    return None if math.isnan(value) else round(value, 3)


def record_summary(event: Event, now: datetime, milepost: Any = None) -> dict[str, Any]:
    """One row in the record list: enough to triage without opening the trace.

    ``milepost`` is an optional callable taking a corridor measure and returning
    ``{state, milepost}`` - injected rather than imported so this module stays free
    of corridor geometry, which may be unavailable locally (and whose absence must
    cost one convenience field rather than the whole listing).
    """
    last_source_update = max(
        (s.source_updated_at or s.retrieved_at for s in event.sources), default=None
    )
    return {
        "event_id": event.event_id,
        "event_class": event.event_class,
        "event_subtype": event.event_subtype,
        "lifecycle_state": event.lifecycle_state,
        "version": event.version,
        "confidence": round(event.confidence.value, 4),
        "severity": event.severity.computed,
        "direction": event.extent.direction,
        "states": list(event.extent.states),
        "begin_measure": _measure(event.extent.begin_measure),
        "end_measure": _measure(event.extent.end_measure),
        "milepost_begin": milepost(event.extent.begin_measure) if milepost else None,
        "milepost_end": milepost(event.extent.end_measure) if milepost else None,
        "conflation_method": event.extent.conflation_method,
        "agencies": sorted({s.agency for s in event.sources}),
        "source_ids": sorted({s.source_id for s in event.sources}),
        "native_ids": [s.native_id for s in event.sources],
        "independent_source_count": len(_independent_groups(event)),
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "last_source_update_at": last_source_update,
        "ttl_expires_at": ttl_expires_at(event),
        "ttl_expired": is_stale(event, now),
        "seconds_until_ttl": seconds_until_ttl(event, now),
        "age_seconds": _age_seconds(event.created_at, now),
        "quiet_seconds": _age_seconds(event.updated_at, now),
        "related_event_ids": list(event.related_event_ids),
        # An extent that never conflated is stored and queryable by id, but it is
        # absent from the corridor index - so a corridor query cannot see it. That
        # is a property of the record worth carrying in the row rather than
        # discovering later.
        "unresolved_extent": math.isnan(event.extent.begin_measure),
        "terminal": event.lifecycle_state in TERMINAL_STATES,
    }


# ---------------------------------------------------------------------------
# Steps: the audit chain, joined to the version it produced
# ---------------------------------------------------------------------------


def _payload_ref(audit: AuditRecord) -> str | None:
    return audit.trigger_payload_ref or None


def is_transition(record: AuditRecord) -> bool:
    """Whether this audit record moved the record between states.

    MOST STEPS DO NOT. A feed re-reporting an active work zone appends a version and
    an audit record with ``from_state == to_state``, which is a confirmation rather
    than a transition - the transition table has no ``active -> active`` edge and is
    not supposed to. Conflating the two made every routine update read as an illegal
    transition, which would have buried the one that genuinely was.
    """
    return record.from_state is not None and record.from_state != record.to_state


def build_steps(versions: list[Event], audit: list[AuditRecord]) -> list[dict[str, Any]]:
    """One entry per recorded transition, with the version it wrote and its diff.

    JOINED BY POSITION, and that is not an assumption - core/eventstore.py writes a
    version and its audit record in ONE transaction with the same sequence, so the
    two lists are the same length and in the same order by construction. Where they
    are NOT, that is a finding (see ``findings``) rather than something to paper
    over: an audit trail with a hole in it is not an audit trail.
    """
    by_sequence = {v.version: v for v in versions}
    steps: list[dict[str, Any]] = []
    for record in sorted(audit, key=lambda a: a.sequence):
        version = by_sequence.get(record.sequence)
        previous = by_sequence.get(record.sequence - 1)
        changes = diff_versions(previous, version) if version else []
        steps.append(
            {
                "sequence": record.sequence,
                "stage": TRIGGER_STAGE.get(record.trigger, "lifecycle"),
                "from_state": record.from_state,
                "to_state": record.to_state,
                "trigger": record.trigger,
                "actor": record.actor,
                "reason": record.reason,
                "rule_version": record.rule_version,
                # Two clocks, never collapsed into one. `occurred_at` is when
                # the world changed, `recorded_at` is when we wrote it down, and the
                # gap between them is ingest latency.
                "occurred_at": record.occurred_at,
                "recorded_at": record.recorded_at,
                "lag_seconds": _between(record.occurred_at, record.recorded_at),
                "operator_id": record.operator_id,
                # The exact bytes that caused this step. This is the link
                # that makes the trace go all the way back to ingestion.
                "payload_ref": _payload_ref(record),
                # A confirmation is not a transition, and only a transition can be
                # illegal. See is_transition().
                "transition": is_transition(record),
                "legal": (
                    is_legal_transition(record.from_state, record.to_state, record.trigger)
                    if is_transition(record)
                    else True
                ),
                "version": version.version if version else None,
                "confidence": round(version.confidence.value, 4) if version else None,
                "version_missing": version is None,
                "changes": changes,
                # True when nothing changed except re-fetch bookkeeping: the version
                # exists because the poll produced a different content hash, not
                # because the agency said anything new. Collapsible in the UI, and
                # the basis of the `version_churn` finding.
                "confirmation_only": (
                    not is_transition(record)
                    and bool(changes)
                    and all(c["bookkeeping"] for c in changes)
                ),
                "diff_unavailable": previous is None and record.sequence > 1,
            }
        )
    return steps


def _between(start: str | None, end: str | None) -> float | None:
    first, second = parse_iso(start), parse_iso(end)
    if first is None or second is None:
        return None
    return round((second - first).total_seconds(), 3)


def state_durations(
    audit: list[AuditRecord], current: Event, now: datetime
) -> list[dict[str, Any]]:
    """How long the record spent in each state it has occupied, in order.

    Consecutive steps that do not change state are collapsed: a work zone updated
    forty times while `active` occupied ONE state, and rendering forty identical
    bands would bury the two transitions that matter. ``updates`` keeps the count
    that was collapsed, because "40 confirmations" is itself a trust signal.
    """
    spans: list[dict[str, Any]] = []
    for record in sorted(audit, key=lambda a: a.sequence):
        if spans and spans[-1]["state"] == record.to_state:
            spans[-1]["updates"] += 1
            spans[-1]["last_step"] = record.sequence
            continue
        if spans:
            spans[-1]["exited_at"] = record.recorded_at
        spans.append(
            {
                "state": record.to_state,
                "entered_at": record.recorded_at,
                "exited_at": None,
                "first_step": record.sequence,
                "last_step": record.sequence,
                "updates": 0,
                "trigger_in": record.trigger,
            }
        )

    for span in spans:
        end = span["exited_at"]
        span["seconds"] = (
            _between(span["entered_at"], end) if end else _age_seconds(span["entered_at"], now)
        )
        span["current"] = end is None
    if spans and spans[-1]["state"] != current.lifecycle_state:
        # The audit trail and the `current` pointer disagree about where the record
        # is. Reported here rather than reconciled, and raised as a finding below.
        spans.append(
            {
                "state": current.lifecycle_state,
                "entered_at": current.updated_at,
                "exited_at": None,
                "first_step": None,
                "last_step": None,
                "updates": 0,
                "trigger_in": None,
                "seconds": _age_seconds(current.updated_at, now),
                "current": True,
                "unaudited": True,
            }
        )
    return spans


# ---------------------------------------------------------------------------
# Stages: the five-stage spine
# ---------------------------------------------------------------------------


def build_stages(
    current: Event,
    versions: list[Event],
    audit: list[AuditRecord],
    now: datetime,
    totals: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The record's journey rolled up to five stages, each stating its evidence.

    ``created_at`` is read from the CURRENT version rather than from ``versions[0]``,
    which is only the first version when the whole history was read. It is carried
    forward unchanged on every version precisely so that birth time survives a
    windowed read.
    """
    version_total = (totals or {}).get("versions", len(versions))
    audit_total = (totals or {}).get("audit", len(audit))
    ingest_steps = [a for a in audit if a.trigger == "source_update"]
    payload_refs = sorted({a.trigger_payload_ref for a in audit if a.trigger_payload_ref})
    timer_steps = [a for a in audit if a.trigger in ("timer_ttl", "source_absent")]
    dedup_steps = [a for a in audit if a.trigger == "dedup_decision"]
    operator_steps = [a for a in audit if a.actor == "human"]
    profile = profile_for(current.event_class)
    stale = is_stale(current, now)

    stages = {
        "ingest": {
            "status": "done",
            "at": min((s.retrieved_at for s in current.sources), default=current.created_at),
            "detail": [
                f"{len(current.sources)} source record(s) from "
                f"{len({s.source_id for s in current.sources})} feed(s)",
                f"{len(ingest_steps)} of the step(s) read were driven by a fetched payload",
                f"{len(payload_refs)} distinct raw payload(s) in the raw zone",
            ],
            "evidence": {
                "payload_refs": payload_refs[-10:],
                "payload_ref_count": len(payload_refs),
                "latest_retrieved_at": max((s.retrieved_at for s in current.sources), default=None),
                "latest_source_updated_at": max(
                    (s.source_updated_at or "" for s in current.sources), default=None
                )
                or None,
            },
        },
        "normalize": {
            "status": "warning" if math.isnan(current.extent.begin_measure) else "done",
            "at": current.created_at,
            "detail": [
                f"class {current.event_class} / subtype {current.event_subtype}",
                f"conflated by {current.extent.conflation_method}"
                + (
                    f" (+/- {current.extent.positional_accuracy_meters:.0f} m)"
                    if current.extent.positional_accuracy_meters is not None
                    else ""
                ),
                f"{len(current.lane_impacts)} lane impact(s), "
                f"{sum(1 for lane in current.lane_impacts if lane.inferred)} inferred",
            ],
            "evidence": {
                "conflation_method": current.extent.conflation_method,
                "positional_accuracy_meters": current.extent.positional_accuracy_meters,
                "states": list(current.extent.states),
                "contributed_fields": sorted(
                    {f for s in current.sources for f in s.contributed_fields}
                ),
                # Stated rather than left as an inferrable absence: the normalizer
                # meters and logs mapping issues but does not persist them
                # on the event, so a per-record count is not available from the
                # store and this trace must not imply one exists.
                "mapping_issues_note": (
                    "Mapping issues are metered and logged by the normalizer, not stored "
                    "per event - see the CorridorEventHub/Ingest mapping-issue metrics and the "
                    "normalizer log group. `npm run probe` shows them for a live fetch."
                ),
            },
        },
        "resolve": {
            "status": "done",
            "at": current.created_at,
            "detail": [
                f"event id assigned {current.created_at}",
                f"{version_total} immutable version(s), {audit_total} audit record(s)",
                f"{len(dedup_steps)} dedup decision(s)",
            ],
            "evidence": {
                "version_count": version_total,
                "audit_count": audit_total,
                "dedup_decisions": [
                    {
                        "sequence": a.sequence,
                        "from_state": a.from_state,
                        "to_state": a.to_state,
                        "reason": a.reason,
                        "recorded_at": a.recorded_at,
                    }
                    for a in dedup_steps
                ],
                "field_provenance_fields": sorted(
                    (current.extensions.get("field_provenance") or {}).keys()
                ),
                "retained_alternate_fields": sorted(
                    (current.extensions.get("alternates") or {}).keys()
                ),
                "related_event_ids": list(current.related_event_ids),
            },
        },
        "lifecycle": {
            "status": "warning" if stale else "current",
            "at": current.updated_at,
            "detail": [
                f"state {current.lifecycle_state}, version {current.version}",
                f"{len(timer_steps)} timer/absence step(s), {len(operator_steps)} operator step(s)",
                (
                    "TTL EXPIRED and not yet moved"
                    if stale
                    else f"TTL expires {ttl_expires_at(current) or 'never'}"
                ),
            ],
            "evidence": {
                "state": current.lifecycle_state,
                "ttl_seconds": profile.ttl_seconds.get(current.lifecycle_state),
                "ttl_expires_at": ttl_expires_at(current),
                "ttl_expired": stale,
                "seconds_until_ttl": seconds_until_ttl(current, now),
                "ttl_ladder_next": TTL_LADDER.get(current.lifecycle_state),
                "legal_next_states": legal_targets_from(current.lifecycle_state),
                "reopen_window_seconds": profile.reopen_window_seconds,
                "confidence_half_life_seconds": profile.confidence_half_life_seconds,
            },
        },
        "terminal": {
            "status": ("done" if current.lifecycle_state in TERMINAL_STATES else "pending"),
            "at": current.updated_at if current.lifecycle_state in TERMINAL_STATES else None,
            "detail": (
                [f"{current.lifecycle_state} at {current.updated_at}"]
                if current.lifecycle_state in TERMINAL_STATES
                else [
                    "still live",
                    f"next by timer: {TTL_LADDER.get(current.lifecycle_state) or 'nothing scheduled'}",
                ]
            ),
            "evidence": {
                "state": current.lifecycle_state,
                "archived": current.lifecycle_state == "archived",
                "merged_into": (
                    list(current.related_event_ids) if current.lifecycle_state == "merged" else []
                ),
                # `cleared` is not the end of the road: a recurrence at the same
                # place inside a class window re-opens it, which is common with
                # secondary crashes and re-closures.
                "reopenable_until": _plus_seconds(current.updated_at, profile.reopen_window_seconds)
                if current.lifecycle_state == "cleared"
                else None,
            },
        },
    }

    out = []
    for spec in STAGES:
        out.append({**spec, **stages[spec["stage"]]})
    return out


def _plus_seconds(value: str | None, seconds: int) -> str | None:
    from datetime import timedelta

    from .timeutil import iso_utc

    moment = parse_iso(value)
    return iso_utc(moment + timedelta(seconds=seconds)) if moment else None


# ---------------------------------------------------------------------------
# Findings: what is WRONG with this record, said plainly
# ---------------------------------------------------------------------------
#
# THIS IS THE PART THAT EARNS THE VIEW. A timeline that only renders what the store
# says is a pretty way to read DynamoDB. The questions an operator actually has are
# "is this record's timer dead", "does its audit trail have a hole", "is this
# published on the strength of one witness" - and every one of those is answerable
# from the trace, so it is answered here rather than left to a reader's eye.


def findings(
    current: Event,
    versions: list[Event],
    audit: list[AuditRecord],
    now: datetime,
    steps: list[dict[str, Any]] | None = None,
    totals: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """What is wrong with this record, in severity-agnostic order of discovery.

    ``totals`` carries the true version and audit counts when the caller read only a
    window of a long history. Without it, a windowed read would report every long
    record as having an audit gap - the checks below have to know the difference
    between "sequence 1 is missing" and "sequence 1 was not read".
    """
    out: list[dict[str, Any]] = []
    windowed = bool(totals and totals.get("windowed"))

    def add(code: str, severity: str, detail: str) -> None:
        out.append({"code": code, "severity": severity, "detail": detail})

    sequences = sorted(a.sequence for a in audit)
    # Contiguity is checked WITHIN whatever range was read, so a window starting at
    # sequence 1400 is judged against 1400..1564 rather than against 1..164.
    expected = list(range(sequences[0], sequences[0] + len(sequences))) if sequences else []
    if sequences and sequences != expected:
        missing = sorted(set(range(sequences[0], max(sequences) + 1)) - set(sequences))
        add(
            "audit_gap",
            "error",
            f"audit sequences are not contiguous - missing {missing}. An append-only "
            f"trail with holes cannot support an incident review.",
        )
    version_total = (totals or {}).get("versions", len(versions))
    audit_total = (totals or {}).get("audit", len(audit))
    if version_total != audit_total:
        add(
            "version_audit_mismatch",
            "error",
            f"{version_total} version(s) but {audit_total} audit record(s). The event store "
            f"writes both in one transaction, so a mismatch means a write path bypassed it.",
        )
    if not audit and not windowed:
        add(
            "no_audit_trail",
            "error",
            "no audit records at all for this event id.",
        )
    if current.version != version_total:
        add(
            "version_count_mismatch",
            "warn",
            f"the current pointer is at version {current.version} but {version_total} version "
            f"item(s) are stored. Versions are monotonic per event id, so the two should agree.",
        )

    for record in audit:
        # Legality is only meaningful for a step that MOVED the record: a
        # confirmation has no edge to look up. Everything after it applies to every
        # audit record, transition or not - an unattributed operator action is a
        # finding whether or not it changed the state.
        if is_transition(record) and not is_legal_transition(
            record.from_state, record.to_state, record.trigger
        ):
            add(
                "illegal_transition_stored",
                "error",
                f"step {record.sequence}: {record.from_state} -> {record.to_state} on "
                f"{record.trigger} is not in the transition table. Illegal transitions are "
                f"supposed to be rejected, not stored.",
            )
        if record.trigger not in TRANSITION_TRIGGERS:
            add(
                "unknown_trigger",
                "warn",
                f"step {record.sequence} carries trigger {record.trigger!r}, which is not "
                f"in the declared trigger set.",
            )
        if record.actor == "human" and not record.operator_id:
            add(
                "operator_unattributed",
                "error",
                f"step {record.sequence} was made by a human with no operator_id recorded.",
            )
        if record.to_state not in LIFECYCLE_STATES:
            add(
                "unknown_state",
                "error",
                f"step {record.sequence} moved to {record.to_state!r}, not one of the seven "
                f"declared states.",
            )

    # Version churn. Found by this tool against live data on the first run:
    # an Oklahoma work zone had 1,564 immutable versions in 26 hours - one per
    # 60-second poll - and every diff contained nothing but `raw_ref` and
    # `retrieved_at` moving. The cause is upstream and documented in
    # adapters/ok_odot_wzdx.regenerated_end_date_minutes: ODOT computes `end_date` as
    # request-time + fixed offset, so the candidate's content hash differs on every
    # fetch even though the canonical record is identical. Idempotency then cannot
    # fire, and "how many times did this event actually change" - the question the
    # version chain answers - is buried under 1,563 confirmations.
    confirmations = [s for s in (steps or []) if s.get("confirmation_only")]
    sampled = [s for s in (steps or []) if s.get("changes") and not s.get("transition")]
    if len(sampled) >= 10 and len(confirmations) >= 0.8 * len(sampled):
        add(
            "version_churn",
            "warn",
            f"{len(confirmations)} of the {len(sampled)} sampled non-transition step(s) changed "
            f"nothing but re-fetch bookkeeping, across {version_total} version(s) total. Each "
            f"poll is writing an immutable version for a record the agency did not change, so "
            f"the content-hash idempotency is not firing - the usual cause is a source field "
            f"the feed regenerates on every request, which changes the content hash without "
            f"changing the record. The adapter for this source names the field if it knows "
            f"about the defect.",
        )

    if is_stale(current, now) and current.lifecycle_state in TTL_LADDER:
        seconds = _age_seconds(ttl_expires_at(current), now)
        add(
            "ttl_expired_not_moved",
            "warn" if (seconds or 0) < 3600 else "error",
            f"TTL for {current.lifecycle_state} lapsed "
            f"{_human(seconds)} ago and the record has not moved to "
            f"{TTL_LADDER[current.lifecycle_state]}. Briefly this is the gap between a "
            f"deadline and the tick that acts on it; persistently it means this event's "
            f"timer chain died (watch CorridorEventHub-lifecycle-executions-failed).",
        )

    if math.isnan(current.extent.begin_measure):
        add(
            "unresolved_extent",
            "warn",
            "no corridor measure: the record is stored and readable by id, but it is absent "
            "from the corridor index, so no corridor or look-ahead query can return it.",
        )

    if current.lifecycle_state == "merged" and not current.related_event_ids:
        add(
            "merged_without_parent",
            "error",
            "state is `merged` but no related event id records what it merged into, so the "
            "un-merge path has nothing to restore from.",
        )

    if current.lifecycle_state in ("active", "validated") and len(_independent_groups(current)) < 2:
        add(
            "single_witness",
            "info",
            f"published on one independent source ({', '.join(_independent_groups(current))}). "
            f"Corroboration is capped for this record - a second feed in the same "
            f"independence group would not change that.",
        )

    for source in current.sources:
        retrieved = parse_iso(source.retrieved_at)
        updated = parse_iso(source.source_updated_at)
        if retrieved and updated and updated > retrieved:
            add(
                "clock_skew",
                "warn",
                f"{source.source_id} says the record changed at {source.source_updated_at}, "
                f"which is after we fetched it at {source.retrieved_at}.",
            )
        slo = _freshness_slo(source.source_id)
        basis = source.source_updated_at or source.retrieved_at
        age = _age_seconds(basis, now)
        if slo and age is not None and age > slo:
            add(
                "source_behind_slo",
                "warn",
                f"{source.source_id} last confirmed this record {_human(age)} ago, past its "
                f"{_human(float(slo))} freshness SLO. Confidence decays with it.",
            )

    return out


def _freshness_slo(source_id: str) -> int | None:
    """The per-feed staleness threshold from the source catalog.

    Per feed rather than global, because an hour of silence is nothing from a daily
    work-zone feed and a fault from a 60-second one. Read from config rather than
    hardcoded; a source with no SLO declared gets no finding rather than a
    guessed one.
    """
    try:
        for source in load_json("sources.json").get("sources", []):
            if source.get("sourceId") == source_id:
                value = source.get("freshnessSloSeconds")
                return int(value) if value else None
    except (FileNotFoundError, KeyError, ValueError):
        return None
    return None


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "an unknown time"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ---------------------------------------------------------------------------
# The trace
# ---------------------------------------------------------------------------


def build_trace(
    current: Event,
    versions: list[Event],
    audit: list[AuditRecord],
    now: datetime,
    milepost: Any = None,
    totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything known about one record's life, in one document.

    ``versions`` and ``audit`` are what ``EventStore.history`` returns. They are
    passed in rather than fetched so this stays testable without an account, and so
    the caller decides what a read costs.

    ``totals`` describes a WINDOWED read - the most recent N of a longer history -
    and it is required whenever the caller windowed, because a document that shows
    164 of 1,564 versions without saying so is a lie of omission about exactly the
    thing this view is for. The window is reported in `window`, and `counts` always
    carries the true totals.
    """
    ordered = sorted(versions, key=lambda v: v.version)
    trail = sorted(audit, key=lambda a: a.sequence)
    steps = build_steps(ordered, trail)
    return {
        "event_id": current.event_id,
        "summary": record_summary(current, now, milepost=milepost),
        "current": to_jsonable(current),
        "stages": build_stages(current, ordered, trail, now, totals=totals),
        "steps": steps,
        "states": state_durations(trail, current, now),
        "findings": findings(current, ordered, trail, now, steps=steps, totals=totals),
        "window": {
            "windowed": bool(totals and totals.get("windowed")),
            "steps_shown": len(steps),
            "steps_total": (totals or {}).get("audit", len(trail)),
            "first_sequence_shown": trail[0].sequence if trail else None,
            "note": (
                f"showing the most recent {len(steps)} of "
                f"{(totals or {}).get('audit', len(trail))} steps - earlier history is stored "
                f"and readable, it was not read"
                if totals and totals.get("windowed")
                else None
            ),
        },
        "sources": [to_jsonable(s) for s in current.sources],
        # Which source won each field, and what the losers said. Both
        # halves, because "source A won" without the values is an assertion.
        "field_provenance": current.extensions.get("field_provenance") or {},
        "alternates": current.extensions.get("alternates") or {},
        "extensions": {
            k: v
            for k, v in current.extensions.items()
            if k not in ("field_provenance", "alternates")
        },
        "counts": {
            "versions": (totals or {}).get("versions", len(ordered)),
            "audit": (totals or {}).get("audit", len(trail)),
            "versions_read": len(ordered),
            "audit_read": len(trail),
            "sources": len(current.sources),
            "payload_refs": len({a.trigger_payload_ref for a in trail if a.trigger_payload_ref}),
        },
    }
