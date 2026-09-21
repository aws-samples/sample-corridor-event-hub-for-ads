"""Handler tests.

The earlier TypeScript scaffold had no tests here, and the gap showed: the handlers are
where the pipeline's contracts actually live. Two of them are load-bearing enough
to be worth pinning:

1. THE LOG FORMAT IS A CONTRACT. Every CloudWatch metric in
   lib/observability-stack.ts is a metric filter over ``$.msg``, ``$.sourceId``,
   ``$.candidates``, ``$.issues``, ``$.offCorridor``, and ``$.latencyMs``. Rename
   one field and the dashboards go silently blank - no error, no failed deploy,
   just flat lines. ``scripts/check-metric-filters.sh`` checks the CloudFormation
   side; these tests check the emitting side.

2. A DOWN FEED IS NOT AN EXCEPTION. The isolation NFR requires one misbehaving
   state feed not stall the others, so the collector must record a failure and
   return rather than raise.

AWS clients are replaced with recording stubs. boto3 is a dev dependency (the
Lambda runtime provides it), and a real client would need credentials that the test
suite deliberately does not require.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from conftest import load_fixture


class _StubS3:
    def __init__(self, body: str = "") -> None:
        self.body = body
        self.put_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}

    def get_object(self, **kwargs):
        class _Body:
            def __init__(self, data: str) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data.encode("utf-8")

        return {"Body": _Body(self.body)}


class _StubEvents:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries):  # noqa: N803 - boto3's own parameter name
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class _StubTable:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}


class _StubDynamoResource:
    def __init__(self) -> None:
        self.table = _StubTable()

    def Table(self, name):  # noqa: N802 - boto3's own method name
        return self.table


class _StubSecrets:
    """Every credentialed feed resolves through Secrets Manager.

    Before that, ok-odot-wzdx read a token literal out of the catalog and needed no
    client at all - which is why this stub did not exist and why the collector fixture
    got away without it. It records the ids asked for, so a test can assert that a
    credential came from the secret rather than from anywhere else.
    """

    def __init__(self, value: str = "test-credential") -> None:
        self.value = value
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's own parameter name
        self.requested.append(SecretId)
        return {"SecretString": self.value}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAW_BUCKET", "amzn-s3-demo-rawzone")
    monkeypatch.setenv("EVENT_BUS", "test-bus")
    monkeypatch.setenv("CATALOG_TABLE", "test-catalog")


@pytest.fixture
def collector(env, monkeypatch):
    """Import the collector with stubbed clients.

    The module builds its boto3 clients at import time (deliberately: a Lambda
    should pay that cost once per container, not once per invocation), so the stubs
    have to be installed after import rather than before.
    """
    module = importlib.import_module("corridor_event_hub.handlers.collector")
    importlib.reload(module)
    monkeypatch.setattr(module, "_s3", _StubS3())
    monkeypatch.setattr(module, "_events", _StubEvents())
    monkeypatch.setattr(module, "_dynamodb", _StubDynamoResource())
    monkeypatch.setattr(module, "_secrets", _StubSecrets())
    # The reload above gives a fresh cache, but stating it keeps a future change to
    # module scoping from leaking one test's credential into the next.
    module._secret_cache.clear()
    return module


@pytest.fixture
def normalizer(env, monkeypatch):
    module = importlib.import_module("corridor_event_hub.handlers.normalizer")
    importlib.reload(module)
    monkeypatch.setattr(module, "_s3", _StubS3(load_fixture("ok-odot-wzdx.json")))
    monkeypatch.setattr(module, "_events", _StubEvents())
    return module


def _logged(capsys) -> list[dict[str, Any]]:
    """Every structured log line the handler emitted, parsed."""
    out = []
    for line in capsys.readouterr().out.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


RAW_PAYLOAD_STORED = {
    "detail": {
        "sourceId": "ok-odot-wzdx",
        "agency": "Oklahoma DOT",
        "rawRef": "s3://amzn-s3-demo-rawzone/raw/x.json",
        "bucket": "amzn-s3-demo-rawzone",
        "key": "raw/x.json",
        "checksum": "abc123",
        "retrievedAt": "2026-08-07T22:00:00.000Z",
        "bytes": 100,
    }
}


class TestCollector:
    def test_rejects_an_unknown_source_id(self, collector):
        # A sourceId not in the catalog is a configuration error, and unlike a down
        # feed it SHOULD raise - it can only mean the schedule and the catalog have
        # diverged.
        with pytest.raises(ValueError, match="not in catalog"):
            collector.handler({"sourceId": "no-such-source"})

    def test_records_a_failed_fetch_without_raising(self, collector, monkeypatch, capsys):
        # The isolation NFR: one down state feed must not look like a system failure
        # or stop the other three.
        monkeypatch.setattr(
            collector, "_fetch", lambda url, headers: (503, "", "HTTP 503: unavailable")
        )
        result = collector.handler({"sourceId": "ok-odot-wzdx"})

        assert result["status"] == 503
        assert result["rawRef"] is None
        assert collector._s3.put_calls == []  # nothing stored
        assert collector._events.entries == []  # nothing announced

        logged = _logged(capsys)
        assert logged[0]["msg"] == "fetch_failed"
        assert logged[0]["sourceId"] == "ok-odot-wzdx"

    def test_records_source_health_even_when_the_fetch_fails(self, collector, monkeypatch):
        # A failed fetch is data. It degrades the confidence of every record
        # derived from that source.
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (0, "", "timeout"))
        collector.handler({"sourceId": "ok-odot-wzdx"})

        update = collector._dynamodb.table.updates[0]
        assert update["Key"] == {"sourceId": "ok-odot-wzdx"}
        assert "lastFailureAt" in update["UpdateExpression"]
        assert update["ExpressionAttributeValues"][":error"] == "timeout"

    def test_an_unresolvable_credential_reports_as_a_fetch_failure(
        self, collector, monkeypatch, capsys
    ):
        """A failure BEFORE the HTTP call must still reach the fetch-failure signals.

        This is the defect this test exists for. `_build_url` resolves the credential
        from Secrets Manager, and it used to sit outside the never-raises contract that
        `_fetch` and everything downstream of it observe - so a missing or denied secret
        ended the invocation early and the failure existed only as a Lambda error. No
        `fetch_failed` line, therefore no `CorridorEventHub/FetchFailures` data point, therefore
        an empty "Fetch failures" widget and per-source alarms that stayed OK while the
        source collected nothing. Measured against the deployed stack on 2026-08-17: 23
        such invocations, zero data points, every `CorridorEventHub-fetch-failures-*` alarm green.
        """

        class _MissingSecret:
            requested: list[str] = []

            def get_secret_value(self, SecretId):  # noqa: N803 - boto3's parameter name
                raise RuntimeError("ResourceNotFoundException: can't find the secret")

        monkeypatch.setattr(collector, "_secrets", _MissingSecret())
        monkeypatch.delenv("TOKEN_OK_ODOT_WZDX", raising=False)

        result = collector.handler({"sourceId": "ok-odot-wzdx"})

        assert result["status"] == 0
        assert result["rawRef"] is None
        assert collector._s3.put_calls == []

        logged = _logged(capsys)
        assert logged[0]["msg"] == "fetch_failed"
        assert logged[0]["sourceId"] == "ok-odot-wzdx"
        assert "ResourceNotFoundException" in logged[0]["error"]

        # And the catalog carries it, so `npm run status` does not call the source
        # healthy on the strength of a success from ten minutes ago.
        update = collector._dynamodb.table.updates[0]
        assert "lastFailureAt" in update["UpdateExpression"]

    def test_stores_raw_bytes_with_fetch_metadata(self, collector, monkeypatch):
        # Fetch metadata travels WITH the bytes, so a normalized record can
        # always be traced to the exact payload it came from.
        body = '{"features":[]}'
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, body, None))
        result = collector.handler({"sourceId": "ok-odot-wzdx"})

        put = collector._s3.put_calls[0]
        assert put["Bucket"] == "amzn-s3-demo-rawzone"
        assert put["Body"] == body.encode("utf-8")
        assert put["ChecksumAlgorithm"] == "SHA256"
        assert put["Metadata"]["source-id"] == "ok-odot-wzdx"
        assert put["Metadata"]["http-status"] == "200"
        assert result["rawRef"].startswith("s3://amzn-s3-demo-rawzone/raw/source=ok-odot-wzdx/")

    def test_never_writes_a_token_into_object_metadata(self, collector, monkeypatch):
        # The OK endpoint carries its token in the query string. Persisting the full
        # URL would leak a credential into S3 metadata, where nobody would think to
        # look for one.
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, "{}", None))
        collector.handler({"sourceId": "ok-odot-wzdx"})

        fetch_url = collector._s3.put_calls[0]["Metadata"]["fetch-url"]
        assert "?" not in fetch_url
        assert "access_token" not in fetch_url

    def test_no_source_carries_a_credential_literal_in_the_catalog(self, collector):
        """NO CREDENTIAL IN THE CATALOG. It is committed to git; a credential in it is committed.

        Asserted over EVERY source rather than over the one that had the problem, and
        by key shape rather than by name, because the failure mode is a future
        contributor adding `"apiKey"` or `"publicToken"` to a new entry - a
        one-source assertion would pass while the pattern spread.
        """
        forbidden = ("token", "key", "secret", "password", "credential")
        for source in collector._SOURCE_CATALOG["sources"]:
            for field, value in source.items():
                if field.startswith("$") or field in {"secretId", "eventClasses"}:
                    continue  # a pointer and a list of classes, not a value
                if not isinstance(value, str) or len(value) < 20:
                    continue
                assert not any(word in field.lower() for word in forbidden), (
                    f"{source['sourceId']}.{field} looks like a committed credential. "
                    "Put it in Secrets Manager and point `secretId` at it (ADR 0004)."
                )

    def test_resolves_the_oklahoma_token_from_secrets_manager(self, collector, monkeypatch):
        """It used to come from a literal in the handler module, which is the defect this pins.

        The literal is gone, so the only way this URL can carry a token is the secret.
        """
        seen = {}
        monkeypatch.setattr(
            collector, "_fetch", lambda url, headers: (seen.update(url=url), (200, "{}", None))[1]
        )
        collector.handler({"sourceId": "ok-odot-wzdx"})

        assert collector._secrets.requested == ["corridor-event-hub/ok-odot-wzdx-token"]
        assert "access_token=test-credential" in seen["url"]

    def test_the_env_override_wins_over_the_secret(self, collector, monkeypatch):
        # The path that makes a feed reachable from a laptop with no AWS credentials.
        seen = {}
        monkeypatch.setenv("TOKEN_OK_ODOT_WZDX", "from-the-environment")
        monkeypatch.setattr(
            collector, "_fetch", lambda url, headers: (seen.update(url=url), (200, "{}", None))[1]
        )
        collector.handler({"sourceId": "ok-odot-wzdx"})

        assert "access_token=from-the-environment" in seen["url"]
        assert collector._secrets.requested == []  # not even fetched

    def test_uses_the_query_parameter_name_the_catalog_gives(self, collector, monkeypatch):
        # Oklahoma spells it `access_token`, Texas and Arizona spell it `key`. The name
        # is catalog DATA, which is what let Oklahoma move to api_key_secret with no
        # new code branch.
        seen = {}
        monkeypatch.setattr(
            collector, "_fetch", lambda url, headers: (seen.update(url=url), (200, "{}", None))[1]
        )
        collector.handler({"sourceId": "tx-dot-wzdx"})

        assert "key=test-credential" in seen["url"]
        assert "access_token" not in seen["url"]

    def test_sends_an_identifying_user_agent_to_every_source(self, collector, monkeypatch):
        # FOUND THE HARD WAY. urllib defaults to `Python-urllib/3.x`, and
        # Cloudflare-fronted agency endpoints reject that outright: oktraffic.org
        # returns 403 with body `error code: 1010` for it while serving the identical
        # URL to curl. An entire state's feed disappears behind a 403 that reads like
        # an auth failure, so the UA is not optional and not per-source.
        seen = {}

        def capture(url, headers):
            seen.update(headers)
            return 200, "{}", None

        monkeypatch.setattr(collector, "_fetch", capture)
        monkeypatch.setenv("CEH_USER_AGENT", "TestApp (test@example.com)")
        collector.handler({"sourceId": "ok-odot-wzdx"})

        assert seen.get("User-Agent") == "TestApp (test@example.com)"
        assert "urllib" not in seen["User-Agent"]

    def test_sends_a_user_agent_even_when_the_catalog_does_not_demand_one(
        self, collector, monkeypatch
    ):
        # ok-odot-wzdx is `api_key_secret`, NOT `none_user_agent_required` - so a
        # UA keyed off the auth method would skip exactly the source that needs it.
        source = collector._catalog_entry("ok-odot-wzdx")
        assert source["authMethod"] != "none_user_agent_required"
        assert "User-Agent" in collector._build_headers(source)

    def test_uses_a_content_addressed_key_so_identical_bytes_do_not_duplicate(
        self, collector, monkeypatch
    ):
        # Idempotency. Two fetches of identical bytes in the same hour differ
        # only by timestamp; the checksum suffix is what makes duplicates visible.
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, "{}", None))
        collector.handler({"sourceId": "ok-odot-wzdx"})
        collector.handler({"sourceId": "ok-odot-wzdx"})

        first, second = (c["Key"] for c in collector._s3.put_calls)
        assert first.split("-")[-1] == second.split("-")[-1]  # same checksum

    def test_announces_raw_payload_stored(self, collector, monkeypatch):
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, "{}", None))
        collector.handler({"sourceId": "ok-odot-wzdx"})

        entry = collector._events.entries[0]
        assert entry["DetailType"] == "RawPayloadStored"
        assert entry["Source"] == "corridor-event-hub.collector"
        detail = json.loads(entry["Detail"])
        assert detail["sourceId"] == "ok-odot-wzdx"
        assert detail["checksum"]

    def test_emits_the_log_fields_the_dashboards_filter_on(self, collector, monkeypatch, capsys):
        # These names are a contract with lib/observability-stack.ts. Renaming one
        # blanks a dashboard widget silently.
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, "{}", None))
        collector.handler({"sourceId": "ok-odot-wzdx"})

        collected = next(line for line in _logged(capsys) if line["msg"] == "collected")
        for required in ("sourceId", "bytes", "latencyMs", "rawRef"):
            assert required in collected, f"metric filter reads $.{required}"

    def test_partitions_the_raw_key_for_replay_by_window(self, collector, monkeypatch):
        # Hive-style partitions so a replay can scan one hour and
        # Athena does not read the whole corridor's history.
        monkeypatch.setattr(collector, "_fetch", lambda url, headers: (200, "{}", None))
        collector.handler({"sourceId": "ok-odot-wzdx"})

        key = collector._s3.put_calls[0]["Key"]
        assert key.startswith("raw/source=ok-odot-wzdx/year=")
        for part in ("month=", "day=", "hour="):
            assert part in key


class TestCollectorTiledSource:
    """Collector - the tiled source path

    A tiled source's catalog endpoint is a {Z}/{X}/{Y} TEMPLATE, so routing it
    through the ordinary URL GET would request the placeholder literally and report
    a 403 that reads like an auth failure. These pin the branch that prevents it.
    """

    def test_does_not_url_fetch_a_source_whose_endpoint_is_a_tile_template(
        self, collector, monkeypatch
    ):
        url_fetches: list[str] = []
        monkeypatch.setattr(
            collector,
            "_fetch",
            lambda url, headers: (url_fetches.append(url), (200, "{}", None))[1],
        )
        monkeypatch.setattr(
            collector,
            "_fetch_tiles",
            lambda source: (200, '{"tiles": []}', None),
        )
        collector.handler({"sourceId": "aws-location-traffic"})

        # The URL path must not have been used at all.
        assert url_fetches == []

    def test_stores_the_tile_envelope_like_any_other_payload(self, collector, monkeypatch):
        monkeypatch.setattr(
            collector,
            "_fetch_tiles",
            lambda source: (200, '{"tiles": [{"z": 8, "x": 52, "y": 101}]}', None),
        )
        result = collector.handler({"sourceId": "aws-location-traffic"})

        assert result["status"] == 200
        assert result["rawRef"]
        # Raw persistence, health, and the announcement are all unchanged - that
        # uniformity is the point of the (status, body, error) shape.
        assert collector._s3.put_calls
        assert collector._events.entries

    def test_a_tile_fetch_failure_is_recorded_not_raised(self, collector, monkeypatch):
        # Same isolation rule as a down agency feed.
        monkeypatch.setattr(
            collector,
            "_fetch_tiles",
            lambda source: (0, "", "AccessDeniedException"),
        )
        result = collector.handler({"sourceId": "aws-location-traffic"})

        assert result["rawRef"] is None
        assert collector._s3.put_calls == []
        update = collector._dynamodb.table.updates[0]
        assert update["ExpressionAttributeValues"][":error"] == "AccessDeniedException"

    def test_an_empty_tile_set_is_a_failure_not_a_clear_corridor(self, collector, monkeypatch):
        # THE failure mode worth guarding: fetching zero tiles must not be stored as
        # a valid "no congestion anywhere" observation.
        def _no_tiles(region, zoom):
            return 0, '{"tiles": []}'

        monkeypatch.setattr(
            "corridor_event_hub.adapters.feeds.fetch_traffic_tiles", _no_tiles, raising=True
        )
        status, _body, error = collector._fetch_tiles(
            {"sourceId": "aws-location-traffic", "tileZoom": 8}
        )
        assert status != 200
        assert error

    def test_loads_the_corridor_from_postgres_before_walking_tiles(
        self, collector, monkeypatch
    ):
        """THE REGRESSION: tile addresses come from the corridor, and the corridor
        moved into Postgres. This handler kept reading a JSON file that is no longer
        bundled, so every poll failed with "no offline corridor found" while the five
        URL-fetched sources stayed green.
        """
        from corridor_event_hub.core import lrs

        monkeypatch.setenv("SPATIAL_DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:x")
        monkeypatch.setenv("CEH_ROUTE", "I-40")
        monkeypatch.setattr(lrs, "_corridor_source", None, raising=False)
        monkeypatch.setattr(lrs, "_active_route", None, raising=False)

        asked_for: list[str] = []

        def _fake_load(route, connection=None):
            asked_for.append(route)
            raise RuntimeError("database reached")

        monkeypatch.setattr("corridor_event_hub.core.postgis.load_corridor", _fake_load)

        # Reads the corridor the way the real tile walk does, so this exercises the
        # whole chain rather than just asserting an attribute got set.
        def _tiles_needing_the_corridor(region, zoom):
            from corridor_event_hub.core.lrs import corridor

            return 200, json.dumps({"tiles": [], "vertices": len(corridor.centerline)})

        monkeypatch.setattr(
            "corridor_event_hub.adapters.feeds.fetch_traffic_tiles", _tiles_needing_the_corridor
        )

        status, _body, error = collector._fetch_tiles(
            {"sourceId": "aws-location-traffic", "tileZoom": 8}
        )

        # Asked the DATABASE, for the CONFIGURED route. Without set_active_route it
        # would ask for "" - which sends active_corridor() back to the JSON file that
        # is no longer bundled, which is exactly the production failure.
        assert asked_for == ["I-40"]
        # And the reached-the-database error is recorded, not raised (isolation NFR).
        assert status == 0
        assert "database reached" in error

    def test_leaves_the_offline_corridor_alone_with_no_database(
        self, collector, monkeypatch
    ):
        # `npm run probe`, the UI API and this whole suite resolve the corridor from
        # JSON with no AWS account. Wiring Postgres in unconditionally would break
        # every one of them.
        from corridor_event_hub.core import lrs

        monkeypatch.delenv("SPATIAL_DB_SECRET_ARN", raising=False)
        monkeypatch.setattr(lrs, "_corridor_source", None, raising=False)

        collector._use_database_corridor()

        assert lrs._corridor_source is None


class TestNormalizer:
    def test_produces_candidates_from_a_stored_payload(self, normalizer, capsys):
        result = normalizer.handler(RAW_PAYLOAD_STORED)
        assert result["candidates"] > 0
        # `offCorridor` is REPORTED, not required to be non-zero.
        #
        # This asserted `> 0` and passed only because the placeholder centerline
        # rejected records that are genuinely on I-40. The contract being pinned
        # here is that the key is present and countable, because every metric
        # filter in lib/observability-stack.ts reads it - not that anything was
        # actually discarded.
        assert isinstance(result["offCorridor"], int)
        assert result["offCorridor"] == 0

    def test_never_re_fetches_the_source(self, normalizer):
        # Reading from S3 rather than the feed is what makes replay work.
        # The stub has no network, so a re-fetch would fail loudly here.
        normalizer.handler(RAW_PAYLOAD_STORED)
        assert normalizer._events.entries

    def test_publishes_candidate_events_with_a_provisional_confidence(self, normalizer):
        normalizer.handler(RAW_PAYLOAD_STORED)
        candidate_entries = [
            e for e in normalizer._events.entries if e["DetailType"] == "CandidateEventProduced"
        ]
        assert candidate_entries
        detail = json.loads(candidate_entries[0]["Detail"])
        assert detail["candidate"]["event_class"] == "work_zone"
        # A score never travels without its breakdown.
        assert len(detail["provisionalConfidence"]["breakdown"]) == 6

    def test_publishes_mapping_issues_as_first_class_output(self, normalizer):
        # Unmappable values go to the review queue, not to a log line nobody
        # reads.
        normalizer.handler(RAW_PAYLOAD_STORED)
        issue_entries = [
            e for e in normalizer._events.entries if e["DetailType"] == "MappingIssuesFound"
        ]
        assert len(issue_entries) == 1
        detail = json.loads(issue_entries[0]["Detail"])
        assert detail["totalIssues"] > 0
        assert detail["adapterSchemaVersion"] == "wzdx-4.0"

    def test_caps_the_issue_sample_but_reports_the_true_total(self, normalizer, monkeypatch):
        # An EventBridge entry is capped at 256KB. Truncating the sample is correct;
        # truncating the COUNT would understate a feed-wide format break.
        from corridor_event_hub.adapters.adapter import AdapterResult
        from corridor_event_hub.core.types import MappingIssue

        many = AdapterResult(
            candidates=[],
            off_corridor=0,
            issues=[
                MappingIssue(field=f"f{i}", raw_value=i, reason="unparseable")
                for i in range(250)
            ],
        )
        monkeypatch.setattr(
            normalizer.adapter_for("ok-odot-wzdx"), "parse", lambda body, ctx: many
        )
        normalizer.handler(RAW_PAYLOAD_STORED)

        detail = json.loads(
            next(
                e
                for e in normalizer._events.entries
                if e["DetailType"] == "MappingIssuesFound"
            )["Detail"]
        )
        assert len(detail["issues"]) == 100
        assert detail["totalIssues"] == 250

    def test_batches_put_events_at_ten_entries(self, normalizer, monkeypatch):
        # EventBridge rejects more than 10 entries per call. This is the kind of
        # limit that only bites once a feed gets busy.
        calls: list[int] = []
        original = normalizer._events.put_events

        def counting(Entries):  # noqa: N803
            calls.append(len(Entries))
            return original(Entries)

        monkeypatch.setattr(normalizer._events, "put_events", counting)
        normalizer.handler(RAW_PAYLOAD_STORED)
        assert all(count <= 10 for count in calls)

    def test_quarantines_a_payload_from_an_unregistered_source(self, normalizer, capsys):
        # An unregistered source is a real problem, not a no-op.
        event = {"detail": {**RAW_PAYLOAD_STORED["detail"], "sourceId": "not-registered"}}
        result = normalizer.handler(event)

        assert result == {"candidates": 0, "issues": 1, "offCorridor": 0}
        assert normalizer._events.entries[0]["DetailType"] == "PayloadQuarantined"
        assert _logged(capsys)[0]["msg"] == "no_adapter_registered"

    def test_emits_the_log_fields_the_dashboards_filter_on(self, normalizer, capsys):
        normalizer.handler(RAW_PAYLOAD_STORED)
        line = next(entry for entry in _logged(capsys) if entry["msg"] == "normalized")
        for required in ("sourceId", "candidates", "offCorridor", "issues"):
            assert required in line, f"metric filter reads $.{required}"
        assert len(line["confidenceRange"]) == 2

    def test_emits_valid_json_even_when_an_extent_is_unresolved(self, normalizer, monkeypatch):
        # The NaN case, end to end: EventBridge would reject a bare NaN token at
        # runtime, and this is the path where one could reach it.
        def reject(value):
            raise AssertionError(f"invalid JSON constant: {value}")

        normalizer.handler(RAW_PAYLOAD_STORED)
        for entry in normalizer._events.entries:
            json.loads(entry["Detail"], parse_constant=reject)
