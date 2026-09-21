#!/usr/bin/env bash
#
# Health check for the deployed query API.
#
#   npm run api-check                      signed GET /health
#   npm run api-check -- --all              every route the stack declares
#   API_URL=https://... npm run api-check   skip discovery, check that endpoint
#
# Built to run in an account this checkout has never seen: credentials come from
# environment variables (or a profile, if that is what the shell has), the region
# comes from the environment or the API's own hostname, and every name is discovered
# from CloudFormation rather than hardcoded. Reads only - safe against anything.
#
# WHY THIS IS A SEPARATE SCRIPT FROM status.sh: that one answers "is data flowing",
# by reading the catalog table, the raw zone, the normalizer's logs and the DLQs.
# None of those touch the API, so a query API that 403s every caller looks perfectly
# healthy there. This answers the other question - "can a consumer read it" - which
# is the one an integrator asks first.
#
# WHY THE IDENTITY IS PRINTED FIRST: the API is IAM-authorized, and API Gateway
# returns the identical `{"message":"Forbidden"}` body for an unsigned request, a
# signature from a principal with no execute-api:Invoke, and a signature from an
# entirely different account. The response cannot tell you which. Who you are
# signing as is the only thing that can, so it is reported before the result rather
# than left for you to go and check after a 403.

set -uo pipefail
cd "$(dirname "$0")/.."

# npm strips the `--` separator before passing args along. Same guard as invoke.sh.
[ "${1:-}" = "--" ] && shift
ALL=0
[ "${1:-}" = "--all" ] && ALL=1

STACK_INGEST="${STACK_INGEST:-CorridorEventHubIngest}"

# us-west-2 last, matching the stack default in bin/corridor-event-hub.ts, so a shell with no
# region configured checks the same place a plain `npm run deploy` would have built.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null)}}"
REGION="${REGION:-us-west-2}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

# Hand the signer explicit credentials when the shell only has a profile.
#
# This matters more than it looks: static credentials in the environment OUTRANK
# AWS_PROFILE in every AWS SDK's resolution chain. Resolving the profile to real
# keys here, in this process only, means the signer and the `aws` calls below cannot
# end up as two different principals - which is precisely the failure that produces
# an unexplained 403.
if [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  CREDS=$(aws configure export-credentials --format env 2>/dev/null)
  [ -n "$CREDS" ] && eval "$CREDS"
fi

hr() { printf '%.0s-' {1..76}; echo; }

echo "Corridor Event Hub query API check"
hr

# --- who are we ------------------------------------------------------------
IDENTITY=$(aws sts get-caller-identity --output json 2>&1)
if ! printf '%s' "$IDENTITY" | grep -q '"Account"'; then
  echo "no usable AWS credentials in this shell:"
  printf '%s\n' "$IDENTITY" | head -4 | sed 's/^/  /'
  echo
  echo "export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (plus AWS_SESSION_TOKEN if"
  echo "they are temporary), or set AWS_PROFILE, then re-run."
  exit 1
fi
ACCOUNT=$(printf '%s' "$IDENTITY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')
ARN=$(printf '%s' "$IDENTITY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')
SOURCE="environment credentials"
[ -n "${AWS_PROFILE:-}" ] && SOURCE="profile ${AWS_PROFILE}"

echo "SIGNING AS"
printf '  account   %s\n' "$ACCOUNT"
printf '  arn       %s\n' "$ARN"
printf '  from      %s in %s\n' "$SOURCE" "$REGION"

# --- find the API ----------------------------------------------------------
API="${API_URL:-}"
if [ -z "$API" ]; then
  API=$(aws cloudformation describe-stacks --stack-name "$STACK_INGEST" \
    --query "Stacks[0].Outputs[?OutputKey=='QueryApiUrl'].OutputValue | [0]" \
    --output text 2>/dev/null)
fi
case "${API:-None}" in
  ''|None)
    echo
    echo "no QueryApiUrl output on stack $STACK_INGEST in $REGION (account $ACCOUNT)."
    echo "Either it is not deployed here, or it is deployed under another name:"
    echo "  deploy       npm run deploy"
    echo "  other name   STACK_INGEST=<name> npm run api-check"
    echo "  known URL    API_URL=https://<id>.execute-api.<region>.amazonaws.com npm run api-check"
    exit 1
    ;;
esac

API_ID=${API#https://}
API_ID=${API_ID%%.*}

echo
echo "API"
printf '  url       %s\n' "$API"

# get-api with the SAME credentials that will sign the request. If this fails, the
# signed GET is guaranteed to 403 and the API is not the reason - so say so here,
# where the cause is still visible, instead of letting it surface as Forbidden.
API_NAME=$(aws apigatewayv2 get-api --api-id "$API_ID" --query Name --output text 2>/dev/null)
case "${API_NAME:-None}" in
  ''|None)
    printf '  id        %s  NOT VISIBLE to this principal\n' "$API_ID"
    echo
    echo "  These credentials cannot describe that API, so they almost certainly cannot"
    echo "  invoke it either. Usually the API belongs to a different account than the one"
    echo "  above; less often the principal lacks apigateway:GET. Expect 403 below."
    VISIBLE=0
    ;;
  *)
    printf '  id        %s  %s\n' "$API_ID" "$API_NAME"
    VISIBLE=1
    ;;
esac

# --- stage and routes ------------------------------------------------------
ROUTE_PATHS="/health"
if [ "$VISIBLE" -eq 1 ]; then
  echo
  echo "STAGES"
  aws apigatewayv2 get-stages --api-id "$API_ID" \
    --query 'Items[].[StageName,AutoDeploy,DefaultRouteSettings.DetailedMetricsEnabled,DefaultRouteSettings.ThrottlingRateLimit]' \
    --output text 2>/dev/null \
    | awk -F'\t' '{printf "  %-12s autoDeploy=%-6s detailedMetrics=%-6s rate=%s\n",$1,$2,$3,$4}'

  echo
  echo "ROUTES"
  # -F'\t' because a RouteKey is `GET /health` - it CONTAINS a space, so default awk
  # splitting turns one field into two and silently drops the authorizer column.
  aws apigatewayv2 get-routes --api-id "$API_ID" \
    --query 'Items[].[RouteKey,AuthorizationType]' --output text 2>/dev/null \
    | sort | awk -F'\t' '{printf "  %-34s %s\n",$1,$2}'

  if [ "$ALL" -eq 1 ]; then
    # Path-parameter routes are skipped rather than guessed: /events/{eventId} needs
    # a real id, and a fabricated one returns 404 - which would read as a broken
    # route. Named in the output so the gap is not mistaken for full coverage.
    ROUTE_PATHS=$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query 'Items[].RouteKey' --output text 2>/dev/null \
      | tr '\t' '\n' | sed 's/^GET //' | grep -v '{' | sort)
    SKIPPED=$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query 'Items[].RouteKey' --output text 2>/dev/null \
      | tr '\t' '\n' | sed 's/^GET //' | grep '{' | sort | tr '\n' ' ')
  fi
fi

# --- the actual request ----------------------------------------------------
echo
echo "SIGNED GET  (status, latency, body)"
URLS=""
for path in $ROUTE_PATHS; do
  # Routes with required parameters get a valid minimal query, so a 400 that only
  # means "you did not pass direction" is not mistaken for an unhealthy route. The
  # small `limit` keeps a health check from pulling a full feed on every run.
  case "$path" in
    /ahead)  query="?direction=BOTH&position=0" ;;
    /events|/review) query="?limit=1" ;;
    *)       query="" ;;
  esac
  URLS="$URLS ${API}${path}${query}"
done
# shellcheck disable=SC2086 - word splitting is how the URL list is passed
python3 scripts/lib/sigv4_get.py --region "$REGION" $URLS
PROBE=$?
[ -n "${SKIPPED:-}" ] && echo "  (skipped, needs a real id: ${SKIPPED% })"

# --- what CloudWatch saw ---------------------------------------------------
if [ "$VISIBLE" -eq 1 ]; then
  # python3 rather than `date -u -d` or `date -u -v`: those two flags are
  # GNU-only and BSD-only respectively, and this script is meant to run on both.
  START=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  END=$(python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')

  echo
  echo "LAST HOUR  (AWS/ApiGateway, ApiId=$API_ID)"
  for metric in Count 4xx 5xx Latency IntegrationLatency; do
    case "$metric" in
      Count|4xx|5xx) stat=Sum ;;
      *) stat=Average ;;
    esac
    value=$(aws cloudwatch get-metric-statistics --namespace AWS/ApiGateway \
      --metric-name "$metric" --dimensions Name=ApiId,Value="$API_ID" \
      --start-time "$START" --end-time "$END" --period 3600 --statistics "$stat" \
      --query "Datapoints[0].$stat" --output text 2>/dev/null)
    case "${value:-None}" in
      ''|None) value="-" ;;
      *) value=$(printf '%.1f' "$value" 2>/dev/null || printf '%s' "$value") ;;
    esac
    printf '  %-20s %-8s %s\n' "$metric" "$stat" "$value"
  done
  # Deliberately NOT "subtract one from the other to get gateway overhead". That holds
  # per request, but these are averages over DIFFERENT populations: a 403 is counted by
  # Latency and never reaches the integration at all, so with rejected traffic in the
  # window the IntegrationLatency average can exceed the Latency average. Reading the
  # difference as overhead there produces a negative number and a wrong conclusion.
  echo "  4xx includes signature rejections, which never reach the integration - so with"
  echo "  403s in the window these two averages cover different requests."
fi

# --- verdict ---------------------------------------------------------------
hr
if [ "$PROBE" -eq 0 ]; then
  echo "OK - the API is reachable and authorizing this principal."
else
  cat <<EOF
FAILED - see the status above.

  403  the signature was rejected. The body is the same for an unsigned request, a
       principal without execute-api:Invoke, and a signature from another account -
       so compare the account printed at the top ($ACCOUNT) against the account the
       API is deployed in. Stale AWS_ACCESS_KEY_ID / AWS_SESSION_TOKEN left in the
       shell are the usual cause: they override AWS_PROFILE silently.
  404  the route is not declared on the API. Compare against ROUTES above.
  5xx  API Gateway reached the query function and it failed. Look at the function:
       npm run logs
EOF
fi

echo
echo "access log (who called what):  aws logs tail \$(aws logs describe-log-groups \\"
echo "  --log-group-name-prefix CorridorEventHub --query \"logGroups[?contains(logGroupName,'QueryApiAccessLogs')].logGroupName | [0]\" \\"
echo "  --output text) --since 1h --format short"
exit "$PROBE"
