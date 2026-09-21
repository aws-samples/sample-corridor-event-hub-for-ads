#!/usr/bin/env node
/**
 * CDK app entry point.
 *
 * Two stacks: network (VPC, endpoints, security groups) and ingest (collect,
 * store, normalize). Split because the VPC is slow to create and rarely
 * changes, while the ingest stack changes constantly — nobody should wait on NAT
 * gateway creation to redeploy a Lambda.
 */

import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { NetworkStack } from '../lib/network-stack';
import { IngestStack } from '../lib/ingest-stack';
import { ObservabilityStack } from '../lib/observability-stack';
import { SpatialStack } from '../lib/spatial-stack';
import { PrototypeSecurityNagPack } from "./prototype-security";
import { solutionDescription } from '../lib/solution';


const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-west-2',
};

const prefix = app.node.tryGetContext('prefix') ?? 'CorridorEventHub';

const network = new NetworkStack(app, `${prefix}Network`, {
  env,
  // 1 NAT is ~$32/mo. Set 2 for AZ-independent egress in production.
  natGateways: Number(app.node.tryGetContext('natGateways') ?? 1),
  enableFlowLogs: app.node.tryGetContext('flowLogs') === 'true',
  description: solutionDescription('Corridor Event Hub for ADS network - VPC for all Lambda functions'),
});

/**
 * Aurora Serverless v2 + PostGIS - the recommended conflation implementation
 * from ADR 0002. Separate stack because a database has a very different change
 * cadence from Lambda code: nobody should wait on cluster creation to redeploy
 * an adapter, and nobody should risk a cluster replacement to fix a typo.
 *
 * Deploy explicitly:  npx cdk deploy CorridorEventHubSpatial
 */
const spatial = new SpatialStack(app, `${prefix}Spatial`, {
  env,
  network,
  minCapacityAcu: Number(app.node.tryGetContext('dbMinAcu') ?? 0.5),
  maxCapacityAcu: Number(app.node.tryGetContext('dbMaxAcu') ?? 4),
  deletionProtection: app.node.tryGetContext('dbDeletionProtection') === 'true',
  // Data API on by default; -c dbDataApi=false to turn it off.
  enableDataApi: app.node.tryGetContext('dbDataApi') !== 'false',
  description: solutionDescription(
    'Corridor Event Hub for ADS spatial - Aurora Serverless v2 PostgreSQL with PostGIS',
  ),
});

/**
 * The whole pipeline: collect, normalize, resolve, serve.
 *
 * ONE STACK RATHER THAN FOUR, unlike network and spatial, and for the same reason
 * those two are separate: change cadence. A VPC and a database cluster are slow to
 * create and almost never change, so waiting on them to redeploy a handler is pure
 * cost. The four pipeline stages change together, several times a day under active
 * development, and they share the event bus and the event store - splitting them would
 * turn those into CloudFormation exports, which cannot change without a two-phase
 * deploy. See the note on queue NAMES below for the same trap.
 */
const ingest = new IngestStack(app, `${prefix}Ingest`, {
  env,
  network,
  // Serve the query API unauthenticated. Off by default; see
  // IngestStackProps.publicQueryApi before turning it on:
  //   npx cdk deploy CorridorEventHubIngest -c publicQueryApi=true
  publicQueryApi: app.node.tryGetContext('publicQueryApi') === 'true',
  // WZDx feed_info identity. An adopting DOT publishes under its own name:
  //   npx cdk deploy -c wzdxPublisher='Oklahoma DOT' -c wzdxContactEmail=...
  wzdxPublisher: app.node.tryGetContext('wzdxPublisher'),
  wzdxContactName: app.node.tryGetContext('wzdxContactName'),
  wzdxContactEmail: app.node.tryGetContext('wzdxContactEmail'),
  wzdxLicense: app.node.tryGetContext('wzdxLicense'),
  description: solutionDescription(
    'Corridor Event Hub for ADS pipeline - collect, store raw, normalize, resolve, serve',
  ),
});

/**
 * Observability is a separate stack so alarm thresholds can be tuned without
 * redeploying the pipeline - thresholds get adjusted far more often than handlers do.
 *
 * Subscribe an email or the alarms are theatre:
 *   npx cdk deploy --all -c alarmEmail=you@example.com
 */
new ObservabilityStack(app, `${prefix}Observability`, {
  env,
  collectorLogGroupName: ingest.collectorLogGroup.logGroupName,
  normalizerLogGroupName: ingest.normalizerLogGroup.logGroupName,
  resolverLogGroupName: ingest.resolverLogGroup.logGroupName,
  collectorFunctionName: ingest.collectorFunction.functionName,
  normalizerFunctionName: ingest.normalizerFunction.functionName,
  sourceIds: ingest.activeSourceIds,
  // Cadence travels with the source list: the dashboard's bucket size and each
  // staleness window are derived from it rather than hardcoded, so changing a poll
  // interval in the catalog cannot leave the monitoring describing the old one.
  sourceCadenceSeconds: ingest.activeSourceCadenceSeconds,
  // Per-source healthy mapping-issue ceiling, for the same reason: the spike threshold
  // is a property of the feed, not of this stack, so it lives in the catalog next to the
  // cadence and a re-baselining is a catalog edit rather than an alarm rewrite.
  sourceMappingIssueCeiling: ingest.activeSourceMappingIssueCeiling,
  // Queue NAMES rather than the constructs, matching the log-group and function
  // props above: passing constructs across stacks creates CloudFormation exports,
  // which then cannot be changed without a two-phase deploy. A DLQ is exactly the
  // kind of thing that gets retuned repeatedly.
  normalizerDlqName: ingest.normalizerDlq.queueName,
  ruleDlqName: ingest.ruleDlq.queueName,
  resolverDlqName: ingest.resolverDlq.queueName,
  resolverRuleDlqName: ingest.resolverRuleDlq.queueName,
  lifecycleStateMachineName: ingest.lifecycleStateMachine.stateMachineName,
  alarmEmail: app.node.tryGetContext('alarmEmail'),
  description: solutionDescription('Corridor Event Hub for ADS observability - dashboard, alarms, metric filters'),
});

cdk.Tags.of(app).add('Project', 'Corridor-Event-Hub-ADS');
/**
 * Exempts these resources from automated cleanup sweeps. Applied at the app
 * level so it reaches every taggable resource in every stack - including ones
 * added later, which is the point: a resource that appears after the fact and
 * silently lacks the tag is exactly what gets reaped.
 *
 * NOT a substitute for the real protections. The raw S3 bucket carries
 * RemovalPolicy.RETAIN and Object Lock because the raw zone is the one thing
 * that cannot be regenerated; this tag only tells external
 * janitors to leave things alone.
 */
cdk.Tags.of(app).add('auto-delete', 'no');
// Per-source and per-class cost attribution starts with tagging. Adopters
// should set this to whatever their own cost-allocation scheme expects.
cdk.Tags.of(app).add('CostCenter', 'corridor-event-hub');

/**
 * =========================================================================
 * CDK-NAG - the AWS Solutions rule pack, at SYNTH
 * =========================================================================
 *
 * WHY AT SYNTH RATHER THAN AS A REVIEW STEP. A DOT security review happens once,
 * late, against whatever the template says that week. This runs on every `cdk
 * synth`, which means on every `npm run check` and in front of every deploy: a
 * resource that arrives without encryption, without a dead-letter path, or with a
 * wildcard nobody justified fails the build where it was introduced. The
 * reference architecture has to be defensible by an adopter who was not in
 * the room, and an unexplained wildcard is exactly what they cannot defend.
 *
 * EVERY FINDING IS EITHER FIXED OR ACKNOWLEDGED IN WRITING. The suppressions live
 * next to the resources they excuse - lib/network-stack.ts, lib/ingest-stack.ts,
 * lib/spatial-stack.ts - never in a central allowlist, because a list far from the
 * code is how a temporary exception becomes permanent. `verbose` keeps the rule's
 * own explanation in the failure so nobody has to look up a rule ID, and the pack
 * also writes a per-stack CSV of what it checked into cdk.out.
 *
 * VERSION 2, DELIBERATELY. cdk-nag 3.x moves suppression onto CDK's native
 * `Validations.of(x).acknowledge()`, which rejects any rule ID containing more
 * than one `::` - and the ID for the most common finding in any CDK app,
 * `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/...]`, contains
 * four. There is no way to acknowledge the managed policies CDK itself attaches to
 * a Lambda execution role. Revisit when that is fixed upstream; until then v2's
 * `NagSuppressions` is the API that can express what this stack needs.
 */
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
cdk.Aspects.of(app).add(new PrototypeSecurityNagPack({ verbose: true, reports: true }));


app.synth();
