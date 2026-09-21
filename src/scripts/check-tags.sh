#!/usr/bin/env bash
#
# Required-tag check against the synthesized templates.
#
# WHY: `auto-delete=no` exempts these resources from automated cleanup sweeps.
# A resource that appears later and silently lacks the tag is exactly what gets
# reaped - and you find out when it is already gone. So assert it at synth.
#
# The check understands that many CloudFormation resource types do not support
# tags at all (route table associations, IAM policies, metric filters,
# dashboards, Lambda permissions, ...). Those are reported as skipped, not as
# failures - flagging them would train people to ignore this check.
#
# Run: npm run lint:tags   (after cdk synth)

set -uo pipefail
cd "$(dirname "$0")/.."

REQUIRED_TAGS="${REQUIRED_TAGS:-auto-delete=no Project=Corridor-Event-Hub-ADS}"

if [ ! -d cdk.out ]; then
  echo "cdk.out not found - run 'npx cdk synth' first"
  exit 1
fi

python3 - "$REQUIRED_TAGS" <<'PY'
import json, glob, sys, collections

required = dict(pair.split('=', 1) for pair in sys.argv[1].split())

# Types CloudFormation does not accept a Tags property on. Empirically: CDK
# never emits Tags for these, which is the reliable signal.
UNTAGGABLE = {
    'AWS::CDK::Metadata',
    'AWS::CloudWatch::Dashboard',
    'AWS::EC2::Route',
    'AWS::EC2::SecurityGroupIngress',
    'AWS::EC2::SecurityGroupEgress',
    'AWS::EC2::SubnetRouteTableAssociation',
    'AWS::EC2::VPCEndpoint',
    'AWS::EC2::VPCGatewayAttachment',
    'AWS::Events::Rule',
    'AWS::IAM::Policy',
    'AWS::Lambda::Permission',
    'AWS::Logs::MetricFilter',
    'AWS::S3::BucketPolicy',
    'AWS::Scheduler::Schedule',
    'AWS::SNS::Subscription',
    'AWS::SNS::TopicPolicy',
    # A resource policy attached to a queue, not a thing of its own - same
    # category as BucketPolicy and TopicPolicy above. Verified: CDK emits Tags
    # for AWS::SQS::Queue but not for this.
    'AWS::SQS::QueuePolicy',
    'AWS::Lambda::EventInvokeConfig',
    'AWS::Lambda::LayerVersion',
    # An HTTP API's routes and integrations are parts OF the api, not resources of
    # their own, and CloudFormation gives neither a Tags property. Verified against
    # aws-cdk-lib's generated spec: CfnApiProps and CfnStageProps have `tags`,
    # CfnRouteProps and CfnIntegrationProps do not - so the api and the stage
    # carrying the cost tags is the whole of what can be attributed here.
    'AWS::ApiGatewayV2::Route',
    'AWS::ApiGatewayV2::Integration',
    # A link between a secret and its target, not a taggable thing of its own.
    # Verified: CDK emits Tags for AWS::SecretsManager::Secret but not for this.
    'AWS::SecretsManager::SecretTargetAttachment',
    'AWS::RDS::DBSubnetGroup',
    'AWS::RDS::DBClusterParameterGroup',
}

# Resources whose tags live under a NON-STANDARD property name.
#
# The trap this guards against: `cdk.Tags.of(app)` DOES tag such a resource, but
# writes a differently-named key, so a checker looking only for `Tags` reports a
# false missing-tag failure. Found when a Cognito user pool (since removed) was
# flagged despite its UserPoolTags being complete. Kept because the next service
# with a non-standard tag property will hit exactly this, and an empty dict makes
# the extension point obvious.
TAG_PROPERTY_ALIASES: dict[str, str] = {}

def tag_keys(props, rtype=''):
    tags = props.get(TAG_PROPERTY_ALIASES.get(rtype, 'Tags'))
    if isinstance(tags, list):
        return {t.get('Key'): t.get('Value') for t in tags if isinstance(t, dict)}
    if isinstance(tags, dict):
        return dict(tags)
    return {}

ok = skipped = 0
problems = []
skipped_types = collections.Counter()

for f in sorted(glob.glob('cdk.out/*.template.json')):
    if 'assembly' in f:
        continue
    stack = f.split('/')[-1].replace('.template.json', '')
    for name, res in json.load(open(f)).get('Resources', {}).items():
        rtype = res.get('Type', '')
        if rtype in UNTAGGABLE:
            skipped += 1
            skipped_types[rtype] += 1
            continue
        found = tag_keys(res.get('Properties', {}) or {}, rtype)
        for key, want in required.items():
            got = found.get(key)
            if got is None:
                problems.append(f'{stack}/{name} [{rtype}] missing {key}')
            elif str(got) != want:
                problems.append(
                    f'{stack}/{name} [{rtype}] {key}={got!r}, expected {want!r}'
                )
        if all(str(found.get(k)) == v for k, v in required.items()):
            ok += 1

req = ' '.join(f'{k}={v}' for k, v in required.items())
print(f'  required: {req}')
print(f'  tagged correctly: {ok}')
print(f'  skipped (type does not support tags): {skipped}')
for rtype, n in skipped_types.most_common():
    print(f'      {n:3d}  {rtype}')

if problems:
    print()
    for p in problems:
        print(f'  FAIL  {p}')

sys.exit(1 if problems else 0)
PY
rc=$?

echo
if [ "$rc" -eq 0 ]; then
  echo "PASS  every taggable resource carries the required tags"
else
  cat <<'EOF'
FAILED - a taggable resource is missing a required tag.

Tags are applied app-wide in bin/corridor-event-hub.ts via cdk.Tags.of(app), so a gap here
usually means either:
  - a resource created outside the CDK app (check the console / CLI), or
  - a new resource type that DOES support tags but is listed in this script's
    UNTAGGABLE set. Remove it from that set.
EOF
fi

exit "$rc"
