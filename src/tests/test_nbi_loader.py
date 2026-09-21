"""NBI loader tests.

WHY THESE EXIST: every one of these functions fails SILENTLY and produces a
plausible wrong answer, on the one class where being wrong strands a truck under a
bridge.

  - a sentinel read as a measurement makes every over-height check PASS
  - one digit width used for both coordinates puts longitude in the Atlantic,
    at a number that looks like a longitude
  - an unanchored route match picks up '40TH ST'

None of them raise. All of them were verified against the real 2025 files, and the
numbers in these tests are the ones those files actually contain - so a change in
next year's vintage shows up as a failing test rather than as a shifted milepost.

THE MOST IMPORTANT TEST IN THIS FILE is
``test_the_loaders_sentinel_threshold_matches_the_schema_constraint``. The loader
maps implausible clearances to NULL and the schema CHECK rejects them; if the two
numbers ever disagree, one of them turns a mapping decision into a failed
migration. That is exactly what happened: the 2025 files carry 30.48 m (100.00 ft
exactly) on 19 corridor structures, the CHECK bound is 30, and the load would have
aborted.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    """Import scripts/fetch-nbi.py, whose hyphen makes it un-importable by name."""
    path = ROOT / "scripts" / "fetch-nbi.py"
    spec = importlib.util.spec_from_file_location("fetch_nbi", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nbi = _load_script()


class TestCoordinateDecoding:
    def test_latitude_is_eight_digits_and_longitude_is_nine(self):
        # Real values from OK25.txt, structure 1 on I-40. The whole trap: same
        # string layout, different degree widths.
        assert nbi.decode_dms("35251672", 2) == pytest.approx(35.4213, abs=1e-4)
        assert nbi.decode_dms("099134878", 3) == pytest.approx(99.2302, abs=1e-4)

    def test_using_the_wrong_width_for_longitude_is_rejected_not_guessed(self):
        # A 9-digit longitude read with 2 degree digits used to yield ~10.5 - a
        # plausible-looking number in the Atlantic. Returning None instead means the
        # structure is unresolved rather than confidently misplaced.
        assert nbi.decode_dms("099134878", 2) is None

    def test_blank_and_all_zero_mean_not_recorded(self):
        assert nbi.decode_dms("", 2) is None
        assert nbi.decode_dms("   ", 2) is None
        assert nbi.decode_dms("00000000", 2) is None

    def test_an_impossible_latitude_is_decoded_not_corrected(self):
        # NM 000000000007211 really carries '03585664' -> 3.98 N, off Africa. The
        # parser must NOT invent a fix; conflate_point's buffer test rejects it, and
        # the row lands with a NULL measure. Unresolved beats confidently wrong.
        assert nbi.decode_dms("03585664", 2) == pytest.approx(3.9824, abs=1e-3)


class TestSentinelClearances:
    @pytest.mark.parametrize("value", ["99.99", "0", "0.00", "30.48", "30.45", "31.00", ""])
    def test_every_flavour_of_no_restriction_becomes_unknown(self, value):
        metres, reason = nbi.clearance_metres(value)
        assert metres is None, f"{value} must not be loaded as a real clearance"
        assert reason, "an unknown must carry a reason so the ratio can be reported"

    @pytest.mark.parametrize("value,feet", [("4.36", 14.30), ("4.42", 14.50), ("9.14", 29.99)])
    def test_real_measurements_survive(self, value, feet):
        metres, reason = nbi.clearance_metres(value)
        assert reason is None
        assert metres * 3.28084 == pytest.approx(feet, abs=0.01)

    def test_the_hundred_foot_sentinel_says_so_in_feet(self):
        # 30.48 m reads as an ordinary number in metres and is obviously a sentinel
        # in feet. The reason string carries both so a log line explains itself.
        _, reason = nbi.clearance_metres("30.48")
        assert "100.00 ft" in reason

    def test_garbage_is_unknown_rather_than_an_exception(self):
        metres, reason = nbi.clearance_metres("N/A")
        assert metres is None
        assert "unparseable" in reason

    def test_the_loaders_sentinel_threshold_matches_the_schema_constraint(self):
        """The coupling that broke, pinned.

        The loader decides what is a sentinel; the CHECK decides what may be stored.
        If the loader's bound is HIGHER than the constraint's, a value between them
        aborts the migration. If it is LOWER, real clearances are silently discarded.
        They have to be the same number, and it is written down in two files.
        """
        schema = (ROOT / "sql" / "001-init.sql").read_text(encoding="utf-8")
        match = re.search(r"min_vert_clearance_m\s*<\s*([0-9.]+)", schema)
        assert match, "clearance_sane CHECK not found in 001-init.sql - did it move?"
        assert float(match.group(1)) == nbi.SENTINEL_CLEARANCE_M


class TestRouteMatching:
    @pytest.mark.parametrize(
        "text",
        ["I 40", "I-40", "I40", "IH 40", "IH 0040", "IH0040", "Interstate 40", "'I-40 EB'"],
    )
    def test_matches_how_agencies_actually_write_it(self, text):
        assert nbi.route_pattern("I-40").search(text), text

    @pytest.mark.parametrize("text", ["I 40TH ST", "IH 20 N FR", "US 40", "I-4", "SH 400"])
    def test_does_not_match_a_different_road(self, text):
        # 'I 40TH ST' is the one that matters: an unanchored substring test picks it
        # up and puts a city street's clearance on the interstate.
        assert not nbi.route_pattern("I-40").search(text), text

    def test_the_corridor_number_comes_from_config_not_from_here(self):
        # This module names no corridor. Another route must just work.
        assert nbi.route_pattern("I-10").search("IH 0010")
        assert not nbi.route_pattern("I-10").search("I 40")


class TestSelection:
    def _row(self, **overrides):
        row = {
            "STRUCTURE_NUMBER_008": "X1",
            "ROUTE_PREFIX_005B": "5",
            "ROUTE_NUMBER_005D": "00066",
            "MIN_VERT_CLR_010": "99.99",
            "VERT_CLR_UND_REF_054A": "N",
            "VERT_CLR_UND_054B": "0",
            "FEATURES_DESC_006A": "DRY WASH",
            "FACILITY_CARRIED_007": "SOME RD",
            "LAT_016": "35251672",
            "LONG_017": "099134878",
        }
        row.update(overrides)
        return row

    def _select(self, rows):
        import collections

        return nbi.select(rows, "I-40", nbi.route_pattern("I-40"), "OK", collections.Counter())

    def test_ignores_a_structure_unrelated_to_the_corridor(self):
        assert self._select([self._row()]) == {}

    def test_a_structure_carrying_the_corridor_uses_item_10(self):
        row = self._row(ROUTE_PREFIX_005B="1", ROUTE_NUMBER_005D="00040", MIN_VERT_CLR_010="4.42")
        picked = self._select([row])["X1"]
        assert picked["relation"] == "carries"
        assert picked["clearance_item"] == "010"
        assert picked["min_vert_clearance_m"] == pytest.approx(4.42)

    def test_a_structure_crossing_over_the_corridor_uses_item_54B(self):
        # The case the original design missed entirely, and where 367 of the 387
        # usable clearances live.
        row = self._row(
            VERT_CLR_UND_REF_054A="H", VERT_CLR_UND_054B="4.80", FEATURES_DESC_006A="I 40"
        )
        picked = self._select([row])["X1"]
        assert picked["relation"] == "crosses"
        assert picked["clearance_item"] == "054B"
        assert picked["min_vert_clearance_m"] == pytest.approx(4.80)

    def test_underclearance_only_counts_when_a_HIGHWAY_is_underneath(self):
        # 54A says what is below. A railroad's clearance is not a truck's.
        row = self._row(
            VERT_CLR_UND_REF_054A="R", VERT_CLR_UND_054B="4.80", FEATURES_DESC_006A="I 40"
        )
        assert self._select([row]) == {}

    def test_when_both_readings_apply_the_MORE_RESTRICTIVE_one_wins(self):
        # 25 real structures both carry and cross the corridor. Taking the larger
        # number would publish headroom that is not there.
        row = self._row(
            ROUTE_PREFIX_005B="1",
            ROUTE_NUMBER_005D="00040",
            MIN_VERT_CLR_010="5.50",
            VERT_CLR_UND_REF_054A="H",
            VERT_CLR_UND_054B="4.10",
            FEATURES_DESC_006A="I 40",
        )
        picked = self._select([row])["X1"]
        assert picked["min_vert_clearance_m"] == pytest.approx(4.10)
        assert picked["clearance_item"] == "054B"

    def test_a_sentinel_does_not_beat_a_real_measurement(self):
        # min() over both readings would be wrong if a sentinel became a number.
        row = self._row(
            ROUTE_PREFIX_005B="1",
            ROUTE_NUMBER_005D="00040",
            MIN_VERT_CLR_010="99.99",
            VERT_CLR_UND_REF_054A="H",
            VERT_CLR_UND_054B="4.10",
            FEATURES_DESC_006A="I 40",
        )
        assert self._select([row])["X1"]["min_vert_clearance_m"] == pytest.approx(4.10)

    def test_a_structure_with_no_known_clearance_is_still_recorded(self):
        # Present-but-unknown is a fact worth storing. Dropping it would make the
        # table look like the corridor has fewer structures than it does.
        row = self._row(ROUTE_PREFIX_005B="1", ROUTE_NUMBER_005D="00040")
        picked = self._select([row])["X1"]
        assert picked["min_vert_clearance_m"] is None
        assert picked["raw"], "The full record is kept regardless"

    def test_longitude_is_negated_because_NBI_publishes_it_unsigned(self):
        row = self._row(ROUTE_PREFIX_005B="1", ROUTE_NUMBER_005D="00040")
        assert self._select([row])["X1"]["lon"] < 0

    def test_route_is_the_CORRIDOR_not_the_road_the_structure_carries(self):
        # What the LRS view joins on. A county road here yields a NULL milepost.
        row = self._row(
            VERT_CLR_UND_REF_054A="H",
            VERT_CLR_UND_054B="4.80",
            FEATURES_DESC_006A="I 40",
            FACILITY_CARRIED_007="COUNTY RD 12",
        )
        picked = self._select([row])["X1"]
        assert picked["route"] == "I-40"
        assert picked["facility_carried"] == "COUNTY RD 12"

    def test_keeps_every_raw_field(self):
        row = self._row(ROUTE_PREFIX_005B="1", ROUTE_NUMBER_005D="00040")
        assert set(self._select([row])["X1"]["raw"]) == set(row)


class TestSqlEscaping:
    def test_an_apostrophe_in_a_facility_name_is_doubled(self):
        # NBI is full of names like "O'BRIEN CREEK". One unescaped quote in a 5 MB
        # generated file is a syntax error 1,500 statements from where you are looking.
        assert nbi.sql_string("O'BRIEN CREEK") == "'O''BRIEN CREEK'"

    def test_blank_becomes_NULL_not_an_empty_string(self):
        assert nbi.sql_string("") == "NULL"
        assert nbi.sql_string(None) == "NULL"
