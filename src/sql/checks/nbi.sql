-- NBI STRUCTURE LOAD HEALTH  (class 7, dimensional_restriction)
--
-- Run:  make db-nbi
--
-- Four numbers that are easy to conflate and must not be:
--
--   structures       rows loaded
--   clearance_known  rows with a REAL clearance. NBI encodes "no restriction" as
--                    the sentinel 99.99 and sometimes 0, and both must have become
--                    NULL on the way in. Of 409 I-40 structures in Oklahoma, only
--                    30 had a real clearance - a low ratio here is EXPECTED.
--   conflated        rows with corridor_measure set, i.e. matched to the corridor
--   usable_c3        rows with BOTH. This is the only number the over-height query
--                    can act on, and it is what corridor_clearances contains.
--
-- ROWS ABSENT FROM usable_c3 ARE UNKNOWN, NOT UNRESTRICTED. A structure
-- with no clearance on file is not a structure a 15-foot load may pass under.
--
-- no_route matters more than it looks: corridor_clearances joins back through
-- measure_to_milepost(route, ...), so a NULL route yields a NULL milepost. The row
-- still appears, with no state or milepost to locate it by.
--
-- sentinel_suspects should be 0. The clearance_sane CHECK makes 99.99 and 0
-- impossible to insert, so this catches the subtler version: a value just inside
-- the constraint that is really a sentinel in different units.

-- WHERE THE NUMBERS SHOULD COME FROM, measured against the 2025 vintage:
-- from_054B should DWARF from_010. Item 10 is the clearance over the roadway a
-- structure carries, and 96% of those on this corridor are the no-restriction
-- sentinel; item 54B is the clearance UNDER a structure spanning the corridor,
-- which is what actually stops a tall load. Expect roughly 20 from item 10 and
-- 367 from 54B. from_010 alone means the loader is only seeing half the picture.

SELECT
  count(*)                                                          AS structures,
  count(*) FILTER (WHERE min_vert_clearance_m IS NOT NULL)           AS clearance_known,
  -- KNOWN clearances only. Counting every row labelled 010 instead answers a
  -- different and useless question - 1,176 structures carry the corridor and 96% of
  -- them report no restriction, so the label count says nothing about usable data.
  count(*) FILTER (WHERE clearance_item = '010'
                     AND min_vert_clearance_m IS NOT NULL)           AS known_010,
  count(*) FILTER (WHERE clearance_item = '054B'
                     AND min_vert_clearance_m IS NOT NULL)           AS known_054B,
  count(*) FILTER (WHERE relation = 'crosses')                       AS crosses,
  count(*) FILTER (WHERE relation = 'carries')                       AS carries,
  count(*) FILTER (WHERE corridor_measure IS NOT NULL)               AS conflated,
  count(*) FILTER (WHERE min_vert_clearance_m IS NOT NULL
                     AND corridor_measure IS NOT NULL)               AS usable_c3,
  count(*) FILTER (WHERE location IS NULL)                           AS no_location,
  count(*) FILTER (WHERE location IS NOT NULL
                     AND corridor_measure IS NULL)                   AS located_but_off_corridor,
  count(DISTINCT state)                                              AS states,
  min(nbi_year)::text || '-' || max(nbi_year)::text                  AS nbi_years,
  ROUND(MIN(min_vert_clearance_m) * 3.28084, 2)                      AS lowest_ft,
  count(*) FILTER (WHERE min_vert_clearance_m * 3.28084 < 14.0)      AS under_14ft,
  count(*) FILTER (WHERE min_vert_clearance_m > 25)                  AS sentinel_suspects
FROM bridge_structure;
