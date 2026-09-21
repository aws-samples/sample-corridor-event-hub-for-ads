"""Tests for the cloud reader's PURE parts.

WHAT IS AND IS NOT TESTED HERE, stated plainly so the coverage is not overread:
the AWS calls in core/cloud.py are not exercised - CI has no account, and a moto
double would assert that boto3 works rather than that these reads are right. What
IS tested is everything that decides what those calls mean, because that is where
a mistake is silent:

  - the Decimal conversion, without which every number in a trace document is a
    Decimal and the JSON encoder refuses the whole response;
  - pulling one agency record out of a multi-megabyte payload by its native id,
    across the differently-shaped payloads the six feeds return;
  - the s3:// reference parsing, including the refusals - this is the one endpoint
    that turns stored data into a GetObject;
  - whether a history slice knows it is a slice, which is what stops a windowed
    read from being presented as a whole history.

The live reads are verified by running the thing: `npm run trace` prints the
account, table and bucket it resolved before serving a byte.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from corridor_event_hub.core.cloud import (
    HistorySlice,
    _content_range_total,
    _find_record,
    _plain,
    read_raw,
)


class TestPlain:
    def test_an_integral_decimal_becomes_an_int_not_a_float(self):
        """`version: 3.0` would read as a float to any consumer with a typed schema."""
        assert _plain(Decimal("3")) == 3
        assert isinstance(_plain(Decimal("3")), int)

    def test_a_fractional_decimal_becomes_a_float(self):
        assert _plain(Decimal("0.7089")) == pytest.approx(0.7089)

    def test_nested_structures_are_converted_throughout(self):
        got = _plain({"a": [Decimal("1"), {"b": Decimal("2.5")}], "c": "x"})
        assert got == {"a": [1, {"b": 2.5}], "c": "x"}

    def test_booleans_survive(self):
        """bool is an int subclass, which is how it gets turned into 1 by accident."""
        assert _plain({"inferred": True}) == {"inferred": True}
        assert _plain({"inferred": True})["inferred"] is True


class TestFindRecord:
    def test_a_wzdx_style_feature_collection_is_searched_by_feature_id(self):
        payload = '{"features": [{"id": "250182-1", "properties": {"core_details": {}}}]}'
        assert _find_record(payload, "250182-1")["id"] == "250182-1"

    def test_an_nws_style_id_under_properties_is_found(self):
        """NWS carries the urn on the feature AND under properties; either must hit."""
        payload = '{"features": [{"type": "Feature", "properties": {"id": "urn:oid:1.2.3"}}]}'
        found = _find_record(payload, "urn:oid:1.2.3")
        assert found is not None
        assert found["properties"]["id"] == "urn:oid:1.2.3"

    def test_an_az511_style_uppercase_id_is_found(self):
        assert _find_record('[{"ID": 12345, "Description": "x"}]', "12345") is not None

    def test_a_missing_id_returns_none_rather_than_a_wrong_record(self):
        assert _find_record('{"features": [{"id": "a"}]}', "b") is None

    def test_a_non_json_payload_returns_none_rather_than_raising(self):
        """A traffic tile is protobuf. Not finding a record in it is expected."""
        assert _find_record("\x00\x01not json", "anything") is None


class TestContentRange:
    def test_the_total_is_read_from_the_range_header(self):
        assert _content_range_total("bytes 0-511999/4284445") == 4284445

    def test_a_missing_or_unparseable_header_is_none_rather_than_zero(self):
        """Zero would read as "an empty object", which is a different claim."""
        assert _content_range_total(None) is None
        assert _content_range_total("bytes */*") is None


class TestRawRefParsing:
    def test_a_non_s3_reference_is_refused(self):
        with pytest.raises(ValueError, match="not an s3 reference"):
            read_raw("https://example.com/x.json")

    def test_a_reference_with_no_key_is_refused(self):
        with pytest.raises(ValueError, match="malformed"):
            read_raw("s3://amzn-s3-demo-rawzone")


class TestHistorySlice:
    def test_a_complete_read_does_not_claim_to_be_windowed(self):
        slice_ = HistorySlice(
            versions=[1, 2], audit=[1, 2], version_total=2, audit_total=2, window=200
        )
        assert slice_.windowed is False
        assert slice_.totals()["windowed"] is False

    def test_a_partial_read_knows_it_is_partial(self):
        """The whole point: 200 of 1,564 steps must never look like all of them."""
        slice_ = HistorySlice(
            versions=[1] * 201, audit=[1] * 200, version_total=1564, audit_total=1564, window=200
        )
        assert slice_.windowed is True
        assert slice_.totals() == {
            "versions": 1564,
            "audit": 1564,
            "windowed": True,
            "window": 200,
        }
