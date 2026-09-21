"""The adapter contract.

An adapter does EXACTLY this, in order:
    parse -> field map -> vocabulary map -> spatial conflate -> emit candidate

An adapter MUST NOT: deduplicate, score confidence, decide lifecycle state, or
write to the event store. Those are downstream concerns. The boundary is enforced
by the return type here - an adapter can only return ``CandidateEvent`` objects,
and ``CandidateEvent`` has no field for a lifecycle state, so an adapter has no
vocabulary for expressing a lifecycle decision.

This matters beyond tidiness. An outside agency has to be able to author an adapter
against a published contract without access to core internals, and
README.md § Not built yet names "adapters accumulate
business logic" as one of the ways this project fails quietly. The moment an adapter sets lifecycle state, the reference
architecture stops generalizing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.lrs import Conflator
from ..core.types import CandidateEvent, MappingIssue


@dataclass
class AdapterContext:
    conflator: Conflator
    # s3:// URI of the raw payload this invocation is parsing.
    raw_ref: str
    # When we fetched it - distinct from when the source says it changed.
    retrieved_at: str


@dataclass
class AdapterResult:
    candidates: list[CandidateEvent] = field(default_factory=list)
    # Records the adapter saw but could not place on the corridor.
    off_corridor: int = 0
    # Everything unmappable, for the review queue. Never dropped.
    issues: list[MappingIssue] = field(default_factory=list)


class Adapter:
    """Base class for every source adapter.

    ``source_id`` must match the source catalog entry. ``agency`` and
    ``expected_schema_version`` are declared rather than inferred so schema drift
    is detectable: a feed changing spec version raises an alert and
    quarantines records rather than silently mismapping them.
    """

    source_id: str
    agency: str
    expected_schema_version: str

    def parse(self, raw_body: str, ctx: AdapterContext) -> AdapterResult:
        raise NotImplementedError


def issue(
    field_name: str,
    raw_value: Any,
    reason: str,
    detail: str | None = None,
) -> MappingIssue:
    """Helper so every adapter reports issues the same way."""
    return MappingIssue(field=field_name, raw_value=raw_value, reason=reason, detail=detail)


def record_issues(issues: list[MappingIssue], mark: int) -> list[MappingIssue]:
    """The issues raised since ``mark`` - those belonging to the record being mapped.

    TWO SCOPES, BOTH REQUIRED, which is why this exists rather than adapters choosing
    one. ``AdapterResult.issues`` is the FEED's list and drives the review queue: it
    must include issues from records that never became candidates at all (a feature
    with unusable geometry, a value that made the record unplaceable), so it cannot be
    reconstructed from the candidates. ``CandidateEvent.mapping_issues`` is the
    RECORD's list, and without it no consumer can ask "how much of THIS event could
    not be mapped" - only "how much of this feed".

    The second scope was declared in the model from the start and then left
    empty by every adapter, so the strip published ``issueCount: 0`` on all 88
    candidates while the same document reported 131 issues at feed level. Both numbers
    were right and the pair was incoherent.

    Usage: take ``mark = len(issues)`` before mapping a record, and call this when
    constructing its candidate. A record that yields several candidates gives each of
    them the same list, because a value that could not be mapped was not mapped for
    any of them.
    """
    return issues[mark:]
