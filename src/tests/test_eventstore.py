"""Event store tests - the append-only guarantee and the DynamoDB item shape.

TWO IMPLEMENTATIONS, TESTED FOR DIFFERENT THINGS. ``InMemoryEventStore`` is tested
on BEHAVIOUR - append-only, optimistic concurrency, range overlap - because those
rules are what the resolver depends on and a store that quietly accepted anything
would make every resolver test meaningless. ``DynamoEventStore`` is tested on the
ITEM AND REQUEST SHAPE, against a recording stub, because that is where the errors
live that only appear in a deployed Lambda:

  - a bare Python float, which DynamoDB rejects outright
  - a missing condition expression, which turns an append-only table into an
    overwritable one and loses an audit record with no error at all
  - a version sort key that sorts lexicographically instead of numerically, so
    v#10 comes before v#9 and "the latest version" is wrong from the tenth poll on

None of those fail in a unit test that mocks the store away, and all three fail in
production. boto3 is a dev dependency (the Lambda runtime provides it) and the test
suite must run with no AWS credentials at all, hence a stub rather than moto.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.eventstore import (
    LIVE_STATES,
    RANGE_QUERY_LOOKBACK_MILES,
    ConcurrentModification,
    DynamoEventStore,
    InMemoryEventStore,
    VersionAlreadyExists,
    audit_sk,
    gsi_partition,
    version_sk,
)
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.resolution import resolve_new


def created(candidate=None, event_id=None, now=None):
    """One resolved candidate, as an EventChange ready to store."""
    at = now or NOW
    subject = candidate or make_candidate()
    result = resolve_new(subject, score(subject, at), event_id or new_event_id(at), [], at)
    return result.changes[0]


class TestSortKeys:
    def test_versions_sort_numerically_as_strings(self):
        # The bug this prevents: 'v#10' < 'v#9' lexicographically, so an unpadded
        # key makes "the latest version" wrong from the tenth version onward - which
        # a feed polled every 60 seconds reaches in ten minutes.
        keys = [version_sk(n) for n in (1, 2, 9, 10, 11, 100)]
        assert keys == sorted(keys)

    def test_audit_records_sort_with_their_versions(self):
        assert audit_sk(9) < audit_sk(10)

    def test_the_gsi_partition_names_both_state_and_route(self):
        # Route alone would put a corridor's entire cleared history in the same
        # partition as its handful of active events, and every query would read
        # and discard them.
        assert gsi_partition("active", "TEST-ROUTE") == "active#TEST-ROUTE"


class TestAppendOnly:
    def test_stores_a_version_an_audit_record_and_a_current_pointer(self):
        store = InMemoryEventStore()
        change = created()
        store.write_change(change)

        assert store.get_current(change.event_id).version == change.final.version
        history = store.history(change.event_id)
        assert len(history.versions) == len(change.steps)
        # One audit record per version, sequences matching.
        assert [a.sequence for a in history.audit] == [v.version for v in history.versions]

    def test_rewriting_a_stored_version_is_refused(self):
        # Replay must be safe: re-running a payload must not be able to
        # rewrite history, even by accident.
        store = InMemoryEventStore()
        change = created()
        store.write_change(change)
        with pytest.raises((VersionAlreadyExists, ConcurrentModification)):
            store.write_change(change)

    def test_creating_an_event_that_already_exists_is_refused(self):
        store = InMemoryEventStore()
        change = created(event_id="EVENT-ONE")
        store.write_change(change)
        with pytest.raises(ConcurrentModification):
            store.write_change(created(event_id="EVENT-ONE"))

    def test_appending_from_a_stale_version_is_refused(self):
        # Optimistic concurrency. Two payloads landing on one event in the
        # same second is normal on a polled corridor; silently letting the second
        # overwrite the first is how a merge gets lost.
        from corridor_event_hub.core.resolution import EventChange, Step

        store = InMemoryEventStore()
        change = created()
        store.write_change(change)

        stale = change.steps[0]  # version 1, when the store already holds 3
        with pytest.raises(ConcurrentModification, match="expected version"):
            store.write_change(EventChange(event_id=change.event_id, steps=[Step(
                event=stale.event, audit=stale.audit)]))

    def test_history_is_returned_in_order(self):
        store = InMemoryEventStore()
        change = created()
        store.write_change(change)
        history = store.history(change.event_id)
        assert [v.version for v in history.versions] == sorted(
            v.version for v in history.versions
        )


class TestRangeQueries:
    def setup_method(self):
        self.store = InMemoryEventStore()
        for measure in (10.0, 100.0, 250.0):
            change = created(make_candidate(begin=measure, end=measure + 0.5,
                                            native_id=f"n{measure}"))
            self.store.write_change(change)

    def test_finds_events_whose_range_overlaps_the_window(self):
        found = self.store.events_overlapping("TEST-ROUTE", 99.0, 101.0)
        assert [round(e.extent.begin_measure) for e in found] == [100]

    def test_returns_events_sorted_west_to_east(self):
        found = self.store.events_overlapping("TEST-ROUTE", 0.0, 1000.0)
        measures = [e.extent.begin_measure for e in found]
        assert measures == sorted(measures)

    def test_a_different_corridor_is_not_returned(self):
        # Two corridors are two sets of events, not one deployment's worth
        # of cross-contamination.
        other = created(make_candidate(route="OTHER-ROUTE", begin=100.0, native_id="x"))
        self.store.write_change(other)
        found = self.store.events_overlapping("TEST-ROUTE", 99.0, 101.0)
        assert all(e.extent.route == "TEST-ROUTE" for e in found)

    def test_an_event_starting_west_of_the_window_but_running_into_it_is_found(self):
        # THE INTERVAL-QUERY CASE, and the reason RANGE_QUERY_LOOKBACK_MILES exists.
        # A long weather alert or a state-line work zone starts outside the window
        # and overlaps it; a query keyed on begin_measure alone misses it entirely.
        long_alert = created(
            make_candidate(
                event_class="weather", begin=50.0, end=140.0, native_id="long",
                conflation_method="polygon_intersect",
            )
        )
        self.store.write_change(long_alert)
        found = self.store.events_overlapping("TEST-ROUTE", 130.0, 135.0)
        assert long_alert.event_id in {e.event_id for e in found}

    def test_the_lookback_is_documented_as_a_real_limit(self):
        # An extent longer than the lookback IS missed, and the constant is the
        # honest statement of where that starts. Asserted so a change to it is a
        # deliberate act rather than a silent narrowing of coverage.
        assert RANGE_QUERY_LOOKBACK_MILES >= 250.0

    def test_only_live_states_by_default(self):
        assert "merged" not in LIVE_STATES
        assert "cleared" not in LIVE_STATES
        assert "archived" not in LIVE_STATES


class _StubDynamo:
    """Records every request. Returns empty results - the shape is what is asserted."""

    def __init__(self) -> None:
        self.transactions: list[list[dict]] = []
        self.puts: list[dict] = []
        self.queries: list[dict] = []
        self.items: dict[tuple[str, str], dict] = {}

    def transact_write_items(self, TransactItems):  # noqa: N803 - boto3's parameter name
        self.transactions.append(TransactItems)
        return {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": []}

    def get_item(self, **kwargs):
        return {}


class TestDynamoItemShape:
    """The failures that only appear in a deployed Lambda."""

    def setup_method(self):
        self.stub = _StubDynamo()
        self.store = DynamoEventStore(table_name="test-events", client=self.stub)
        self.change = created()
        self.store.write_change(self.change)

    def test_one_transaction_per_version_with_three_items_each(self):
        # Version, audit record, and current pointer TOGETHER. Written separately
        # they can diverge, and an audit trail with holes is not an audit trail.
        assert len(self.stub.transactions) == len(self.change.steps)
        for transaction in self.stub.transactions:
            assert len(transaction) == 3
            keys = [list(entry.keys())[0] for entry in transaction]
            assert keys == ["Put", "Put", "Put"]

    def test_no_bare_float_reaches_dynamodb(self):
        # A float raises at runtime in the Lambda and nowhere else. Every number in
        # every item has to be a Decimal or an int.
        def walk(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    if key == "N":
                        assert isinstance(inner, str), f"N must be a string, got {inner!r}"
                    walk(inner)
            elif isinstance(value, list):
                for inner in value:
                    walk(inner)
            else:
                assert not isinstance(value, float), f"bare float {value!r} in an item"

        walk(self.stub.transactions)

    def test_the_version_and_audit_items_refuse_to_overwrite(self):
        for transaction in self.stub.transactions:
            version_put, audit_put, _current = transaction
            assert version_put["Put"]["ConditionExpression"] == "attribute_not_exists(sk)"
            assert audit_put["Put"]["ConditionExpression"] == "attribute_not_exists(sk)"

    def test_the_first_version_requires_that_nothing_exists(self):
        first_current = self.stub.transactions[0][2]["Put"]
        assert first_current["ConditionExpression"] == "attribute_not_exists(eventId)"

    def test_later_versions_require_the_version_they_read(self):
        # Optimistic concurrency: an unexpected version fails the whole
        # transaction rather than overwriting a fresher decision.
        second_current = self.stub.transactions[1][2]["Put"]
        assert second_current["ConditionExpression"] == "version = :expected"
        expected = second_current["ExpressionAttributeValues"][":expected"]
        assert expected == {"N": "1"}

    def test_only_the_current_item_carries_the_gsi_keys(self):
        # A version snapshot in the corridor index would put every historical
        # version of every event into every range query's results.
        version_item = self.stub.transactions[0][0]["Put"]["Item"]
        current_item = self.stub.transactions[0][2]["Put"]["Item"]
        assert "gsiLifecycleRoute" not in version_item
        assert "gsiBeginMeasure" not in version_item
        assert current_item["gsiLifecycleRoute"]["S"] == "reported#TEST-ROUTE"
        assert current_item["gsiBeginMeasure"] == {"N": "100.0"}

    def test_an_unresolved_extent_is_stored_but_not_indexed(self):
        # The record is kept; there is no honest answer to "where is it", so it
        # does not appear in a range scan. Storing measure 0 instead would place it
        # at the corridor's western terminus.
        import math

        stub = _StubDynamo()
        store = DynamoEventStore(table_name="test-events", client=stub)
        store.write_change(
            created(make_candidate(begin=math.nan, end=math.nan, states=[],
                                   conflation_method="unresolved"))
        )
        current = stub.transactions[0][2]["Put"]["Item"]
        assert "gsiBeginMeasure" not in current
        assert "gsiLifecycleRoute" not in current
        assert current["event"]  # but the event itself IS there

    def test_reads_are_strongly_consistent(self):
        # The resolver decides what to write from this read. An eventually
        # consistent one means deciding against a version already superseded.
        stub = _StubDynamo()
        store = DynamoEventStore(table_name="test-events", client=stub)
        store.history("EVENT-ONE")
        assert stub.queries[0]["ConsistentRead"] is True

    def test_the_range_query_widens_the_lower_bound(self):
        stub = _StubDynamo()
        store = DynamoEventStore(table_name="test-events", client=stub)
        store.events_overlapping("TEST-ROUTE", 300.0, 350.0, states=["active"])

        query = stub.queries[0]
        assert query["IndexName"] == "by-state-measure"
        values = query["ExpressionAttributeValues"]
        assert values[":partition"] == {"S": "active#TEST-ROUTE"}
        assert Decimal(values[":lo"]["N"]) == Decimal("300.0") - Decimal(
            str(RANGE_QUERY_LOOKBACK_MILES)
        )
        assert Decimal(values[":hi"]["N"]) == Decimal("350.0")

    def test_a_query_per_lifecycle_state(self):
        stub = _StubDynamo()
        store = DynamoEventStore(table_name="test-events", client=stub)
        store.events_overlapping("TEST-ROUTE", 0.0, 10.0)
        assert len(stub.queries) == len(LIVE_STATES)
