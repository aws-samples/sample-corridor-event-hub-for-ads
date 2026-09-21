#!/usr/bin/env bash
#
# Assert the AWS Solution attribution is complete and consistent everywhere it appears.
#
# WHY THIS EXISTS: onboarding SO0358 requires two strings that change on every release -
# `(SO0358) - ... Version v1.0.0` in each stack description, and
# `AWSSOLUTION/SO0358/v1.0.0` in the User-Agent of every AWS SDK call - and BOTH fail
# silently. A stack whose description lost the prefix still deploys. A boto3 client
# built without the config still works. Nothing in a test run, a synth or a deploy
# notices, and the first symptom is AWS reporting no usage for the solution, months
# later, with no way to backfill it.
#
# So four things are checked, all of them against lib/solution.ts as the single source
# of truth:
#
#   1. corridor_event_hub/core/awsclients.py declares the SAME id and version. The string
#      necessarily exists twice - the TypeScript builds the template, the Python is the
#      fallback for local tools that have no template - and this is what keeps a release
#      from bumping one of them.
#   2. every synthesized template's Description is in the attributed form.
#   3. every template containing a Lambda carries the Solution mapping, still as a
#      mapping: a CDK upgrade that folded Fn::FindInMap to a literal would leave a
#      template that deploys and no longer has one place to bump.
#   4. every Lambda reads SOLUTION_USER_AGENT from that mapping, and no code under
#      corridor_event_hub builds a boto3 client outside core/awsclients.py.
#
# Run: npm run lint:solution   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

# The single source of truth is the TypeScript, so this reads it rather than carrying a
# third copy of the version.
SOLUTION_ID=$(grep -E "^export const SOLUTION_ID = " lib/solution.ts | cut -d"'" -f2)
SOLUTION_VERSION=$(grep -E "^export const SOLUTION_VERSION = " lib/solution.ts | cut -d"'" -f2)

if [ -z "$SOLUTION_ID" ] || [ -z "$SOLUTION_VERSION" ]; then
  echo "FAIL  could not read SOLUTION_ID / SOLUTION_VERSION from lib/solution.ts"
  exit 1
fi

USER_AGENT="AWSSOLUTION/${SOLUTION_ID}/${SOLUTION_VERSION}"
echo "solution: $SOLUTION_ID $SOLUTION_VERSION  ->  $USER_AGENT"
echo

FAILED=0

# --- 1 & 4b: the Python side, which needs no synth ---------------------------
out=$(python3 - "$SOLUTION_ID" "$SOLUTION_VERSION" <<'PY'
import ast
import pathlib
import sys

want_id, want_version = sys.argv[1], sys.argv[2]
problems = []

# --- the constants must agree with lib/solution.ts -------------------------
helper = pathlib.Path('corridor_event_hub/core/awsclients.py')
found = {}
for node in ast.parse(helper.read_text(encoding='utf-8')).body:
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = node.value.value

for name, want in (('SOLUTION_ID', want_id), ('SOLUTION_VERSION', want_version)):
    got = found.get(name)
    if got != want:
        problems.append(
            f'{helper}: {name} is {got!r}, lib/solution.ts says {want!r} - '
            f'a release bumped one and not the other'
        )

# --- no client construction outside the helper -----------------------------
# An AST walk rather than a grep: `boto3.resource('dynamodb')` appears in prose in
# core/eventstore.py's module docstring, and a lint that fails on documentation is a
# lint people delete.
BANNED = {('boto3', 'client'), ('boto3', 'resource'), ('boto3', 'session', 'Session')}


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


for path in sorted(pathlib.Path('corridor_event_hub').rglob('*.py')):
    if path == helper:
        continue
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and dotted(node.func) in BANNED:
            problems.append(
                f'{path}:{node.lineno}: builds an AWS client directly. Use '
                f'core.awsclients.client/resource so the call carries {want_id}.'
            )

for problem in problems:
    print(problem)
sys.exit(1 if problems else 0)
PY
)
status=$?
if [ $status -ne 0 ]; then
  echo "$out"
  echo "FAIL  python side"
  FAILED=1
else
  echo "PASS  python constants match lib/solution.ts, and every client goes through core.awsclients"
fi

# --- 2, 3 & 4a: the templates ------------------------------------------------
if [ ! -d cdk.out ]; then
  echo
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

echo
out=$(python3 - "$SOLUTION_ID" "$SOLUTION_VERSION" "$USER_AGENT" <<'PY'
import glob
import json
import re
import sys

solution_id, version, user_agent = sys.argv[1], sys.argv[2], sys.argv[3]
problems = []
checked = 0
# `(SO0358) - <something>. Version v1.0.0`
pattern = re.compile(rf'^\({re.escape(solution_id)}\) - .+\. Version {re.escape(version)}$')

for path in sorted(glob.glob('cdk.out/*.template.json')):
    if 'assembly' in path:
        continue
    with open(path, encoding='utf-8') as fh:
        template = json.load(fh)
    checked += 1

    description = template.get('Description') or ''
    if not pattern.match(description):
        problems.append(
            f'{path}: Description is {description!r}, expected '
            f'"({solution_id}) - <text>. Version {version}" - wrap it in '
            f'solutionDescription() from lib/solution.ts'
        )

    functions = {
        name: body
        for name, body in template.get('Resources', {}).items()
        if body.get('Type') == 'AWS::Lambda::Function'
    }
    mapped = template.get('Mappings', {}).get('Solution', {}).get('Metadata', {})

    if not functions:
        # No SDK calls originate from this template, so it needs no mapping. It still
        # needs the description above, which is why this is not an early continue.
        print(f'{path}: no Lambda functions - description only')
        continue

    if mapped.get('CustomUserAgent') != user_agent:
        problems.append(
            f'{path}: Mappings.Solution.Metadata.CustomUserAgent is '
            f'{mapped.get("CustomUserAgent")!r}, expected {user_agent!r}'
        )

    for name, body in sorted(functions.items()):
        value = body.get('Properties', {}).get('Environment', {}).get('Variables', {}).get(
            'SOLUTION_USER_AGENT'
        )
        if value is None:
            problems.append(
                f'{path}: {name} has no SOLUTION_USER_AGENT - spread '
                f'solutionUserAgentEnv(this) into its environment, or its boto3 calls '
                f'go out unattributed'
            )
        elif value != {'Fn::FindInMap': ['Solution', 'Metadata', 'CustomUserAgent']}:
            problems.append(
                f'{path}: {name} SOLUTION_USER_AGENT is {value!r}, expected an '
                f'Fn::FindInMap into the Solution mapping. A literal here means the '
                f'mapping is no longer the one place a release bumps.'
            )
    print(f'{path}: {len(functions)} function(s) attributed via the Solution mapping')

print()
print(f'{checked} template(s) checked')
for problem in problems:
    print(f'  {problem}')
sys.exit(1 if problems else 0)
PY
)
status=$?
echo "$out"
if [ $status -ne 0 ]; then
  echo "FAIL  templates"
  FAILED=1
else
  echo "PASS  every template is attributed, every Lambda reads the mapping"
fi

echo
if [ $FAILED -ne 0 ]; then
  echo "FAIL  solution attribution is incomplete - see above"
  exit 1
fi
echo "OK  solution attribution complete ($USER_AGENT)"
