# ADR 0003 — A record disappearing from a feed means UNKNOWN, not CLEARED

Status: Accepted

**This is the most consequential decision in the implementation, and the one most
likely to be got wrong by accident.**

## Context

Most state 511 feeds publish a **snapshot**: here is everything active right now.
When a record present at 10:00 is absent at 10:01, that means one of two things:

1. The event **cleared** — the crash was towed, the lane reopened.
2. The event's status is **unknown** — the record aged out of a window, an
   operator forgot to update it, a pagination bug dropped it, the feed
   partially failed.

**The distinction differs by state, and cannot be inferred from the data.**
Nothing in the payload distinguishes case 1 from case 2. Only the agency knows
its own publication semantics, which makes this a phone call, not an engineering
problem (Open Question 2).

Getting it wrong fails silently in one of two directions:

- Assume **cleared** when it means unknown → live hazards vanish from the feed.
  A closed lane is published as open.
- Assume **unknown** when it means cleared → stale events linger past their
  actual clearance.

Neither announces itself. Both corrupt behavior for an entire state's data.

## Decision

Until a source's catalog entry explicitly records
`snapshotSemantics: "cleared"`, a `source_absent` trigger routes the event to
**`clearing`** with reason `stale_no_updates_semantics_unconfirmed` — never
directly to `cleared`.

Enforced in three places:

1. `target_for_source_absent()` in `corridor_event_hub/core/lifecycle.py` is the single decision
   point, and any unrecognized value returns `clearing`.
2. The transition table contains **no edge** from any state to `cleared` on
   `source_absent`. A test asserts its absence.
3. `config/sources.json` carries `snapshotSemantics` per source, defaulting to
   `"UNKNOWN"` with a comment naming the required phone call.

NWS is the confirmed contrast case: an alert leaving the active list genuinely
means expired or cancelled, so its entry reads `"cleared"`.

## Consequences

**The failure mode is now the recoverable one.** Events linger slightly too long
rather than disappearing while still blocking a lane. Given that the consumer is
an automated heavy truck deciding whether a hazard exists beyond sensor range,
that asymmetry is not close: publishing "clear" about a blocked lane is
the worse error by a wide margin.

**Events still expire**, via TTL with a per-class profile, so nothing
lives forever. `clearing` is a waypoint, not a terminus. This is not "keep
everything active indefinitely" — it is "let the timer decide, not the absence."

**Confidence decays** while an event sits in `clearing` without confirmation, so a
consumer filtering on confidence naturally de-weights it.

**Four phone calls remain outstanding**, and the catalog makes that visible
rather than buried. Each confirmed answer is a one-line config change with no
code impact — which is the point of putting it in config.

## Why the conservative default is not obviously right

Worth stating, because the tradeoff is real: treating absence as `unknown` means
a genuinely cleared event stays published until its TTL expires. On a short-TTL
class like congestion that is minutes; on a work zone it could be a week. A
consumer seeing a work zone that finished six days ago is a real quality problem,
and the deck's own critique of existing feeds is that "stale conditions" never
expire.

The resolution is that TTL profiles are per class and should be tuned
tighter for classes where clearance is common and consequential, rather than
relaxing the safety default. But it does mean **this decision does not remove the
need for the phone calls — it only makes waiting for them safe.**

## Alternatives considered

**Assume `cleared`, the intuitive reading.** Matches what most 511 snapshot feeds
probably mean, and avoids stale events. Rejected because the failure direction is
unsafe and silent, and because "probably" is not a basis for a decision affecting
an automated vehicle's understanding of the roadway.

**Per-source heuristic learning** — observe whether records reappear after
disappearing, and infer semantics. Genuinely appealing, and worth building later
as corroborating evidence for the phone calls. Rejected for now: it needs
weeks of observation, and an inferred answer to a
question with a definitive answer available is the wrong trade.

**Block ingest until every source's semantics are confirmed.** Safest, and
unshippable. The conservative default lets work proceed while the answers are
pending.
