/**
 * Spatial stack - Aurora Serverless v2 PostgreSQL + PostGIS.
 *
 * This is the recommended conflation implementation from ADR 0002. It sits
 * behind the `Conflator` interface in corridor_event_hub/core/lrs.py, so adopting it is a
 * constructor change rather than a rewrite - and backing it out again is too.
 *
 * WHY POSTGIS AND NOT JUST THE LAMBDA GEOMETRY LAYER (ADR 0002 in brief):
 *   1. Adoption legibility - state DOT GIS shops already work in PostGIS and
 *      Esri. The deliverable is a reference architecture, so expressing
 *      linear referencing in the adopters' idiom matters more than saving a
 *      database.
 *   2. SQL for researchers - this is a university project whose output includes
 *      analysis, not only a service.
 *   3. Deriving the state-line milepost offsets is materially easier
 *      interactively in PostGIS than in code.
 *   4. The LRS becomes inspectable DATA rather than logic inside a Lambda.
 *
 * WHAT LIVES HERE (per ADR 0002's scope analysis - the spatial surface is
 * narrower than it first appears):
 *   - corridor centerline + per-state milepost offset tables
 *   - NBI bridge structures (class 7) - 409 on I-40 in OK alone
 *   - TMC segments and other facility inventories, when those arrive
 *   - ST_LineLocatePoint conflation, NWS polygon x corridor, RWIS snapping
 *
 * WHAT DOES NOT: event versions, audit trail, lifecycle state, confidence.
 * Those are DynamoDB, keyed by event_id. Once conflation yields
 * `route + measure range`, everything downstream is arithmetic.
 *
 * COST. Serverless v2 bills by ACU-hour and scales to a floor, not to zero -
 * `minCapacity: 0` is supported on recent engine versions and pauses after
 * inactivity, but a resumed cluster takes seconds to accept the first
 * connection. For a prototype polling every 60-300s the cluster effectively
 * never idles long enough to pause, so budget for the floor being always-on.
 * At 0.5 ACU that is roughly $43/mo on top of the ~$60/mo network (ADR 0001).
 * Verify current regional pricing before quoting to a DOT.
 */

import {
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  CfnOutput,
  SecretValue,
  aws_ec2 as ec2,
  aws_lambda as lambda,
  aws_rds as rds,
  aws_logs as logs,
  aws_secretsmanager as secretsmanager,
} from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import type { NetworkStack } from './network-stack';
import { lambdaBundleCode } from './lambda-bundle';
import { solutionUserAgentEnv } from './solution';

export interface SpatialStackProps extends StackProps {
  readonly network: NetworkStack;
  /**
   * Serverless v2 floor in ACUs. 0 allows auto-pause but adds a cold resume of
   * several seconds on the first connection; 0.5 is the lowest always-warm
   * setting. Default 0.5 because the ingest cadence never idles long enough for
   * pausing to pay off.
   */
  readonly minCapacityAcu?: number;
  readonly maxCapacityAcu?: number;
  /**
   * Deletion protection. FALSE by default because this is a prototype someone
   * will want to tear down - unlike the S3 raw zone, everything in this database
   * is DERIVED and can be rebuilt from raw payloads. Set true for a
   * production DOT deployment.
   */
  readonly deletionProtection?: boolean;
  /**
   * RDS Data API (`enableDataApi`). Turns on an HTTPS endpoint for the cluster,
   * which is what the console Query Editor requires.
   *
   * Default TRUE here, for a reason worth stating: Aurora sits in isolated
   * subnets with no public endpoint (ADR 0001), so without the Data API there is
   * no way to run an ad-hoc QUERY from a laptop at all. That matters for a project
   * whose output includes researcher SQL (ADR 0002 reason 2).
   *
   * It is no longer needed for SCHEMA CHANGES - the migration function below runs
   * inside the VPC and does not use it, which is what lets `enableDataApi: false`
   * be a real option rather than one that removes the only way to migrate.
   *
   * The tradeoff: it exposes an IAM-authenticated HTTPS endpoint. That is a
   * genuinely different access path from "inside the VPC only", so it is IAM
   * that protects the database rather than the network. Acceptable for a
   * prototype in a research account; a production DOT deployment should decide
   * deliberately and probably set this false - and now can.
   */
  readonly enableDataApi?: boolean;
}

export class SpatialStack extends Stack {
  public readonly cluster: rds.DatabaseCluster;
  public readonly credentialsSecret: secretsmanager.ISecret;
  public readonly databaseName = 'corridoreventhub';
  /**
   * Applies sql/*.sql. Exposed so a later stack (or a pipeline) can invoke it
   * without rediscovering the function by name.
   */
  public readonly migrationFunction: lambda.Function;
  public readonly migrationLogGroup: logs.LogGroup;

  constructor(scope: Construct, id: string, props: SpatialStackProps) {
    super(scope, id, props);

    const { network } = props;
    const minCapacity = props.minCapacityAcu ?? 0.5;
    const maxCapacity = props.maxCapacityAcu ?? 4;

    /**
     * Credentials in Secrets Manager with rotation available, never in code or
     * environment variables - the same rule as source API keys (ADR 0004).
     * `manageMasterUserPassword` lets RDS own the secret and its rotation.
     */
    const credentials = rds.Credentials.fromGeneratedSecret('corridoreventhub_admin', {
      secretName: 'corridor-event-hub/spatial-db-credentials',
    });

    /**
     * ENGINE VERSION: PostgreSQL 18.4, pinned via `of()` rather than the CDK enum.
     *
     * CDK 2.173's `AuroraPostgresEngineVersion` enum stops at VER_16_6 - and AWS
     * has since RETIRED 16.6 entirely. The enum cannot express 18.x at all, and
     * a retired enum value synths cleanly then fails at CREATE with an invalid
     * engine version: the same synth-passes/deploy-fails shape as the em-dash
     * and metric-filter bugs. `of(fullVersion, majorVersion)` sidesteps it.
     *
     * Verified available in us-west-2 on 2026-08-08:
     *   - 18.4, status `available`, family `aurora-postgresql18`
     *   - `db.serverless` orderable, so Serverless v2 is supported
     *   - Serverless v2 capacity range 0.0 - 256 ACU on this version
     *
     * scripts/check-engine-versions.sh re-verifies this against the live API on
     * every `npm run check`.
     *
     * PostGIS ships with Aurora PostgreSQL and is enabled per-database with
     * CREATE EXTENSION (see sql/001-init.sql), not at the cluster level. Confirm
     * which PostGIS version 18.x carries after the first bootstrap:
     *   SELECT postgis_full_version();
     */
    const engine = rds.DatabaseClusterEngine.auroraPostgres({
      version: rds.AuroraPostgresEngineVersion.of('18.4', '18'),
    });

    /**
     * PARAMETER SCOPE MATTERS ON PG18.
     *
     * `log_min_duration_statement` and `shared_preload_libraries` are
     * INSTANCE-level parameters on the `aurora-postgresql18` family, not
     * cluster-level. Verified against the API:
     *
     *   describe-engine-default-cluster-parameters -> neither appears
     *   describe-engine-default-parameters         -> both appear
     *
     * Setting them on a DBClusterParameterGroup (as an earlier revision did)
     * puts them where the engine will not read them. So they go in an instance
     * parameter group attached to the writer instead.
     */
    /**
     * TLS IS ENFORCED AT THE ENGINE, not only requested by the client.
     *
     * `rds.force_ssl = 1` makes Postgres REFUSE a non-TLS connection. The client
     * side already passed an explicit SSL context so it could never silently fall
     * back to plaintext (core/dbconn.py), but that is a promise one client keeps
     * about itself. This is the half that holds for every client - a psql from a
     * bastion, a future reader, an adopter's own tool - and it is the half a DOT
     * security review asks for, because it is inspectable without reading Python.
     *
     * CLUSTER-LEVEL, deliberately: `rds.force_ssl` appears in
     * `describe-engine-default-cluster-parameters` for aurora-postgresql18 and NOT
     * in the instance defaults, which is the opposite of the two parameters below.
     * Put it in the instance group and it is silently never applied.
     * scripts/check-engine-versions.sh re-verifies both scopes against the live API.
     *
     * SET EXPLICITLY EVEN THOUGH THE ENGINE DEFAULT IS ALREADY 1 (verified against
     * the API on aurora-postgresql18). A default is a fact about today's engine
     * family; this is a statement in the template that survives a major-version
     * upgrade changing its mind, and it puts the control somewhere `cdk diff` can
     * show someone turning it off.
     */
    const clusterParams = new rds.ParameterGroup(this, 'ClusterParams', {
      engine,
      description: 'Corridor Event Hub spatial - TLS required for every connection',
      parameters: {
        'rds.force_ssl': '1',
      },
    });

    const instanceParams = new rds.ParameterGroup(this, 'InstanceParams', {
      engine,
      description: 'Corridor Event Hub spatial - slow query logging and preloaded libraries',
      parameters: {
        // Log anything slower than 1s. Conflation should be milliseconds; a
        // slow query here means a missing index on the corridor geometry.
        log_min_duration_statement: '1000',
        // pg_stat_statements is how you find the slow conflation query later.
        // PostGIS itself does NOT need preloading - it loads on first use via
        // CREATE EXTENSION.
        shared_preload_libraries: 'pg_stat_statements',
      },
    });

    this.cluster = new rds.DatabaseCluster(this, 'Cluster', {
      engine,
      // Serverless v2: one writer, no reader. A reader doubles cost and this
      // workload is write-light reference data plus point lookups.
      writer: rds.ClusterInstance.serverlessV2('writer', {
        autoMinorVersionUpgrade: true,
        // Set here as well as at cluster level: the cluster setting propagates
        // anyway, but stating it silences a CDK warning about the mismatch.
        enablePerformanceInsights: true,
        parameterGroup: instanceParams,
      }),
      parameterGroup: clusterParams,
      serverlessV2MinCapacity: minCapacity,
      serverlessV2MaxCapacity: maxCapacity,

      vpc: network.vpc,
      // PRIVATE_ISOLATED: no internet route at all. The database has no reason
      // to reach out, and nothing outside the VPC has a reason to reach it.
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [network.databaseSecurityGroup],

      credentials,
      defaultDatabaseName: this.databaseName,

      storageEncrypted: true,
      backup: {
        retention: Duration.days(7),
        preferredWindow: '08:00-09:00', // UTC, off-peak for a US corridor
      },
      cloudwatchLogsExports: ['postgresql'],
      /**
       * `cloudwatchLogsRetention` is deliberately NOT set. Like `logRetention`
       * on a Lambda, it makes CDK inject a singleton custom-resource function
       * that runs OUTSIDE the VPC - silently violating the all-Lambdas-in-VPC
       * requirement (ADR 0001). Caught by scripts/check-vpc.sh.
       *
       * Consequence: the exported postgresql log group retains forever by
       * default. Set retention on the group directly, out of band, or accept it
       * for a prototype. Aurora creates the group itself, so CDK cannot own it
       * without the custom resource.
       */

      /**
       * Data API. Required by the console Query Editor, and the only way to
       * reach an isolated-subnet cluster without a bastion. See props for the
       * security tradeoff.
       */
      enableDataApi: props.enableDataApi ?? true,

      // Derived data - rebuildable from the S3 raw zone. See props.
      deletionProtection: props.deletionProtection ?? false,
      removalPolicy: RemovalPolicy.SNAPSHOT,

      // Serverless v2 supports Performance Insights; useful while tuning the
      // conflation queries on day one.
      enablePerformanceInsights: true,
      performanceInsightRetention: rds.PerformanceInsightRetention.DEFAULT,
    });

    this.credentialsSecret = this.cluster.secret!;

    // -----------------------------------------------------------------------
    // Migration runner - the schema's only deployed write path
    // -----------------------------------------------------------------------

    /**
     * THE SCHEMA-CHANGE PATH. Aurora sits in PRIVATE_ISOLATED subnets with no
     * public endpoint, so applying a schema change needs SOMETHING inside the VPC.
     * The alternatives were a bastion host (a long-lived access path to justify in
     * a security review, and a thing to remember to tear down), SSM port
     * forwarding (needs an EC2 instance the network stack does not create), or the
     * Data API - which works today and stops working the moment a production
     * deployment sets `enableDataApi: false`, as the props above recommend it
     * should.
     *
     * A Lambda is the cheapest of the four to own: no standing access, nothing to
     * patch, and it runs from CI as readily as from a laptop. It also needs
     * EXACTLY the placement and secret access that PostgisConflator will need, so
     * building it now answers the connectivity question once.
     *
     * WHAT IT IS NOT: a deploy-time custom resource. See the handler's docstring -
     * the Provider framework would inject functions outside the VPC (ADR 0001),
     * and a schema change as a side effect of deploying code is the wrong default
     * for a system a DOT operates. `npm run db-migrate` is a decision someone makes.
     */
    this.migrationLogGroup = new logs.LogGroup(this, 'MigrationLogs', {
      // Longer than the two weeks the pipeline functions keep: this log IS the
      // audit trail for schema changes, and schema_migration.applied_by points at
      // it by request id. A month is the least that is useful.
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.migrationFunction = new lambda.Function(this, 'MigrateFn', {
      // Must match the ingest stack and scripts/build-lambda.sh; asserted by
      // scripts/check-python-runtime.sh, which reads every template.
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      // `sql` is named as well as the package: the runner reads the migrations from
      // the bundle at invoke time, so a bundle built before sql/ was included would
      // deploy and then report an empty migration directory.
      code: lambdaBundleCode(['corridor_event_hub', 'sql', 'certs']),
      handler: 'corridor_event_hub.handlers.db_migrate.handler',
      logGroup: this.migrationLogGroup,
      /**
       * Five minutes covers the current schema many times over - the first
       * bootstrap ran in seconds. It is not a budget for arbitrary migrations: a
       * backfill or an index build over millions of rows does not belong in a
       * Lambda at all, and the handler sets `statement_timeout` from the remaining
       * time so such a statement is CANCELLED by Postgres with a clear error
       * rather than killed halfway by the platform.
       */
      timeout: Duration.minutes(5),
      // DDL is not memory-hungry; this is mostly for a faster cold start, which is
      // the dominant cost of a VPC Lambda invoked by hand.
      memorySize: 512,
      tracing: lambda.Tracing.ACTIVE,
      /**
       * ONE AT A TIME, enforced twice. This caps concurrency at the platform, and
       * the handler also takes a Postgres advisory lock - because reserved
       * concurrency does not protect against the case that actually matters, which
       * is an operator running `npm run db-migrate` while a pipeline does the same
       * from another account's credentials against the same cluster.
       */
      reservedConcurrentExecutions: 1,
      /**
       * No retries. This function is invoked synchronously, where retryAttempts
       * does not apply - it is set for the async path, so that an accidental
       * `--invocation-type Event` cannot re-enter a migration that is already
       * running. The tracking table makes a repeat harmless; not needing to rely
       * on that is better.
       */
      retryAttempts: 0,
      // ALL Lambdas in the VPC (ADR 0001). Here it is not a policy choice: the
      // cluster is unreachable from anywhere else.
      ...network.lambdaVpcConfig,
      environment: {
        // The AWS Solutions user-agent string, read by this function's boto3 client
        // (corridor_event_hub/core/awsclients.py). See lib/solution.ts.
        ...solutionUserAgentEnv(this),
        // Host and port from the CDK-wired endpoint rather than the secret body.
        // A replaced cluster leaves a stale host inside the secret, and connecting
        // to the wrong instance is far harder to notice than failing to connect.
        SPATIAL_DB_HOST: this.cluster.clusterEndpoint.hostname,
        SPATIAL_DB_PORT: String(this.cluster.clusterEndpoint.port),
        SPATIAL_DB_NAME: this.databaseName,
        // The ARN only. The credential itself is fetched at runtime and never
        // reaches an environment variable (ADR 0004).
        SPATIAL_DB_SECRET_ARN: this.credentialsSecret.secretArn,
      },
      description: 'Apply pending sql/ migrations to the spatial database. Manual or CI.',
    });

    // Read, not write: this function has no business rotating the credential it
    // uses. Scoped to the one secret rather than the corridor-event-hub/* prefix the
    // collector needs, because it only ever reads this one.
    this.credentialsSecret.grantRead(this.migrationFunction);

    /**
     * NOTE: no `grantDataApiAccess` and no rds-data permissions. The runner speaks
     * the Postgres wire protocol over 5432 from inside the VPC, which is what makes
     * it independent of `enableDataApi`. The path is already open - the network
     * stack's database security group accepts 5432 from the Lambda security group,
     * which this function is in.
     */

    // -----------------------------------------------------------------------
    // cdk-nag - what this stack does not comply with, and why
    // -----------------------------------------------------------------------
    //
    // The database findings are the ones a DOT security review will read first, so
    // each says what the alternative would cost and what a production deployment
    // should do differently. cdk-nag is applied in bin/corridor-event-hub.ts and runs on every
    // synth. Two of these are conditional on the props above: turn the prop on and
    // the exemption disappears with it.

    /**
     * RDS6 - IAM database authentication.
     *
     * Off because both clients authenticate with the RDS-managed secret (ADR 0004):
     * the migration function fetches it at invoke time, and PostgisConflator will do
     * the same. Enabling IAM auth without changing the clients adds a second,
     * unused way in - and two auth paths where one is untested is how a credential
     * rotation quietly stops mattering. Worth revisiting when the conflator lands,
     * as a change to both sides at once.
     */
    NagSuppressions.addResourceSuppressions(this.cluster, [
      {
        id: 'AwsSolutions-RDS6',
        reason:
          'Both clients authenticate with the RDS-managed secret (ADR 0004), fetched at ' +
          'invoke time and never held in an environment variable. Enabling IAM auth without ' +
          'moving the clients to it would add an unused second auth path, not remove one.',
      },
    ]);

    /**
     * SMG4 - no rotation schedule on the cluster credential.
     *
     * RDS owns the secret and `cluster.addRotationSingleUser()` would schedule
     * rotation, but it also deploys the AWS-provided rotation function into this VPC:
     * another function to review, to keep working, and to explain when it fails at
     * 03:00. For a prototype whose credential is read at invoke time by two callers
     * and appears in no environment variable, that is not where the risk is.
     *
     * A production DOT deployment SHOULD schedule rotation, and should test it
     * against `npm run db-migrate` - the migration runner takes an advisory lock and
     * holds a connection, which is exactly the kind of thing rotation surprises.
     */
    NagSuppressions.addResourceSuppressions(
      this.cluster,
      [
        {
          id: 'AwsSolutions-SMG4',
          reason:
            'RDS-managed credential with no rotation schedule: addRotationSingleUser deploys ' +
            'another function into the VPC, which a prototype does not need. The credential ' +
            'is never in code or an env var - only its ARN is (ADR 0004). Production should ' +
            'schedule rotation and test it against npm run db-migrate.',
        },
      ],
      true, // the finding lands on the cluster's generated Secret
    );

    /**
     * RDS10 - deletion protection, suppressed only while it is off.
     *
     * Everything in this database is DERIVED: the centerline comes from the states'
     * LRS services, the structures from FHWA, the conflation results from raw
     * payloads still under Object Lock. `npm run corridor && npm run db-migrate`
     * rebuilds it. RemovalPolicy.SNAPSHOT still takes a final snapshot on the way
     * out. And a prototype nobody can tear down is its own kind of cost problem
     *.
     *
     * `-c dbDeletionProtection=true` both enables it and clears this finding.
     */
    if (!(props.deletionProtection ?? false)) {
      NagSuppressions.addResourceSuppressions(this.cluster, [
        {
          id: 'AwsSolutions-RDS10',
          reason:
            'Off by default because every table here is derived and rebuildable from the raw ' +
            'zone and the state LRS services, and RemovalPolicy.SNAPSHOT still takes ' +
            'a final snapshot. Set -c dbDeletionProtection=true for production, which also ' +
            'clears this finding.',
        },
      ]);
    }

    /**
     * The migration runner's role - the same three Lambda findings as the ingest
     * functions, for the same reasons. Stated here rather than shared, because this
     * stack deploys independently and a reader should not have to open another file
     * to find out why its only function is exempt.
     */
    NagSuppressions.addResourceSuppressions(
      this.migrationFunction,
      [
        {
          id: 'AwsSolutions-L1',
          reason:
            'The runtime is pinned to the interpreter the bundle was built for ' +
            '(scripts/build-lambda.sh) and asserted by scripts/check-python-runtime.sh. A ' +
            'newer runtime without a rebuilt bundle fails at import, not at synth.',
        },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: [
            'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
            'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
          ],
          reason:
            'Attached by CDK to the generated execution role. Basic execution grants only log ' +
            'stream writes - and this log group IS the audit trail for schema changes. VPC ' +
            'access is mandatory here: the cluster is unreachable from anywhere else.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason:
            'xray:PutTraceSegments and xray:PutTelemetryRecords take no resource ARN - the ' +
            'wildcard is the API, not a broadened scope. This role holds no other wildcard: ' +
            'the credential grant is scoped to the one secret.',
        },
      ],
      true,
    );

    // -----------------------------------------------------------------------
    // Outputs
    // -----------------------------------------------------------------------

    new CfnOutput(this, 'ClusterEndpoint', {
      value: this.cluster.clusterEndpoint.hostname,
      description: 'Writer endpoint. Reachable only from inside the VPC.',
    });
    new CfnOutput(this, 'ClusterPort', {
      value: String(this.cluster.clusterEndpoint.port),
    });
    new CfnOutput(this, 'DatabaseName', { value: this.databaseName });
    new CfnOutput(this, 'CredentialsSecretArn', {
      value: this.credentialsSecret.secretArn,
      description: 'RDS-managed credentials. Never read these into code or env vars.',
    });
    new CfnOutput(this, 'CostNote', {
      value:
        `Serverless v2 ${minCapacity}-${maxCapacity} ACU. At the ${minCapacity} ACU floor ` +
        'roughly $43/mo always-on, on top of the ~$60/mo network. The ingest cadence ' +
        'never idles long enough for auto-pause to help. Verify regional pricing.',
    });
    new CfnOutput(this, 'DataApiEnabled', {
      value: String(props.enableDataApi ?? true),
      description:
        'Data API / HTTPS endpoint. Required for the console Query Editor and for ' +
        'reaching an isolated-subnet cluster without a bastion.',
    });
    new CfnOutput(this, 'QueryEditorHint', {
      value:
        'RDS console > Query editor. Database: corridoreventhub, user: corridoreventhub_admin, ' +
        'secret: corridor-event-hub/spatial-db-credentials. Connecting to `postgres` instead ' +
        'of `corridoreventhub` shows an empty schema.',
    });
    new CfnOutput(this, 'MigrationFunctionName', {
      value: this.migrationFunction.functionName,
      description:
        'Applies sql/*.sql inside the VPC, independent of the Data API. ' +
        'Run: npm run db-migrate-plan (reports), then npm run db-migrate (applies).',
    });
    new CfnOutput(this, 'NextStep', {
      value:
        'A NEW cluster has no schema. Apply it: npm run db-migrate-plan to see what would ' +
        'run, then npm run db-migrate (postgis extension, corridor tables, structures ' +
        'table, LRS functions). Every application is recorded in schema_migration.',
    });
  }
}
