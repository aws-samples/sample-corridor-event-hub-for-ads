#!/usr/bin/env bash
#
# Assert the synthesized Lambda runtime matches the bundle that was built.
#
# WHY THIS EXISTS: shapely ships COMPILED wheels tagged for one CPython minor
# version. scripts/build-lambda.sh installs cp313 wheels; if lib/ingest-stack.ts
# ever says PYTHON_3_12, the deploy SUCCEEDS and every invocation fails with:
#
#   Unable to import module 'corridor_event_hub.handlers.collector': No module named 'shapely'
#
# because /var/runtime/python3.12 does not see a cp313 extension. Nothing catches
# that at synth - two files just quietly disagree about a version number.
#
# Same for architecture: a bundle built manylinux2014_aarch64 needs ARM_64.
#
# Run: npm run lint:runtime   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d cdk.out ]; then
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

# The single source of truth is the build script, so this reads it rather than
# hardcoding a second copy of the version.
BUNDLE_PY=$(grep -E '^PYTHON_VERSION=' scripts/build-lambda.sh | cut -d'"' -f2)
BUNDLE_PLATFORM=$(grep -E '^PLATFORM=' scripts/build-lambda.sh | cut -d'"' -f2)

if [ -z "$BUNDLE_PY" ]; then
  echo "FAIL  could not read PYTHON_VERSION from scripts/build-lambda.sh"
  exit 1
fi

EXPECTED_RUNTIME="python${BUNDLE_PY}"
case "$BUNDLE_PLATFORM" in
  *aarch64*|*arm64*) EXPECTED_ARCH="arm64" ;;
  *x86_64*)          EXPECTED_ARCH="x86_64" ;;
  *)                 EXPECTED_ARCH="" ;;
esac

echo "bundle built for: $EXPECTED_RUNTIME / ${EXPECTED_ARCH:-unknown}"
echo

out=$(python3 - "$EXPECTED_RUNTIME" "$EXPECTED_ARCH" <<'PY'
import glob
import json
import sys

expected_runtime, expected_arch = sys.argv[1], sys.argv[2]
problems = []
checked = 0

for path in sorted(glob.glob('cdk.out/*.template.json')):
    if 'assembly' in path:
        continue
    with open(path, encoding='utf-8') as fh:
        template = json.load(fh)

    for name, resource in (template.get('Resources') or {}).items():
        if resource.get('Type') != 'AWS::Lambda::Function':
            continue
        props = resource.get('Properties') or {}
        runtime = props.get('Runtime')

        # Only assert on Python functions. A Node function here would be a
        # CDK-injected custom resource, which check-vpc.sh is the right place to
        # complain about.
        if not isinstance(runtime, str) or not runtime.startswith('python'):
            continue

        checked += 1
        stack = path.split('/')[-1].replace('.template.json', '')

        if runtime != expected_runtime:
            problems.append(
                f'{stack}.{name}: Runtime is {runtime}, bundle is {expected_runtime}. '
                'The compiled shapely wheel will not import.'
            )

        architectures = props.get('Architectures') or ['x86_64']
        if expected_arch and architectures != [expected_arch]:
            problems.append(
                f'{stack}.{name}: Architectures is {architectures}, bundle is '
                f'[{expected_arch}]. The compiled extension is the wrong machine type.'
            )

        # A Python handler is a dotted module path. `index.handler` would mean an
        # inline-code function that never got a real handler.
        handler = props.get('Handler')
        if isinstance(handler, str) and handler.count('.') < 2:
            problems.append(
                f'{stack}.{name}: Handler "{handler}" is not a dotted module path '
                '(expected e.g. corridor_event_hub.handlers.collector.handler).'
            )

if checked == 0:
    problems.append(
        'no Python Lambda functions found in any template - has the stack been '
        'ported, or did synth produce nothing?'
    )

for problem in problems:
    print(f'  FAIL  {problem}')

print(f'  checked {checked} Python function(s)')
sys.exit(1 if problems else 0)
PY
)
rc=$?
echo "$out"

echo
if [ "$rc" -eq 0 ]; then
  echo "PASS  every Python Lambda matches the bundle's runtime and architecture"
  echo
  echo "note: this catches a version MISMATCH, not a bad bundle. The build script"
  echo "      verifies the compiled extension is really ELF aarch64."
else
  cat <<EOF
FAILED - the synthesized runtime does not match the built bundle.

Fix ONE of these so they agree:
  - lib/ingest-stack.ts       lambda.Runtime.PYTHON_3_* / Architecture
  - scripts/build-lambda.sh   PYTHON_VERSION / PLATFORM

This mismatch deploys cleanly and fails at every invocation with
"No module named 'shapely'". There is no signal at synth.
EOF
fi

exit "$rc"
