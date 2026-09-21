#!/usr/bin/env bash
#
# Metric filter validation against the synthesized template.
#
# WHY THIS EXISTS: CloudWatch Logs rejects `Dimensions` and `DefaultValue`
# together on a MetricTransformation. They are mutually exclusive:
#
#   Invalid metric transformation: dimensions and default value are
#   mutually exclusive properties
#
# CDK accepts both, `cdk synth` accepts both, and the rejection arrives at CREATE
# time - after the stack has begun building and has to roll back. This is the
# third failure in this project with that exact shape (see also check-ascii.sh
# and the logRetention/VPC case in ADR 0001), which is why template assertions
# are worth writing rather than relying on care.
#
# Also checks a dimension limit CloudWatch enforces at runtime rather than at
# create: a metric filter may define at most 3 dimensions.
#
# AND THE ONE THAT ACTUALLY BIT US: an alarm watching a dimensioned filter's metric
# without carrying that filter's dimensions. In CloudWatch the dimensions are part of
# a metric's identity, so such an alarm watches a metric nothing publishes and sits
# `OK` forever - it cannot fire, and nothing anywhere reports a problem. Two alarms
# shipped this way and stayed green through a three-hour feed outage on 2026-08-12.
#
# It is invisible in source because `MetricFilter.metric()` reads like it returns the
# filter's metric, and it copies the namespace and the metric NAME but NOT the
# dimensions - so the correct and the broken call differ only by an argument that is
# easy to leave out. That is precisely the shape of defect worth asserting against the
# synthesized template rather than trusting to review.
#
# Run: npm run lint:metrics   (after cdk synth)

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

tpl = json.load(open(sys.argv[1], encoding='utf-8'))
problems = []
checked = 0

for name, res in tpl.get('Resources', {}).items():
    if res.get('Type') != 'AWS::Logs::MetricFilter':
        continue
    checked += 1
    for i, mt in enumerate(res.get('Properties', {}).get('MetricTransformations', []) or []):
        where = f'{name}.MetricTransformations[{i}]'
        dims = mt.get('Dimensions')
        has_default = 'DefaultValue' in mt

        # The hard failure: mutually exclusive properties.
        if dims and has_default:
            problems.append((
                'FAIL', where,
                'Dimensions and DefaultValue are mutually exclusive - '
                'CloudWatch Logs rejects this at CREATE time. Drop DefaultValue.'
            ))

        # CloudWatch caps a metric filter at 3 dimensions.
        if dims and len(dims) > 3:
            problems.append((
                'FAIL', where,
                f'{len(dims)} dimensions defined; CloudWatch allows at most 3.'
            ))

        # A dimensioned filter with no default means gaps stay gaps. That is
        # usually what you want, but an alarm on it must handle missing data
        # explicitly - so surface it rather than let it be a surprise.
        if dims and not has_default:
            problems.append((
                'INFO', where,
                'dimensioned, no DefaultValue -> missing data stays MISSING. '
                'Alarms on this metric need an explicit treatMissingData.'
            ))

# ---------------------------------------------------------------------------
# Alarms must carry the dimensions of the filter they watch.
#
# Build (namespace, metricName) -> required dimension keys from the filters in this
# template, then check every alarm pointed at one of them. An alarm on a dimensioned
# metric that names no dimensions, or omits one of the keys, can never receive a
# datapoint.
# ---------------------------------------------------------------------------
filter_dims: dict[tuple[str, str], set[str]] = {}
for name, res in tpl.get('Resources', {}).items():
    if res.get('Type') != 'AWS::Logs::MetricFilter':
        continue
    for mt in res.get('Properties', {}).get('MetricTransformations', []) or []:
        dims = mt.get('Dimensions')
        if not dims:
            continue
        key = (mt.get('MetricNamespace'), mt.get('MetricName'))
        filter_dims.setdefault(key, set()).update(
            d['Key'] for d in dims if isinstance(d, dict) and 'Key' in d
        )

alarms_checked = 0
for name, res in tpl.get('Resources', {}).items():
    if res.get('Type') != 'AWS::CloudWatch::Alarm':
        continue
    props = res.get('Properties', {})
    # Metric-math alarms carry their metrics in `Metrics`; each MetricStat there has
    # its own Dimensions, so check those individually.
    stats = []
    if 'Metrics' in props:
        for m in props['Metrics']:
            ms = m.get('MetricStat', {}).get('Metric')
            if ms:
                stats.append((m.get('Id', '?'), ms.get('Namespace'), ms.get('MetricName'),
                              ms.get('Dimensions') or []))
    else:
        stats.append((None, props.get('Namespace'), props.get('MetricName'),
                      props.get('Dimensions') or []))

    for sub, ns, mn, dims in stats:
        required = filter_dims.get((ns, mn))
        if not required:
            continue
        alarms_checked += 1
        present = {d['Name'] for d in dims if isinstance(d, dict) and 'Name' in d}
        missing = required - present
        if missing:
            label = props.get('AlarmName') or name
            where = f'{label}[{sub}]' if sub else label
            problems.append((
                'FAIL', where,
                f'watches {ns}/{mn} but does not set dimension(s) '
                f'{sorted(missing)}, which the metric filter publishes with. In '
                'CloudWatch the dimensions are part of the identity of a metric, so this '
                'alarm can NEVER receive a datapoint and will read OK forever. Pass '
                'dimensionsMap on the metric, one alarm per dimension value, or use a '
                'metric-math SEARCH expression to aggregate across it.'
            ))

for level, where, msg in problems:
    print(f'  {level:5s} {where}')
    print(f'        {msg}')

print(f'  ({checked} metric filter(s), {alarms_checked} alarm metric(s) checked)')
sys.exit(1 if any(l == 'FAIL' for l, _, _ in problems) else 0)
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
  echo "PASS  metric filters are valid, and every alarm carries the dimensions of the"
  echo "      filter it watches"
else
  echo "FAILED - a metric filter would be rejected at CREATE time, or an alarm watches"
  echo "         a dimensioned metric without its dimensions and can never fire."
fi

exit "$FAIL"
