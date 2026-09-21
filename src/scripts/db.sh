#!/usr/bin/env bash
#
# Query the spatial database via the RDS Data API.
#
#   ./scripts/db.sh                              # interactive-ish: run a smoke test
#   ./scripts/db.sh "SELECT * FROM state_segment"
#   ./scripts/db.sh --file sql/001-init.sql      # apply a whole file
#   ./scripts/db.sh --info                       # show discovered resources
#
# WHY THIS EXISTS: every ARN is DISCOVERED, never hardcoded. An earlier session
# pasted literal ARNs from one account into commands, and they silently stopped
# working the moment the stack was deployed to a second account - the failure
# looked like "the database is unreachable" when in fact the queries were aimed
# at a cluster in a different account.
#
# Honours AWS_PROFILE and AWS_DEFAULT_REGION, so it follows whatever account your
# shell or .vscode/settings.json is pointed at.
#
# The database is in an isolated subnet with no public endpoint, so the Data API
# is the access path - see docs/SPATIAL-DB.md for the security tradeoff.

set -uo pipefail
cd "$(dirname "$0")/.."

DB_NAME="${DB_NAME:-corridoreventhub}"

# --- discover ---------------------------------------------------------------

# One call, both fields. `describe-db-clusters` has NO --db-cluster-arn flag
# (only --db-cluster-identifier), and passing the wrong one fails in a way that
# looks like "the Data API is off" rather than "your flag is invalid".
read -r CLUSTER_ARN HTTP_ENABLED <<<"$(aws rds describe-db-clusters \
  --query "DBClusters[?starts_with(DBClusterIdentifier,'corridoreventhubspatial')].[DBClusterArn,HttpEndpointEnabled] | [0]" \
  --output text 2>/dev/null)"

if [ -z "$CLUSTER_ARN" ] || [ "$CLUSTER_ARN" = "None" ]; then
  cat <<EOF
no Corridor Event Hub spatial cluster found.

  account: $(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '<no credentials>')
  region : ${AWS_DEFAULT_REGION:-${AWS_REGION:-<unset>}}
  profile: ${AWS_PROFILE:-<default>}

Either the stack is not deployed here, or the shell is pointed at the wrong
account. Deploy with:  npx cdk deploy CorridorEventHubSpatial
EOF
  exit 1
fi

SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id corridor-event-hub/spatial-db-credentials --query ARN --output text 2>/dev/null)

if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" = "None" ]; then
  echo "cluster found but secret corridor-event-hub/spatial-db-credentials is missing in this account"
  exit 1
fi

if [ "$HTTP_ENABLED" != "True" ]; then
  cat <<'EOF'
the Data API is DISABLED on this cluster, so there is no path to it from here.

The cluster sits in an isolated subnet with no public endpoint, so psql cannot
reach it either. Enable the Data API:

  npx cdk deploy CorridorEventHubSpatial          # enableDataApi defaults to true

EOF
  exit 1
fi

run_sql() {
  aws rds-data execute-statement \
    --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
    --database "$DB_NAME" --sql "$1" \
    --include-result-metadata \
    --output json 2>&1
}

# Render Data API JSON as a table. The renderer is a FILE, not a heredoc:
# `python3 - <<'PY'` makes the heredoc become stdin, so a piped payload collides
# with the script and python sees `{"records":...}import sys`.
render() {
  python3 scripts/lib/render_dataapi.py
}

# --- modes ------------------------------------------------------------------

if [ "${1:-}" = "--info" ]; then
  echo "account : $(aws sts get-caller-identity --query Account --output text)"
  echo "region  : ${AWS_DEFAULT_REGION:-${AWS_REGION:-default}}"
  echo "profile : ${AWS_PROFILE:-default}"
  echo "cluster : $CLUSTER_ARN"
  echo "secret  : $SECRET_ARN"
  echo "database: $DB_NAME"
  echo "data api: enabled"
  exit 0
fi

if [ "${1:-}" = "--file" ]; then
  FILE="${2:?usage: db.sh --file <path.sql>}"
  [ -f "$FILE" ] || { echo "no such file: $FILE"; exit 1; }
  echo "applying $FILE to $DB_NAME"
  echo

  # The Data API takes ONE statement per call, and a naive split(';') destroys
  # every function body - so the splitting is dollar-quote and string aware.
  #
  # IMPORTED rather than reimplemented here. This script used to carry its own
  # line-based splitter, and the migration Lambda needed the same logic: two
  # copies of "where does a statement end" is exactly the kind of drift that makes
  # one path apply a file correctly and the other cut a function in half. The
  # shared one lives in corridor_event_hub/core/migrations.py and is unit-tested.
  python3 - "$FILE" "$CLUSTER_ARN" "$SECRET_ARN" "$DB_NAME" <<'PY'
import subprocess, sys, re, pathlib

sys.path.insert(0, str(pathlib.Path.cwd()))
try:
    from corridor_event_hub.core.migrations import split_statements
except ImportError as exc:
    sys.exit(
        f'cannot import the statement splitter ({exc}).\n'
        'Run from the src/ checkout - it reads corridor_event_hub/core/migrations.py.'
    )

path, cluster, secret, database = sys.argv[1:5]
statements = split_statements(open(path, encoding='utf-8').read())

ok = failed = 0
for stmt in statements:
    s = stmt.strip()
    if not s:
        continue
    label = re.sub(r'\s+', ' ', s)[:62]
    r = subprocess.run(
        ['aws', 'rds-data', 'execute-statement',
         '--resource-arn', cluster, '--secret-arn', secret,
         '--database', database, '--sql', s],
        capture_output=True, text=True, timeout=180)
    if r.returncode == 0:
        ok += 1
        print(f'  ok   {label}')
    else:
        failed += 1
        tail = (r.stderr or '').strip().splitlines()
        print(f'  FAIL {label}')
        if tail:
            print(f'       {tail[-1][:180]}')

print(f'\n{ok} statement(s) ok, {failed} failed')
sys.exit(1 if failed else 0)
PY
  exit $?
fi

if [ -n "${1:-}" ]; then
  run_sql "$1" | render
  exit $?
fi

# Default: smoke test. Proves the connection AND the LRS invariants.
echo "spatial database smoke test  (account $(aws sts get-caller-identity --query Account --output text), db $DB_NAME)"
echo
echo "postgis:"
run_sql "SELECT postgis_version() AS postgis, current_database() AS db" | render
echo
echo "row counts:"
run_sql "SELECT 'corridor' AS tbl, count(*) AS rows FROM corridor
         UNION ALL SELECT 'state_segment', count(*) FROM state_segment
         UNION ALL SELECT 'bridge_structure', count(*) FROM bridge_structure
         ORDER BY tbl" | render
echo
echo "state-line continuity - both values MUST match:"
# The AZ milepost is READ FROM state_segment, not written as a literal.
#
# It used to be 359.5, from the published per-state mileage. Real ADOT LRS
# geometry measures Arizona's I-40 at 359.349, and milepost_to_measure does not
# clamp, so the old literal became out-of-range and returned NULL - a
# check that had quietly stopped checking anything. Reading the boundary back
# means this cannot go stale again when the measured extent moves.
run_sql "SELECT (SELECT state_mp_max FROM state_segment
                  WHERE route='I-40' AND state='AZ')            AS az_line_mp,
                milepost_to_measure('I-40','AZ',
                  (SELECT state_mp_max FROM state_segment
                    WHERE route='I-40' AND state='AZ'))          AS az_at_line,
                milepost_to_measure('I-40','NM',0)               AS nm_mp_0" | render
