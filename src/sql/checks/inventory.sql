-- WHAT IS ACTUALLY IN THE DATABASE
--
-- Run:  make db-inventory
--
-- The first thing to run when you are not sure what a cluster holds. Exact counts
-- for every table and view in `public`, next to what each SHOULD hold.
--
-- IT ENUMERATES WHAT EXISTS RATHER THAN NAMING WHAT SHOULD, and that is the whole
-- design. The obvious version - `SELECT count(*) FROM corridor UNION ALL SELECT
-- count(*) FROM bridge_structure ...` - fails ENTIRELY with `relation does not
-- exist` if any one table is missing, because Postgres parses the whole statement
-- before running it. So the check you reach for when you suspect a migration did
-- not apply is precisely the one that tells you nothing when it did not. This
-- version returns rows either way, and a missing table shows up as an absent line
-- rather than an error.
--
-- It also surfaces objects that are NOT in sql/ - anything created by hand shows
-- up with `expect = ?`, which is worth knowing on a cluster several people have
-- touched.
--
-- HOW THE COUNTS WORK: query_to_xml runs one exact `count(*)` per object and the
-- xpath pulls the number back out. Unusual, but it is the only way to count tables
-- discovered at runtime inside a single statement. The alternative, n_live_tup
-- from pg_stat_user_tables, is an ESTIMATE that reads 0 for a freshly loaded table
-- nobody has ANALYZEd - the worst possible failure mode for a load check.
--
-- PostGIS's own tables are excluded via pg_depend: spatial_ref_sys alone has ~8500
-- rows and would bury everything that matters.

WITH obj AS (
  SELECT c.oid,
         c.relname::text AS object,
         CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' ELSE 'table' END AS kind
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public'
    AND c.relkind IN ('r', 'p', 'v', 'm')
    -- Extension members: spatial_ref_sys, geometry_columns, geography_columns.
    AND NOT EXISTS (
      SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e'
    )
),
counted AS (
  SELECT object, kind,
         (xpath('/row/c/text()',
                query_to_xml(format('SELECT count(*) AS c FROM public.%I', object),
                             false, true, '')))[1]::text::bigint AS rows
  FROM obj
),
expected(object, expect, note) AS (VALUES
  ('schema_migration',    '>= 2', 'files applied by make db-migrate; 0 rows means db-bootstrap was used, which records nothing'),
  ('corridor',            '1',    'the route definition'),
  ('state_segment',       '4',    'per-state milepost offsets'),
  ('corridor_geom_load',  '= 002 chunk count', 'staging, left populated BY DESIGN: 002 clears it at the start, inserts the chunks, assembles centerline_m from them, and does not clean up. Safe to truncate. 0 rows next to a NULL centerline_m means the assembly found nothing'),
  ('bridge_structure',    '> 0',  'NBI structures, one row per structure'),
  ('corridor_clearances', '> 0',  'over-height query usable: KNOWN clearance AND resolved position')
)
SELECT c.kind,
       c.object,
       c.rows,
       COALESCE(e.expect, '?')                                     AS expect,
       COALESCE(e.note, 'not defined in sql/ - created by hand?')   AS note
FROM counted c
LEFT JOIN expected e ON e.object = c.object
ORDER BY c.kind DESC, c.object;
