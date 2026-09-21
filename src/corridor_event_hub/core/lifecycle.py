"""Lifecycle state machine definition.

The legal transition set is declared as MACHINE-READABLE DATA, and illegal
transitions are rejected and alarmed rather than coerced. That is why this is a
table rather than a chain of ``if`` statements - the table can be validated,
diffed, published in the reference architecture, and reasoned about by someone
who does not read Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TRANSITION_TRIGGERS = (
    "source_update",  # a feed reported something new
    "source_absent",  # record disappeared from a snapshot - SEE WARNING BELOW
    "timer_ttl",  # Nothing heard, TTL expired
    "derived_signal",  # congestion decay, lane impacts reducing
    "dedup_decision",  # matcher merged or un-merged
    "validation_pass",
    "validation_fail",
    "operator_override",  # First-class, audited, reason required
    "retention_rule",
)

ACTORS = ("adapter", "rule", "timer", "human")


@dataclass(frozen=True)
class TransitionRule:
    from_state: str
    to_state: str
    triggers: tuple[str, ...]
    # Why this edge exists. Published in the reference architecture.
    rationale: str


# ============================================================================
# THE CRITICAL AMBIGUITY - read before trusting `source_absent` anywhere.
# ============================================================================
#
# When a record disappears from a state 511 snapshot, does that mean the event
# CLEARED, or that its status is UNKNOWN?
#
# It differs by state. It cannot be inferred from the data. Getting it wrong
# silently corrupts lifecycle behavior for that entire state - either events
# linger active long after they cleared, or they clear while still blocking a
# lane. Neither failure announces itself.
#
# Resolving it is one phone call per DOT (OPEN QUESTION 2), and until each
# source's catalog entry says otherwise, `snapshotSemantics: "UNKNOWN"` means
# `source_absent` MUST route to `clearing` with reason `stale_no_updates` - never
# straight to `cleared`. Lingering slightly too long is the recoverable failure;
# clearing a live hazard in front of an automated truck is not.
#
# NWS is the contrast case: an alert leaving the active list genuinely means
# expired or cancelled, so its catalog entry says `cleared`.
TRANSITIONS: tuple[TransitionRule, ...] = (
    TransitionRule(
        "reported",
        "validated",
        ("validation_pass",),
        "Schema, spatial, and plausibility checks passed; confidence above class threshold.",
    ),
    TransitionRule(
        "reported",
        "cleared",
        ("validation_fail", "timer_ttl"),
        "Never corroborated or never geolocated. Closed without ever publishing.",
    ),
    TransitionRule(
        "validated",
        "active",
        ("source_update", "timer_ttl"),
        "Within its active time window; published to consumers.",
    ),
    TransitionRule(
        "validated",
        "merged",
        ("dedup_decision",),
        "Matched an existing event before it was ever published separately.",
    ),
    TransitionRule(
        "active",
        "merged",
        ("dedup_decision",),
        "Determined to duplicate another event; absorbed into it.",
    ),
    TransitionRule(
        "active",
        "clearing",
        ("source_update", "derived_signal", "source_absent", "timer_ttl"),
        "Response underway or impacts reducing. ALSO the destination for "
        "source_absent under UNKNOWN snapshot semantics - the conservative path.",
    ),
    TransitionRule(
        "active",
        "cleared",
        ("source_update", "operator_override"),
        "Agency explicitly cleared it, or an operator did. NOTE: reachable by "
        "source_absent ONLY where the catalog confirms snapshotSemantics=cleared.",
    ),
    TransitionRule(
        "clearing",
        "cleared",
        ("source_update", "derived_signal", "timer_ttl"),
        "No longer affecting traffic.",
    ),
    TransitionRule(
        "clearing",
        "active",
        ("source_update", "derived_signal"),
        "Impacts increased again. Congestion cycles this edge constantly "
        "and never waits for an agency clear.",
    ),
    TransitionRule(
        "cleared",
        "active",
        ("source_update",),
        "Re-open: the event recurred at the same location inside a "
        "class-specific window. Common with secondary crashes and re-closures.",
    ),
    TransitionRule(
        "merged",
        "active",
        ("dedup_decision", "operator_override"),
        "Un-merge: the merge was judged wrong. MUST restore the child its "
        "own identity and history.",
    ),
    TransitionRule(
        "cleared",
        "archived",
        ("retention_rule",),
        "Immutable historical record.",
    ),
    TransitionRule(
        "merged",
        "archived",
        ("retention_rule",),
        "Merged events archive too; provenance survives.",
    ),
)


def is_legal_transition(from_state: str, to_state: str, trigger: str) -> bool:
    """Reject, do not coerce."""
    # An operator can force any edge, but it is audited with a reason.
    if trigger == "operator_override":
        return any(t.from_state == from_state for t in TRANSITIONS) or from_state != to_state
    return any(
        t.from_state == from_state and t.to_state == to_state and trigger in t.triggers
        for t in TRANSITIONS
    )


def legal_targets_from(from_state: str) -> list[str]:
    """Legal destinations from a state, in table order, deduplicated."""
    seen: dict[str, None] = {}
    for t in TRANSITIONS:
        if t.from_state == from_state:
            seen.setdefault(t.to_state, None)
    return list(seen)


def target_for_source_absent(snapshot_semantics: str) -> tuple[str, str]:
    """Where ``source_absent`` sends an event, given what we know about the source.

    This function is the single place the snapshot ambiguity is decided, so the
    conservative default is impossible to bypass accidentally. Returns
    ``(to_state, reason)``.
    """
    if snapshot_semantics == "cleared":
        return "cleared", "source_snapshot_confirmed_cleared"
    return "clearing", "stale_no_updates_semantics_unconfirmed"


@dataclass(frozen=True)
class LifecycleProfile:
    """Every state has a TTL. Expiry moves an event toward cleared with a
    reason, rather than leaving it active forever - the failure mode of existing feeds, where "every incoming report is an independent
    alert" and stale conditions never expire.

    Profiles are PER CLASS, expressed as data. A congestion event and a
    three-year work zone cannot share a timeout.
    """

    event_class: str
    ttl_seconds: dict[str, int]
    # Window inside which a recurrence re-opens rather than creating new.
    reopen_window_seconds: int
    # Confidence decay half-life.
    confidence_half_life_seconds: int


LIFECYCLE_PROFILES: dict[str, LifecycleProfile] = {
    "work_zone": LifecycleProfile(
        event_class="work_zone",
        # Scheduled and long-lived. A work zone missing for an hour is not cleared.
        ttl_seconds={"reported": 3600, "validated": 86400, "active": 604800, "clearing": 86400},
        reopen_window_seconds=604800,
        confidence_half_life_seconds=604800,
    ),
    "incident": LifecycleProfile(
        event_class="incident",
        # Short-lived, high update rate, highest duplicate rate.
        ttl_seconds={"reported": 600, "validated": 1800, "active": 7200, "clearing": 1800},
        reopen_window_seconds=3600,
        confidence_half_life_seconds=1800,
    ),
    "closure": LifecycleProfile(
        event_class="closure",
        ttl_seconds={"reported": 900, "validated": 3600, "active": 43200, "clearing": 3600},
        reopen_window_seconds=7200,
        confidence_half_life_seconds=7200,
    ),
    "congestion": LifecycleProfile(
        event_class="congestion",
        # Derived from probe data; cycles active<->clearing and never waits for an
        # operator. Short TTL because absence of probe data means absence of queue.
        ttl_seconds={"reported": 300, "validated": 300, "active": 900, "clearing": 600},
        reopen_window_seconds=900,
        confidence_half_life_seconds=600,
    ),
    "weather": LifecycleProfile(
        event_class="weather",
        ttl_seconds={"reported": 1800, "validated": 3600, "active": 21600, "clearing": 3600},
        reopen_window_seconds=10800,
        confidence_half_life_seconds=10800,
    ),
    "road_surface": LifecycleProfile(
        event_class="road_surface",
        # Sensed, not reported. Surface conditions change faster than the weather
        # that caused them - the reason classes 5 and 6 are split.
        ttl_seconds={"reported": 1800, "validated": 3600, "active": 14400, "clearing": 3600},
        reopen_window_seconds=7200,
        confidence_half_life_seconds=5400,
    ),
    "dimensional_restriction": LifecycleProfile(
        event_class="dimensional_restriction",
        # Mostly static NBI baseline. Effectively never times out.
        ttl_seconds={"reported": 86400, "validated": 2592000, "active": 31536000},
        reopen_window_seconds=2592000,
        confidence_half_life_seconds=31536000,
    ),
    "truck_parking": LifecycleProfile(
        event_class="truck_parking",
        # Inventory is static; occupancy, when it exists, is minutes-fresh.
        ttl_seconds={"reported": 3600, "validated": 86400, "active": 1800, "clearing": 3600},
        reopen_window_seconds=3600,
        confidence_half_life_seconds=900,
    ),
}


def profile_for(event_class: str) -> LifecycleProfile:
    """Fall back to the incident profile: the shortest realistic TTLs, so an
    unrecognized class expires early rather than lingering active forever.
    """
    return LIFECYCLE_PROFILES.get(event_class, LIFECYCLE_PROFILES["incident"])


@dataclass
class AuditRecord:
    """What every transition must record. Append-only, immutable."""

    event_id: str
    sequence: int
    from_state: str | None
    to_state: str
    trigger: str
    actor: str
    # Pointer to the payload that caused this.
    trigger_payload_ref: str | None
    # Which version of the rules made this decision.
    rule_version: str
    reason: str
    occurred_at: str
    recorded_at: str
    # Required when actor == 'human'.
    operator_id: str | None = field(default=None)
