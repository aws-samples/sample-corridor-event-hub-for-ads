-- LANDMARK ACCURACY CHECK
--
-- The only check that catches SYSTEMATIC conflation bias. Two implementations
-- agreeing with each other proves they share a formula, not that the formula is
-- right (see docs/CORRIDOR-GEOMETRY.md). This compares conflated positions
-- against independently known mileposts, so it has an actual source of truth.
--
-- Run:  ./scripts/db.sh --file sql/checks/landmarks.sql
--
-- HISTORICAL BASELINE, placeholder centerline: every err_mi negative, roughly
-- -7 to -29, with Gallup and Winslow rejected as off-corridor. Recorded so the
-- change is visible.
--
-- EXPECTED NOW, real NTAD geometry loaded by scripts/fetch-arnold.py:
-- |err_mi| under ~0.35 mi, on_corridor true for all seven, off_corridor_mi
-- effectively zero because the probes ARE points on the centerline.
--
-- Residual err_mi is mostly each agency's own offset between its marker layer
-- and its posted signs: scripts/fetch-arnold.py also compares against the
-- states' published marker measures and agrees EXACTLY in AZ and TX. New Mexico
-- is the exception at up to 0.180 mi, which IS ours - the cost of interpolating
-- measures along 2D NTAD segments, bounded per segment. Run that script if
-- err_mi moves and you need to know whose error it is.
--
-- Errors ALL THE SAME SIGN remains the tell for systematic bias, but judge it
-- with the magnitudes: at a tenth of a mile it is agency rounding, and at whole
-- miles it is the fraction-times-total_miles bug returning.

-- PROBE COORDINATES ARE GENERATED, NOT HAND-ENTERED.
--
-- scripts/fetch-arnold.py --rebuild-landmarks resolves each one from the
-- state's own milepost marker layer, so the position is agency-surveyed and
-- the milepost is the number on the physical sign.
--
-- The previous hand-entered values were town centroids and they made this
-- check unusable against real geometry: 'Oklahoma City MP 145' sat 5.08 mi
-- east of the actual MP 145 sign, and three of the seven were far enough off
-- the true centerline to be rejected by the 1600 m buffer. Albuquerque landed
-- exactly on the placeholder centerline for the circular reason that the
-- placeholder was drawn through town centroids as well.

WITH probe(name, lon, lat, real_state, real_mp) AS (VALUES
  ('Flagstaff AZ',      -111.668481, 35.173890, 'AZ',  195.0),
  ('Winslow AZ',        -110.708941, 35.039115, 'AZ',  253.0),
  ('Gallup NM',         -108.773390, 35.524510, 'NM',   20.0),
  ('Albuquerque NM',    -106.638543, 35.104934, 'NM',  159.0),
  ('Tucumcari NM',      -103.729898, 35.151421, 'NM',  332.0),
  ('Amarillo TX',       -101.844865, 35.194064, 'TX',   70.0),
  ('Oklahoma City OK',   -97.609414, 35.460196, 'OK',  145.0)
)
SELECT
  p.name,
  p.real_state || ' ' || p.real_mp                                  AS real_ref,
  ROUND(milepost_to_measure('I-40', p.real_state, p.real_mp), 1)    AS real_measure,
  ROUND(c.corridor_measure, 1)                                      AS conflated,
  ROUND(c.corridor_measure
        - milepost_to_measure('I-40', p.real_state, p.real_mp), 1)  AS err_mi,
  ROUND((c.offset_meters / 1609.34)::numeric, 1)                    AS off_corridor_mi,
  c.on_corridor
FROM probe p, conflate_point('I-40', p.lon, p.lat) c
ORDER BY 3;
