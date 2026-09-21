#!/usr/bin/env bash
#
# Every read-only check against the spatial database, in one pass.
#
#   npm run db-check
#   bash scripts/db-check.sh
#
# READS ONLY. Nothing here writes, locks, or migrates - safe to run against
# anything, including while a migration is in flight.
#
# Ordered so each section explains the next one's numbers: what exists, then
# whether the geometry is right, then what the NBI load produced, then whether
# conflation actually lands where the signs say it should. A failure early makes
# the later sections meaningless, so they are labelled rather than merged.
#
# Delegates to db.sh per section rather than opening its own connection, so ARN
# discovery, the account banner, and the result rendering all stay in one place.
# The cost is a couple of extra describe calls per section, which is nothing
# against the round trip to Aurora.

set -uo pipefail
cd "$(dirname "$0")/.."

section() {
  echo
  echo "=============================================================="
  echo "  $1"
  echo "=============================================================="
}

# A check whose SQL lives in sql/checks/. Comment lines are stripped because the
# files carry more prose than SQL and the Data API echoes the statement on error.
check() {
  local title="$1" file="$2"
  section "$title"
  bash scripts/db.sh "$(grep -v '^--' "$file")"
}

echo "spatial database checks - READ ONLY"
bash scripts/db.sh --info || exit 1

section "MIGRATIONS APPLIED"
bash scripts/db.sh "SELECT filename, left(checksum,12) AS checksum, statements,
       repeatable, applied_at, applied_ms FROM schema_migration ORDER BY filename"

check "WHAT IS LOADED"        sql/checks/inventory.sql
check "CORRIDOR GEOMETRY"     sql/checks/geometry.sql
check "NBI STRUCTURES"        sql/checks/nbi.sql
check "LANDMARK ACCURACY"     sql/checks/landmarks.sql

# A LEGEND, NOT A VERDICT - and it has to say so in its own heading.
#
# This block used to be titled "READ THIS BEFORE TRUSTING THE ABOVE" and its lines
# read as findings: "missing schema_migration -> nothing was applied",
# "centerline_m NOT LOADED". Printed directly beneath a report showing three applied
# migrations and 11,873 loaded vertices, it was read as the script contradicting
# itself, and a healthy database was nearly reported as unmigrated. Nothing here
# inspects anything; every line is `if you saw X above, it means Y`.
#
# The conditions are left as prose rather than evaluated because the SQL checks above
# already print the values, and a second implementation of the same thresholds in
# bash is a second place for them to drift.
section "HOW TO READ THE ABOVE (a legend - none of this is a finding)"
cat <<'EOF'
  Each line is `if you saw this VALUE above -> here is what it MEANS`. These are not
  results. The results are in the tables above this block.

  MIGRATIONS      IF schema_migration is missing -> nothing was applied by
                  db-migrate; the schema may still exist via db-bootstrap, which
                  records nothing

  GEOMETRY        IF centerline_m is NOT LOADED -> conflate_point falls back to
                  fraction-times-total_miles, which is SYSTEMATICALLY biased
                  IF M end - total_miles != 0   -> the LRS and the config disagree
                  IF largest offset gap != 0    -> cross-state dedup is broken

  NBI             usable_c3 is the ONLY number the over-height query can act on.
                  A low clearance_known ratio is EXPECTED, not a load failure -
                  most NBI structures have no clearance on file. Rows absent from
                  usable_c3 are UNKNOWN, not unrestricted.

  LANDMARKS       errors ALL THE SAME SIGN is the tell for systematic bias.
                  Tenths of a mile is agency rounding; whole miles is a bug.
EOF
echo
