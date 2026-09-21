#!/usr/bin/env bash
#
# One-command health check for a deployed Corridor Event Hub stack.
#
#   npm run status
#
# Discovers resource names from CloudFormation outputs rather than hardcoding
# them, so this works against any deployment of these stacks - which is what makes
# it an adoptable artifact.
#
# Reads only. Safe to run against anything.

set -uo pipefail
cd "$(dirname "$0")/.."

STACK_INGEST="${STACK_INGEST:-CorridorEventHubIngest}"
REGION_ARG=""
[ -n "${AWS_REGION:-}" ] && REGION_ARG="--region $AWS_REGION"

hr() { printf '%.0s-' {1..72}; echo; }

out() {
  aws cloudformation describe-stacks $REGION_ARG --stack-name "$STACK_INGEST" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

echo "Corridor Event Hub status"
hr

# --- stacks ----------------------------------------------------------------
echo "STACKS"
aws cloudformation describe-stacks $REGION_ARG \
  --query "Stacks[?starts_with(StackName,'CorridorEventHub')].[StackName,StackStatus]" \
  --output text 2>/dev/null | sed 's/^/  /' || echo "  none found"

BUCKET=$(out RawBucketName)
EVENT_TABLE=$(out EventTableName)
if [ -z "$BUCKET" ]; then
  echo
  echo "could not read stack outputs - is $STACK_INGEST deployed in this region?"
  exit 1
fi

CATALOG_TABLE=$(aws dynamodb list-tables $REGION_ARG --no-paginate \
  --query "TableNames[?contains(@,'SourceCatalog')]" --output text 2>/dev/null \
  | tr '\t' '\n' | grep -v '^None$' | grep . | head -1)

# --- source health ---------------------------------------------------------
echo
echo "SOURCE HEALTH  (written by the collector on every attempt)"
if [ "$CATALOG_TABLE" != "None" ] && [ -n "$CATALOG_TABLE" ]; then
  aws dynamodb scan $REGION_ARG --table-name "$CATALOG_TABLE" --output json 2>/dev/null \
  | python3 -c "
import json,sys,datetime
d=json.load(sys.stdin)
items=d.get('Items',[])
if not items:
    print('  catalog empty - collector has not run yet')
now=datetime.datetime.now(datetime.timezone.utc)
for it in sorted(items,key=lambda x:list(x.get('sourceId',{}).values())[0]):
    f={k:list(v.values())[0] for k,v in it.items()}
    sid=f.get('sourceId','?'); status=f.get('lastStatus','?')
    last=f.get('lastSuccessAt')
    age='never'
    flag='  '
    if last:
        try:
            age_s=(now-datetime.datetime.fromisoformat(last.replace('Z','+00:00'))).total_seconds()
            age=f'{int(age_s)}s ago'
            # A stale feed degrades confidence in its records.
            if age_s > 1800: flag='! '
        except Exception: age=last
    ok='ok ' if str(status)=='200' else 'BAD'
    print(f'  {flag}{sid:16s} {ok} http={status:>4} {f.get(\"lastBytes\",\"?\"):>8}B {f.get(\"lastLatencyMs\",\"?\"):>5}ms  last success {age}')
"
else
  echo "  catalog table not found"
fi

# --- raw zone --------------------------------------------------------------
echo
echo "RAW ZONE  s3://$BUCKET"
for src in $(aws s3 ls "s3://$BUCKET/raw/" 2>/dev/null | awk '{print $2}' | tr -d '/'); do
  n=$(aws s3 ls "s3://$BUCKET/raw/$src/" --recursive 2>/dev/null | wc -l | tr -d ' ')
  latest=$(aws s3 ls "s3://$BUCKET/raw/$src/" --recursive 2>/dev/null | tail -1 | awk '{print $1" "$2}')
  printf '  %-24s %4s payload(s)   latest %s\n' "${src#source=}" "$n" "${latest:-none}"
done
[ -z "$(aws s3 ls "s3://$BUCKET/raw/" 2>/dev/null)" ] && echo "  empty - no payloads collected yet"

# --- normalizer output -----------------------------------------------------
echo
echo "NORMALIZER  (last 30 min)"
NORM_LG=$(aws logs describe-log-groups $REGION_ARG --log-group-name-prefix CorridorEventHub \
  --no-paginate --query "logGroups[?contains(logGroupName,'NormalizerLogs')].logGroupName" \
  --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | grep . | head -1)
if [ "$NORM_LG" != "None" ] && [ -n "$NORM_LG" ]; then
  # TOLERATE BOTH JSON SPACINGS. This read `"msg":"normalized"` and therefore never
  # matched anything: the handler logs with `json.dumps` defaults, which emit
  # `{"msg": "normalized", ...}` WITH a space after the colon. The symptom was the
  # worst kind - "no normalize events in the window" on a pipeline that was
  # normalizing every minute, which reads as a dead stage rather than as a broken
  # grep. Only the resolver's S3 payloads use compact separators.
  aws logs tail "$NORM_LG" $REGION_ARG --since 30m --format short 2>/dev/null \
    | grep -E '"msg": *"normalized"' \
    | python3 -c "
import sys,json,re
rows=[]
for line in sys.stdin:
    m=re.search(r'(\{.*\})',line)
    if not m: continue
    try: j=json.loads(m.group(1))
    except Exception: continue
    rows.append(j)
if not rows:
    print('  no normalize events in the window')
else:
    agg={}
    for r in rows:
        s=r['sourceId']
        a=agg.setdefault(s,{'runs':0,'cand':0,'off':0,'iss':0,'lo':1.0,'hi':0.0})
        a['runs']+=1; a['cand']+=r.get('candidates',0)
        a['off']+=r.get('offCorridor',0); a['iss']+=r.get('issues',0)
        cr=r.get('confidenceRange')
        if cr: a['lo']=min(a['lo'],cr[0]); a['hi']=max(a['hi'],cr[1])
    for s,a in sorted(agg.items()):
        conf='-' if a['hi']==0 else f\"{a['lo']:.3f}-{a['hi']:.3f}\"
        print(f\"  {s:16s} {a['runs']:>3} run(s)  {a['cand']:>4} candidates  {a['off']:>4} off-corridor  {a['iss']:>4} issues  confidence {conf}\")
"
else
  echo "  normalizer log group not found"
fi

# --- errors ----------------------------------------------------------------
echo
echo "ERRORS  (last 30 min)"
ERRS=0
for lg in $(aws logs describe-log-groups $REGION_ARG --log-group-name-prefix CorridorEventHub \
    --no-paginate --query "logGroups[].logGroupName" --output text 2>/dev/null \
    | tr '\t' '\n' | grep -v '^None$' | grep .); do
  hits=$(aws logs tail "$lg" $REGION_ARG --since 30m --format short 2>/dev/null \
    | grep -icE 'ERROR|errorMessage|Task timed out|fetch_failed|quarantin' || true)
  if [ "${hits:-0}" -gt 0 ]; then
    echo "  ${hits} in $(basename "$lg")"
    aws logs tail "$lg" $REGION_ARG --since 30m --format short 2>/dev/null \
      | grep -iE 'ERROR|errorMessage|Task timed out|fetch_failed|quarantin' | tail -3 | cut -c1-160 | sed 's/^/      /'
    ERRS=1
  fi
done
[ "$ERRS" -eq 0 ] && echo "  none"

# --- dead letters ----------------------------------------------------------
# On the one-screen health check because a dead letter is invisible everywhere
# else: the logs show the failure scrolling past, but only the queue depth says
# a payload is STILL not normalized.
echo
echo "DEAD LETTERS"
DLQ_TOTAL=0
DLQ_FOUND=0
for out_key in NormalizerDlqUrl RuleDlqUrl; do
  url=$(aws cloudformation describe-stacks $REGION_ARG --stack-name CorridorEventHubIngest \
    --query "Stacks[0].Outputs[?OutputKey=='$out_key'].OutputValue | [0]" \
    --output text 2>/dev/null)
  [ -z "$url" ] || [ "$url" = "None" ] && continue
  DLQ_FOUND=1
  n=$(aws sqs get-queue-attributes $REGION_ARG --queue-url "$url" \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text 2>/dev/null)
  printf "  %-14s %s\n" "$(basename "$url")" "${n:-?}"
  case "${n:-0}" in ''|0) ;; *) DLQ_TOTAL=$((DLQ_TOTAL + n)) ;; esac
done
if [ "$DLQ_FOUND" -eq 0 ]; then
  echo "  no DLQ outputs on the stack - redeploy to add them (npm run deploy)"
elif [ "$DLQ_TOTAL" -gt 0 ]; then
  echo "  $DLQ_TOTAL dead letter(s) - inspect: npm run dlq-peek   replay: npm run dlq-replay"
else
  echo "  none - every fetched payload became events"
fi

# --- event store -----------------------------------------------------------
echo
echo "EVENT STORE  $EVENT_TABLE"
CNT=$(aws dynamodb scan $REGION_ARG --table-name "$EVENT_TABLE" --select COUNT \
  --query Count --output text 2>/dev/null)
echo "  ${CNT:-?} item(s)"
if [ "${CNT:-0}" = "0" ]; then
  cat <<'EOF'
  NOT EXPECTED. The resolver and the lifecycle state machine are built and
  deployed, so an empty event store means candidates are reaching EventBridge
  and nothing is persisting them. Check, in this order:
    npm run dlq                  resolver DLQs - a handler that raised
    npm run logs                 resolver invocations and their errors
    npm run invoke -- ok-odot-wzdx   force one collection end to end
EOF
fi

hr
echo "raw payloads:  aws s3 cp s3://$BUCKET/<key> - | python3 -m json.tool | head -40"
echo "live tail:     npm run logs"
echo "force a run:   npm run invoke -- ok-odot-wzdx"
