#!/usr/bin/env bash
#
# Apply pending schema migrations, by invoking the migration Lambda.
#
#   ./scripts/db-migrate.sh --plan     # what WOULD run. Changes nothing.
#   ./scripts/db-migrate.sh            # apply it
#
# WHY A LAMBDA AND NOT db.sh: the cluster is in an isolated subnet, so something
# has to run inside the VPC. db.sh reaches it over the Data API, which a production
# deployment is advised to turn off - and db.sh has no record of what it applied,
# keeps going after a failed statement, and so can leave a file half-applied. The
# Lambda records every application in `schema_migration` and wraps each file in one
# transaction. See docs/SPATIAL-DB.md.
#
# The FUNCTION is discovered, never hardcoded, for the same reason every ARN in
# db.sh is: this stack has been deployed to more than one account, and a pasted
# name fails in a way that looks like a broken deployment.
#
# Honours AWS_PROFILE / AWS_DEFAULT_REGION, so it follows whatever account your
# shell is pointed at. Run --plan first if you are unsure which that is.

set -uo pipefail
cd "$(dirname "$0")/.."

# npm strips `--` before passing args along. Skip it if it survives.
[ "${1:-}" = "--" ] && shift

PAYLOAD='{}'
MODE="apply"
if [ "${1:-}" = "--plan" ] || [ "${1:-}" = "--dry-run" ]; then
  PAYLOAD='{"dryRun":true}'
  MODE="plan"
fi

STACK="${STACK_SPATIAL:-CorridorEventHubSpatial}"
REGION_ARG=""
[ -n "${AWS_REGION:-}" ] && REGION_ARG="--region $AWS_REGION"

# Stack output first - it is the authoritative name. Falls back to listing
# functions, which covers a stack deployed under a different -c prefix.
FN=$(aws cloudformation describe-stacks $REGION_ARG --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='MigrationFunctionName'].OutputValue" \
  --output text 2>/dev/null | grep -v '^None$' | grep . | head -1)

if [ -z "$FN" ]; then
  FN=$(aws lambda list-functions $REGION_ARG --no-paginate \
    --query "Functions[?contains(FunctionName,'MigrateFn')].FunctionName" --output text 2>/dev/null \
    | tr '\t' '\n' | grep -v '^None$' | grep . | head -1)
fi

if [ -z "$FN" ]; then
  cat <<EOF
migration function not found.

  account: $(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '<no credentials>')
  region : ${AWS_DEFAULT_REGION:-${AWS_REGION:-<unset>}}
  profile: ${AWS_PROFILE:-<default>}
  stack  : $STACK

Either the spatial stack is not deployed here, or the shell is pointed at another
account. Deploy with:  npm run bundle && npx cdk deploy $STACK
EOF
  exit 1
fi

echo "migration runner: $FN"
echo "mode            : $MODE"
echo

TMP=$(mktemp)
# RequestResponse (the default) on purpose: an async invoke would return
# immediately and leave the operator with no idea whether the schema changed.
STATUS=$(aws lambda invoke $REGION_ARG \
  --function-name "$FN" \
  --payload "$(printf '%s' "$PAYLOAD" | base64)" \
  --cli-binary-format base64 \
  --query '[StatusCode,FunctionError]' --output text \
  "$TMP" 2>&1)

echo "invoke: $STATUS"
echo

# The renderer is a FILE, not a heredoc. Same reason as scripts/lib/render_dataapi.py
# and the lesson in tests/test_dlq_format.py: the tool you read during an incident
# is the worst one to leave untested, and shell-quoted Python is untestable.
# It owns the exit code - nonzero on drift, even for --plan.
python3 scripts/lib/render_migration.py "$TMP"
rc=$?
rm -f "$TMP"
exit $rc
