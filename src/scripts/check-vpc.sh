#!/usr/bin/env bash
#
# VPC placement check.
#
# Requirement: EVERY Lambda function runs in the VPC.
#
# This is a CI check rather than a convention because the violation is invisible
# in source code. CDK helper props — `logRetention` is the notorious one — inject
# singleton custom-resource Lambdas that run OUTSIDE the VPC. Nothing in the
# stack file mentions them; they only appear in the synthesized template. So the
# only reliable place to assert this is against the template.
#
# Run: npm run lint:vpc   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d cdk.out ]; then
  echo "cdk.out not found — run 'npx cdk synth' first"
  exit 1
fi

FAIL=0
FOUND=0

for tpl in cdk.out/*.template.json; do
  case "$tpl" in *"assembly"*) continue;; esac

  while IFS=$'\t' read -r name has_vpc; do
    FOUND=$((FOUND + 1))
    if [ "$has_vpc" = "yes" ]; then
      echo "  ok    $(basename "$tpl" .template.json)/$name"
    else
      echo "  FAIL  $(basename "$tpl" .template.json)/$name — NOT in VPC"
      FAIL=1
    fi
  done < <(python3 -c "
import json,sys
t=json.load(open('$tpl'))
for k,v in t.get('Resources',{}).items():
    if v.get('Type')=='AWS::Lambda::Function':
        print(k+'\t'+('yes' if 'VpcConfig' in v.get('Properties',{}) else 'no'))
")
done

echo
if [ "$FOUND" -eq 0 ]; then
  echo "no Lambda functions found in synthesized templates"
  exit 1
fi

if [ "$FAIL" -eq 0 ]; then
  echo "PASS  all $FOUND Lambda function(s) are in the VPC"
else
  cat <<'EOF'
FAILED — a Lambda is running outside the VPC.

Most likely cause: a CDK prop that injects a helper function. Known offenders:
  - `logRetention`      -> use an explicit logs.LogGroup + `logGroup` instead
  - `autoDeleteObjects` -> on s3.Bucket, injects a cleanup Lambda
  - custom resources    -> Provider framework functions need explicit vpc props

See lib/ingest-stack.ts for the logRetention workaround.
EOF
fi

exit "$FAIL"
