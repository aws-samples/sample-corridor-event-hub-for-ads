"""Shared test fixtures.

THREE OF THE SIX FIXTURES ARE GENUINE CAPTURES and three are generated. That
distinction changes what a passing test proves, so it is recorded per file in
``tests/fixtures/README.md`` rather than summarized here.

  captured   ok-odot-wzdx, tx-dot-wzdx, nws-alerts   (2026-08-07/08)
  generated  az511-events, nm-dot-weathershare, aws-location-traffic

A captured fixture matters because it encodes what the agency ACTUALLY SENT, and the
entire difficulty of this project is that agency data does not look like what you
expect. The three generated ones exist because their sources do not grant
redistribution and this repository is published under MIT-0 - see
``scripts/make-synthetic-fixtures.py``, which states per record which observed
characteristic that record reproduces. They prove the adapter handles the shapes we
RECORDED those feeds having; the observations themselves live in
``docs/DATA-SOURCES.md``.

For the captured three, these tests double as the replay path: the same bytes, the
same adapter, the same output, forever.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.core.confidence import ScoringInput, score_confidence
from corridor_event_hub.core.lrs import LocalConflator
from corridor_event_hub.core.types import CandidateEvent, Confidence, Extent, LaneImpact, SourceRef

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def conflator() -> LocalConflator:
    """One conflator for the whole session: it is stateless, and rebuilding the
    centerline per test is wasted time.
    """
    return LocalConflator()


@pytest.fixture
def ctx(conflator: LocalConflator) -> AdapterContext:
    """A fixed ``retrieved_at`` so recency scoring is deterministic."""
    return AdapterContext(
        conflator=conflator,
        raw_ref="s3://amzn-s3-demo-rawzone/fixture",
        retrieved_at="2026-08-07T22:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# Candidate factory, for the resolver / event store / query tests
# ---------------------------------------------------------------------------
#
# Shared here rather than per module because the resolver, the store, and the query
# API all need the SAME candidate to follow one record end to end - and a factory
# copied three times drifts until the three tests are no longer talking about the
# same event. test_matcher.py keeps its own narrower one on purpose: it needs only
# the fields the matcher reads.

_UNSET = object()

#: A fixed clock for every test that uses this factory. Confidence decays on wall
#: time, so a candidate timestamped `now` scores differently every run and a
#: threshold assertion built on it fails at some unpredictable future date.
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def make_candidate(
    *,
    source_id: str = "ok-odot-wzdx",
    native_id: str | None = None,
    begin: float = 100.0,
    end: float = 100.5,
    event_class: str = "incident",
    event_subtype: str = "crash",
    direction: str = "EB",
    route: str = "TEST-ROUTE",
    states: list[str] | None = None,
    start_time: str = "2026-08-10T11:30:00.000Z",
    end_time: Any = _UNSET,
    time_confidence: str = "observed",
    conflation_method: str = "coordinate",
    positional_accuracy_meters: float | None = 100.0,
    lane_impacts: list[LaneImpact] | None = None,
    agency_severity: str | None = None,
    agency_duration_minutes: int | None = None,
    source_updated_at: str | None = "2026-08-10T11:45:00.000Z",
    retrieved_at: str = "2026-08-10T12:00:00.000Z",
    raw_ref: str = "s3://amzn-s3-demo-rawzone/raw/one.json",
    extensions: dict[str, Any] | None = None,
) -> CandidateEvent:
    return CandidateEvent(
        event_class=event_class,
        event_subtype=event_subtype,
        extent=Extent(
            route=route,
            begin_measure=begin,
            end_measure=end,
            direction=direction,
            states=["OK"] if states is None else states,
            geometry=None,
            positional_accuracy_meters=positional_accuracy_meters,
            conflation_method=conflation_method,
        ),
        lane_impacts=list(lane_impacts or []),
        start_time=start_time,
        end_time="2026-08-10T14:00:00.000Z" if end_time is _UNSET else end_time,
        time_confidence=time_confidence,
        agency_severity=agency_severity,
        agency_duration_minutes=agency_duration_minutes,
        source=SourceRef(
            source_id=source_id,
            agency=source_id,
            native_id=native_id or f"{source_id}-1",
            retrieved_at=retrieved_at,
            source_updated_at=source_updated_at,
            contributed_fields=[],
            raw_ref=raw_ref,
        ),
        extensions=dict(extensions or {}),
        mapping_issues=[],
    )


def score(candidate: CandidateEvent, now: datetime | None = None) -> Confidence:
    """The provisional, single-source confidence the normalizer would attach."""
    return score_confidence(
        ScoringInput(
            candidate=candidate,
            sources=[candidate.source],
            last_confirmed_at=(
                candidate.source.source_updated_at or candidate.source.retrieved_at
            ),
            now=now or NOW,
        )
    )
