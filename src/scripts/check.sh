#!/usr/bin/env bash
#
# Everything CI checks, in the order it checks it.
#
# Ordered cheapest-first so a typo fails in seconds rather than after a synth.
# The synth-dependent checks come last because they need cdk.out to exist.
#
# Run: npm run check   (or make check)

set -uo pipefail
cd "$(dirname "$0")/.."

VENV=.venv
PY="$VENV/bin/python"
RUFF="$VENV/bin/ruff"

if [ ! -x "$PY" ]; then
  echo "no venv found - run 'npm run setup' first"
  exit 1
fi

FAILED=()

step() {
  local name="$1"
  shift
  echo
  echo "=============================================================="
  echo "  $name"
  echo "=============================================================="
  if "$@"; then
    echo "-> PASS  $name"
  else
    echo "-> FAIL  $name"
    FAILED+=("$name")
  fi
}

# --- Python: fast, and where the pipeline logic lives ----------------------
step "ruff (lint)" "$RUFF" check corridor_event_hub tests
step "pytest" "$PY" -m pytest -q

# --- Portability: the reference-architecture guarantee ---------------------
step "portability" bash scripts/check-portability.sh

# --- Secrets and personal information (DP-4) -------------------------------
# HIGH IN THE ORDER, deliberately. It is fast, needs no network and no synth, and it
# guards the finding that mattered most in the security review: real personal
# information in three committed fixtures, which no infrastructure scan could see
# because none of them read fixture CONTENT. A contributor should learn about it in
# the first ten seconds of a check rather than after a two-minute synth.
step "secrets and personal information" bash scripts/check-secrets.sh

# --- Generated fixtures match their generator ------------------------------
# Three of the six fixtures are generated rather than captured, because their sources
# do not grant redistribution (tests/fixtures/README.md). A hand-edit to one of those
# files is silently reverted by the next generator run, and the edit's author would
# find out when a test they thought they fixed broke again. Cheap, and it belongs next
# to the secrets check for the same reason: fixture problems should surface in the
# first ten seconds, not after a two-minute synth.
step "generated fixtures up to date" \
  bash -c '.venv/bin/python scripts/make-synthetic-fixtures.py --check'

# --- Standards conformance -------------------------------------------------
# Before the synth steps because it needs only Python, and because a
# non-conformant published feed is a requirement failure rather than a deploy one.
step "WZDx conformance" bash scripts/check-wzdx.sh

# --- The UI: typecheck and the geometry tests ------------------------------
# Skipped rather than failed when deps are absent, so `npm run check` still works for
# someone who only touched the Python pipeline. CI runs `npm run setup` first, so
# there the checks always execute.
if [ -d ui/node_modules ]; then
  step "ui tsc (typecheck)" bash -c 'cd ui && npx tsc --noEmit'
  step "ui vitest (strip geometry)" bash -c 'cd ui && npx vitest run'
else
  echo
  echo "SKIP  ui checks - run 'npm run install-ui' to enable them"
fi

# The tracker UI. Its tests need no AWS account either - the arithmetic they cover
# (step-run collapsing, band widths, filters) is pure, which is why it lives in
# ui-trace/src/derive.ts rather than inside the components.
if [ -d ui-trace/node_modules ]; then
  step "ui-trace tsc (typecheck)" bash -c 'cd ui-trace && npx tsc --noEmit'
  step "ui-trace vitest (trace derivation)" bash -c 'cd ui-trace && npx vitest run'
else
  echo
  echo "SKIP  ui-trace checks - run 'npm run install-trace-ui' to enable them"
fi

# --- The Lambda bundle, then TypeScript, then synth ------------------------
# The bundle has to exist before synth: lib/ingest-stack.ts throws without it.
step "lambda bundle" bash scripts/build-lambda.sh
step "tsc (CDK typecheck)" npx tsc --noEmit
# cdk-nag (AwsSolutions pack) is an aspect in bin/corridor-event-hub.ts, so it runs HERE
# rather than as a step of its own: an unsuppressed finding fails the synth. Per-stack
# reports land in cdk.out/AwsSolutions--*-NagReport.csv.
step "cdk synth (+ cdk-nag AwsSolutions)" npx cdk synth --quiet

# --- Template assertions: everything below reads cdk.out -------------------
step "all Lambdas in VPC" bash scripts/check-vpc.sh
step "python runtime matches the bundle" bash scripts/check-python-runtime.sh
step "dead-letter coverage" bash scripts/check-dlq.sh
step "ASCII in strict properties" bash scripts/check-ascii.sh
step "metric filter shape" bash scripts/check-metric-filters.sh
step "auto-delete tags" bash scripts/check-tags.sh
# The AWS Solution attribution (SO0358): stack descriptions, the user-agent mapping, and
# the boto3 funnel. Here rather than earlier because three of its four assertions read
# cdk.out - and it belongs among the template assertions for the same reason they do:
# every one of them guards something that deploys perfectly well while being wrong.
step "solution id and user agent" bash scripts/check-solution-id.sh
step "engine versions" bash scripts/check-engine-versions.sh

# --- Dependency advisories -------------------------------------------------
# LAST, because it is the ONE step that needs the network - it queries two advisory
# databases - and it SKIPS rather than fails when it cannot reach them. Putting a
# network-dependent step early would make an offline run look broken from the second
# line. Note that a green check from an offline machine is unaudited, not clean; the
# step says so when it skips.
step "dependency advisories" bash scripts/check-deps.sh

echo
echo "=============================================================="
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "  ALL CHECKS PASSED"
  echo "=============================================================="
  exit 0
fi

echo "  ${#FAILED[@]} CHECK(S) FAILED"
for name in "${FAILED[@]}"; do
  echo "    - $name"
done
echo "=============================================================="
exit 1
