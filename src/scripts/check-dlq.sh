#!/usr/bin/env bash
#
# Dead-letter coverage check.
#
# WHY THIS IS A CI CHECK: the gap it guards was a `TODO` comment in the stack file
# for weeks. Nothing failed, nothing alarmed, and the pipeline looked healthy the
# whole time - because the failure mode is a payload that silently never becomes
# events. A comment does not hold that line; a build failure does.
#
# It asserts BOTH failure paths, because they are genuinely different and covering
# only one looks complete:
#
#   1. The EventBridge rule target has a DLQ    -> undeliverable events
#   2. The normalizer has an async failure path -> the handler raised
#
# Scanning the TEMPLATE rather than the source is the point: it catches a prop that
# was removed, renamed, or wired to the wrong queue - none of which a grep of
# *.ts would reliably find.
#
# Run: npm run lint:dlq   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d cdk.out ]; then
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

TPL=cdk.out/CorridorEventHubIngest.template.json
if [ ! -f "$TPL" ]; then
  echo "FAIL  $TPL not found - did the ingest stack synthesize?"
  exit 1
fi

out=$(python3 - "$TPL" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as fh:
    template = json.load(fh)

resources = template.get('Resources', {})
problems = []
notes = []

queues = {
    name: res for name, res in resources.items() if res.get('Type') == 'AWS::SQS::Queue'
}
if not queues:
    problems.append('no SQS queue in the template - the DLQs are missing entirely')

# --- 1. the EventBridge rule target -----------------------------------------
rules = {n: r for n, r in resources.items() if r.get('Type') == 'AWS::Events::Rule'}
normalizer_rules = {
    n: r
    for n, r in rules.items()
    if 'RawPayloadStored' in json.dumps(r.get('Properties', {}).get('EventPattern', {}))
}
if not normalizer_rules:
    problems.append('no rule matching RawPayloadStored - the normalizer is not wired')

for name, rule in normalizer_rules.items():
    for target in rule.get('Properties', {}).get('Targets', []) or []:
        if not target.get('DeadLetterConfig', {}).get('Arn'):
            problems.append(
                f'{name}: rule target has no DeadLetterConfig - an event EventBridge '
                'cannot deliver is discarded silently'
            )
        else:
            notes.append(f'ok    {name}: rule target has a DLQ')
        retry = target.get('RetryPolicy') or {}
        if retry.get('MaximumRetryAttempts') is None:
            notes.append(f'WARN  {name}: no explicit MaximumRetryAttempts (defaults to 185)')

# --- 2. the normalizer's own failure path -----------------------------------
# Lambda destinations synthesize as AWS::Lambda::EventInvokeConfig, NOT as a
# property of the function. Checking only the function would wrongly report a gap.
invoke_configs = [
    r for r in resources.values() if r.get('Type') == 'AWS::Lambda::EventInvokeConfig'
]
functions = {n: r for n, r in resources.items() if r.get('Type') == 'AWS::Lambda::Function'}
normalizers = {
    n: r
    for n, r in functions.items()
    if 'normalizer' in json.dumps(r.get('Properties', {}).get('Handler', '')).lower()
}

if not normalizers:
    problems.append('no normalizer function found in the template')

covered = False
for cfg in invoke_configs:
    props = cfg.get('Properties', {})
    on_failure = (props.get('DestinationConfig') or {}).get('OnFailure', {})
    if on_failure.get('Destination'):
        covered = True
        attempts = props.get('MaximumRetryAttempts')
        notes.append(
            f'ok    normalizer has an onFailure destination (retries={attempts})'
        )

# A legacy function-level DeadLetterConfig satisfies the requirement too.
for name, fn in normalizers.items():
    if (fn.get('Properties', {}).get('DeadLetterConfig') or {}).get('TargetArn'):
        covered = True
        notes.append(f'ok    {name}: function-level DeadLetterConfig')

if not covered:
    problems.append(
        'the normalizer has NO async failure path (no onFailure destination and no '
        'DeadLetterConfig). A handler that raises loses the payload after its '
        'retries - this is the LIKELIEST failure in practice and the one a '
        'rule-target DLQ does not catch.'
    )

# --- 3. the queues must actually retain long enough to be useful ------------
for name, queue in queues.items():
    retention = queue.get('Properties', {}).get('MessageRetentionPeriod')
    if retention is None:
        notes.append(f'WARN  {name}: no MessageRetentionPeriod (defaults to 4 days)')
    elif retention < 604800:
        notes.append(
            f'WARN  {name}: retention {retention}s < 7 days - a Friday failure may '
            'expire before Monday'
        )

for note in notes:
    print(f'  {note}')
for problem in problems:
    print(f'  FAIL  {problem}')

sys.exit(1 if problems else 0)
PY
)
rc=$?
echo "$out"

echo
if [ "$rc" -eq 0 ]; then
  echo "PASS  both dead-letter paths are covered"
  echo
  echo "note: this asserts the WIRING exists. That the queues are actually watched"
  echo "      is the CorridorEventHub-normalizer-dlq / CorridorEventHub-rule-dlq alarms, and that a"
  echo "      human can read them is scripts/dlq.sh."
else
  cat <<'EOF'
FAILED - a normalization failure can be lost silently.

An unmappable payload has to reach a review queue rather than being dropped. Fix in lib/ingest-stack.ts:

  - rule target      -> `deadLetterQueue: this.ruleDlq`
  - normalizer func  -> `onFailure: new destinations.SqsDestination(this.normalizerDlq)`

Both are needed. They catch different failures.
EOF
fi

exit "$rc"
