-- Corridor Event Hub spatial schema
--
-- This is the "LRS as inspectable data" half of ADR 0002. Everything here is
-- reference data or derived-and-rebuildable; no event state lives in Postgres.
--
-- Run via: make db-migrate    (or make db-bootstrap, which applies this file alone)
-- Idempotent: safe to re-run.

-- migration: repeatable
--
-- Read by the migration runner (corridor_event_hub/core/migrations.py). It means: apply
-- this file again whenever its contents change, rather than treating it as history.
--
-- Correct here and ONLY because every statement below is CREATE ... IF NOT EXISTS
-- or CREATE OR REPLACE, which is what makes docs/SPATIAL-DB.md able to say "edit
-- this file for anything additive and re-run it". A file that cannot tolerate a
-- second application must NOT carry this line - leave it off and it is run-once,
-- which is the safe default.

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS postgis;
-- postgis_topology is NOT installed: linear referencing needs only core PostGIS
-- (ST_LineLocatePoint, ST_LineSubstring, ST_Intersection) and topology adds
-- schema surface with no benefit here.

-- ---------------------------------------------------------------------------
-- Corridor definition (The corridor is CONFIGURATION)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS corridor (
  route              text PRIMARY KEY,
  description        text,
  -- SRID 4326 for storage (matches every source feed), but all length and
  -- distance math casts to geography so results are in metres, not degrees.
  centerline         geography(LineString, 4326) NOT NULL,
  -- The SAME line, carrying the corridor measure in its M dimension.
  --
  -- geometry rather than geography for two reasons: geography(LineString, 4326)
  -- cannot hold a measure dimension at all, and ST_InterpolatePoint needs
  -- geometry. Keeping both columns means the geography one stays right for
  -- distance and buffer work while this one carries the linear reference.
  --
  -- NULL until real LRS geometry is loaded by scripts/fetch-arnold.py.
  -- conflate_point PREFERS this column when it is present: reading M off the
  -- line is unbiased, and deriving a fraction and scaling it is not.
  centerline_m       geometry(LineStringM, 4326),
  buffer_meters      integer NOT NULL DEFAULT 1600,
  -- Geometry tripwire: false means the geometry is a placeholder and positions
  -- are not publishable. The probe and API should surface this.
  verified           boolean NOT NULL DEFAULT false,
  total_miles        numeric(10,3),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN corridor.verified IS
  'False = placeholder centerline, positions accurate to miles not metres. Do not publish.';

-- Columns added after a cluster was already bootstrapped need an explicit ALTER.
--
-- CREATE TABLE IF NOT EXISTS above is a NO-OP once the table exists, so adding a
-- column to its body does nothing to a database that has already been through
-- this file. Every addition needs a line here too, or it applies on fresh
-- clusters only - and the symptom shows up somewhere else entirely, as whatever
-- later statement first references the missing column. centerline_m was added
-- this way and failed in conflate_point with "column centerline_m does not
-- exist", pointing at the function rather than at the table.
ALTER TABLE corridor ADD COLUMN IF NOT EXISTS centerline_m geometry(LineStringM, 4326);

COMMENT ON COLUMN corridor.centerline_m IS
  'M-calibrated centerline: M IS the corridor measure in miles, chained from each '
  'state''s own LRS measures. NULL means no calibrated geometry, and conflate_point '
  'falls back to a biased approximation - see docs/CORRIDOR-GEOMETRY.md.';

-- ---------------------------------------------------------------------------
-- Per-state milepost offsets
--
-- THE TABLE THAT MAKES CROSS-STATE DEDUP POSSIBLE. Mileposts restart at every
-- state line, so AZ MP 359.5 and NM MP 0 are the same physical place. Only
-- after applying corridor_offset do the two become comparable numbers.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS state_segment (
  route              text NOT NULL REFERENCES corridor(route) ON DELETE CASCADE,
  state              char(2) NOT NULL,
  state_mp_min       numeric(10,3) NOT NULL,
  state_mp_max       numeric(10,3) NOT NULL,
  corridor_offset    numeric(10,3) NOT NULL,
  -- Where this state's extent actually is on the centerline, so the boundary
  -- can be checked geometrically rather than trusted from the numbers.
  segment            geography(LineString, 4326),
  verified           boolean NOT NULL DEFAULT false,
  PRIMARY KEY (route, state),
  CONSTRAINT state_mp_range CHECK (state_mp_max > state_mp_min),
  CONSTRAINT corridor_offset_nonneg CHECK (corridor_offset >= 0)
);

-- ---------------------------------------------------------------------------
-- NBI bridge structures (class 7, dimensional_restriction)
--
-- Reference data, refreshed annually. The three traps documented in
-- DATA-SOURCES.md are handled at INGEST, not here - but the schema makes the
-- important one impossible to reintroduce: min_vert_clearance_m is NULL when
-- unknown, never 99.99.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bridge_structure (
  structure_number   text PRIMARY KEY,
  state              char(2) NOT NULL,
  -- The CORRIDOR this structure restricts, not the road it carries - that is
  -- facility_carried. What the LRS view joins on, so it must be a route present
  -- in state_segment. A structure crossing OVER the corridor on a county road
  -- still carries the corridor's route here.
  route              text,
  -- NULL means UNKNOWN. NBI encodes "no restriction" as a FAMILY of sentinels,
  -- not just one: 99.99, 0, and - found in the 2025 vintage - 30.48 (exactly
  -- 100.00 ft) and 30.45 (99.90 ft). All must become NULL on the way in. Reading
  -- any of them as a real clearance makes every over-height check pass.
  min_vert_clearance_m   numeric(6,2),
  -- Which NBI field the clearance came from, and how the structure relates to the
  -- corridor. Load-bearing: item 10 and item 54B answer different questions, and
  -- 96% of item 10 values on this corridor are the no-restriction sentinel. See
  -- scripts/fetch-nbi.py, which measured it.
  clearance_item     text,
  relation           text,
  location           geography(Point, 4326),
  -- Conflated position. NULL until the structure is matched to the corridor - and
  -- NULL is also the correct answer for a structure whose published coordinates
  -- fall off the corridor, which two in the 2025 vintage do.
  corridor_measure   numeric(10,3),
  facility_carried   text,
  features_intersected text,
  nbi_year           integer NOT NULL,
  -- Nothing dropped. The full NBI record, all ~120 fields.
  raw                jsonb NOT NULL,
  ingested_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT clearance_sane CHECK (
    min_vert_clearance_m IS NULL
    OR (min_vert_clearance_m > 0 AND min_vert_clearance_m < 30)
  )
);

-- Same rule as centerline_m above, and it was learned the same way twice: these
-- two columns were added to the CREATE TABLE body only, and 001 then failed on an
-- existing cluster with "column b.relation does not exist" pointing at
-- corridor_clearances - a view three hundred lines away from the table it is
-- really complaining about. The ALTER that created them lived in 003, which runs
-- AFTER this file.
ALTER TABLE bridge_structure
  ADD COLUMN IF NOT EXISTS clearance_item text,
  ADD COLUMN IF NOT EXISTS relation       text;

COMMENT ON COLUMN bridge_structure.clearance_item IS
  'Which NBI field the clearance came from. 010 = MIN_VERT_CLR_010, clearance OVER '
  'the roadway this structure carries. 054B = VERT_CLR_UND_054B, clearance UNDER it '
  'for the road passing beneath. 96 percent of item 10 values on this corridor are '
  'the no-restriction sentinel, so 054B is where the real limits are.';

COMMENT ON COLUMN bridge_structure.relation IS
  'carries = this structure carries the corridor. crosses = it spans over the '
  'corridor. Where both are true the more restrictive clearance is stored.';

COMMENT ON COLUMN bridge_structure.min_vert_clearance_m IS
  'NULL = unknown, NOT unrestricted. NBI sentinels 99.99, 0, 30.48 '
  '(100.00 ft) and 30.45 (99.90 ft) MUST all be mapped to NULL. The CHECK '
  'constraint is the backstop: a sentinel cannot be inserted, so a missed one is a '
  'failed transaction rather than a wrong answer to an over-height query.';

CREATE INDEX IF NOT EXISTS bridge_location_gix
  ON bridge_structure USING GIST (location);
CREATE INDEX IF NOT EXISTS bridge_measure_idx
  ON bridge_structure (route, corridor_measure)
  WHERE corridor_measure IS NOT NULL;
-- The query that matters for an over-height truck: lowest clearances ahead.
CREATE INDEX IF NOT EXISTS bridge_clearance_idx
  ON bridge_structure (route, min_vert_clearance_m)
  WHERE min_vert_clearance_m IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Conflation functions
--
-- These are the whole reason PostGIS is here rather than turf in a Lambda:
-- the linear referencing is expressed in the idiom a DOT GIS analyst reads,
-- and can be called from SQL for ad-hoc research (ADR 0002 reasons 1 and 2).
-- ---------------------------------------------------------------------------

-- Milepost -> corridor measure. Pure arithmetic, but it belongs next to the
-- offset table so the two cannot drift apart.
CREATE OR REPLACE FUNCTION milepost_to_measure(
  p_route text,
  p_state char(2),
  p_milepost numeric
) RETURNS numeric
LANGUAGE sql STABLE AS $$
  SELECT s.corridor_offset + (p_milepost - s.state_mp_min)
  FROM state_segment s
  WHERE s.route = p_route
    AND s.state = upper(p_state)
    AND p_milepost BETWEEN s.state_mp_min AND s.state_mp_max;
$$;

COMMENT ON FUNCTION milepost_to_measure IS
  'Returns NULL for an out-of-range milepost rather than clamping - an invalid '
  'input is a mapping issue, not something to coerce.';

-- Inverse, for rendering back into a state's own reference.
CREATE OR REPLACE FUNCTION measure_to_milepost(
  p_route text,
  p_measure numeric
) RETURNS TABLE (state char(2), milepost numeric)
LANGUAGE sql STABLE AS $$
  SELECT s.state,
         s.state_mp_min + (p_measure - s.corridor_offset)
  FROM state_segment s
  WHERE s.route = p_route
    AND p_measure BETWEEN s.corridor_offset
                      AND s.corridor_offset + (s.state_mp_max - s.state_mp_min)
  LIMIT 1;
$$;

-- Coordinate -> corridor measure. The ST_LineLocatePoint path, and the reason
-- a spatial database earns its place: this is a real projection onto the
-- centerline rather than a sampled approximation.
CREATE OR REPLACE FUNCTION conflate_point(
  p_route text,
  p_lon numeric,
  p_lat numeric
) RETURNS TABLE (
  corridor_measure numeric,
  offset_meters    numeric,
  on_corridor      boolean
)
LANGUAGE sql STABLE AS $$
  WITH c AS (
    SELECT centerline, centerline_m, buffer_meters, total_miles
    FROM corridor WHERE route = p_route
  ),
  pt AS (
    SELECT ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326)::geography AS g
  )
  SELECT
    ROUND((CASE WHEN c.centerline_m IS NOT NULL
      -- PREFERRED. Reads the measure off the line at the closest point. There is
      -- no fraction and no multiplication, so there is no ruler to mismatch.
      THEN ST_InterpolatePoint(c.centerline_m, pt.g::geometry)
      -- FALLBACK, and SYSTEMATICALLY BIASED - see docs/CORRIDOR-GEOMETRY.md.
      -- The fraction is of the GEOMETRY's length; total_miles is a CONFIG
      -- mileage. Two different rulers, and because coarse geometry loses length
      -- unevenly the error does not cancel: against the 40-point placeholder
      -- every landmark came out 7 to 28 miles west of truth. Kept only so a
      -- corridor with no calibrated geometry still resolves at all.
      ELSE ST_LineLocatePoint(c.centerline::geometry, pt.g::geometry) * c.total_miles
    END)::numeric, 3),
    ROUND(ST_Distance(c.centerline, pt.g)::numeric, 1),
    ST_DWithin(c.centerline, pt.g, c.buffer_meters)
  FROM c, pt;
$$;

COMMENT ON FUNCTION conflate_point IS
  'Corridor measure for a coordinate. Reads M from centerline_m when it is '
  'populated, which is unbiased. Falls back to fraction-times-total_miles only '
  'when it is NULL, which is not - see docs/CORRIDOR-GEOMETRY.md.';

-- Polygon x corridor. Needed for NWS alerts, which arrive as county-sized
-- polygons. ST_Intersection against the real centerline is materially more
-- accurate than the 400-point sampling the turf implementation uses.
CREATE OR REPLACE FUNCTION conflate_polygon(
  p_route text,
  p_geojson text
) RETURNS TABLE (
  begin_measure numeric,
  end_measure   numeric,
  on_corridor   boolean
)
-- PREFERS centerline_m, exactly as conflate_point does. This function did NOT, and
-- the gap was invisible until a second implementation existed to compare against.
--
-- Measured on an 0.8 x 0.4 degree box near Amarillo:
--
--   this function, before          776.504 -> 823.539
--   this function, after           782.215 -> 828.455
--   in-process, 6400 samples       782.235 -> 828.390
--   in-process, 400 samples        784.756 -> 828.196
--
-- So the OLD SQL was the wrong one, by 5.7 miles, on the class whose extents are
-- the largest in the system. The in-process path was within its own declared
-- 3.1-mile sampling floor and converged to the calibrated answer; this function
-- converged to nothing, because its error was a formula rather than a resolution.
--
-- The cause was the same fraction-times-total_miles formula the comment inside
-- conflate_point calls systematically biased. ST_LineLocatePoint returns a fraction
-- OF THE GEOMETRY's length; total_miles is a CONFIGURED mileage from the LRS. The
-- two disagree wherever the geometry loses length unevenly, which is everywhere,
-- and the error does not cancel.
--
-- ST_InterpolatePoint reads the M value off the calibrated line instead - the same
-- number the local implementation interpolates out of its parallel measures array,
-- which is why they now agree.
LANGUAGE sql STABLE AS $$
  WITH c AS (
    SELECT centerline, centerline_m, total_miles FROM corridor WHERE route = p_route
  ),
  poly AS (
    SELECT ST_SetSRID(ST_GeomFromGeoJSON(p_geojson), 4326) AS g
  ),
  hit AS (
    -- ST_LineMerge collapses the intersection to a single line where it can. A
    -- polygon the corridor leaves and re-enters yields a MultiLineString, and
    -- StartPoint/EndPoint of that spans the whole union - which is the correct
    -- reading for an alert extent, and deliberately not per-part.
    SELECT ST_LineMerge(ST_Intersection(c.centerline::geometry, poly.g)) AS seg,
           c.centerline, c.centerline_m, c.total_miles
    FROM c, poly
  ),
  ends AS (
    SELECT seg, centerline, centerline_m, total_miles,
           ST_StartPoint(seg) AS p_begin,
           ST_EndPoint(seg)   AS p_end
    FROM hit
  )
  SELECT
    CASE WHEN ST_IsEmpty(seg) OR p_begin IS NULL THEN NULL
         WHEN centerline_m IS NOT NULL
           THEN ROUND(ST_InterpolatePoint(centerline_m, p_begin)::numeric, 3)
         ELSE ROUND((ST_LineLocatePoint(centerline::geometry, p_begin) * total_miles)::numeric, 3)
    END,
    CASE WHEN ST_IsEmpty(seg) OR p_end IS NULL THEN NULL
         WHEN centerline_m IS NOT NULL
           THEN ROUND(ST_InterpolatePoint(centerline_m, p_end)::numeric, 3)
         ELSE ROUND((ST_LineLocatePoint(centerline::geometry, p_end) * total_miles)::numeric, 3)
    END,
    NOT ST_IsEmpty(seg)
  FROM ends;
$$;

-- ---------------------------------------------------------------------------
-- Convenience view: what an over-height truck needs
-- ---------------------------------------------------------------------------

-- NOTE the LATERAL join with an explicit alias rather than
-- `(measure_to_milepost(...)).*`. That expansion emits its own `state` column,
-- which collides with bridge_structure.state:
--   ERROR: column "state" specified more than once
-- Naming the columns keeps both, and keeps them distinguishable.
-- DROP THEN CREATE, not CREATE OR REPLACE, and this is the third rule in this file
-- learned by breaking it. CREATE OR REPLACE VIEW may only APPEND columns: it cannot
-- rename one or insert one in the middle. Adding `relation` before
-- `facility_carried` therefore failed with
--
--   cannot change name of view column "facility_carried" to "relation"   (42P16)
--
-- which reads like a rename nobody wrote. Dropping first makes the view freely
-- editable, which is what a repeatable migration needs - the alternative is a
-- standing rule that new columns may only ever go at the end, and this file has
-- already demonstrated twice that such rules do not survive contact.
--
-- No CASCADE, deliberately: nothing depends on this view today, and if something
-- ever does, failing here is the correct outcome rather than silently dropping it.
-- The cost of the drop is that any GRANT on the view goes with it - worth knowing
-- before this schema has real users.
DROP VIEW IF EXISTS corridor_clearances;

CREATE VIEW corridor_clearances AS
SELECT
  b.structure_number,
  b.state                                        AS nbi_state,
  b.route,
  b.min_vert_clearance_m,
  ROUND(b.min_vert_clearance_m * 3.28084, 2)     AS min_vert_clearance_ft,
  b.corridor_measure,
  mp.state                                       AS lrs_state,
  ROUND(mp.milepost, 3)                          AS milepost,
  -- Exposed because "14.5 ft" is not one fact. `crosses` means a structure spans
  -- the corridor and that is the room beneath it; `carries` means the corridor runs
  -- ON the structure and something else is overhead. A router needs to know which.
  b.relation,
  b.clearance_item,
  b.facility_carried,
  b.features_intersected,
  b.nbi_year
FROM bridge_structure b
LEFT JOIN LATERAL measure_to_milepost(b.route, b.corridor_measure) AS mp
  ON true
WHERE b.min_vert_clearance_m IS NOT NULL
  AND b.corridor_measure IS NOT NULL
ORDER BY b.corridor_measure;

COMMENT ON VIEW corridor_clearances IS
  'Only structures with a KNOWN clearance and a resolved position. Rows absent '
  'here are unknown, NOT unrestricted - absence of a record is not evidence of '
  'clearance.';
