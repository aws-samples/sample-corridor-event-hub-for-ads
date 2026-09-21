#!/usr/bin/env bash
#
# RDS engine-version check against what AWS actually offers today.
#
# WHY: CDK's AuroraPostgresEngineVersion enum lags AWS. CDK 2.173 stops at
# VER_16_6, which AWS has RETIRED - us-west-2 now offers 16.8 through 16.14.
# Using the enum synths cleanly and fails at CREATE with an invalid engine
# version: the same synth-passes/deploy-fails shape as the em-dash and
# metric-filter bugs.
#
# This calls describe-db-engine-versions and confirms the pinned version is
# still available in the target region. Needs AWS credentials, so it is a
# best-effort check that SKIPS rather than fails when offline.
#
# Run: npm run lint:engines   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

if [ ! -d cdk.out ]; then
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

# Collect (engine, version) pairs from the synthesized templates.
PAIRS=$(python3 - <<'PY'
import json, glob
seen = set()
for f in glob.glob('cdk.out/*.template.json'):
    if 'assembly' in f:
        continue
    for name, res in json.load(open(f)).get('Resources', {}).items():
        if res.get('Type') not in ('AWS::RDS::DBCluster', 'AWS::RDS::DBInstance'):
            continue
        p = res.get('Properties', {}) or {}
        engine, version = p.get('Engine'), p.get('EngineVersion')
        if isinstance(engine, str) and isinstance(version, str):
            seen.add((engine, version))
for engine, version in sorted(seen):
    print(f'{engine} {version}')
PY
)

if [ -z "$PAIRS" ]; then
  echo "  no RDS resources in the synthesized templates - nothing to check"
  echo
  echo "PASS  (no engine versions to verify)"
  exit 0
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "$PAIRS" | sed 's/^/  pinned: /'
  echo
  echo "SKIP  no AWS credentials - cannot verify against live engine versions."
  echo "      Run this before deploying; a retired version fails at CREATE, not synth."
  exit 0
fi

FAIL=0
while read -r engine version; do
  [ -z "$engine" ] && continue
  printf '  %-22s %-10s ' "$engine" "$version"
  avail=$(aws rds describe-db-engine-versions \
    --engine "$engine" --engine-version "$version" \
    --query 'DBEngineVersions[0].EngineVersion' --output text 2>/dev/null)

  if [ "$avail" = "$version" ]; then
    echo "available"
  else
    echo "NOT AVAILABLE"
    major="${version%%.*}"
    echo "        currently offered for ${major}.x:"
    aws rds describe-db-engine-versions --engine "$engine" \
      --query "DBEngineVersions[?starts_with(EngineVersion,'${major}.')].EngineVersion" \
      --output text 2>/dev/null | tr '\t' '\n' | grep -v limitless | sed 's/^/          /'
    FAIL=1
  fi
done <<< "$PAIRS"

# ---------------------------------------------------------------------------
# Parameter SCOPE check.
#
# Aurora splits parameters into cluster-level and instance-level, and the split
# MOVES between engine majors. On aurora-postgresql18 both
# log_min_duration_statement and shared_preload_libraries are INSTANCE-level; an
# earlier revision of spatial-stack.ts set them on a DBClusterParameterGroup,
# where the engine never reads them. That fails SILENTLY - no deploy error, the
# settings just do not apply.
# ---------------------------------------------------------------------------
echo
echo "  parameter scope:"
python3 - <<'PYEOF' > /tmp/corridoreventhub_params.txt
import json, glob
for f in glob.glob('cdk.out/*.template.json'):
    if 'assembly' in f: continue
    for name, res in json.load(open(f)).get('Resources', {}).items():
        ty = res.get('Type')
        if ty not in ('AWS::RDS::DBClusterParameterGroup', 'AWS::RDS::DBParameterGroup'):
            continue
        p = res.get('Properties', {}) or {}
        fam = p.get('Family')
        scope = 'cluster' if ty.endswith('DBClusterParameterGroup') else 'instance'
        for pname in (p.get('Parameters') or {}):
            print(f'{scope}\t{fam}\t{pname}')
PYEOF

while IFS=$'\t' read -r scope family pname; do
  [ -z "${pname:-}" ] && continue
  if [ "$scope" = "cluster" ]; then
    hits=$(aws rds describe-engine-default-cluster-parameters \
      --db-parameter-group-family "$family" \
      --query "EngineDefaults.Parameters[?ParameterName=='$pname'].ParameterName" \
      --output text 2>/dev/null)
  else
    hits=$(aws rds describe-engine-default-parameters \
      --db-parameter-group-family "$family" \
      --query "EngineDefaults.Parameters[?ParameterName=='$pname'].ParameterName" \
      --output text 2>/dev/null)
  fi
  printf '    %-9s %-24s ' "$scope" "$pname"
  if [ "$hits" = "$pname" ]; then
    echo "ok"
  else
    echo "WRONG SCOPE - not a $scope parameter on $family"
    FAIL=1
  fi
done < /tmp/corridoreventhub_params.txt
rm -f /tmp/corridoreventhub_params.txt

echo
if [ "$FAIL" -eq 0 ]; then
  echo "PASS  engine versions available and parameters correctly scoped"
else
  cat <<'EOF'
FAILED - either a pinned engine version is no longer offered (CREATE will fail),
or a parameter is set at the wrong scope (fails SILENTLY - the setting is simply
never applied).

Fix in lib/spatial-stack.ts. The version is pinned with
`AuroraPostgresEngineVersion.of('18.4', '18')` rather than the CDK enum, because
the enum lags AWS retirements and cannot express 18.x at all.

For a scope error: move the parameter between the cluster parameter group and the
instance parameter group. The split differs by engine major.
EOF
fi

exit "$FAIL"
