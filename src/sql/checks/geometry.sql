-- CORRIDOR GEOMETRY HEALTH
--
-- Run:  make db-geometry
--
-- Answers "did the real centerline actually land, and is it calibrated?" - which
-- is not the same question as "did 002 apply". 002 can report every statement ok
-- and still leave centerline_m NULL if the chunked assembly produced nothing.
--
-- THE ROW THAT MATTERS MOST is `M end - total_miles`. centerline_m carries the
-- corridor measure in its M dimension, chained from each state's own LRS. If the
-- last vertex's M does not equal total_miles, then conflate_point and
-- measure_to_milepost disagree about where the corridor ends, and every position
-- past the divergence is wrong by that much.
--
-- `largest offset gap` is the state-line invariant in aggregate: each state's extent
-- must start exactly where the previous one ended. A nonzero gap means a state is
-- missing, doubled, or has the wrong milepost range - the failure that makes AZ
-- MP 359.349 and NM MP 0 stop being the same physical place.
--
-- One corridor is assumed (LIMIT 1). Reading the route from the table rather than
-- naming it keeps this file portable to another corridor.

WITH c AS (
  SELECT route, centerline, centerline_m, total_miles, verified, buffer_meters
  FROM corridor ORDER BY route LIMIT 1
),
g AS (
  SELECT
    ST_NPoints(centerline::geometry)                      AS pts_2d,
    ST_NPoints(centerline_m)                              AS pts_m,
    ST_M(ST_StartPoint(centerline_m))                     AS m_start,
    ST_M(ST_EndPoint(centerline_m))                       AS m_end,
    ST_SRID(centerline_m)                                 AS srid_m,
    ST_IsValid(centerline_m)                              AS valid_m,
    ROUND((ST_Length(centerline) / 1609.344)::numeric, 3) AS geodesic_mi,
    route, total_miles, verified, buffer_meters
  FROM c
),
seg AS (
  SELECT state, corridor_offset, segment, verified,
         state_mp_max - state_mp_min                            AS mp_span,
         corridor_offset + (state_mp_max - state_mp_min)        AS ends_at,
         lead(corridor_offset) OVER (ORDER BY corridor_offset)  AS next_offset,
         lead(segment)         OVER (ORDER BY corridor_offset)  AS next_segment
  FROM state_segment
),
-- The GEOMETRIC state-line check, which is what state_segment.segment exists for.
--
-- `largest offset gap` below checks the boundary ARITHMETICALLY: state N's offset
-- plus its span against state N+1's offset. That can be perfectly self-consistent
-- and still wrong, because it only compares numbers we wrote to numbers we wrote.
-- This compares PLACES - the last point of one state's geometry against the first
-- point of the next. If they are not the same spot, the offsets are lying about
-- where the border is, which is exactly how AZ MP 359.349 and NM MP 0 stop being
-- the same physical location and cross-state dedup goes quietly wrong.
boundary AS (
  SELECT ST_Distance(ST_EndPoint(segment::geometry)::geography,
                     ST_StartPoint(next_segment::geometry)::geography) AS seam_m
  FROM seg
  WHERE segment IS NOT NULL AND next_segment IS NOT NULL
)
SELECT fact, value, expect FROM (
          SELECT 1 AS ord, 'route'::text AS fact, route::text AS value,
                 'one corridor'::text AS expect FROM g
UNION ALL SELECT 2,  'centerline 2D vertices', pts_2d::text,
                 'thousands; 40 means the placeholder is still loaded' FROM g
UNION ALL SELECT 3,  'centerline_m vertices',
                 COALESCE(pts_m::text, 'NOT LOADED - the M column is NULL'),
                 'same as the 2D count' FROM g
UNION ALL SELECT 4,  'M range (miles)',
                 COALESCE(ROUND(m_start::numeric, 3)::text || ' -> '
                          || ROUND(m_end::numeric, 3)::text, 'NOT LOADED'),
                 'starts at 0' FROM g
UNION ALL SELECT 5,  'corridor.total_miles', COALESCE(total_miles::text, 'NULL'),
                 'set; conflate_point scales by it when M is absent' FROM g
UNION ALL SELECT 6,  'M end - total_miles',
                 COALESCE(ROUND((m_end - total_miles)::numeric, 3)::text, 'n/a'),
                 '0.000 - anything else is a real inconsistency' FROM g
UNION ALL SELECT 7,  'geodesic length (miles)', geodesic_mi::text,
                 'a little UNDER total_miles - LRS mileage exceeds chord length' FROM g
UNION ALL SELECT 8,  'SRID of centerline_m', COALESCE(srid_m::text, 'n/a'),
                 '4326' FROM g
UNION ALL SELECT 9,  'centerline_m valid', COALESCE(valid_m::text, 'n/a'),
                 'true' FROM g
UNION ALL SELECT 10, 'corridor.verified', verified::text,
                 'true - EARNED by the landmark check, not signed off' FROM g
UNION ALL SELECT 11, 'buffer_meters', buffer_meters::text,
                 '1600 - the on-corridor test radius' FROM g
UNION ALL SELECT 12, 'state_segment rows', count(*)::text, '4' FROM seg
UNION ALL SELECT 13, 'largest offset gap (miles)',
                 COALESCE(ROUND(MAX(ABS(next_offset - ends_at)), 3)::text, 'n/a'),
                 '0.000 - a gap breaks cross-state dedup' FROM seg
                 WHERE next_offset IS NOT NULL
UNION ALL SELECT 14, 'state_segment.segment populated',
                 count(*) FILTER (WHERE segment IS NOT NULL)::text
                 || ' of ' || count(*)::text,
                 '4 of 4 - NULL means boundaries rest on arithmetic alone' FROM seg
UNION ALL SELECT 15, 'segments that are one LineString',
                 count(*) FILTER (WHERE segment IS NOT NULL
                   AND GeometryType(segment::geometry) = 'LINESTRING')::text,
                 '4 - a MULTILINESTRING means the corridor has a hole' FROM seg
UNION ALL SELECT 16, 'worst segment length shortfall',
                 COALESCE(ROUND(MAX(
                   100.0 * (mp_span - ST_Length(segment) / 1609.344) / mp_span
                 )::numeric, 3)::text || ' %', 'n/a'),
                 'under 1 % - measured 0.15 to 0.21 per state' FROM seg
                 WHERE segment IS NOT NULL
UNION ALL SELECT 17, 'largest GEOMETRIC seam gap (m)',
                 COALESCE(ROUND(MAX(seam_m)::numeric, 1)::text, 'n/a'),
                 '0.0 - states share a boundary vertex. THE check row 13 cannot do'
                 FROM boundary
UNION ALL SELECT 18, 'state_segment.verified',
                 count(*) FILTER (WHERE verified)::text || ' of ' || count(*)::text,
                 '4 of 4 - EARNED per state from its own segment geometry' FROM seg
) t ORDER BY ord;
