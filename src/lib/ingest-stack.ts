/**
 * Ingest stack — collect, store raw, normalize.
 *
 * Shape of the pipeline: EventBridge
 * Scheduler (one schedule per source, so cadence is config) -> collector Lambda -> S3 raw zone (immutable)
 * -> EventBridge -> adapter Lambda -> candidate events.
 *
 * ISOLATION is the reason for one schedule and one function per source: the
 * NFR requires that one misbehaving state feed cannot delay or corrupt the
 * other three.
 *
 * All Lambdas run in the VPC — see network-stack.ts for why that costs more
 * than you would expect.
 *
 * THE INFRASTRUCTURE IS TypeScript AND THE HANDLERS ARE PYTHON. That split is
 * deliberate: CDK's TypeScript surface is its best-documented one, while the
 * pipeline's own logic — conflation, adapters, scoring — belongs in the language
 * the team and its adopters work in, alongside shapely and the wider geospatial
 * ecosystem. The seam between them is `scripts/build-lambda.sh`, which produces
 * `build/lambda` and is asserted by `scripts/check-python-bundle.sh`.
 */

import {
  Arn,
  ArnFormat,
  CfnElement,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  CfnOutput,
  aws_apigateway as apigateway,
  aws_apigatewayv2 as apigwv2,
  aws_apigatewayv2_authorizers as apigwv2auth,
  aws_apigatewayv2_integrations as apigwv2int,
  aws_cloudtrail as cloudtrail,
  aws_events as events,
  aws_events_targets as targets,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_lambda_destinations as destinations,
  aws_logs as logs,
  aws_s3 as s3,
  aws_dynamodb as dynamodb,
  aws_scheduler as scheduler,
  aws_secretsmanager as secretsmanager,
  aws_sqs as sqs,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as sfnTasks,
} from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';
import type { NetworkStack } from './network-stack';
import { lambdaBundleCode } from './lambda-bundle';
import { solutionUserAgentEnv } from './solution';
import sourceCatalog from '../config/sources.json';

/**
 * How cdk-nag renders a CloudFormation `Fn::GetAtt <resource>.Arn` inside an IAM5
 * finding: `<LogicalId.Arn>`. A suppression for a wildcard under one of those has
 * to match that string exactly.
 *
 * Derived rather than pasted. The logical ID is a hash of the construct path, so a
 * pasted one stops matching the moment a construct is renamed or moved - and the
 * failure is a resurfaced finding on an unrelated change, which is the sort of
 * thing that gets suppressed broadly to make it go away.
 */
function arnRef(resource: Construct): string {
  const cfn = resource.node.defaultChild as CfnElement;
  return `<${Stack.of(resource).getLogicalId(cfn)}.Arn>`;
}

/**
 * The corridor's route designation, from the offline corridor document.
 *
 * Read here rather than imported so the file's absence is a clear error at synth
 * rather than a TypeScript resolution failure - and so nothing in lib/ ends up
 * holding a route name of its own, which check-portability.sh would reject.
 */
function corridorRoute(): string {
  const candidate = path.join(__dirname, '../reference/corridor.json');
  if (!fs.existsSync(candidate)) {
    throw new Error(
      `corridor document not found at ${candidate}. It carries the route name the ` +
        'normalizer needs; the geometry itself comes from Postgres at runtime.',
    );
  }
  const parsed = JSON.parse(fs.readFileSync(candidate, 'utf-8')) as { route?: string };
  if (!parsed.route) {
    throw new Error(`${candidate} has no "route" field.`);
  }
  return parsed.route;
}

export interface IngestStackProps extends StackProps {
  readonly network: NetworkStack;
  /** Raw payload retention. Production wants 7 years; prototypes should not. */
  readonly rawRetentionYears?: number;
  /**
   * Serve the query API without IAM authorization.
   *
   * DEFAULTS TO SECURE, and the default is the one to keep. With IAM auth an
   * integrator signs requests with SigV4 - no key to issue, store, or rotate, which
   * is the same reasoning ADR 0004 gives for the tiled source. Opening it is a
   * deliberate act with a visible cost: an unauthenticated endpoint on the public
   * internet, and a cdk-nag APIG4 suppression that names this flag as the reason.
   *
   * The case for turning it on is a demo where a browser or a plain `curl` has to
   * reach the API on stage. If that is the whole need, prefer `npm run serve` - the
   * local API in strip_server.py runs the same code with no endpoint at all.
   */
  readonly publicQueryApi?: boolean;
  /**
   * WZDx `feed_info` identity. An adopting DOT publishes under its
   * own name and licence, so these are configuration rather than literals in the
   * projection - the same portability rule as the corridor and the source catalog.
   */
  readonly wzdxPublisher?: string;
  readonly wzdxContactName?: string;
  readonly wzdxContactEmail?: string;
  readonly wzdxLicense?: string;
}

interface CatalogSource {
  sourceId: string;
  agency: string;
  endpoint: string;
  publishCadenceSeconds?: number;
  status: string;
  authMethod: string;
  mappingIssueCeilingPerRun?: number;
}

/**
 * How often this source is ACTUALLY polled: the catalog's cadence, defaulted and
 * floored the way the scheduler does it.
 *
 * One function rather than the same expression at each use site, because the
 * observability stack sizes its dashboard period and its staleness windows from
 * this number. Two copies of "how often do we poll" is how a dashboard ends up
 * bucketing at five minutes for a source polled every ten - which draws a healthy
 * feed as a broken line.
 */
function effectiveCadenceSeconds(source: CatalogSource): number {
  return Math.max(source.publishCadenceSeconds ?? 300, 60);
}

export class IngestStack extends Stack {
  public readonly rawBucket: s3.Bucket;
  public readonly eventBus: events.EventBus;
  public readonly catalogTable: dynamodb.Table;
  public readonly eventTable: dynamodb.Table;
  /** Exposed so the observability stack can build metric filters over them. */
  public readonly collectorLogGroup: logs.LogGroup;
  public readonly normalizerLogGroup: logs.LogGroup;
  public readonly resolverLogGroup: logs.LogGroup;
  public readonly queryLogGroup: logs.LogGroup;
  public readonly lifecycleLogGroup: logs.LogGroup;
  public readonly collectorFunction: lambda.Function;
  public readonly normalizerFunction: lambda.Function;
  public readonly resolverFunction: lambda.Function;
  public readonly queryFunction: lambda.Function;
  public readonly lifecycleFunction: lambda.Function;
  /** One execution per event, holding its TTL timers. */
  public readonly lifecycleStateMachine: sfn.StateMachine;
  /** The query API's base URL. */
  public readonly queryApi: apigwv2.HttpApi;
  /**
   * The dead-letter queues, exposed so the observability stack can alarm on
   * depth. A normalization failure must be VISIBLE, not silent.
   *
   * Four of them, two per async stage, because an EventBridge delivery failure and
   * a handler that raised are genuinely different failures - see the long comment
   * on the normalizer's pair.
   */
  public readonly normalizerDlq: sqs.Queue;
  public readonly ruleDlq: sqs.Queue;
  public readonly resolverDlq: sqs.Queue;
  public readonly resolverRuleDlq: sqs.Queue;
  /** Sources with status=verified_live, for per-source freshness alarms. */
  public activeSourceIds: string[] = [];
  /**
   * Effective poll interval per active source, so the observability stack can size
   * itself to the pipeline rather than to a hardcoded five minutes.
   */
  public activeSourceCadenceSeconds: Record<string, number> = {};
  /**
   * The highest mapping-issue count a HEALTHY run of each source produces, from the
   * catalog. An unmappable value is recorded rather than dropped, so a
   * steady nonzero rate is correct behaviour and only a SPIKE means the feed's format
   * drifted - which makes the alarm threshold a per-source number by nature.
   * Measured live, these span 4 to 97 issues per run across the six sources, so the
   * one global threshold this replaced could not have fit them all.
   */
  public activeSourceMappingIssueCeiling: Record<string, number> = {};

  constructor(scope: Construct, id: string, props: IngestStackProps) {
    super(scope, id, props);

    const { network } = props;

    // -----------------------------------------------------------------------
    // Raw zone
    // -----------------------------------------------------------------------

    /**
     * Raw payloads are written ONCE and never modified. This is what makes
     * replay and backfill possible: re-running fixed adapter logic over
     * archived bytes reproduces records deterministically, so a mapping
     * correction can be applied historically without touching live state.
     *
     * Object Lock in GOVERNANCE mode: prevents accidental deletion while
     * allowing a privileged override. COMPLIANCE mode cannot be undone even by
     * the root account — correct for a 7-year regulatory archive, wrong for a
     * prototype someone will want to tear down.
     */
    this.rawBucket = new s3.Bucket(this, 'RawZone', {
      objectLockEnabled: true,
      objectLockDefaultRetention: s3.ObjectLockRetention.governance(
        Duration.days(30), // prototype value; production wants 7 years
      ),
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN, // raw data is the one thing not to lose
      lifecycleRules: [
        {
          id: 'tier-to-glacier-ir',
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: Duration.days(30),
            },
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: Duration.days(90),
            },
          ],
        },
      ],
    });

    /**
     * WHO READ, OR DELETED, A RAW PAYLOAD.
     *
     * This trail exists because the S1 suppression further down - server access
     * logging off - argues that the record that matters for an immutable zone is a
     * CloudTrail S3 DATA event rather than an access-log line. The argument was
     * right and unimplemented. Data events are off by default, they are a property
     * of a TRAIL rather than of a bucket (which is why nothing on `RawZone` above
     * could switch them on), and the prototype account's only trail carries
     * management events alone - verified 2026-08-19: `DataResources: []`, no
     * advanced selectors, no Lake store. So the raw zone had neither record, and
     * the suppression was resting on a setting nobody had made.
     *
     * A TRAIL HERE RATHER THAN ADVICE IN A COMMENT, for the same reason the nag
     * pack runs at synth rather than at review: an adopting DOT gets the audit
     * record by deploying, not by reading a suppression and remembering to go
     * and configure something in an account this repository cannot see.
     *
     * SCOPED TO ONE BUCKET, NOT THE ACCOUNT. `addS3EventSelector` on the raw zone
     * rather than `logAllS3DataEvents`, because account-wide data events would bill
     * for every CDK asset upload in the account and bury the events that are
     * actually about the raw zone in them.
     *
     * COST, against the live catalog rather than a guess: 2,736 polls a day across
     * the six verified_live sources, each producing one collector PutObject plus
     * one normalizer GetObject, so ~165k data events a month - about $0.16 at $0.10
     * per 100k, plus a few MB of trail log. It is cheap because the selector names
     * one bucket; it would not be if it named the account.
     */
    const auditLogBucket = new s3.Bucket(this, 'RawZoneAuditLogs', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // An audit record that a teardown deletes is not one. Same reasoning as the
      // raw zone, weaker claim: this bucket's contents CAN be regenerated, just
      // not retroactively - nothing recreates who read an object last March.
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          id: 'expire-audit-log',
          // The bound the raw zone deliberately does not have. Prototype value;
          // A seven-year archive would want its own number here, and an
          // adopting DOT's retention schedule outranks this one either way.
          expiration: Duration.days(365),
        },
      ],
    });

    /**
     * Declared explicitly rather than left to CDK's default (which is a bare
     * `new Bucket(this, 'S3', { enforceSSL: true })`) so it carries the same posture
     * as everything else here and so its growth is BOUNDED - which is the honest
     * part of this change. S1 was declined partly because logging needs a second
     * bucket that accumulates forever behind a zone that is never deleted. This IS
     * that second bucket. What makes it a different trade rather than the same one:
     * it records caller identity per object read and delete instead of an
     * access-log line, and it expires on a rule.
     *
     * MANAGEMENT EVENTS OFF, deliberately (`ReadWriteType.NONE`). Every account
     * that can deploy this already has a trail carrying them; a second copy pays
     * twice for one record. Data events are the part that is off by default and the
     * part this stack is here to supply.
     *
     * SINGLE-REGION: S3 data events are recorded by a trail in the BUCKET's region,
     * and the raw zone is created by this stack in this region. Global service
     * events must be off when the trail is single-region, and they are the account
     * trail's job regardless.
     */
    const rawZoneTrail = new cloudtrail.Trail(this, 'RawZoneTrail', {
      bucket: auditLogBucket,
      managementEvents: cloudtrail.ReadWriteType.NONE,
      isMultiRegionTrail: false,
      includeGlobalServiceEvents: false,
      enableFileValidation: true,
    });

    /**
     * ReadWriteType.ALL, and the READ half is the point. Raw-zone immutability already
     * makes writes the boring direction; the question the raw zone could not answer
     * is who FETCHED an archived payload. `DeleteObject` lands here too, which is
     * what turns a GOVERNANCE-mode bypass, or a delete after the 30-day retention
     * lapses, into an event rather than an absence - the half of the S1 argument
     * that versioning and Object Lock alone do not cover.
     */
    rawZoneTrail.addS3EventSelector([{ bucket: this.rawBucket }], {
      readWriteType: cloudtrail.ReadWriteType.ALL,
    });

    // -----------------------------------------------------------------------
    // Event bus — the spine
    // -----------------------------------------------------------------------

    this.eventBus = new events.EventBus(this, 'Bus', {
      eventBusName: `${this.stackName}-bus`,
    });

    // -----------------------------------------------------------------------
    // Source catalog
    // -----------------------------------------------------------------------

    this.catalogTable = new dynamodb.Table(this, 'SourceCatalog', {
      partitionKey: { name: 'sourceId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
      pointInTimeRecovery: true,
    });

    // -----------------------------------------------------------------------
    // Event store
    // -----------------------------------------------------------------------

    /**
     * PK = eventId, SK = 'v#<version>' | 'audit#<ts>' | 'current'.
     *
     * Versions and audit records share the table so an event's entire history —
     * every version AND every lifecycle transition — is one query. Append-only:
     * nothing is ever overwritten, which is what makes the audit trail immutable
     * and bitemporal reconstruction possible.
     *
     * The GSI supports the corridor query pattern: by measure range within a
     * lifecycle state. Recall from ADR 0002 § The split, and why that this is a
     * NUMERIC range query, not a spatial one — the reason the event store does not
     * need to be a spatial database.
     */
    this.eventTable = new dynamodb.Table(this, 'EventStore', {
      partitionKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
      pointInTimeRecovery: true,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES, // feeds change subscribers
    });

    this.eventTable.addGlobalSecondaryIndex({
      indexName: 'by-state-measure',
      partitionKey: { name: 'gsiLifecycleRoute', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsiBeginMeasure', type: dynamodb.AttributeType.NUMBER },
    });

    // -----------------------------------------------------------------------
    // Shared Lambda configuration
    // -----------------------------------------------------------------------

    /**
     * Fail at SYNTH if the Python bundle is missing, with the command that fixes
     * it. `lambda.Code.fromAsset` on an absent directory produces an error far
     * from its cause, and "run npm run bundle" is the entire remedy.
     *
     * `config/` is named as well as the package: these handlers read the corridor
     * and the source catalog at IMPORT time, so a bundle without it does
     * not fail with a missing file, it fails to import the module at all.
     */
    const bundleCode = lambdaBundleCode(['corridor_event_hub', 'config', 'certs']);

    /**
     * NOTE ON logRetention: the `logRetention` prop is deliberately NOT used.
     * It makes CDK inject a singleton "LogRetention" custom-resource Lambda that
     * runs OUTSIDE the VPC — which would silently violate the "all Lambdas in
     * VPC" requirement. Explicit LogGroup constructs achieve the same retention
     * with no extra function. Verified by scripts/check-vpc.sh.
     */
    const commonLambda = {
      // Must match PYTHON_VERSION in scripts/build-lambda.sh: the bundle carries
      // cp313 compiled wheels and will not import on another minor version.
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64, // ~20% cheaper than x86
      tracing: lambda.Tracing.ACTIVE,
      code: bundleCode,
      // ALL Lambdas in the VPC, per instruction. See network-stack.ts on cost.
      ...network.lambdaVpcConfig,
    };

    /**
     * The AWS Solutions user-agent string, which every function's boto3 clients read
     * from the environment (corridor_event_hub/core/awsclients.py).
     *
     * NOT part of `commonLambda`, and that is not an oversight: each function below
     * spreads `...commonLambda` and then declares its own `environment`, which
     * REPLACES the spread map rather than merging into it. An env var placed in
     * `commonLambda` would be silently dropped by all five. So it is spread into each
     * `environment` explicitly, and scripts/check-solution-id.sh asserts no function
     * ever ends up without it.
     */
    const solutionEnv = solutionUserAgentEnv(this);

    // -----------------------------------------------------------------------
    // Collector — one per source (isolation NFR)
    // -----------------------------------------------------------------------

    /**
     * The spatial database's credentials, by name.
     *
     * RESOLVED BY NAME rather than imported from the spatial stack: a construct
     * reference across stacks becomes a CloudFormation export, which then cannot
     * change without a two-phase deploy. By name, the two stacks deploy in either
     * order. Declared up here rather than beside the normalizer because the
     * COLLECTOR needs it too - see its environment below.
     *
     * If the spatial stack has not been deployed, this resolves to an ARN that does
     * not exist and the function fails at its first invocation with a clear Secrets
     * Manager error rather than silently conflating against nothing. That is the
     * deliberate consequence of the corridor living in Postgres: the pipeline now
     * depends on the spatial stack. See docs/SPATIAL-DB.md.
     */
    const spatialDbSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'SpatialDbSecret',
      'corridor-event-hub/spatial-db-credentials',
    );

    /**
     * Is any source fetched as TILES rather than as one URL? That single question
     * drives two grants and two environment variables on the collector, so it is
     * asked once.
     */
    const hasTiledSource = (sourceCatalog.sources as CatalogSource[]).some(
      (s) => s.authMethod === 'aws_sigv4' && s.status === 'verified_live',
    );

    this.collectorLogGroup = new logs.LogGroup(this, 'CollectorLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const collector = (this.collectorFunction = new lambda.Function(this, 'CollectorFn', {
      ...commonLambda,
      logGroup: this.collectorLogGroup,
      // Python handler paths are dotted module paths, not file paths.
      handler: 'corridor_event_hub.handlers.collector.handler',
      // Generous: a slow state feed should not fail, it should be recorded slow.
      timeout: Duration.seconds(60),
      memorySize: 512,
      environment: {
        ...solutionEnv,
        RAW_BUCKET: this.rawBucket.bucketName,
        EVENT_BUS: this.eventBus.eventBusName,
        CATALOG_TABLE: this.catalogTable.tableName,
        /**
         * THE COLLECTOR NEEDS THE CORRIDOR, but only for a tiled source, and only
         * to decide WHICH TILES to ask for: a tiled feed has no single URL, so the
         * addresses are derived from the centerline before anything is fetched.
         *
         * This is the one collector input that is not in the catalog, and leaving
         * it out broke `aws-location-traffic` the moment the corridor moved from
         * the Lambda bundle into Postgres - every poll failed with "no offline
         * corridor found" while the five URL-fetched sources kept working, because
         * they do not touch geometry until normalization. A source that needs the
         * corridor at FETCH time is a different shape from one that needs it at
         * PARSE time, and the deploy that moved the corridor only accounted for the
         * second.
         *
         * Granted only when a tiled source is actually live, so a deployment
         * without one hands the collector no database at all.
         */
        ...(hasTiledSource
          ? {
              CEH_ROUTE: corridorRoute(),
              SPATIAL_DB_SECRET_ARN: spatialDbSecret.secretArn,
            }
          : {}),
      },
      description: 'Fetch one source feed, store raw bytes immutably, announce it',
    }));

    this.rawBucket.grantPut(collector);
    this.eventBus.grantPutEventsTo(collector);
    this.catalogTable.grantReadWriteData(collector);

    /**
     * Source API keys live in Secrets Manager under `corridor-event-hub/*`, never in the
     * catalog (which is committed to git) and never in environment variables.
     * Scoped to the path prefix rather than granting broad secret access.
     *
     * Held in a const because the cdk-nag suppression at the foot of this file has
     * to name the same ARN, and that ARN carries the account and the region: a
     * literal there would pass in this account and fail in an adopter's.
     */
    const sourceSecretsArn = Stack.of(this).formatArn({
      service: 'secretsmanager',
      resource: 'secret',
      resourceName: 'corridor-event-hub/*',
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });

    collector.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['secretsmanager:GetSecretValue'],
        resources: [sourceSecretsArn],
      }),
    );

    /**
     * Traffic tiles are read with the collector's own execution role rather than
     * an API key — the one source in the catalog with no credential to store or
     * rotate, which is most of why it is worth having (ADR 0004).
     *
     * Granted only when a source actually needs it, so a deployment that drops
     * the tiled source does not keep a stray permission.
     *
     * SCOPED TO THE PROVIDER ARN, and the correction is worth recording because the
     * comment that used to sit here asserted the opposite - that `GetTile` "has no
     * resource-level ARN at all", so the action was the only available scope. That
     * was wrong. The Service Authorization Reference for Amazon
     * Location Service Maps defines a `provider` resource type,
     * `arn:<partition>:geo-maps:<region>::provider/default`, and lists it as
     * REQUIRED for both GetTile and GetStaticMap.
     *
     * The security gain here is small - there is exactly one provider - but the
     * wrong comment was the real defect: an adopter copying this pattern AND its
     * justification would have believed further scoping was impossible and stopped
     * looking. A confidently-worded "cannot be scoped" is more durable than an
     * unscoped policy, because nobody revisits it.
     *
     * Note the EMPTY ACCOUNT field. The provider is an AWS-owned resource, not one
     * in this account, so the ARN carries a region and no account id. Getting that
     * backwards produces a policy that synthesizes, deploys, and denies every call.
     */
    if (hasTiledSource) {
      collector.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['geo-maps:GetTile'],
          resources: [
            Arn.format(
              {
                service: 'geo-maps',
                region: this.region,
                account: '',
                resource: 'provider',
                resourceName: 'default',
                arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
              },
              this,
            ),
          ],
        }),
      );
      /**
       * No secretsmanager grant needed for the corridor credential: the collector
       * already holds GetSecretValue on `corridor-event-hub/*` above, and the spatial
       * credential is `corridor-event-hub/spatial-db-credentials`. Reachability is likewise
       * already there - one shared Lambda security group, which the database SG
       * admits on 5432 (network-stack.ts). Asserted here because "it works without
       * a grant" reads like an oversight otherwise.
       */
    }

    // -----------------------------------------------------------------------
    // Normalizer — runs the adapters
    // -----------------------------------------------------------------------

    this.normalizerLogGroup = new logs.LogGroup(this, 'NormalizerLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    /**
     * =====================================================================
     * DEAD-LETTER QUEUES
     * =====================================================================
     *
     * A normalization failure MUST be visible. Without these, a payload that
     * cannot be parsed is retried twice and then discarded with no record: the
     * raw bytes survive in S3, but nothing says they were never
     * normalized, so the corridor silently under-reports and every dashboard
     * still looks green. That is the exact "fails quietly" mode
     * README.md § Not built yet warns about.
     *
     * TWO QUEUES, BECAUSE THERE ARE TWO DISTINCT FAILURE PATHS, and this is the
     * part the original TODO understated. An EventBridge target DLQ and a Lambda
     * async DLQ do NOT catch the same thing:
     *
     *   ruleDlq       - EventBridge could not DELIVER the event to Lambda at all:
     *                   throttling, the function deleted, permissions revoked.
     *                   The handler never ran. The message is the EventBridge
     *                   envelope.
     *
     *   normalizerDlq - Lambda accepted the event, ran the handler, and the
     *                   handler RAISED (a malformed payload, an S3 read failure,
     *                   an adapter bug) through all retries. This is by far the
     *                   likelier failure in practice, and it is the one a
     *                   target-only DLQ misses entirely.
     *
     * Configuring only the first would have looked complete and left the common
     * case silent, which is worse than an obvious gap.
     */
    this.normalizerDlq = new sqs.Queue(this, 'NormalizerDlq', {
      queueName: `${this.stackName}-normalizer-dlq`,
      // 14 days is the SQS maximum. A payload that failed on Friday must still be
      // there on Monday - a DLQ that expires its evidence over a weekend is not a
      // DLQ. The retention is longer than the raw zone's Object Lock window (30d)
      // is short, deliberately: evidence must outlive the incident that produced it.
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
      // Long enough for a human to inspect a message and decide, without it
      // reappearing mid-investigation.
      visibilityTimeout: Duration.minutes(5),
    });

    this.ruleDlq = new sqs.Queue(this, 'RuleDlq', {
      queueName: `${this.stackName}-rule-dlq`,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
      visibilityTimeout: Duration.minutes(5),
    });

    const normalizer = (this.normalizerFunction = new lambda.Function(this, 'NormalizerFn', {
      ...commonLambda,
      logGroup: this.normalizerLogGroup,
      handler: 'corridor_event_hub.handlers.normalizer.handler',
      timeout: Duration.seconds(120), // polygon conflation is the slow path
      // shapely + numpy + the corridor geometry. Memory also buys CPU on Lambda,
      // and the 400-sample polygon intersection is the slow path.
      memorySize: 1024,
      /**
       * Handler-failure path. `onFailure` uses Lambda DESTINATIONS rather than the
       * legacy `deadLetterQueue` prop, because a destination records WHY it failed
       * - the error type, the message, the stack trace, and the original event -
       * whereas a plain DLQ delivers only the payload and leaves you correlating
       * by timestamp against CloudWatch. For a review queue whose entire purpose is
       * explaining what could not be mapped, the reason is the point.
       *
       * `retryAttempts: 2` is Lambda's own async retry, distinct from the
       * EventBridge target retry below. A transient S3 read deserves a retry; a
       * malformed payload will fail all three times and land in the queue, which
       * is the correct outcome.
       */
      onFailure: new destinations.SqsDestination(this.normalizerDlq),
      retryAttempts: 2,
      environment: {
        ...solutionEnv,
        RAW_BUCKET: this.rawBucket.bucketName,
        EVENT_BUS: this.eventBus.eventBusName,
        EVENT_TABLE: this.eventTable.tableName,
        /**
         * WHICH CORRIDOR THIS FUNCTION SERVES, as configuration rather than a
         * literal in the handler. scripts/check-portability.sh enforces that
         *: a route name in corridor_event_hub/handlers is a build failure.
         *
         * Read at SYNTH from reference/corridor.json, which is the offline corridor
         * and deliberately NOT deployed - the function loads the corridor's geometry
         * from Postgres at runtime, keyed by this route. Only the NAME crosses here.
         *
         * A second corridor is a second value, not a code change.
         */
        CEH_ROUTE: corridorRoute(),
        /**
         * THE CORRIDOR ITSELF COMES FROM POSTGRES, and this is the only thing the
         * function needs in order to find it. reference/corridor.json is no longer
         * bundled: a corridor in the deployment package is one route, frozen at
         * build time, and duplicated in every function.
         *
         * Only the secret ARN is passed. Host, port and database name come from the
         * secret body, which RDS populates when CDK attaches it to the cluster - so
         * there is nothing here to go stale if the cluster is replaced.
         *
         * RESOLVED BY NAME rather than imported from the spatial stack. A construct
         * reference across stacks becomes a CloudFormation export, which then cannot
         * change without a two-phase deploy - the same trap the log-group and queue
         * props above avoid by passing names. The secret's name is fixed in
         * lib/spatial-stack.ts, so this needs no export and the two stacks can
         * deploy in either order.
         */
        SPATIAL_DB_SECRET_ARN: spatialDbSecret.secretArn,
      },
      description: 'Parse a raw payload into candidate events. No dedup, no scoring.',
    }));

    this.rawBucket.grantRead(normalizer);
    this.eventBus.grantPutEventsTo(normalizer);
    this.eventTable.grantReadWriteData(normalizer);
    // Read only: the normalizer uses this credential, it does not rotate it.
    spatialDbSecret.grantRead(normalizer);

    // Fire the normalizer when a raw payload lands.
    new events.Rule(this, 'OnRawPayloadStored', {
      eventBus: this.eventBus,
      description: 'RawPayloadStored -> normalize',
      eventPattern: {
        source: ['corridor-event-hub.collector'],
        detailType: ['RawPayloadStored'],
      },
      targets: [
        new targets.LambdaFunction(normalizer, {
          retryAttempts: 2,
          // An UNDELIVERABLE event must be visible, not silent. The
          // handler-failure case is caught by the function's own onFailure
          // destination above - see the comment on the queues for why both exist.
          deadLetterQueue: this.ruleDlq,
          maxEventAge: Duration.hours(2),
        }),
      ],
    });

    // -----------------------------------------------------------------------
    // Schedules — cadence is data, from the catalog
    // -----------------------------------------------------------------------

    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });
    collector.grantInvoke(schedulerRole);

    const group = new scheduler.CfnScheduleGroup(this, 'SourceSchedules', {
      name: `${this.stackName}-sources`,
    });

    const active = (sourceCatalog.sources as CatalogSource[]).filter(
      (s) => s.status === 'verified_live',
    );

    /**
     * Two guards, because `status: verified_live` used to mean "the URL works"
     * and is now also the signal to SCHEDULE a poll. Those are different claims,
     * and conflating them scheduled the annual NBI batch file on a 5-minute
     * timer against a URL still containing a literal `{STATE}` placeholder.
     */
    for (const src of active) {
      /*
       * A template endpoint is normally a bug — except where the source is not
       * fetched by URL at all. A tiled source's endpoint is a {Z}/{X}/{Y}
       * template by nature: the collector derives N addresses from the corridor
       * geometry and signs each with SigV4, so there is no single URL to GET and
       * nothing for this guard to protect. Keyed on authMethod rather than on the
       * sourceId so a second tiled source does not have to amend this check.
       */
      const fetchedByUrl = src.authMethod !== 'aws_sigv4';
      if (fetchedByUrl && src.endpoint.includes('{')) {
        throw new Error(
          `${src.sourceId} is marked verified_live but its endpoint is a TEMPLATE ` +
            `(${src.endpoint}). A scheduled poll would request the placeholder literally. ` +
            'Resolve the template in an adapter/loader, or use a non-scheduling status.',
        );
      }
      // Batch sources do not belong on the poll path at all.
      const cadence = src.publishCadenceSeconds ?? 300;
      if (cadence > 86_400) {
        throw new Error(
          `${src.sourceId} has a cadence of ${cadence}s (>1 day) but is marked ` +
            'verified_live, so it would be scheduled as a poll. Batch sources need a ' +
            'separate loader, not EventBridge Scheduler.',
        );
      }
    }

    for (const src of active) {
      // Poll at the source's stated cadence, floored at 60s. Observed cadence
      // must be MEASURED rather than trusted — the collector records it.
      const seconds = effectiveCadenceSeconds(src);
      new scheduler.CfnSchedule(this, `Schedule-${src.sourceId}`, {
        name: `corridor-event-hub-${src.sourceId}`,
        groupName: group.name,
        flexibleTimeWindow: { mode: 'OFF' },
        scheduleExpression: `rate(${Math.round(seconds / 60)} minutes)`,
        target: {
          arn: collector.functionArn,
          roleArn: schedulerRole.roleArn,
          input: JSON.stringify({ sourceId: src.sourceId }),
          retryPolicy: { maximumRetryAttempts: 2 },
        },
        description: `${src.agency} - ${src.sourceId}`,
      });
    }

    // -----------------------------------------------------------------------
    // Resolver — match, merge, and version
    // -----------------------------------------------------------------------

    /**
     * The stage the architecture calls RESOLVE. Consumes `CandidateEventProduced` from the normalizer and writes versioned events with audit records.
     *
     * ONE CANDIDATE PER INVOCATION, not a batch, and that is a real decision. A
     * batch would let one function compare candidates against each other in memory
     * and be cheaper per record - but every candidate in a batch also has to be
     * compared against what is already STORED, so the store read happens either
     * way, and a batch turns one poisonous payload into a batch-wide failure. Per
     * candidate, a bad record fails alone and lands in the DLQ alone.
     *
     * CONCURRENCY IS DELIBERATELY CAPPED. Two candidates for the same event
     * arriving together contend on the same DynamoDB item, and the store's
     * conditional writes turn that into a retry rather than a corruption -
     * but retries cost latency against the ingest budget. Ten concurrent resolvers
     * is well above corridor arrival rates and low enough that same-event
     * contention stays rare. It also bounds write throughput on the table, which is
     * on-demand and would otherwise absorb a replay storm at full speed.
     */
    this.resolverLogGroup = new logs.LogGroup(this, 'ResolverLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.resolverDlq = new sqs.Queue(this, 'ResolverDlq', {
      queueName: `${this.stackName}-resolver-dlq`,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
      visibilityTimeout: Duration.minutes(5),
    });

    this.resolverRuleDlq = new sqs.Queue(this, 'ResolverRuleDlq', {
      queueName: `${this.stackName}-resolver-rule-dlq`,
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
      visibilityTimeout: Duration.minutes(5),
    });

    const resolver = (this.resolverFunction = new lambda.Function(this, 'ResolverFn', {
      ...commonLambda,
      logGroup: this.resolverLogGroup,
      handler: 'corridor_event_hub.handlers.resolver.handler',
      // Matching is arithmetic over a few hundred events — see
      // ADR 0002 § The split, and why — so the time
      // here is DynamoDB round trips rather than computation.
      timeout: Duration.seconds(60),
      memorySize: 512,
      reservedConcurrentExecutions: 10,
      onFailure: new destinations.SqsDestination(this.resolverDlq),
      retryAttempts: 2,
      environment: {
        ...solutionEnv,
        EVENT_BUS: this.eventBus.eventBusName,
        EVENT_TABLE: this.eventTable.tableName,
        CEH_ROUTE: corridorRoute(),
      },
      description: 'Match a candidate against stored events; merge, update or create',
    }));

    this.eventTable.grantReadWriteData(resolver);
    this.eventBus.grantPutEventsTo(resolver);

    new events.Rule(this, 'OnCandidateEventProduced', {
      eventBus: this.eventBus,
      description: 'CandidateEventProduced -> resolve',
      eventPattern: {
        source: ['corridor-event-hub.normalizer'],
        detailType: ['CandidateEventProduced'],
      },
      targets: [
        new targets.LambdaFunction(resolver, {
          retryAttempts: 2,
          deadLetterQueue: this.resolverRuleDlq,
          maxEventAge: Duration.hours(2),
        }),
      ],
    });

    // -----------------------------------------------------------------------
    // Lifecycle timers — one execution per event
    // -----------------------------------------------------------------------

    /**
     * THE TTL LADDER, and the reason it is not optional: without it an event stays
     * `active` for as long as the store exists, which is precisely the failure mode
     * of existing 511 feeds, so the timer is a MUST rather than a nicety.
     *
     * Shape: Wait -> Tick -> Choice -> Wait. The Lambda decides; the machine only
     * sleeps and loops. See handlers/lifecycle.py on why the tick re-reads the event
     * instead of trusting its input, and on the handoff at 200 ticks.
     *
     * A DETERMINISTIC NAME, because the tick Lambda needs to start a successor
     * execution and a construct reference would be circular: the machine's
     * definition names the function, so a policy on the function that names the
     * machine closes the loop and CDK refuses to synth. Naming the machine here and
     * granting on the CONSTRUCTED arn breaks the cycle - the same technique as
     * resolving the spatial secret by name above.
     */
    const lifecycleStateMachineName = `${this.stackName}-lifecycle`;
    const lifecycleStateMachineArn = Stack.of(this).formatArn({
      service: 'states',
      resource: 'stateMachine',
      resourceName: lifecycleStateMachineName,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });

    this.lifecycleLogGroup = new logs.LogGroup(this, 'LifecycleLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const lifecycle = (this.lifecycleFunction = new lambda.Function(this, 'LifecycleFn', {
      ...commonLambda,
      logGroup: this.lifecycleLogGroup,
      handler: 'corridor_event_hub.handlers.lifecycle.handler',
      // One read, one conditional write, one PutEvents. Nothing here is slow.
      timeout: Duration.seconds(30),
      memorySize: 512,
      environment: {
        ...solutionEnv,
        EVENT_BUS: this.eventBus.eventBusName,
        EVENT_TABLE: this.eventTable.tableName,
        LIFECYCLE_STATE_MACHINE_ARN: lifecycleStateMachineArn,
      },
      description: 'One TTL tick: expire a stale event down the ladder',
    }));

    this.eventTable.grantReadWriteData(lifecycle);
    this.eventBus.grantPutEventsTo(lifecycle);
    lifecycle.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['states:StartExecution'],
        resources: [lifecycleStateMachineArn],
      }),
    );

    const wait = new sfn.Wait(this, 'WaitForTtl', {
      // Dynamic: each tick returns how long until the NEXT expiry, which is a
      // per-class TTL and changes as the event moves down the ladder. A
      // fixed interval would be a polling loop with extra steps.
      time: sfn.WaitTime.secondsPath('$.waitSeconds'),
    });

    const tick = new sfnTasks.LambdaInvoke(this, 'LifecycleTick', {
      lambdaFunction: lifecycle,
      // The handler's return value BECOMES the state, so `$.waitSeconds` and
      // `$.status` on the next loop are what it just decided.
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
    });

    /**
     * A tick that fails for any other reason must not kill the timer silently - that
     * would leave the event unexpiring forever, which is the failure this whole
     * machine exists to prevent. Retry, then fail the execution LOUDLY so the
     * Step Functions failed-execution alarm fires.
     */
    tick.addRetry({
      errors: ['States.ALL'],
      interval: Duration.seconds(10),
      maxAttempts: 3,
      backoffRate: 2,
    });

    const stateMachine = (this.lifecycleStateMachine = new sfn.StateMachine(
      this,
      'LifecycleStateMachine',
      {
        stateMachineName: lifecycleStateMachineName,
        // STANDARD, not Express: these executions run for days to months and
        // Express caps at five minutes. The execution history is also a second
        // audit trail, which Express does not keep.
        stateMachineType: sfn.StateMachineType.STANDARD,
        timeout: Duration.days(400),
        tracingEnabled: true,
        logs: {
          destination: new logs.LogGroup(this, 'LifecycleStateMachineLogs', {
            retention: logs.RetentionDays.TWO_WEEKS,
            removalPolicy: RemovalPolicy.DESTROY,
          }),
          // ALL rather than ERROR, and not merely to satisfy AwsSolutions-SF1.
          // The architecture claims the execution history is a second audit trail for
          // every lifecycle transition; that claim is only true if the successful
          // transitions are logged too, which is exactly what ERROR omits.
          level: sfn.LogLevel.ALL,
          includeExecutionData: true,
        },
        definitionBody: sfn.DefinitionBody.fromChainable(
          wait.next(
            tick.next(
              new sfn.Choice(this, 'MoreTimers')
                .when(
                  sfn.Condition.stringEquals('$.status', 'wait'),
                  // Loops back to the same Wait state.
                  wait,
                )
                .otherwise(new sfn.Succeed(this, 'NoFurtherTimers')),
            ),
          ),
        ),
        comment:
          'Corridor Event Hub lifecycle TTL ladder: reported/validated/active/clearing ' +
          'expire toward cleared with a stale_no_updates reason.',
      },
    ));

    // The resolver arms one execution per event it creates.
    stateMachine.grantStartExecution(resolver);
    resolver.addEnvironment('LIFECYCLE_STATE_MACHINE_ARN', stateMachine.stateMachineArn);

    // -----------------------------------------------------------------------
    // Query API — serve
    // -----------------------------------------------------------------------

    this.queryLogGroup = new logs.LogGroup(this, 'QueryLogs', {
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const query = (this.queryFunction = new lambda.Function(this, 'QueryFn', {
      ...commonLambda,
      logGroup: this.queryLogGroup,
      handler: 'corridor_event_hub.handlers.query.handler',
      // Synchronous: a caller is waiting, so the timeout is a latency budget rather
      // than a safety net. The p95 budget is 90s end to end for INGEST; a query that
      // takes 29s has already failed for its consumer.
      timeout: Duration.seconds(29),
      memorySize: 1024,
      environment: {
        ...solutionEnv,
        EVENT_TABLE: this.eventTable.tableName,
        CEH_ROUTE: corridorRoute(),
        /**
         * The corridor, for the two things that need geometry: a bbox filter and a
         * lat/lon look-ahead. Everything else the API answers is numeric, so
         * handlers/query.py loads this LAZILY and a query that needs no geometry
         * never opens a database connection.
         */
        SPATIAL_DB_SECRET_ARN: spatialDbSecret.secretArn,
        /**
         * WZDx feed_info, which the spec requires to name a publisher and which a
         * consumer uses to attribute the data.
         *
         * CONFIGURATION, not literals in the projection, for the portability reason that
         * runs through this whole stack: another DOT adopting this publishes under
         * its own name, and that must be a context value rather than a code change.
         * The contact fields are optional in the spec and omitted when unset -
         * an empty contact_email is worse than none, because it validates.
         */
        WZDX_PUBLISHER: props.wzdxPublisher ?? 'Corridor Event Hub for ADS',
        WZDX_CONTACT_NAME: props.wzdxContactName ?? '',
        WZDX_CONTACT_EMAIL: props.wzdxContactEmail ?? '',
        WZDX_LICENSE: props.wzdxLicense ?? '',
        // Advertised refresh cadence. The feed is built live per request, so this
        // tells a consumer how often it is worth asking rather than how stale the
        // answer is.
        WZDX_UPDATE_FREQUENCY: '300',
      },
      description:
        'Query events by corridor range, class, state, confidence; look-ahead; WZDx feed',
    }));

    // READ ONLY, and this is the important grant in the stack. The query path must
    // not be able to write an event: a read API with write permission is one bug
    // away from mutating the record it was asked about, and no route needs it.
    this.eventTable.grantReadData(query);
    spatialDbSecret.grantRead(query);

    const apiAccessLogs = new logs.LogGroup(this, 'QueryApiAccessLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    /**
     * IAM authorization by default - see IngestStackProps.publicQueryApi.
     *
     * SigV4 means there is no API key to issue, store, or rotate, and an
     * integrator's access can be revoked by changing a policy rather than by
     * re-keying every consumer.
     */
    const publicQueryApi = props.publicQueryApi === true;

    this.queryApi = new apigwv2.HttpApi(this, 'QueryApi', {
      apiName: `${this.stackName}-query`,
      description: 'Corridor Event Hub query API - events, provenance, look-ahead',
      createDefaultStage: false,
      ...(publicQueryApi ? {} : { defaultAuthorizer: new apigwv2auth.HttpIamAuthorizer() }),
    });

    const integration = new apigwv2int.HttpLambdaIntegration('QueryIntegration', query);

    /**
     * Routes are declared one by one rather than as a single `/{proxy+}`.
     *
     * More lines, and worth them: the API's surface is then visible in the
     * template and in `cdk diff`, an unknown path is rejected by API Gateway
     * instead of reaching Python to be 404'd, and each route can carry its own
     * authorizer later without restructuring. A proxy route also hides the API
     * from anything that reads the template to find out what exists.
     */
    for (const route of [
      '/health',
      '/events',
      '/events/{eventId}',
      '/events/{eventId}/history',
      '/ahead',
      '/review',
      // The WZDx feed's stable URL.
      '/wzdx',
    ]) {
      this.queryApi.addRoutes({
        path: route,
        methods: [apigwv2.HttpMethod.GET],
        integration,
      });
    }

    new apigwv2.HttpStage(this, 'QueryApiStage', {
      httpApi: this.queryApi,
      autoDeploy: true,
      // An access log is how "who asked for what, and did it work" survives
      // the request. Also the only place a 4xx storm from one consumer is visible.
      accessLogSettings: {
        destination: new apigwv2.LogGroupLogDestination(apiAccessLogs),
        format: apigateway.AccessLogFormat.custom(
          JSON.stringify({
            requestId: apigateway.AccessLogField.contextRequestId(),
            ip: apigateway.AccessLogField.contextIdentitySourceIp(),
            requestTime: apigateway.AccessLogField.contextRequestTime(),
            httpMethod: apigateway.AccessLogField.contextHttpMethod(),
            path: apigateway.AccessLogField.contextPath(),
            status: apigateway.AccessLogField.contextStatus(),
            latencyMs: apigateway.AccessLogField.contextResponseLatency(),
            userAgent: apigateway.AccessLogField.contextIdentityUserAgent(),
          }),
        ),
      },
      // A cap, not a capacity plan. It stops one misbehaving consumer - or a
      // retry loop in an integrator's client - from turning into a bill and from
      // starving every other caller.
      throttle: { rateLimit: 50, burstLimit: 100 },
      detailedMetricsEnabled: true,
      description: 'Default stage for the query API',
    });

    // -----------------------------------------------------------------------
    // cdk-nag — what this stack does not comply with, and why
    // -----------------------------------------------------------------------
    //
    // Read this section as the security posture of the ingest path. Every entry is
    // a finding from the AwsSolutions pack that is NOT being fixed, with the reason
    // it is not; a finding that can be fixed is fixed above instead. cdk-nag is
    // applied in bin/corridor-event-hub.ts and runs on every synth, so nothing new arrives
    // here silently.

    /**
     * L1 - "not the latest runtime". The runtime is PINNED, not stale.
     *
     * The deployment bundle is built for one interpreter (PYTHON_VERSION in
     * scripts/build-lambda.sh) and scripts/check-python-runtime.sh asserts that
     * every template agrees with it. Following the newest runtime here without
     * rebuilding the bundle produces a function that deploys and then fails to
     * import - the exact synth-passes/deploy-fails shape this implementation keeps
     * getting bitten by. Upgrading is a bundle change; the template follows.
     */
    const runtimeIsPinned = {
      id: 'AwsSolutions-L1',
      reason:
        'The runtime is pinned to the interpreter the bundle was built for ' +
        '(scripts/build-lambda.sh), and scripts/check-python-runtime.sh asserts the two ' +
        'agree. A newer runtime without a rebuilt bundle fails at import, not at synth.',
    };

    /**
     * IAM4 - CDK attaches these two AWS managed policies to every execution role it
     * generates, and both are required rather than convenient:
     *
     *   AWSLambdaBasicExecutionRole      CreateLogStream / PutLogEvents
     *   AWSLambdaVPCAccessExecutionRole  the ENI calls Lambda makes to run a
     *                                    function in a VPC (ADR 0001)
     *
     * The ENI actions have no resource-level scoping, so a customer-managed
     * replacement would be a byte-for-byte copy of the AWS one plus three more
     * roles to keep in sync. That is more surface to get wrong, not less.
     */
    const cdkManagedLambdaPolicies = {
      id: 'AwsSolutions-IAM4',
      appliesTo: [
        'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
        'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
      ],
      reason:
        'Attached by CDK to the generated execution role. Basic execution grants only log ' +
        'stream writes; VPC access grants the ENI calls a Lambda in a VPC requires (ADR ' +
        '0001) and has no resource-level scoping, so a managed copy would be identical.',
    };

    /**
     * IAM5 Resource::* from X-Ray. `tracing: ACTIVE` grants
     * `xray:PutTraceSegments` and `PutTelemetryRecords`, neither of which accepts a
     * resource - the daemon posts to the service, not to an object. Not a data
     * permission: it cannot read a payload, a table or a queue.
     */
    const xrayHasNoResource = {
      id: 'AwsSolutions-IAM5',
      appliesTo: ['Resource::*'],
      reason:
        'xray:PutTraceSegments and xray:PutTelemetryRecords take no resource ARN - the ' +
        'wildcard is the API, not a broadened scope. Comes from tracing: ACTIVE and grants ' +
        'no access to project data.',
    };

    /**
     * The raw zone's own findings.
     *
     * S1 (server access logs) is the one worth arguing about rather than waving
     * through. Turning it on means a SECOND bucket, which then needs its own S1
     * exemption because a log bucket cannot log to itself, and which accumulates a
     * request record for a zone that is never deleted. The access history
     * that matters for this bucket - who read or attempted to delete an immutable
     * payload - is a CloudTrail S3 data event, and `RawZoneTrail` above now RECORDS
     * ONE. Read that sentence as the load-bearing change it is: until then this
     * suppression asserted the better record as a fact while it was really a
     * per-account setting nobody had made, and the prototype account had neither
     * kind of access record over the raw zone. Versioning plus Object Lock
     * GOVERNANCE cover the deletion half, and only for the 30 days the retention
     * runs - the trail is what covers the rest of it, and the reads.
     *
     * THE CMK FINDING (prototype pack) IS A DELIBERATE TRADE, AND IT RUNS THE OTHER
     * WAY FROM MOST ENCRYPTION ARGUMENTS. A customer-managed key buys control over
     * key lifecycle and access, plus a kms:Decrypt trail per read. What it also buys
     * is a SECOND WAY TO LOSE THE RAW ZONE: this bucket is RETAIN plus Object Lock
     * precisely because its bytes cannot be regenerated, and a key
     * that is scheduled for deletion, has its policy tightened, or is left behind in
     * a region migration turns every archived payload into ciphertext nobody can
     * read. Object Lock cannot protect against that - it guards the object, not the
     * key. An unreadable archive and a deleted archive fail the same requirement.
     *
     * Against that, what the key would be protecting: public-agency feed payloads -
     * state 511 traffic, NWS forecasts, FHWA NBI. Published data, no PII, no
     * credentials (those are in Secrets Manager per ADR 0004). SSE-S3 already
     * encrypts at rest and `enforceSSL` already covers it in transit, so the CMK
     * would add key-management risk to protect data that is already public.
     *
     * WHEN TO REVISIT, concretely: the first non-public source in the raw zone. Every
     * source in the catalog today is a published agency feed; if imagery or anything
     * else with an identifiable subject starts landing here, this trade flips - and the
     * right answer then is a CMK with a key policy that denies deletion plus a
     * multi-region replica, not a bare CMK.
     */
    NagSuppressions.addResourceSuppressions(this.rawBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Server access logging would need a second bucket that needs its own exemption, ' +
          'for a zone that is never deleted. The record that matters here is CloudTrail S3 ' +
          'data events, and RawZoneTrail in this stack records them (read AND write, this ' +
          'bucket only) rather than assuming the account does.',
      },
      {
        id: 'Prototype Security Nag Pack-CMK for S3 buckets',
        reason:
          'SSE-S3 by choice, not by omission. This zone is RETAIN + Object Lock because its ' +
          'bytes cannot be regenerated, and a CMK adds a second way to lose ' +
          'them: a key deleted or re-policied makes the archive permanently unreadable, which ' +
          'Object Lock cannot prevent. The payloads are public agency feeds - no PII, no ' +
          'credentials (ADR 0004) - so a CMK would add key-management risk over already ' +
          'public data. Revisit the day a non-public source lands here, with a ' +
          'no-delete key policy and a replica key rather than a bare CMK.',
      },
    ]);

    /**
     * The trail's log bucket - the same two findings, and the place where the
     * regress has to stop. S1 on an AUDIT bucket asks for an access-log bucket for
     * the audit bucket, which would need its own exemption for the same reason, and
     * so would that one's. The record of who touched the trail log is a management
     * event, which the account trail already carries; that is the difference between
     * stopping here and stopping one bucket earlier.
     *
     * The CMK trade runs the same way as the raw zone's but for a different reason.
     * There the risk was losing irreplaceable bytes; here it is losing the ability
     * to READ the audit record during the incident you need it for, which is when a
     * key problem is least welcome. What the log actually contains is caller
     * identity and object keys over payloads that are themselves public agency
     * feeds - an ARN, not personal information. SSE-S3 plus enforceSSL, and
     * CloudTrail's own digest files (enableFileValidation) are what make tampering
     * detectable, which is the property an audit log needs more than confidentiality.
     * Revisit alongside the raw zone's CMK, on the same trigger: the first
     * non-public source landing in the zone this trail watches.
     */
    NagSuppressions.addResourceSuppressions(auditLogBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'This bucket IS the access record for the raw zone. Access-logging it would need a ' +
          'third bucket needing the same exemption, without end; touches on the trail log ' +
          'itself are management events, which the account trail already carries.',
      },
      {
        id: 'Prototype Security Nag Pack-CMK for S3 buckets',
        reason:
          'SSE-S3 by choice. A CMK here risks an audit log that cannot be read during the ' +
          'incident it exists for, and the content is caller identity plus object keys over ' +
          'already-public feed payloads. Integrity, not confidentiality, is what this log ' +
          'needs, and enableFileValidation gives it. Revisit with the raw zone CMK trade.',
      },
    ]);

    /**
     * The collector's role. Three wildcards, three different reasons - which is why
     * they are listed separately rather than suppressed as one rule.
     */
    NagSuppressions.addResourceSuppressions(
      collector,
      [
        runtimeIsPinned,
        cdkManagedLambdaPolicies,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason:
            'X-Ray only (tracing: ACTIVE). PutTraceSegments and GetSamplingRules have no ' +
            'resource-level ARN, so the action is the scope. This suppression used to cover ' +
            'geo-maps:GetTile too, on the stated grounds that it had no resource ARN either - ' +
            'which was wrong. That grant is now scoped to the provider ARN and ' +
            'no longer needs excusing here.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Action::s3:Abort*', `Resource::${arnRef(this.rawBucket)}/*`],
          reason:
            'grantPut on the raw zone. The action wildcard is CDK expanding multipart upload ' +
            '(AbortMultipartUpload); the resource wildcard is object-level within that one ' +
            'bucket - keys are source/date/uuid and cannot be enumerated in a policy.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${sourceSecretsArn}`],
          reason:
            'Scoped to the corridor-event-hub/ secret path, which is the point of the prefix: a new ' +
            'source feed brings its own key without a policy change (ADR 0004), and nothing ' +
            'outside that path is reachable. The account and region are pinned.',
        },
      ],
      true, // the findings land on the generated ServiceRole and its DefaultPolicy
    );

    /**
     * The normalizer's role. Read-only on the raw zone, read/write on the event
     * store it owns.
     */
    NagSuppressions.addResourceSuppressions(
      normalizer,
      [
        runtimeIsPinned,
        cdkManagedLambdaPolicies,
        xrayHasNoResource,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [
            'Action::s3:GetObject*',
            'Action::s3:GetBucket*',
            'Action::s3:List*',
            `Resource::${arnRef(this.rawBucket)}/*`,
          ],
          reason:
            'grantRead on the raw zone, expanded by CDK. Read-only against one bucket whose ' +
            'contents are immutable under Object Lock, and object-level because a replay ' +
            'may be asked for any archived key.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(this.eventTable)}/index/*`],
          reason:
            'Index-level wildcard on the event store this function owns. by-state-measure is ' +
            'the corridor query; a second index would otherwise be unreadable until ' +
            'someone remembered to widen the policy.',
        },
      ],
      true,
    );

    /**
     * SQS3 asks for a dead-letter queue. These ARE the dead-letter queues -
     * a DLQ behind a DLQ has nothing to catch and only moves the evidence one hop
     * further from the operator who has to read it.
     */
    NagSuppressions.addResourceSuppressions(
      [this.normalizerDlq, this.ruleDlq, this.resolverDlq, this.resolverRuleDlq],
      [
        {
          id: 'AwsSolutions-SQS3',
          reason:
            'This queue IS a dead-letter queue - see the block above on why there are two of ' +
            'them per async stage. Nothing consumes it on a schedule; `npm run dlq-peek` ' +
            'and the depth alarms in the observability stack are what watch it.',
        },
      ],
    );

    /**
     * The resolver. Its only wildcard is the event store's index: a Query against a
     * GSI is authorized on `<table>/index/*`, and the corridor range query
     * (and the resolver's own match lookup) is a GSI query by construction.
     */
    NagSuppressions.addResourceSuppressions(
      resolver,
      [
        runtimeIsPinned,
        cdkManagedLambdaPolicies,
        xrayHasNoResource,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(this.eventTable)}/index/*`],
          reason:
            'grantReadWriteData covers the table and its indexes. The by-state-measure GSI is ' +
            'how a measure-range query is answered at all - a numeric range query rather ' +
            'than a spatial one - and the wildcard is ' +
            "over that one table's indexes rather than over any table.",
        },
      ],
      true,
    );

    /**
     * The lifecycle tick. Same index wildcard as the resolver, for the same reason -
     * it reads the current event through the table, not the index, but
     * `grantReadWriteData` cannot express that distinction.
     */
    NagSuppressions.addResourceSuppressions(
      lifecycle,
      [
        runtimeIsPinned,
        cdkManagedLambdaPolicies,
        xrayHasNoResource,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(this.eventTable)}/index/*`],
          reason:
            'grantReadWriteData covers the table and its indexes. This function reads one ' +
            'event by id and writes one version; the index grant is unavoidable breadth from ' +
            'the same call, over one table.',
        },
      ],
      true,
    );

    /**
     * The state machine's role. Two wildcards, both structural:
     *
     *   <LifecycleFn.Arn>:*  a Lambda invoke grant covers the function's version and
     *                        alias ARNs, which is the trailing :*. Scoped to that
     *                        one function.
     *   Resource::*          X-Ray, from tracingEnabled - see xrayHasNoResource.
     */
    NagSuppressions.addResourceSuppressions(
      stateMachine,
      [
        xrayHasNoResource,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(lifecycle)}:*`],
          reason:
            'The machine invokes exactly one function - the lifecycle tick - and an invoke ' +
            'grant also covers its version and alias ARNs, which is the trailing :*. It can ' +
            'invoke nothing else in the account.',
        },
      ],
      true,
    );

    /**
     * The query function. Same index wildcard, and READ ONLY - it holds
     * dynamodb:Query and GetItem, never PutItem, so the wildcard cannot be used to
     * write anything.
     */
    NagSuppressions.addResourceSuppressions(
      query,
      [
        runtimeIsPinned,
        cdkManagedLambdaPolicies,
        xrayHasNoResource,
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(this.eventTable)}/index/*`],
          reason:
            'grantReadData covers the table and its indexes; the corridor range query is a GSI ' +
            'query. Read actions only - this role cannot write an event.',
        },
      ],
      true,
    );

    if (publicQueryApi) {
      /**
       * APIG4 - no authorizer, because `publicQueryApi` was set.
       *
       * ACKNOWLEDGED, NOT DISMISSED. This is an unauthenticated endpoint on the
       * public internet. What limits the damage: every route is GET, the function's
       * table grant is read-only, the stage is throttled at 50 rps, and the data is
       * public agency feeds rather than anything private. What it does NOT limit is
       * cost and scraping. Prefer `npm run serve` for a demo; if this is deployed
       * open, it should not stay open after one.
       */
      NagSuppressions.addResourceSuppressions(
        this.queryApi,
        [
          {
            id: 'AwsSolutions-APIG4',
            reason:
              'publicQueryApi was set explicitly, which is the documented way to serve this ' +
              'API unauthenticated for a demo. Default is IAM (SigV4) authorization. ' +
              'Mitigations: GET-only routes, read-only table grant, 50 rps stage throttle, ' +
              'public-agency data only. See IngestStackProps.publicQueryApi.',
          },
        ],
        true,
      );
    }

    /**
     * The scheduler's invoke grant. `grantInvoke` covers the function's version and
     * alias ARNs as well as the bare one, which is the trailing `:*`.
     */
    NagSuppressions.addResourceSuppressions(
      schedulerRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: [`Resource::${arnRef(collector)}:*`],
          reason:
            'grantInvoke on the collector also covers its version and alias ARNs, which is ' +
            'the trailing :*. Scoped to that one function - the schedules can invoke nothing ' +
            'else in the account.',
        },
      ],
      true,
    );

    new CfnOutput(this, 'RawBucketName', { value: this.rawBucket.bucketName });
    new CfnOutput(this, 'RawZoneAuditLogBucketName', {
      value: auditLogBucket.bucketName,
      description:
        'CloudTrail S3 data events for the raw zone - who read or deleted a payload. ' +
        'Reads land here too, not only writes.',
    });
    new CfnOutput(this, 'RawZoneTrailArn', {
      value: rawZoneTrail.trailArn,
      description:
        'Data-event trail scoped to the raw zone only. Management events are the account ' +
        "trail's job and are deliberately off here.",
    });
    new CfnOutput(this, 'EventBusName', { value: this.eventBus.eventBusName });
    new CfnOutput(this, 'EventTableName', { value: this.eventTable.tableName });
    new CfnOutput(this, 'NormalizerDlqUrl', {
      value: this.normalizerDlq.queueUrl,
      description: 'Payloads whose handler raised through all retries. Should be EMPTY.',
    });
    new CfnOutput(this, 'RuleDlqUrl', {
      value: this.ruleDlq.queueUrl,
      description: 'Events EventBridge could not deliver to the normalizer. Should be EMPTY.',
    });
    new CfnOutput(this, 'ResolverDlqUrl', {
      value: this.resolverDlq.queueUrl,
      description: 'Candidates whose resolution raised through all retries. Should be EMPTY.',
    });
    new CfnOutput(this, 'ResolverRuleDlqUrl', {
      value: this.resolverRuleDlq.queueUrl,
      description: 'Candidates EventBridge could not deliver to the resolver. Should be EMPTY.',
    });
    new CfnOutput(this, 'LifecycleStateMachineArn', {
      value: stateMachine.stateMachineArn,
      description:
        'TTL timers, one execution per event. A RUNNING execution per live ' +
        'event is normal; a FAILED one means that event will never expire.',
    });
    new CfnOutput(this, 'QueryApiUrl', {
      value: this.queryApi.apiEndpoint,
      description:
        'Query API base URL. IAM auth unless publicQueryApi was set: sign requests ' +
        'with SigV4, or use `npm run serve` locally.',
    });
    this.activeSourceIds = active.map((s) => s.sourceId);
    this.activeSourceCadenceSeconds = Object.fromEntries(
      active.map((s) => [s.sourceId, effectiveCadenceSeconds(s)]),
    );
    this.activeSourceMappingIssueCeiling = Object.fromEntries(
      active
        .filter((s) => s.mappingIssueCeilingPerRun !== undefined)
        .map((s) => [s.sourceId, s.mappingIssueCeilingPerRun as number]),
    );

    new CfnOutput(this, 'ScheduledSources', {
      value: active.map((s) => s.sourceId).join(', ') || 'NONE',
      description: 'Sources with status=verified_live in config/sources.json',
    });
  }
}
