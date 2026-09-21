"""Resolution policy - candidates in, event versions out.

This is the module ``core/matcher.py`` was always pointing at. The matcher answers
"are these two the same event?"; this answers "so what do we WRITE?" - and the
answer is a chain of versions, each with an audit record - the shape a bitemporal
audit trail needs.

PURE ON PURPOSE. Nothing here touches DynamoDB, EventBridge, or the clock unless
it is handed one. ``handlers/resolver.py`` is the thin shell that does the I/O.
That split is what makes the interesting cases - a cross-agency merge, a late
update, an un-mergeable ambiguity - testable without an AWS account, which is the
same reason ``core/lrs.py`` keeps conflation behind a seam.

THE FOUR OUTCOMES, and what each one is for:

  created   a candidate nothing in the store matches becomes a new event, and
            walks reported -> validated -> active as far as its own data allows
           .
  updated   the same source record reported again: a new version of the event it
            already produced, or nothing at all if the bytes are unchanged
            (idempotency).
  merged    a DIFFERENT source's record matched an existing event above the merge
            threshold. Both events persist: the child in `merged`, the parent
            with the child's source unioned into its provenance. Keeping the child
            is not bookkeeping - an un-merge has to restore the child's own
            identity and history, which is impossible if the merge dissolved
            it.
  review    the score landed in the ambiguous band. Both events stay separate and
            separately published, and the PAIR goes to a queue. Guessing
            in the middle is the failure mode that makes a dedup claim
            untrustworthy.

EVERY AUDIT RECORD'S SEQUENCE EQUALS THE VERSION IT PRODUCED. One invariant,
deliberately chosen over the more economical alternative of versioning only
attribute changes: it means `v#7` and `audit#7` describe the same moment, so
reconstructing the event as of any instant is a single query with no
join and no interleaving rule to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from .confidence import (
    DEFAULT_RELIABILITY,
    SEED_SOURCE_RELIABILITY,
    ScoringInput,
    score_confidence,
)
from .derive import derive_duration, derive_severity
from .lifecycle import AuditRecord, is_legal_transition, profile_for
from .matcher import MERGE_THRESHOLD, MatchScore, score_match
from .timeutil import duration_minutes, iso_utc, parse_iso
from .types import CandidateEvent, Confidence, Event, Extent, LaneImpact

RESOLVER_POLICY_VERSION = "0.1.0"

#: Lifecycle states a new candidate may match against. A `cleared` event is
#: deliberately excluded: a recurrence at the same location is a RE-OPEN
#: (`cleared -> active`), which is a different decision from a merge and is
#: driven by the source that owns the event rather than by a match score.
MATCHABLE_STATES = ("reported", "validated", "active", "clearing")

#: Confidence floor for `reported -> validated`, per class where it differs.
#:
#: The transition rationale in core/lifecycle.py says "confidence above class
#: threshold", and this is that threshold. Set low: the gate exists to stop a
#: record that could not be located or timed from ever being published, not to
#: express editorial taste about weak reports - a low-confidence event WITH its
#: breakdown attached is exactly what an integrator needs in order to threshold for
#: itself. The per-class values are uncalibrated and a DOT can correct them.
MIN_VALIDATION_CONFIDENCE: dict[str, float] = {
    # Sensed and derived classes carry lower single-source confidence by
    # construction (no corroboration, coarse conflation), so a shared floor would
    # reject them systematically.
    "congestion": 0.25,
    "weather": 0.25,
    "road_surface": 0.25,
}

DEFAULT_MIN_VALIDATION_CONFIDENCE = 0.35


@dataclass
class ReviewItem:
    """An ambiguous pair, queued, with both events still live."""

    event_id: str
    other_event_id: str
    score: float
    explanation: list[str]
    queued_at: str
    reason: str = "match_score_in_ambiguous_band"


@dataclass
class Step:
    """One new version and the audit record that explains it."""

    event: Event
    audit: AuditRecord


@dataclass
class EventChange:
    """Everything to persist for one event id, in order."""

    event_id: str
    steps: list[Step]
    created: bool = False

    @property
    def final(self) -> Event:
        return self.steps[-1].event


@dataclass
class Resolution:
    action: str  # created | updated | merged | review | unchanged | late_ignored
    changes: list[EventChange] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    # The API has to be able to say WHY, so the reasoning is a value.
    explanation: list[str] = field(default_factory=list)
    subject_event_id: str | None = None
    matched_event_id: str | None = None
    match: MatchScore | None = None
    policy_version: str = RESOLVER_POLICY_VERSION


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def is_stale(event: Event, now: datetime) -> bool:
    """Has this event outlived the TTL for the state it is in?

    READ-ONLY, and used in three places for three different reasons:

      1. ``handlers/lifecycle.py`` acts on it - that is the timer, and it is what
         makes TTL expiry real rather than advisory.
      2. The resolver refuses to match a new report against a stale event. Without
         this, a crash whose TTL lapsed hours ago silently absorbs a fresh crash at
         the same milepost and the fresh one inherits the old one's history.
      3. The query API labels a stale event as stale, which covers the interval
         between a deadline passing and the tick that acts on it. A persistently
         stale event means that event's timer chain died - watched by the
         CorridorEventHub-lifecycle-executions-failed alarm.
    """
    ttl_seconds = profile_for(event.event_class).ttl_seconds.get(event.lifecycle_state)
    if ttl_seconds is None:
        return False
    updated = parse_iso(event.updated_at)
    if updated is None:
        return False
    return now > updated + timedelta(seconds=ttl_seconds)


def ttl_expires_at(event: Event) -> str | None:
    ttl_seconds = profile_for(event.event_class).ttl_seconds.get(event.lifecycle_state)
    updated = parse_iso(event.updated_at)
    if ttl_seconds is None or updated is None:
        return None
    return iso_utc(updated + timedelta(seconds=ttl_seconds))


def seconds_until_ttl(event: Event, now: datetime) -> int | None:
    """How long until this event's current state expires. None means never.

    Never happens for two reasons that are worth distinguishing but need the same
    answer: a terminal state has no TTL in the profile at all, and a
    `dimensional_restriction` in `active` has one measured in years. Either way
    there is nothing to wait for that a fresh source update will not reset.
    """
    ttl_seconds = profile_for(event.event_class).ttl_seconds.get(event.lifecycle_state)
    updated = parse_iso(event.updated_at)
    if ttl_seconds is None or updated is None:
        return None
    remaining = (updated + timedelta(seconds=ttl_seconds) - now).total_seconds()
    return max(0, int(remaining))


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

#: Where a `timer_ttl` expiry sends an event. Every edge here is already in the
#: transition table in core/lifecycle.py with `timer_ttl` among its triggers - this
#: is not a second opinion about legality, it is which of the legal edges the TIMER
#: takes. ``_advance`` still checks the table, so a divergence fails loudly.
#:
#: THE LADDER ENDS AT `cleared`, NOT `archived`. Archiving is a retention decision
#: (`retention_rule`), on a completely different clock and driven by policy rather
#: than by silence, so the timer stops when the event stops affecting traffic.
TTL_LADDER: dict[str, str] = {
    # Never corroborated, never geolocated: closed without ever publishing.
    "reported": "cleared",
    # Its active window arrived while nothing new was heard.
    "validated": "active",
    # THE IMPORTANT ONE. This is the edge that fixes the known failure
    # of existing 511 feeds: a condition that is no longer being reported stops
    # being published as live. It goes to `clearing` rather than straight to
    # `cleared` because silence is not confirmation - see ADR 0003.
    "active": "clearing",
    "clearing": "cleared",
}

#: The reason a TTL expiry records. Kept as a constant because it
#: crosses into audit records that outlive this code.
STALE_REASON = "stale_no_updates"


def expire(
    event: Event,
    now: datetime,
    payload_ref: str | None = None,
) -> Resolution:
    """Apply the TTL ladder if this event has gone quiet for too long.

    "Every state SHALL have a TTL. Expiry moves the event to `clearing` then
    `cleared` with a `stale_no_updates` reason rather than leaving it active
    indefinitely - a leading failure mode of state 511 feeds."

    RETURNS A RESOLUTION EVEN WHEN NOTHING IS DUE, with an action naming why, so the
    caller has one shape to handle and the "nothing happened" cases are as
    inspectable as the transitions:

      terminal      no TTL for this state; the timer should stop
      waiting       not expired yet
      not_yet_due   expired, but the event is not eligible for its ladder edge -
                    currently only a `validated` event whose start time is still in
                    the future. Re-arm rather than publish it early.
      expired       the transition was applied
    """
    state = event.lifecycle_state
    to_state = TTL_LADDER.get(state)
    if to_state is None:
        return Resolution(
            action="terminal",
            explanation=[f"{state} has no TTL: nothing expires from here"],
            subject_event_id=event.event_id,
        )

    if not is_stale(event, now):
        return Resolution(
            action="waiting",
            explanation=[f"{state} expires at {ttl_expires_at(event)}"],
            subject_event_id=event.event_id,
        )

    # `validated -> active` is a PUBLISHING decision, not a decay one, and the TTL
    # firing does not make a future-dated event current. A work zone scheduled for
    # next month whose feed went quiet must not be published as live traffic
    # impact; re-arming is the conservative answer and it is recorded as such.
    if to_state == "active" and not _publishable_now(event, now):
        return Resolution(
            action="not_yet_due",
            explanation=[
                f"TTL lapsed in {state} but the event starts at {event.start_time}, "
                "which is still in the future: not published early"
            ],
            subject_event_id=event.event_id,
        )

    reason = (
        f"{STALE_REASON}: no source update in the {state} TTL for "
        f"{event.event_class} (expired {ttl_expires_at(event)})"
    )
    if to_state == "active":
        reason = "active time window reached with no further source update"

    step = _advance(event, to_state, "timer_ttl", "timer", reason, now, payload_ref)
    return Resolution(
        action="expired",
        changes=[EventChange(event_id=event.event_id, steps=[step])],
        explanation=[
            f"{state} -> {to_state} on timer_ttl ({reason})",
            # Confidence has been decaying the whole time this was quiet, so
            # the state change is not the only signal that trust has dropped.
            f"confidence at expiry {event.confidence.value:.4f}",
        ],
        subject_event_id=event.event_id,
    )


# ---------------------------------------------------------------------------
# Candidate <-> Event
# ---------------------------------------------------------------------------


def candidate_view(event: Event, source_index: int = 0) -> CandidateEvent:
    """An Event seen as a CandidateEvent, for re-scoring after a merge.

    ``score_confidence`` takes a candidate because it runs first in the normalizer,
    but a merged event needs re-scoring with its full source list (corroboration is
    the whole point). Rather than duplicate the scorer for a second input type,
    this projects the event back down - every field the scorer reads exists on
    both types, which is what makes the projection total rather than lossy.
    """
    return CandidateEvent(
        event_class=event.event_class,
        event_subtype=event.event_subtype,
        extent=event.extent,
        lane_impacts=event.lane_impacts,
        start_time=event.start_time,
        end_time=event.end_time,
        time_confidence=event.time_confidence,
        agency_severity=event.severity.agency_asserted,
        agency_duration_minutes=event.expected_duration.estimate_minutes,
        source=event.sources[source_index],
        extensions=event.extensions,
    )


def _contributed(candidate: CandidateEvent) -> list[str]:
    """Which canonical fields this source actually supplied.

    Absence is recorded by omission rather than by a null: a source that says
    nothing about lane impacts has not contributed an empty list, it has
    contributed nothing, and the difference decides who wins the field at merge.
    """
    fields = ["event_class", "extent", "start_time"]
    if candidate.event_subtype:
        fields.append("event_subtype")
    if candidate.lane_impacts:
        fields.append("lane_impacts")
    if candidate.end_time:
        fields.append("end_time")
    if candidate.agency_severity:
        fields.append("agency_severity")
    if candidate.agency_duration_minutes is not None:
        fields.append("agency_duration_minutes")
    return fields


def _source_with_contributions(candidate: CandidateEvent) -> Any:
    source = candidate.source
    if source.contributed_fields:
        return source
    # The adapters do not all populate this; filling it here keeps provenance
    # complete without making every adapter restate the same derivation.
    return replace(source, contributed_fields=_contributed(candidate))


def event_from_candidate(
    candidate: CandidateEvent,
    confidence: Confidence,
    event_id: str,
    now: datetime,
) -> Event:
    """Version 1 of a new event, in `reported`.

    ALWAYS `reported`, never `active`, even for a report that will pass every gate
    a microsecond later. The state chain is what the audit trail is FOR: an event
    that appears already-active has no recorded moment of having been validated,
    and the audit trail carries the trigger and actor behind every state it held.
    """
    at = iso_utc(now)
    return Event(
        event_id=event_id,
        event_class=candidate.event_class,
        event_subtype=candidate.event_subtype,
        lifecycle_state="reported",
        version=1,
        extent=candidate.extent,
        lane_impacts=list(candidate.lane_impacts),
        severity=derive_severity(
            candidate.event_class, candidate.lane_impacts, candidate.agency_severity
        ),
        confidence=confidence,
        expected_duration=derive_duration(
            candidate.event_class,
            candidate.agency_duration_minutes,
            duration_minutes(candidate.start_time, candidate.end_time),
        ),
        start_time=candidate.start_time,
        end_time=candidate.end_time,
        time_confidence=candidate.time_confidence,
        sources=[_source_with_contributions(candidate)],
        related_event_ids=[],
        created_at=at,
        updated_at=at,
        # Nothing an adapter carried is dropped on the way into the store.
        extensions=dict(candidate.extensions),
    )


# ---------------------------------------------------------------------------
# Field precedence and merge explanation
# ---------------------------------------------------------------------------
#
# When two agencies describe one event they will disagree about details. The rule is
# specific: resolve by a DOCUMENTED precedence policy - source reliability,
# specificity, recency - and keep the losing values as alternates rather than
# discarding them.
#
# Discarding is the tempting shortcut and it is unrecoverable. The losing value is
# the evidence that two agencies disagreed, which is a finding about a crosswalk or
# a feed, and it is also what an un-merge needs in order to restore the child.


@dataclass
class FieldClaim:
    """One source's claim on one field, with everything precedence needs."""

    field: str
    value: Any
    source_id: str
    agency: str
    updated_at: str | None
    #: How the SOURCE characterized its own timestamps: observed, estimated, or
    #: scheduled. Carried here because a timestamp string cannot say how
    #: firm it is, and for the time fields that is the whole of their specificity -
    #: without it, "an observed start time beats an estimated one" is a comment
    #: rather than behaviour.
    time_confidence: str | None = None

    @property
    def reliability(self) -> float:
        return SEED_SOURCE_RELIABILITY.get(self.source_id, DEFAULT_RELIABILITY)


# Specificity per field: how much a value actually SAYS. Higher wins.
#
# This is the component that stops "most reliable source" from meaning "most
# reliable source's vaguest answer". A high-reliability feed reporting an
# unresolved extent must not beat a lower-reliability feed that gave coordinates.

_CONFLATION_SPECIFICITY = {
    "native_lrs": 1.0,
    "milepost": 0.9,
    "coordinate": 0.85,
    "sensor_snap": 0.7,
    "polygon_intersect": 0.5,
    "text_geocode": 0.3,
    "unresolved": 0.0,
}

#: How firm a source's own timestamps are. An observed time is a
#: measurement, a scheduled one is a plan, and a merge that cannot tell them apart
#: will let a work zone's planned start overwrite an observed crash time.
_TIME_CONFIDENCE_SPECIFICITY = {"observed": 1.0, "estimated": 0.6, "scheduled": 0.4}

_TIME_FIELDS = ("start_time", "end_time")


def _specificity(claim: FieldClaim) -> float:
    value = claim.value
    if value is None or value == "" or value == []:
        return 0.0
    if claim.field in _TIME_FIELDS:
        # A timestamp string cannot say how firm it is, so the source's own
        # time_confidence is its specificity. Unstated falls to the middle rather
        # than to zero: an unlabelled timestamp is still a timestamp.
        return _TIME_CONFIDENCE_SPECIFICITY.get(claim.time_confidence or "", 0.6)
    if claim.field == "extent" and isinstance(value, Extent):
        base = _CONFLATION_SPECIFICITY.get(value.conflation_method, 0.5)
        accuracy = value.positional_accuracy_meters
        if accuracy is not None and accuracy > 1000:
            # A kilometre-scale extent is a claim about the county, not the road.
            base -= 0.2
        return max(0.0, base)
    if claim.field == "lane_impacts" and isinstance(value, list):
        # Stated lane impacts outrank inferred ones, and more lanes
        # described is a more specific claim than fewer.
        inferred = any(isinstance(lane, LaneImpact) and lane.inferred for lane in value)
        return min(1.0, 0.4 + 0.1 * len(value)) * (0.6 if inferred else 1.0)
    return 0.7  # a stated scalar: specific enough, and ranked by reliability


def _recency_key(claim: FieldClaim) -> float:
    parsed = parse_iso(claim.updated_at)
    return parsed.timestamp() if parsed else 0.0


_FACTORS: dict[str, Callable[[FieldClaim], float]] = {
    "reliability": lambda c: round(c.reliability, 4),
    "specificity": lambda c: round(_specificity(c), 4),
    "recency": _recency_key,
}

#: Three factors decide precedence - source reliability, specificity, recency - and
#: their ORDER is where it stops being obvious. This table is that decision, per
#: field, declared as data.
#:
#: ONE GLOBAL ORDER GETS THE EXTENT WRONG, and it is worth stating the case because
#: it looks fine until you try it. Reading the factors in their default order puts
#: reliability first, and reliability first means a weather alert
#: from a 0.95-reliability national feed - a polygon covering a whole county,
#: positional accuracy in kilometres - overwrites a 160-metre milepost from a
#: 0.8-reliability state feed. The most trustworthy source's vaguest answer beats a
#: less trustworthy source's precise one, and the event moves several miles. For an
#: over-height or lane-blocked hazard that is the difference between something a
#: truck can act on and something it cannot.
#:
#: So: WHERE the claim is a measurement, specificity leads. Where it is an OPINION,
#: reliability leads - which agency's judgement to trust is exactly what a
#: reliability score is for. Where the claim is a PLAN that gets revised, recency
#: leads, because the latest revision is the operative one.
_PRECEDENCE: dict[str, tuple[str, ...]] = {
    # Measurements. How precisely a source located something is a property of its
    # answer, not of the source's reputation.
    "extent": ("specificity", "reliability", "recency"),
    "lane_impacts": ("specificity", "reliability", "recency"),
    # An observed start time beats an estimated one whoever reported it. Recency
    # must NOT lead here: when a crash started is not revised by a later poll.
    "start_time": ("specificity", "reliability", "recency"),
    # A plan. An end time is an estimate that gets revised, and the newest estimate
    # is the one a consumer wants.
    "end_time": ("recency", "specificity", "reliability"),
    # Opinions, in the source's own vocabulary. Which agency to believe is the
    # question a reliability score answers.
    "event_subtype": ("reliability", "specificity", "recency"),
    "agency_severity": ("reliability", "specificity", "recency"),
}

#: For a field with no entry, the default order.
DEFAULT_PRECEDENCE = ("reliability", "specificity", "recency")


def precedence_for(field_name: str) -> tuple[str, ...]:
    return _PRECEDENCE.get(field_name, DEFAULT_PRECEDENCE)


def choose_field(
    claims: list[FieldClaim], order: tuple[str, ...] | None = None
) -> tuple[FieldClaim, list[FieldClaim]]:
    """Rank contending claims by this field's precedence order.

    Empty claims are dropped BEFORE ranking rather than losing on specificity, so
    "said nothing" can never beat "said something" through a tie-break on another
    factor - which is how a silent high-reliability source would otherwise blank a
    field that another source had filled in.
    """
    factors = order or DEFAULT_PRECEDENCE
    stated = [c for c in claims if _specificity(c) > 0.0] or claims
    ranked = sorted(
        stated,
        key=lambda c: tuple(_FACTORS[name](c) for name in factors),
        reverse=True,
    )
    return ranked[0], ranked[1:]


# How each reconcilable field is read off a candidate and off an event, and how the
# winner is written back. Declared as a table rather than a chain of ifs for the
# same reason the transition set is: it can be enumerated, published, and reasoned
# about by someone who is not reading the merge function line by line.
_RECONCILED: dict[str, tuple[Callable[[CandidateEvent], Any], Callable[[Event], Any]]] = {
    "extent": (lambda c: c.extent, lambda e: e.extent),
    "lane_impacts": (lambda c: list(c.lane_impacts), lambda e: list(e.lane_impacts)),
    "event_subtype": (lambda c: c.event_subtype, lambda e: e.event_subtype),
    "start_time": (lambda c: c.start_time, lambda e: e.start_time),
    "end_time": (lambda c: c.end_time, lambda e: e.end_time),
    "agency_severity": (lambda c: c.agency_severity, lambda e: e.severity.agency_asserted),
}


def _existing_claims(event: Event, name: str, superseded_by: str) -> list[FieldClaim]:
    """Reconstruct the claims already on an event: the winner plus any alternates.

    The winner's owner comes from ``extensions['field_provenance']``, which is what
    records "which source won each field". Absent it - an event written
    before a merge ever happened - the event's first source is the only claimant,
    which is correct for a single-source event.

    ``superseded_by`` IS THE SOURCE ABOUT TO SPEAK, and every claim it made earlier
    is dropped. A source revising its own record replaces its own previous word; it
    does not compete with it.

    Without this, a source WITHDRAWING a detail could never take effect. An agency
    that reported a closed lane and then reopened it sends a record with no lane
    impacts - an empty claim, which loses to any stated one, including its own
    earlier one. The lane would stay closed in the store for as long as the event
    lived, sourced to an agency that had already said otherwise, and nothing would
    look wrong. Same for an end time being cleared, or a severity downgraded to
    nothing.
    """
    _of_candidate, of_event = _RECONCILED[name]
    provenance = (event.extensions.get("field_provenance") or {}).get(name)
    owner = next(
        (s for s in event.sources if s.source_id == provenance),
        event.sources[0] if event.sources else None,
    )
    claims = []
    if owner is not None and owner.source_id != superseded_by:
        claims.append(
            FieldClaim(
                field=name,
                value=of_event(event),
                source_id=owner.source_id,
                agency=owner.agency,
                updated_at=owner.source_updated_at or owner.retrieved_at,
                time_confidence=event.time_confidence,
            )
        )
    for alternate in (event.extensions.get("alternates") or {}).get(name, []):
        # Alternates round-trip through JSON, so their values are plain data. Only
        # scalar alternates are re-contended; a structured one (an extent, a lane
        # list) would need rehydrating and is kept for the record rather than
        # re-ranked.
        if alternate.get("source_id") == superseded_by:
            continue
        if isinstance(alternate.get("value"), (str, int, float, bool, type(None))):
            claims.append(
                FieldClaim(
                    field=name,
                    value=alternate.get("value"),
                    source_id=alternate.get("source_id", ""),
                    agency=alternate.get("agency", ""),
                    updated_at=alternate.get("updated_at"),
                    time_confidence=alternate.get("time_confidence"),
                )
            )
    return claims


def reconcile(
    event: Event, candidate: CandidateEvent
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, str], list[str]]:
    """Resolve every contested field between an event and a joining candidate.

    Returns ``(winners, alternates, field_provenance, explanation)``.
    """
    winners: dict[str, Any] = {}
    alternates: dict[str, list[dict[str, Any]]] = {}
    provenance: dict[str, str] = {}
    explanation: list[str] = []

    for name, (of_candidate, _of_event) in _RECONCILED.items():
        claims = _existing_claims(event, name, superseded_by=candidate.source.source_id)
        claims.append(
            FieldClaim(
                field=name,
                value=of_candidate(candidate),
                source_id=candidate.source.source_id,
                agency=candidate.source.agency,
                updated_at=candidate.source.source_updated_at or candidate.source.retrieved_at,
                time_confidence=candidate.time_confidence,
            )
        )
        order = precedence_for(name)
        winner, losers = choose_field(claims, order)
        winners[name] = winner.value
        provenance[name] = winner.source_id

        # A loser is only a CONFLICT if it actually said something different.
        contested = [
            loser
            for loser in losers
            if _specificity(loser) > 0.0 and not _same_value(loser.value, winner.value)
        ]
        if contested:
            alternates[name] = [
                {
                    "value": _plain(loser.value),
                    "source_id": loser.source_id,
                    "agency": loser.agency,
                    "updated_at": loser.updated_at,
                    # Kept so a re-contended alternate ranks the same way it did the
                    # first time. Dropping it would silently re-score a retained
                    # time claim as merely "unstated" on the next merge.
                    "time_confidence": loser.time_confidence,
                }
                for loser in contested
            ]
            explanation.append(
                f"{name}: {winner.source_id} won on {' > '.join(order)} "
                f"(reliability {winner.reliability:.2f}, "
                f"specificity {_specificity(winner):.2f}); "
                f"{len(contested)} alternate(s) retained from "
                f"{', '.join(sorted({loser.source_id for loser in contested}))}"
            )

    return winners, alternates, provenance, explanation


def _same_value(a: Any, b: Any) -> bool:
    from .serde import to_jsonable

    return to_jsonable(a) == to_jsonable(b)


def _plain(value: Any) -> Any:
    from .serde import to_jsonable

    return to_jsonable(value)


# ---------------------------------------------------------------------------
# Building the version chain
# ---------------------------------------------------------------------------


def _audit(
    event: Event,
    from_state: str | None,
    trigger: str,
    actor: str,
    reason: str,
    now: datetime,
    trigger_payload_ref: str | None,
    operator_id: str | None = None,
) -> AuditRecord:
    """One audit record. ``sequence`` is the version it produced - see the module
    docstring on why that invariant is worth the extra versions.
    """
    return AuditRecord(
        event_id=event.event_id,
        sequence=event.version,
        from_state=from_state,
        to_state=event.lifecycle_state,
        trigger=trigger,
        actor=actor,
        trigger_payload_ref=trigger_payload_ref,
        rule_version=RESOLVER_POLICY_VERSION,
        reason=reason,
        # Two clocks. `occurred_at` is when the source says the thing
        # happened; `recorded_at` is when we wrote it down.
        occurred_at=event.updated_at,
        recorded_at=iso_utc(now),
        operator_id=operator_id,
    )


def _advance(
    event: Event,
    to_state: str,
    trigger: str,
    actor: str,
    reason: str,
    now: datetime,
    payload_ref: str | None,
) -> Step:
    """Move an event to a new state as a new version, rejecting illegal edges.

    Reject, do not coerce. An illegal transition raising here means the handler's
    DLQ gets the payload and the alarm fires - rejected and alarmed. Silently
    clamping to a legal state would be exactly the coercion to avoid.
    """
    from_state = event.lifecycle_state
    if from_state != to_state and not is_legal_transition(from_state, to_state, trigger):
        raise IllegalTransition(
            f"{event.event_id}: {from_state} -> {to_state} on {trigger} is not in the "
            "transition table (core/lifecycle.py)"
        )
    moved = _revise(event, now, lifecycle_state=to_state)
    return Step(event=moved, audit=_audit(moved, from_state, trigger, actor, reason, now, payload_ref))


class IllegalTransition(Exception):
    """An illegal transition is rejected and alarmed, never coerced."""


def _revise(event: Event, now: datetime, **changes: Any) -> Event:
    """A new version of an event. Never mutates the old one - the old version is
    an immutable historical record.
    """
    return replace(
        event,
        version=event.version + 1,
        updated_at=iso_utc(now),
        **changes,
    )


def _validation_failure(event: Event, confidence: Confidence) -> str | None:
    """Why this event cannot be validated, or None if it can.

    The three checks named in the `reported -> validated` rationale: spatial,
    plausibility, and confidence. Schema validity is enforced earlier, by the
    adapter contract and the canonical types.
    """
    import math

    if math.isnan(event.extent.begin_measure) or not event.extent.states:
        return "never geolocated: extent did not conflate onto the corridor"

    start = parse_iso(event.start_time)
    if start is None:
        return "unparseable start time: lifecycle timers cannot be set"

    end = parse_iso(event.end_time) if event.end_time else None
    if end is not None and end < start:
        return "implausible time window: end precedes start"

    floor = MIN_VALIDATION_CONFIDENCE.get(event.event_class, DEFAULT_MIN_VALIDATION_CONFIDENCE)
    if confidence.value < floor:
        return (
            f"confidence {confidence.value:.2f} below the {event.event_class} "
            f"validation threshold {floor:.2f}"
        )
    return None


def _publishable_now(event: Event, now: datetime) -> bool:
    """Is this event inside its active window (`validated -> active`)?

    A work zone scheduled for next month is validated and NOT active, which is
    the point of per-class TTLs: a work zone spends most of its life
    scheduled-but-inactive.
    An event whose end time has passed is not active either - it goes to `clearing`
    on its own edge rather than being published as live.
    """
    start = parse_iso(event.start_time)
    if start is None or now < start:
        return False
    end = parse_iso(event.end_time) if event.end_time else None
    return end is None or now <= end


def promote(
    event: Event,
    confidence: Confidence,
    now: datetime,
    payload_ref: str | None,
) -> list[Step]:
    """Walk a freshly reported event as far along the lifecycle as its data allows.

    `reported` -> `validated` -> `active`, or `reported` -> `cleared` when it fails
    validation ("Never corroborated or never geolocated. Closed without ever
    publishing." - the existing edge, used for exactly the case it describes).
    """
    failure = _validation_failure(event, confidence)
    if failure is not None:
        return [
            _advance(event, "cleared", "validation_fail", "rule", failure, now, payload_ref)
        ]

    steps = [
        _advance(
            event,
            "validated",
            "validation_pass",
            "rule",
            "geolocated on the corridor, times plausible, confidence above the class threshold",
            now,
            payload_ref,
        )
    ]
    if _publishable_now(steps[-1].event, now):
        steps.append(
            _advance(
                steps[-1].event,
                "active",
                "source_update",
                "rule",
                "inside its active time window; published to consumers",
                now,
                payload_ref,
            )
        )
    return steps


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def resolve_new(
    candidate: CandidateEvent,
    confidence: Confidence,
    event_id: str,
    nearby: list[Event],
    now: datetime,
    payload_ref: str | None = None,
) -> Resolution:
    """A source record never seen before. Match it, then write what that implies.

    ``nearby`` is whatever the store found in a matchable state near this
    candidate's measure range. Note what this function does NOT do: it never
    re-fetches, never re-conflates, and never looks at more than the events it was
    handed. All three would make the resolver's behaviour depend on how the caller
    happened to query, and a resolver whose decisions are not reproducible from its
    inputs cannot be replayed.
    """
    matchable = [
        event
        for event in nearby
        if event.lifecycle_state in MATCHABLE_STATES
        and not is_stale(event, now)
        # A source never merges with itself: that is one agency reporting one
        # situation twice, which is a same-source duplicate rather than
        # corroboration. The match score already penalizes it; excluding
        # it here means it cannot chain two of one agency's records into one event.
        and not any(s.source_id == candidate.source.source_id for s in event.sources)
    ]

    best: tuple[Event, MatchScore] | None = None
    reviews: list[ReviewItem] = []
    for existing in matchable:
        score = score_match(candidate, candidate_view(existing))
        if score.decision == "merge" and (best is None or score.value > best[1].value):
            best = (existing, score)
        elif score.decision == "review":
            reviews.append(
                ReviewItem(
                    event_id=event_id,
                    other_event_id=existing.event_id,
                    score=score.value,
                    explanation=score.explanation,
                    queued_at=iso_utc(now),
                )
            )

    if best is not None:
        # A merge beats a review: if the same candidate is ambiguous against one
        # event and a clear match to another, the clear match is the answer and the
        # ambiguity is resolved by it rather than left in the queue.
        return _merge(candidate, confidence, event_id, best[0], best[1], now, payload_ref)

    created = event_from_candidate(candidate, confidence, event_id, now)
    steps = [
        Step(
            event=created,
            audit=_audit(
                created,
                None,
                "source_update",
                "adapter",
                f"first report of this record from {candidate.source.source_id}",
                now,
                payload_ref,
            ),
        )
    ]
    steps.extend(promote(steps[-1].event, confidence, now, payload_ref))

    explanation = [
        f"no event in {', '.join(MATCHABLE_STATES)} matched above {MERGE_THRESHOLD} "
        f"among {len(matchable)} candidate(s) near measure "
        f"{candidate.extent.begin_measure:.1f}-{candidate.extent.end_measure:.1f}",
        f"created {event_id} in {steps[-1].event.lifecycle_state}",
    ]
    if reviews:
        explanation.append(
            f"{len(reviews)} ambiguous pair(s) queued for review; both events stay "
            "separately published"
        )
    return Resolution(
        action="created",
        changes=[EventChange(event_id=event_id, steps=steps, created=True)],
        reviews=reviews,
        explanation=explanation,
        subject_event_id=event_id,
    )


def _merge(
    candidate: CandidateEvent,
    confidence: Confidence,
    child_id: str,
    parent: Event,
    score: MatchScore,
    now: datetime,
    payload_ref: str | None,
) -> Resolution:
    """Absorb a candidate into an existing event, keeping both identities.

    TWO events are written, and that is the part worth understanding. The child is
    created and immediately walked to `merged`; the parent gains a version whose
    provenance includes the child's source. An un-merge has to restore the child's
    own identity and history, and a merged event has to retain the full source list
    of all its parents - neither is possible if the merge only ever touched the
    parent.
    """
    child = event_from_candidate(candidate, confidence, child_id, now)
    child_steps = [
        Step(
            event=child,
            audit=_audit(
                child,
                None,
                "source_update",
                "adapter",
                f"first report of this record from {candidate.source.source_id}",
                now,
                payload_ref,
            ),
        )
    ]
    # Through `validated` rather than straight to `merged`: the table has no
    # `reported -> merged` edge, and inventing one would hide the fact that the
    # child WAS independently validated - which is what makes the merge evidence
    # rather than an assumption.
    child_steps.append(
        _advance(
            child_steps[-1].event,
            "validated",
            "validation_pass",
            "rule",
            "geolocated on the corridor; validated before the merge decision",
            now,
            payload_ref,
        )
    )
    child_steps.append(
        _advance(
            child_steps[-1].event,
            "merged",
            "dedup_decision",
            "rule",
            f"match {score.value:.4f} >= {MERGE_THRESHOLD} against {parent.event_id}",
            now,
            payload_ref,
        )
    )
    child_final = replace(
        child_steps[-1].event,
        related_event_ids=sorted({*child_steps[-1].event.related_event_ids, parent.event_id}),
    )
    child_steps[-1] = Step(event=child_final, audit=child_steps[-1].audit)

    # --- the parent -------------------------------------------------------
    winners, alternates, provenance, field_explanation = reconcile(parent, candidate)
    sources = list(parent.sources) + [_source_with_contributions(candidate)]

    # Recency decays from the FRESHEST confirming report, so one stale
    # corroborator must not drag down an otherwise fresh event.
    last_confirmed_at = max(
        (s.source_updated_at or s.retrieved_at) for s in sources
    )
    extensions = dict(parent.extensions)
    extensions["field_provenance"] = {**(extensions.get("field_provenance") or {}), **provenance}
    if alternates:
        # Losing values are RETAINED, not discarded.
        existing_alternates = dict(extensions.get("alternates") or {})
        existing_alternates.update(alternates)
        extensions["alternates"] = existing_alternates

    severity = derive_severity(
        parent.event_class, winners["lane_impacts"], winners["agency_severity"]
    )
    merged_extent = winners["extent"]
    updated_parent = _revise(
        parent,
        now,
        extent=merged_extent,
        lane_impacts=winners["lane_impacts"],
        event_subtype=winners["event_subtype"] or parent.event_subtype,
        start_time=winners["start_time"] or parent.start_time,
        end_time=winners["end_time"],
        severity=severity,
        expected_duration=derive_duration(
            parent.event_class,
            candidate.agency_duration_minutes or parent.expected_duration.estimate_minutes,
            duration_minutes(winners["start_time"], winners["end_time"]),
        ),
        sources=sources,
        related_event_ids=sorted({*parent.related_event_ids, child_id}),
        extensions=extensions,
    )
    # Re-score with the full source list: corroboration is the component that
    # should move on a merge, and the conflict count feeds internal consistency.
    rescored = score_confidence(
        ScoringInput(
            candidate=candidate_view(updated_parent),
            sources=sources,
            last_confirmed_at=last_confirmed_at,
            conflict_count=len(alternates),
            now=now,
        )
    )
    updated_parent = replace(updated_parent, confidence=rescored)
    parent_step = Step(
        event=updated_parent,
        audit=_audit(
            updated_parent,
            parent.lifecycle_state,  # unchanged: a merge into a parent is not a transition
            "dedup_decision",
            "rule",
            f"absorbed {child_id} from {candidate.source.source_id} at match {score.value:.4f}",
            now,
            payload_ref,
        ),
    )

    explanation = [
        f"matched {parent.event_id} at {score.value:.4f} (>= {MERGE_THRESHOLD})",
        *score.explanation,
        f"provenance now names {len({s.source_id for s in sources})} source(s) across "
        f"{len({s.agency for s in sources})} agency(ies)",
        f"confidence {parent.confidence.value:.4f} -> {rescored.value:.4f} "
        f"(corroboration {parent.confidence.breakdown.corroboration:.2f} -> "
        f"{rescored.breakdown.corroboration:.2f})",
        *field_explanation,
        f"child {child_id} retained in `merged` so the merge is reversible",
    ]

    return Resolution(
        action="merged",
        changes=[
            EventChange(event_id=child_id, steps=child_steps, created=True),
            EventChange(event_id=parent.event_id, steps=[parent_step]),
        ],
        explanation=explanation,
        subject_event_id=child_id,
        matched_event_id=parent.event_id,
        match=score,
    )


def resolve_update(
    candidate: CandidateEvent,
    confidence: Confidence,
    existing: Event,
    now: datetime,
    payload_ref: str | None = None,
    content_unchanged: bool = False,
) -> Resolution:
    """The same source record, reported again.

    THREE OUTCOMES, all of them idempotency and ordering:

      unchanged     identical content. No version, no audit record, nothing
                    emitted. The feeds are polled every 60 seconds and most
                    records are unchanged most of the time; writing a version per
                    poll would bury the real changes in noise and make "how many
                    times did this event actually change" unanswerable.

      late_ignored  the update is OLDER than what we already hold. Recorded as an
                    audit record with no state change: a late update is recorded
                    but must not regress published state.

      updated       a genuine change: a new version, and a re-derived severity and
                    duration.
    """
    if content_unchanged:
        return Resolution(
            action="unchanged",
            explanation=["identical content hash: no new version (idempotency)"],
            subject_event_id=existing.event_id,
        )

    incoming_at = candidate.source.source_updated_at
    held = next(
        (s for s in existing.sources if s.source_id == candidate.source.source_id),
        None,
    )
    held_at = held.source_updated_at if held else None
    incoming = parse_iso(incoming_at)
    previous = parse_iso(held_at)
    if incoming is not None and previous is not None and incoming < previous:
        stale_version = _revise(existing, now)
        return Resolution(
            action="late_ignored",
            changes=[
                EventChange(
                    event_id=existing.event_id,
                    steps=[
                        Step(
                            event=stale_version,
                            audit=_audit(
                                stale_version,
                                existing.lifecycle_state,
                                "source_update",
                                "adapter",
                                f"late update from {candidate.source.source_id} "
                                f"({incoming_at} < {held_at}) recorded but not applied",
                                now,
                                payload_ref,
                            ),
                        )
                    ],
                )
            ],
            explanation=[
                f"source-side timestamp {incoming_at} precedes the held {held_at}: "
                "recorded, not applied - published state does not regress"
            ],
            subject_event_id=existing.event_id,
        )

    winners, alternates, provenance, field_explanation = reconcile(existing, candidate)
    sources = [
        s for s in existing.sources if s.source_id != candidate.source.source_id
    ] + [_source_with_contributions(candidate)]
    extensions = dict(existing.extensions)
    extensions["field_provenance"] = {**(extensions.get("field_provenance") or {}), **provenance}
    if alternates:
        merged_alternates = dict(extensions.get("alternates") or {})
        merged_alternates.update(alternates)
        extensions["alternates"] = merged_alternates

    rescored = score_confidence(
        ScoringInput(
            candidate=candidate,
            sources=sources,
            last_confirmed_at=max((s.source_updated_at or s.retrieved_at) for s in sources),
            conflict_count=len(alternates),
            now=now,
        )
    )
    updated = _revise(
        existing,
        now,
        extent=winners["extent"],
        lane_impacts=winners["lane_impacts"],
        event_subtype=winners["event_subtype"] or existing.event_subtype,
        start_time=winners["start_time"] or existing.start_time,
        end_time=winners["end_time"],
        severity=derive_severity(
            existing.event_class, winners["lane_impacts"], winners["agency_severity"]
        ),
        confidence=rescored,
        expected_duration=derive_duration(
            existing.event_class,
            candidate.agency_duration_minutes,
            duration_minutes(winners["start_time"], winners["end_time"]),
        ),
        sources=sources,
        extensions=extensions,
    )
    steps = [
        Step(
            event=updated,
            audit=_audit(
                updated,
                existing.lifecycle_state,
                "source_update",
                "adapter",
                f"{candidate.source.source_id} updated its record",
                now,
                payload_ref,
            ),
        )
    ]

    # An update can also move the event: a validated-but-not-yet-started work zone
    # whose start time has now passed becomes active on this same source update.
    if steps[-1].event.lifecycle_state == "validated" and _publishable_now(steps[-1].event, now):
        steps.append(
            _advance(
                steps[-1].event,
                "active",
                "source_update",
                "rule",
                "start time reached on this update; published to consumers",
                now,
                payload_ref,
            )
        )

    return Resolution(
        action="updated",
        changes=[EventChange(event_id=existing.event_id, steps=steps)],
        explanation=[
            f"same source record from {candidate.source.source_id}: version "
            f"{existing.version} -> {steps[-1].event.version}",
            *field_explanation,
        ],
        subject_event_id=existing.event_id,
    )
