"""Config integrity tests.

Portability pushes the corridor, the source catalog, and the reliability priors out
of code and into ``config/``. That is the right call for portability, and it
moves a class of error from "compile failure" to "nothing happens at runtime".
These tests are what replaces the compiler.

The registry/catalog drift check is the one that matters most: a source in the
catalog with no adapter quarantines every payload it produces, and an
adapter with no catalog entry is never scheduled at all. Neither announces itself.
"""

from __future__ import annotations

import pytest

from corridor_event_hub.adapters.registry import ADAPTERS, registered_source_ids
from corridor_event_hub.core.confidence import (
    DEFAULT_RELIABILITY,
    INDEPENDENCE_GROUPS,
    SEED_SOURCE_RELIABILITY,
)
from corridor_event_hub.core.config import load_json
from corridor_event_hub.core.lrs import CORRIDOR_TOTAL_MILES, corridor


@pytest.fixture(scope="module")
def catalog():
    return load_json("sources.json")


@pytest.fixture(scope="module")
def catalog_ids(catalog):
    return {s["sourceId"] for s in catalog["sources"]}


class TestSourceCatalog:
    def test_every_registered_adapter_has_a_catalog_entry(self, catalog_ids):
        # Onboarding a source is an adapter module PLUS a catalog entry. An
        # adapter with no entry is never scheduled, so it never runs.
        missing = set(registered_source_ids()) - catalog_ids
        assert not missing, f"adapters with no catalog entry: {sorted(missing)}"

    def test_every_adapter_key_matches_the_adapter_it_maps_to(self):
        # A key/source_id mismatch would route payloads to the wrong parser, which
        # is worse than not parsing them.
        for key, adapter in ADAPTERS.items():
            assert key == adapter.source_id

    def test_every_scheduled_source_has_an_adapter(self, catalog):
        # A verified_live source with no adapter gets polled, stored, and then
        # quarantined on every single run - burning NAT egress to produce alarms.
        scheduled = {
            s["sourceId"] for s in catalog["sources"] if s.get("status") == "verified_live"
        }
        missing = scheduled - set(registered_source_ids())
        assert not missing, f"scheduled sources with no adapter: {sorted(missing)}"

    def test_every_source_declares_snapshot_semantics(self, catalog):
        # OPEN QUESTION 2. The field being ABSENT is the dangerous case,
        # because a reader might then infer a default - and the wrong default
        # silently corrupts lifecycle behavior for a whole state.
        for source in catalog["sources"]:
            assert "snapshotSemantics" in source, (
                f"{source['sourceId']} does not say what a disappearing record means"
            )

    def test_no_source_carries_a_credential(self, catalog):
        # ADR 0004: keys live in Secrets Manager. The catalog is committed to git.
        for source in catalog["sources"]:
            for key, value in source.items():
                if not isinstance(value, str):
                    continue
                if key in ("secretId", "$authComment", "authMethod"):
                    continue
                assert "key=" not in value.lower(), f"{source['sourceId']}.{key}"
                assert "access_token=" not in value.lower(), f"{source['sourceId']}.{key}"

    def test_sources_needing_a_key_name_a_secret_not_a_value(self, catalog):
        for source in catalog["sources"]:
            if source.get("authMethod") == "api_key_secret":
                assert source.get("secretId", "").startswith("corridor-event-hub/"), (
                    f"{source['sourceId']} must name a secret under corridor-event-hub/*"
                )


class TestSourceReliability:
    def test_every_registered_adapter_has_a_reliability_prior(self):
        # A source with no prior silently scores DEFAULT_RELIABILITY, which is a
        # legitimate fallback for a brand-new feed and a mistake for one we ship.
        missing = set(registered_source_ids()) - set(SEED_SOURCE_RELIABILITY)
        assert not missing, f"no reliability prior for: {sorted(missing)}"

    def test_every_registered_adapter_has_an_independence_group(self):
        # A missing group falls back to the sourceId, which would make a
        # feed independent of itself under a rename. Be explicit.
        missing = set(registered_source_ids()) - set(INDEPENDENCE_GROUPS)
        assert not missing, f"no independence group for: {sorted(missing)}"

    def test_reliability_values_are_probabilities(self):
        for source_id, value in SEED_SOURCE_RELIABILITY.items():
            assert 0.0 <= value <= 1.0, f"{source_id} = {value}"
        assert 0.0 <= DEFAULT_RELIABILITY <= 1.0

    def test_priors_and_groups_live_in_the_catalog_not_in_core(self):
        # If someone reintroduces a literal table in
        # core/confidence.py, this test keeps passing but the values diverge from
        # the catalog - so assert they are actually READ from it.
        catalog = {
            s["sourceId"]: s
            for s in load_json("sources.json")["sources"]
            if "seedReliability" in s
        }
        assert catalog, "the catalog carries no seedReliability values at all"
        for source_id, value in SEED_SOURCE_RELIABILITY.items():
            assert catalog[source_id]["seedReliability"] == value


class TestCorridorConfig:
    def test_state_segments_are_contiguous_and_ordered_west_to_east(self):
        # The state-line arithmetic in lrs.py assumes this ordering. A reordered or
        # gapped config would produce measures that look fine and place events in
        # the wrong state.
        for current, following in zip(corridor.states, corridor.states[1:]):
            assert following.corridor_offset == pytest.approx(
                current.corridor_offset + current.length_miles, abs=1e-6
            )

    def test_the_first_state_starts_at_corridor_zero(self):
        assert corridor.states[0].corridor_offset == 0

    def test_every_state_has_positive_length(self):
        for segment in corridor.states:
            assert segment.length_miles > 0, segment.state

    def test_the_centerline_has_enough_points_to_be_a_line(self):
        assert len(corridor.centerline) >= 2

    def test_the_centerline_spans_a_plausible_corridor_length(self):
        # Guards against a centerline that parses but does not cover the corridor -
        # e.g. one state's worth of geometry with four states of mileposts, which
        # would scale every measure wrongly and silently.
        from corridor_event_hub.core.geo import line_length_miles

        length = line_length_miles(corridor.centerline)
        assert 0.5 * CORRIDOR_TOTAL_MILES < length < 2.0 * CORRIDOR_TOTAL_MILES

    def test_the_verified_flag_and_the_measures_agree(self):
        # THE TRIPWIRE, now guarding a CONSISTENCY invariant rather than one state.
        #
        # It has pointed both ways. It first asserted `verified is False`, because
        # the centerline was a 40-point placeholder. Real state LRS geometry landed,
        # the seven-landmark check passed, fetch-arnold.py wrote the flag true, and
        # the assertion was flipped to match.
        #
        # It now has to accept BOTH, because the two states are no longer "before"
        # and "after" - they are "committed" and "local":
        #
        #   verified=false  the placeholder. What this repository SHIPS, and it ships
        #                   it for a LICENSING reason. The real geometry derives from
        #                   four state DOT LRS layers, none of which licenses
        #                   redistribution: TxDOT asserts copyright and requires
        #                   written consent to pass the data to a third party, ODOT
        #                   publishes "Authorized reference use only". This package
        #                   is MIT-0. See /NOTICE and docs/CORRIDOR-GEOMETRY.md.
        #   verified=true   someone ran `fetch-arnold.py --write-config` locally to
        #                   get metre accuracy. Legitimate, and must not be committed
        #                   (`git checkout -- reference/corridor.json`).
        #
        # Asserting either state alone would fail the other for no good reason. What
        # is ALWAYS wrong is the flag disagreeing with the data, because `verified`
        # is a claim that positions are accurate to metres, and without measures
        # conflation silently falls back to the biased fraction path
        # (docs/CORRIDOR-GEOMETRY.md, problem 2).
        if not corridor.verified:
            assert corridor.measures is None, (
                "verified=false but the centerline carries measures - if you "
                "regenerated the geometry the flag should be true, so this file has "
                "been hand-edited into a state fetch-arnold.py never writes"
            )
            return

        assert corridor.measures is not None, (
            "verified=true but the centerline carries no measures, so conflation "
            "falls back to the biased fraction path"
        )
        assert len(corridor.measures) == len(corridor.centerline)
        # Ascending, because a non-monotonic measure makes position ambiguous and
        # breaks every range scan downstream.
        assert all(
            b > a for a, b in zip(corridor.measures, corridor.measures[1:])
        ), "corridor measures are not strictly ascending"
