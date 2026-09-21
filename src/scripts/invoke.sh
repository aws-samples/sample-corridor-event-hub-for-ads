#!/usr/bin/env bash
#
# Force one collection run for a source, then show what the pipeline did with it.
#
#   npm run invoke -- ok-odot-wzdx
#   bash scripts/invoke.sh nws-alerts
#
# Waiting for a schedule tick to test a change wastes minutes at a time. This
# invokes the collector directly and follows the raw payload through to the
# normalizer's output, which is the actual end-to-end assertion.

set -uo pipefail
cd "$(dirname "$0")/.."

# npm strips the `--` separator before passing args along, but a `--` that survives
# would arrive here as $1 and be read as the sourceId. Skip a bare one.
[ "${1:-}" = "--" ] && shift

SRC="${1:-ok-odot-wzdx}"

# Fail loudly on an unknown source rather than sending it to Lambda, where it
# surfaces as an opaque handler error.
if ! python3 -c "
import json,sys
ids=[s['sourceId'] for s in json.load(open('config/sources.json'))['sources']]
sys.exit(0 if '$SRC' in ids else 1)
" 2>/dev/null; then
  echo "unknown sourceId: $SRC"
  echo
  echo "sources in config/sources.json:"
  python3 -c "
import json
for s in json.load(open('config/sources.json'))['sources']:
    mark = '*' if s['status'] == 'verified_live' else ' '
    print(f\"  {mark} {s['sourceId']:16s} {s['status']}\")
print()
print('  * = scheduled and collecting')
"
  exit 1
fi
REGION_ARG=""
[ -n "${AWS_REGION:-}" ] && REGION_ARG="--region $AWS_REGION"

FN=$(aws lambda list-functions $REGION_ARG --no-paginate \
  --query "Functions[?contains(FunctionName,'CollectorFn')].FunctionName" --output text 2>/dev/null \
  | tr '\t' '\n' | grep -v '^None$' | grep . | head -1)

if [ -z "$FN" ]; then
  echo "collector function not found - is the stack deployed in this region?"
  exit 1
fi

echo "invoking $FN for sourceId=$SRC"
echo

TMP=$(mktemp)
aws lambda invoke $REGION_ARG \
  --function-name "$FN" \
  --payload "$(printf '{"sourceId":"%s"}' "$SRC" | base64)" \
  --cli-binary-format base64 \
  "$TMP" \
  --query '[StatusCode,FunctionError]' --output text 2>&1

echo "response:"
python3 -m json.tool < "$TMP" 2>/dev/null | sed 's/^/  /' || sed 's/^/  /' "$TMP"
rm -f "$TMP"

# The normalizer runs asynchronously off EventBridge, so give it a moment.
echo
echo "waiting 12s for the normalizer to pick it up..."
sleep 12

NORM_LG=$(aws logs describe-log-groups $REGION_ARG --log-group-name-prefix CorridorEventHub \
  --no-paginate --query "logGroups[?contains(logGroupName,'NormalizerLogs')].logGroupName" \
  --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | grep . | head -1)

echo
echo "normalizer output:"
aws logs tail "$NORM_LG" $REGION_ARG --since 2m --format short 2>/dev/null \
  | grep -E '"msg": *"(normalized|no_adapter_registered)"' \
  | tail -3 \
  | python3 -c "
import sys,json,re
found=False
for line in sys.stdin:
    m=re.search(r'(\{.*\})',line)
    if not m: continue
    try: j=json.loads(m.group(1))
    except Exception: continue
    found=True
    if j.get('msg')=='normalized':
        cr=j.get('confidenceRange')
        conf=f\"{cr[0]:.3f}-{cr[1]:.3f}\" if cr else 'n/a'
        print(f\"  {j['sourceId']}: {j.get('candidates')} candidates, \"
              f\"{j.get('offCorridor')} off-corridor, {j.get('issues')} issues, confidence {conf}\")
    else:
        print('  '+json.dumps(j))
if not found:
    print('  nothing yet - the normalizer may still be running. Try: npm run logs')
"
