#!/usr/bin/env bash
#
# ASCII check for synthesized CloudFormation property values.
#
# WHY THIS EXISTS: several AWS services reject non-ASCII in string properties.
# EC2 SecurityGroup GroupDescription is the one that bit us:
#
#   Value (Corridor Event Hub Lambda functions - egress only) for parameter
#   GroupDescription is invalid. Character sets beyond ASCII are not supported.
#
# An em dash in a description is invisible in review, passes typecheck, passes
# `cdk synth`, and fails only at CREATE time - after the stack has begun building
# and has to roll back. That is the most expensive place to find it.
#
# Scanning the TEMPLATE rather than the source is the point: it catches values
# built by string interpolation and values CDK generates, not just literals a
# grep of *.ts would find.
#
# Run: npm run lint:ascii   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d cdk.out ]; then
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

FAIL=0

for tpl in cdk.out/*.template.json; do
  case "$tpl" in *"assembly"*) continue;; esac

  out=$(python3 - "$tpl" <<'PY'
import json, sys

path = sys.argv[1]
tpl = json.load(open(path, encoding='utf-8'))

# Properties known to reject non-ASCII outright. Add as they are discovered.
# EC2 is the strict family; most other services tolerate UTF-8 fine.
STRICT = {
    'AWS::EC2::SecurityGroup': ['GroupDescription'],
    'AWS::EC2::SecurityGroupIngress': ['Description'],
    'AWS::EC2::SecurityGroupEgress': ['Description'],
}

def nonascii(s):
    return [c for c in s if ord(c) > 127]

problems = []

for name, res in tpl.get('Resources', {}).items():
    rtype = res.get('Type', '')
    props = res.get('Properties', {}) or {}

    # Hard failures: strict properties.
    for key in STRICT.get(rtype, []):
        val = props.get(key)
        if isinstance(val, str):
            bad = nonascii(val)
            if bad:
                problems.append(('FAIL', name, rtype, key, val, bad))

    # Advisory: any other string property carrying non-ASCII. Usually harmless,
    # but worth surfacing because the strict list is certainly incomplete.
    def walk(obj, trail):
        if isinstance(obj, str):
            bad = nonascii(obj)
            if bad:
                problems.append(('WARN', name, rtype, trail, obj, bad))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f'{trail}.{k}' if trail else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f'{trail}[{i}]')

    strict_keys = STRICT.get(rtype, [])
    for k, v in props.items():
        if k in strict_keys:
            continue
        walk(v, k)

# Stack-level Description also goes to CloudFormation.
desc = tpl.get('Description')
if isinstance(desc, str) and nonascii(desc):
    problems.append(('WARN', '<stack>', 'AWS::CloudFormation::Stack',
                     'Description', desc, nonascii(desc)))

rc = 0
for level, name, rtype, key, val, bad in problems:
    chars = ' '.join(f'U+{ord(c):04X}({c})' for c in dict.fromkeys(bad))
    snippet = val if len(val) <= 90 else val[:87] + '...'
    print(f'  {level}  {name} [{rtype}]')
    print(f'        {key} = {snippet}')
    print(f'        non-ASCII: {chars}')
    if level == 'FAIL':
        rc = 1
sys.exit(rc)
PY
)
  rc=$?
  if [ -n "$out" ]; then
    echo "$(basename "$tpl" .template.json):"
    echo "$out"
  fi
  [ "$rc" -ne 0 ] && FAIL=1
done

echo
if [ "$FAIL" -eq 0 ]; then
  echo "PASS  no non-ASCII in strict CloudFormation properties"
  echo
  echo "note: WARN lines are advisory. Most services accept UTF-8; the strict"
  echo "      list in this script is what we know rejects it. Add to it when a"
  echo "      new service turns out to be strict."
else
  cat <<'EOF'
FAILED - a strict CloudFormation property contains non-ASCII.

Typically an em dash (U+2014) in a description. Replace with a plain hyphen.
Comments and docs can keep their typography; only values that reach
CloudFormation properties need to be ASCII.
EOF
fi

exit "$FAIL"
