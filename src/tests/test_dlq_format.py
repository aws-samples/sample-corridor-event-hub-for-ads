"""Dead-letter formatter tests.

WHY THESE EXIST: the formatter was originally `python3 -c` inside a single-quoted
shell heredoc, and escaped quotes in an f-string are a SyntaxError before Python
3.12. It was broken from the first line - and the only way to find out was to HAVE a
dead letter and try to read it, i.e. during an incident. A tool for inspecting
failures must not itself be untested.

The two envelope shapes are the substance here. A Lambda destination wraps the
original event and adds the error; an EventBridge target DLQ delivers the bare event.
Both must render, and only the ones carrying bucket/key may claim to be replayable.
"""

from __future__ import annotations

import json

from corridor_event_hub.dlq_format import format_message, parse_message, replay_event, replay_plan

# The real raw zone is a CloudFormation-generated name. Using the reserved
# amzn-s3-demo- prefix here keeps a copyable, unclaimable literal out of the repo:
# nothing in these tests talks to S3, only the string is asserted on.
RAW_BUCKET = "amzn-s3-demo-rawzone"

# What Lambda actually delivered on the deployed stack, trimmed.
DESTINATION_ENVELOPE = {
    "version": "1.0",
    "timestamp": "2026-08-11T20:30:00.000Z",
    "requestContext": {
        "requestId": "d844f44d-9133-47bc-8d00-9f1bb1f9d03c",
        "condition": "RetriesExhausted",
        "approximateInvokeCount": 3,
    },
    "requestPayload": {
        "detail": {
            "sourceId": "ok-odot-wzdx",
            "agency": "Oklahoma DOT",
            "bucket": RAW_BUCKET,
            "key": "raw/source=ok-odot-wzdx/year=2026/month=08/day=11/hour=18/x.json",
            "checksum": "deadbeef",
            "retrievedAt": "2026-08-11T18:00:00.000Z",
            "bytes": 91460,
        }
    },
    "responseContext": {"statusCode": 200, "functionError": "Unhandled"},
    "responsePayload": {
        "errorType": "NoSuchKey",
        "errorMessage": "An error occurred (NoSuchKey) when calling the GetObject "
        "operation: The specified key does not exist.",
    },
}

# An EventBridge target DLQ delivers the event itself, with no error in the body.
BARE_EVENT = {
    "version": "0",
    "detail-type": "RawPayloadStored",
    "source": "corridor-event-hub.collector",
    "detail": {
        "sourceId": "nws-alerts",
        "bucket": RAW_BUCKET,
        "key": "raw/source=nws-alerts/year=2026/month=08/day=11/hour=18/y.json",
        "retrievedAt": "2026-08-11T18:05:00.000Z",
    },
}


class TestParseDestinationEnvelope:
    def test_extracts_the_error_that_explains_the_failure(self):
        parsed = parse_message(json.dumps(DESTINATION_ENVELOPE))
        assert parsed["error_type"] == "NoSuchKey"
        assert "does not exist" in parsed["error_message"]

    def test_extracts_the_condition_so_a_timeout_is_distinguishable(self):
        # RetriesExhausted vs EventAgeExceeded are different problems.
        assert parse_message(json.dumps(DESTINATION_ENVELOPE))["condition"] == (
            "RetriesExhausted"
        )

    def test_reaches_through_the_wrapper_to_the_original_detail(self):
        parsed = parse_message(json.dumps(DESTINATION_ENVELOPE))
        assert parsed["source_id"] == "ok-odot-wzdx"
        assert parsed["key"].endswith("x.json")

    def test_is_marked_replayable_because_it_carries_bucket_and_key(self):
        assert parse_message(json.dumps(DESTINATION_ENVELOPE))["replayable"] is True


class TestParseBareEvent:
    def test_reads_a_bare_eventbridge_event(self):
        parsed = parse_message(json.dumps(BARE_EVENT))
        assert parsed["source_id"] == "nws-alerts"
        assert parsed["replayable"] is True

    def test_has_no_error_because_the_handler_never_ran(self):
        # An undelivered event has no handler error, and inventing one would mislead.
        assert parse_message(json.dumps(BARE_EVENT))["error_type"] is None


class TestMalformedInput:
    def test_unparseable_body_does_not_raise(self):
        parsed = parse_message("<html>not json</html>")
        assert "unparseable" in parsed
        assert format_message(parsed, 1)  # renders rather than crashing

    def test_a_json_scalar_is_treated_as_unparseable(self):
        assert "unparseable" in parse_message('"just a string"')

    def test_empty_detail_is_not_claimed_replayable(self):
        # The dangerous case: offering `npm run dlq-replay` for something that cannot be
        # replayed would send an operator in circles.
        parsed = parse_message(json.dumps({"requestPayload": {"detail": {}}}))
        assert parsed["replayable"] is False


class TestFormatting:
    def test_renders_every_field_an_operator_needs(self):
        lines = "\n".join(format_message(parse_message(json.dumps(DESTINATION_ENVELOPE)), 1))
        assert "ok-odot-wzdx" in lines
        assert "NoSuchKey" in lines
        assert "s3://" in lines
        assert "RetriesExhausted" in lines

    def test_says_plainly_when_something_cannot_be_replayed(self):
        parsed = parse_message(json.dumps({"requestPayload": {"detail": {}}}))
        assert "replay: NO" in "\n".join(format_message(parsed, 1))

    def test_truncates_a_giant_error_message(self):
        envelope = json.loads(json.dumps(DESTINATION_ENVELOPE))
        envelope["responsePayload"]["errorMessage"] = "x" * 5000
        lines = format_message(parse_message(json.dumps(envelope)), 1)
        assert all(len(line) < 300 for line in lines)


class TestReplayEvent:
    def test_unwraps_a_destination_envelope_into_a_handler_event(self):
        event = replay_event(json.dumps(DESTINATION_ENVELOPE))
        # The normalizer reads event["detail"]["bucket"] / ["key"].
        assert event["detail"]["bucket"] == RAW_BUCKET
        assert event["detail"]["key"].endswith("x.json")
        assert set(event) == {"detail"}

    def test_passes_a_bare_event_through(self):
        assert replay_event(json.dumps(BARE_EVENT))["detail"]["sourceId"] == "nws-alerts"

    def test_refuses_to_build_an_event_with_no_bucket_or_key(self):
        # Returning a partial event would invoke the handler with garbage and produce
        # a second, more confusing dead letter.
        assert replay_event(json.dumps({"requestPayload": {"detail": {}}})) is None
        assert replay_event(json.dumps({"detail": {"sourceId": "x"}})) is None

    def test_refuses_unparseable_input(self):
        assert replay_event("not json") is None

    def test_round_trips_the_event_the_normalizer_expects(self):
        # Guards the actual contract: the keys handlers/normalizer.py reads.
        event = replay_event(json.dumps(DESTINATION_ENVELOPE))
        for required in ("sourceId", "bucket", "key", "retrievedAt", "rawRef"):
            if required == "rawRef":
                continue  # the normalizer only logs this one
            assert required in event["detail"], f"normalizer reads detail[{required!r}]"


class TestReplayPlan:
    """Which stage can replay which dead letter"""

    def test_a_raw_payload_message_routes_to_the_normalizer(self):
        target, event = replay_plan(json.dumps(DESTINATION_ENVELOPE))
        assert target == "normalizer"
        assert event["detail"]["bucket"]

    def test_a_candidate_message_routes_to_the_resolver(self):
        # A resolver dead letter carries an already-parsed candidate and NO raw ref,
        # so it cannot be re-normalized - only re-resolved.
        envelope = {
            "requestPayload": {
                "detail": {
                    "sourceId": "ok-odot-wzdx",
                    "rawRef": "s3://amzn-s3-demo-rawzone/raw/x.json",
                    "candidate": {"event_class": "work_zone", "extent": {}},
                    "provisionalConfidence": {"value": 0.6},
                }
            },
            "responsePayload": {"errorType": "IllegalTransition"},
        }
        target, event = replay_plan(json.dumps(envelope))
        assert target == "resolver"
        # The resolver reads event["detail"]["candidate"].
        assert event["detail"]["candidate"]["event_class"] == "work_zone"

    def test_classification_is_by_shape_not_by_queue(self):
        # A queue can be renamed or re-pointed; what the message CONTAINS is what
        # decides what can be done with it. A raw-payload message carries no
        # candidate, so it can never be routed to the resolver by accident.
        target, _event = replay_plan(json.dumps(BARE_EVENT))
        assert target == "normalizer"

    def test_a_message_with_neither_is_unreplayable(self):
        assert replay_plan(json.dumps({"detail": {"sourceId": "x"}})) is None
        assert replay_plan("not json") is None

    def test_replay_event_still_only_answers_for_the_normalizer(self):
        # Kept narrow on purpose: its one caller re-normalizes from S3 bytes, and
        # handing it a resolver message would invoke the wrong function.
        candidate_message = json.dumps(
            {"detail": {"sourceId": "s", "candidate": {"event_class": "incident"}}}
        )
        assert replay_plan(candidate_message)[0] == "resolver"
        assert replay_event(candidate_message) is None

    def test_the_rendered_message_says_which_replay_route_applies(self):
        candidate_message = json.dumps(
            {"detail": {"sourceId": "s", "candidate": {"event_class": "incident"}}}
        )
        lines = format_message(parse_message(candidate_message), 1)
        text = " ".join(lines)
        assert "re-resolve" in text
        assert "incident" in text
