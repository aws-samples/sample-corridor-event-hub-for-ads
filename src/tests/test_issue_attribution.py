"""Every adapter must attribute its mapping issues to the record they came from.

WHY THIS IS ONE CROSS-ADAPTER TEST rather than six per-adapter ones: the defect it
guards is an OMISSION, and an omission is exactly what a per-adapter test suite misses.
``CandidateEvent.mapping_issues`` was declared in the model and then left as
``[]`` by all six adapters, so the strip published ``issueCount: 0`` on all 88
candidates while the same document reported 131 issues at feed level. Both numbers were
right by their own code and the pair was incoherent - the same failure shape as the
confidence-basis bug in test_strip_export.py. Nothing failed, because nothing compared
the two.

So this file asserts the RELATIONSHIP between the two scopes, and does it by iterating
the registry rather than a hand-listed set. A seventh adapter is then covered the day it
is added, which is the only way an omission test stays true.

The two scopes and why both exist:

  ``AdapterResult.issues``          the FEED's list - drives the review queue, and must
                                    include issues from records that never became
                                    candidates at all.
  ``CandidateEvent.mapping_issues`` the RECORD's list - lets a consumer ask how much of
                                    THIS event could not be mapped.

Neither is derivable from the other, which is the whole point.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.adapters.registry import ADAPTERS

# Adapter source_id -> its fixture (three captured, three generated - see
# tests/fixtures/README.md). Keyed off the registry so a new adapter without
# an entry here fails loudly rather than being silently skipped.
FIXTURES = {
    "ok-odot-wzdx": "ok-odot-wzdx.json",
    "tx-dot-wzdx": "tx-dot-wzdx.json",
    "az511-events": "az511-events.json",
    "nws-alerts": "nws-alerts.json",
    "nm-dot-weathershare": "nm-dot-weathershare.json",
    "aws-location-traffic": "aws-location-traffic.json",
}


def _parse_all(ctx):
    """Every adapter run against its fixture."""
    results = {}
    # The registry holds adapter INSTANCES, not classes - they are stateless.
    for source_id, adapter in sorted(ADAPTERS.items()):
        fixture = FIXTURES.get(source_id)
        assert fixture is not None, (
            f"{source_id} is in the adapter registry but has no fixture listed in this "
            "test. Add it - an adapter nobody checks is how the last omission survived."
        )
        results[source_id] = adapter.parse(load_fixture(fixture), ctx)
    return results


@pytest.fixture(scope="module")
def results(conflator):
    # Module-scoped: six adapters over six payloads is slow enough to be worth
    # parsing once. `conflator` is session-scoped so it composes; `ctx` is per-test, so
    # the context is built here instead with the same fixed clock.
    return _parse_all(
        AdapterContext(
            conflator=conflator,
            raw_ref="s3://amzn-s3-demo-rawzone/fixture",
            retrieved_at="2026-08-07T22:00:00.000Z",
        )
    )


class TestEveryAdapterAttributesIssuesToRecords:
    def test_registry_and_fixture_list_agree(self, results):
        # The assertion is inside _parse_all; this makes the failure legible as its own
        # test rather than as a collection error in every other test here.
        assert set(results) == set(FIXTURES)

    def test_an_adapter_that_reports_issues_attributes_at_least_one(self, results):
        """The regression itself: issues at feed level, none on any candidate."""
        checked = 0
        for source_id, result in results.items():
            feed_issues = len(result.issues)
            if feed_issues == 0 or not result.candidates:
                # Nothing to attribute, or nothing to attribute it to. NWS is the real
                # case: its issues come from zone-coded alerts that never place on the
                # corridor, so they are correctly feed-only.
                continue
            checked += 1
            attributed = sum(len(c.mapping_issues) for c in result.candidates)
            assert attributed > 0, (
                f"{source_id} reported {feed_issues} issue(s) at feed level and "
                "attributed none to any candidate - mapping_issues is empty again"
            )
        assert checked >= 4, (
            "expected several adapters to have both issues and candidates in the "
            "captured payloads; this test has lost its subject rather than passed"
        )

    def test_every_attributed_issue_also_reaches_the_review_queue(self, results):
        """Per-record attribution must not become a way to LOSE an issue.

        The feed list is what the review queue reads, so an issue attached to a
        candidate and dropped from the result would vanish from the queue while looking
        accounted for.
        """
        for source_id, result in results.items():
            feed = [id(i) for i in result.issues]
            for candidate in result.candidates:
                for attributed in candidate.mapping_issues:
                    assert id(attributed) in feed, (
                        f"{source_id}: an issue on candidate "
                        f"{candidate.source.native_id} is absent from the feed's "
                        "issue list, so the review queue would never show it"
                    )

    def test_no_candidate_claims_an_issue_from_a_different_record(self, results):
        """The failure mode of a mark-and-slice implementation: a mark taken too early,
        or not reset per iteration, silently attributes record N-1's problems to record
        N. Detected via the ``nativeId=...`` detail the adapters write, which names the
        record the issue was raised for.
        """
        for source_id, result in results.items():
            for candidate in result.candidates:
                native_id = candidate.source.native_id
                for attributed in candidate.mapping_issues:
                    detail = attributed.detail or ""
                    if "nativeId=" not in detail:
                        continue
                    named = detail.split("nativeId=", 1)[1].split()[0].strip(",;)")
                    assert named == native_id, (
                        f"{source_id}: candidate {native_id} carries an issue raised "
                        f"for record {named} - the issue mark is misplaced"
                    )

    def test_attribution_never_exceeds_the_feed_total(self, results):
        """A per-record list built by slicing over-counts if the mark fails to advance:
        every candidate would carry every issue seen so far.

        Counted by identity rather than by summing lengths, so the legitimate case where
        one record yields several candidates that share its issues is not mistaken for
        over-counting.
        """
        for source_id, result in results.items():
            distinct = {id(i) for c in result.candidates for i in c.mapping_issues}
            assert len(distinct) <= len(result.issues), (
                f"{source_id} attributed {len(distinct)} distinct issue(s) across "
                f"candidates but the feed reported {len(result.issues)}"
            )

    def test_one_record_yielding_several_candidates_gives_each_the_same_issues(self, results):
        """A value that could not be mapped was not mapped for ANY candidate derived
        from that record, so sharing is correct - but the lists must be separate objects.
        Aliasing one list across candidates is the bug that only appears once something
        downstream edits an event in place.
        """
        result = results["nws-alerts"]
        by_native: dict[str, list] = {}
        for candidate in result.candidates:
            by_native.setdefault(candidate.source.native_id, []).append(candidate)

        multi = [group for group in by_native.values() if len(group) > 1]
        if not multi:
            pytest.skip("no alert in this payload maps to more than one class")

        for group in multi:
            first, *rest = group
            for other in rest:
                assert other.mapping_issues == first.mapping_issues
                assert other.mapping_issues is not first.mapping_issues, (
                    "candidates from one record share a mutable issue list"
                )

    def test_no_issue_is_attributed_to_two_different_records(self, results):
        """The precise failure mode of mark-and-slice, and the one that would otherwise
        pass every count-based check: a mark taken outside the loop, or not reset per
        iteration, gives record N the problems of records 1..N-1.

        A FEED-LEVEL issue leaking is the same bug seen from the other side - the schema
        version check that ODOT and TxDOT run BEFORE their loop would land on every
        candidate. Both show up here as one issue owned by two native ids.

        Sharing within a single record is legal and is asserted separately.
        """
        for source_id, result in results.items():
            owner: dict[int, str] = {}
            for candidate in result.candidates:
                native_id = candidate.source.native_id
                for attributed in candidate.mapping_issues:
                    first = owner.setdefault(id(attributed), native_id)
                    assert first == native_id, (
                        f"{source_id}: the issue on field '{attributed.field}' is "
                        f"attributed to both {first} and {native_id} - either the issue "
                        "mark is outside the loop, or a pre-loop feed issue is leaking "
                        "into every record"
                    )

    def test_a_field_missing_on_every_record_is_reported_on_every_record(self, results):
        """The concrete case worth pinning, from the adapter's own documentation: tile
        features carry no direction/heading field at all, so every candidate from that
        source must say so. This is the difference the change makes - a consumer can now
        see that THIS congestion event has an unknown direction without going to the
        feed's aggregate counts.
        """
        result = results["aws-location-traffic"]
        assert result.candidates
        for candidate in result.candidates:
            fields = [i.field for i in candidate.mapping_issues]
            assert fields.count("direction") == 1, (
                f"expected exactly one direction issue on {candidate.source.native_id}, "
                f"got {fields}"
            )
