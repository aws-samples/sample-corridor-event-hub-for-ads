/**
 * Observability stack - dashboard, alarms, and metric filters.
 *
 * What has to be observable: per-source freshness, mapping-failure rate, dedup
 * precision/recall, transition latency, and confidence distribution. Metric filters now cover the collector, the normalizer
 * AND the resolver, so freshness, mapping-failure rate, ingest-to-queryable
 * latency and the resolver's decision mix are all instrumented.
 *
 * STILL NOT INSTRUMENTED, and worth knowing before reading a green dashboard:
 *   - The LIFECYCLE machine has an alarm on failed executions but no metrics and
 *     no widget - its log group is not passed to this stack, so nothing here
 *     graphs a state transition, so "transition latency" is unmeasured.
 *   - CONFIDENCE DISTRIBUTION. The score is in the normalizer's log line and
 *     nowhere else.
 *   - DEDUP PRECISION/RECALL. `EventsResolved` by action shows the decision mix,
 *     which is not the same thing: it counts merges, it does not tell you which
 *     ones were right.
 *
 * DESIGN NOTE: the metrics come from METRIC FILTERS over the structured JSON
 * the handlers already log, not from explicit PutMetricData calls. That keeps
 * instrumentation out of the handler code, means the numbers cannot drift from
 * the logs, and costs nothing extra per invocation. The tradeoff is a ~1 minute
 * delay and dependence on log format - so the handlers' `msg` field is now a
 * contract, not a convenience.
 */

import {
  ArnFormat,
  Duration,
  Stack,
  StackProps,
  CfnOutput,
  aws_cloudwatch as cw,
  aws_cloudwatch_actions as cwa,
  aws_iam as iam,
  aws_logs as logs,
  aws_sns as sns,
  aws_sns_subscriptions as subs,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface ObservabilityStackProps extends StackProps {
  /** Log group names from the ingest stack. */
  readonly collectorLogGroupName: string;
  readonly normalizerLogGroupName: string;
  readonly resolverLogGroupName: string;
  readonly collectorFunctionName: string;
  readonly normalizerFunctionName: string;
  /**
   * Dead-letter queue names from the ingest stack. A DLQ nobody watches is just a
   * slower silent failure, so these get alarms rather than only outputs.
   */
  readonly normalizerDlqName: string;
  readonly ruleDlqName: string;
  readonly resolverDlqName: string;
  readonly resolverRuleDlqName: string;
  /**
   * The lifecycle TTL machine. A FAILED execution is a timer chain that
   * died, which means one event will never expire again - and nothing else in the
   * system notices, because the event still looks perfectly healthy.
   */
  readonly lifecycleStateMachineName: string;
  /** Optional email for alarm notifications. */
  readonly alarmEmail?: string;
  /** Sources expected to report. Drives per-source freshness alarms. */
  readonly sourceIds: string[];
  /**
   * Effective poll interval per source, from the ingest stack.
   *
   * WHY THE OBSERVABILITY STACK NEEDS TO KNOW THE CADENCE: both the dashboard's
   * bucket size and each staleness window are only meaningful relative to how often
   * a source is polled. Hardcoding either one means the day someone changes a
   * cadence, the monitoring quietly starts lying - see the two derivations below.
   */
  readonly sourceCadenceSeconds: Record<string, number>;
  /**
   * Highest mapping-issue count a HEALTHY run of each source produces, from the
   * catalog. Absent for a source nobody has baselined yet, which falls back to
   * `DEFAULT_MAPPING_ISSUE_CEILING` below.
   */
  readonly sourceMappingIssueCeiling?: Record<string, number>;
}

const NAMESPACE = 'CorridorEventHub';

/**
 * How far above a source's healthy per-run ceiling counts as format drift.
 *
 * A named constant rather than an inline 2 because it is a judgement, not a fact: it
 * trades how fast a real drift is caught against how often a noisy feed cries wolf.
 * Argue about it on a whiteboard, then change it here.
 */
const MAPPING_ISSUE_SPIKE_MULTIPLE = 2;

/**
 * Floor under the spike threshold, in issues per run.
 *
 * Without it, TxDOT's ceiling of 6 gives a threshold of 12, and two extra unmappable
 * values in one run - noise on a 2,000-record payload - reads as a format change.
 */
const MIN_MAPPING_ISSUE_THRESHOLD = 25;

/** Used when a source carries no measured ceiling. Deliberately loose. */
const DEFAULT_MAPPING_ISSUE_CEILING = 50;

/**
 * Consecutive failed polls before a fetch-failure alarm fires.
 *
 * Three, matching the staleness alarms' three-missed-polls window: one cold start or
 * one slow agency response is not a page, three in a row is the feed being down.
 */
const FETCH_FAILURE_POLLS = 3;

/**
 * CloudWatch accepts 1, 5, 10, 30, then multiples of 60. Every period below is
 * derived from a catalog cadence, and a catalog is free to say 650 - which would
 * round to a silently rejected 10.83 minutes.
 */
const toValidPeriodSeconds = (seconds: number): number =>
  Math.max(60, Math.ceil(seconds / 60) * 60);

export class ObservabilityStack extends Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const collectorLogs = logs.LogGroup.fromLogGroupName(
      this,
      'CollectorLogs',
      props.collectorLogGroupName,
    );
    const normalizerLogs = logs.LogGroup.fromLogGroupName(
      this,
      'NormalizerLogs',
      props.normalizerLogGroupName,
    );
    const resolverLogs = logs.LogGroup.fromLogGroupName(
      this,
      'ResolverLogs',
      props.resolverLogGroupName,
    );

    // ------------------------------------------------------------------
    // Metric filters over the structured logs
    // ------------------------------------------------------------------

    /**
     * NOTE ON `defaultValue`: CloudWatch Logs rejects `dimensions` and
     * `defaultValue` together - they are mutually exclusive, and the rejection
     * happens at CREATE time, not at synth. So every dimensioned filter below
     * omits `defaultValue`, and missing data stays MISSING rather than becoming
     * zero.
     *
     * That is the right behavior here anyway. The staleness alarms use
     * `treatMissingData: BREACHING` precisely because a silent source emits no
     * data points at all - if `defaultValue: 0` filled the gap, the alarm would
     * be evaluating a zero instead of a gap, which works, but relies on the
     * filter emitting for a log line that never arrived. Absence is the signal;
     * do not paper over it.
     *
     * Asserted by scripts/check-metric-filters.sh.
     */

    /**
     * Per-source collection success. This is the freshness signal: a
     * source that stops reporting must raise an operational alert AND degrade
     * the confidence of records derived from it.
     */
    const collected = new logs.MetricFilter(this, 'CollectedFilter', {
      logGroup: collectorLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'collected'),
      metricNamespace: NAMESPACE,
      metricName: 'PayloadsCollected',
      metricValue: '1',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.COUNT,
    });

    const fetchFailed = new logs.MetricFilter(this, 'FetchFailedFilter', {
      logGroup: collectorLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'fetch_failed'),
      metricNamespace: NAMESPACE,
      metricName: 'FetchFailures',
      metricValue: '1',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.COUNT,
    });

    /** Feed latency - a slow agency feed is a leading indicator, not an error. */
    new logs.MetricFilter(this, 'FetchLatencyFilter', {
      logGroup: collectorLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'collected'),
      metricNamespace: NAMESPACE,
      metricName: 'FetchLatencyMs',
      metricValue: '$.latencyMs',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.MILLISECONDS,
    });

    /** Candidates produced per normalize run. A drop to zero is a real signal. */
    new logs.MetricFilter(this, 'CandidatesFilter', {
      logGroup: normalizerLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'normalized'),
      metricNamespace: NAMESPACE,
      metricName: 'CandidatesProduced',
      metricValue: '$.candidates',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.COUNT,
    });

    /**
     * Mapping-failure rate. This is the metric that tells you an agency
     * changed its feed format before anyone notices bad output.
     */
    const issues = new logs.MetricFilter(this, 'IssuesFilter', {
      logGroup: normalizerLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'normalized'),
      metricNamespace: NAMESPACE,
      metricName: 'MappingIssues',
      metricValue: '$.issues',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.COUNT,
    });

    new logs.MetricFilter(this, 'OffCorridorFilter', {
      logGroup: normalizerLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'normalized'),
      metricNamespace: NAMESPACE,
      metricName: 'OffCorridorRecords',
      metricValue: '$.offCorridor',
      dimensions: { sourceId: '$.sourceId' },
      unit: cw.Unit.COUNT,
    });

    /** An unregistered source or a quarantined payload is never acceptable. */
    const quarantined = new logs.MetricFilter(this, 'QuarantineFilter', {
      logGroup: normalizerLogs,
      filterPattern: logs.FilterPattern.stringValue(
        '$.msg',
        '=',
        'no_adapter_registered',
      ),
      metricNamespace: NAMESPACE,
      metricName: 'PayloadsQuarantined',
      metricValue: '1',
      defaultValue: 0,
      unit: cw.Unit.COUNT,
    });

    /**
     * ==================================================================
     * P95 INGEST-TO-QUERYABLE LATENCY
     * ==================================================================
     *
     * Acceptance criterion 9 - "p95 ingest-to-queryable latency for incidents is at
     * or under 90 s under 10x load" - and until the resolver existed this was not
     * measurable at all: nothing persisted events, so there was no "queryable"
     * moment to measure to.
     *
     * MEASURED END TO END, not per function. The resolver computes it from the
     * `retrieved_at` the collector stamped on the payload to the moment the version
     * lands in the store, so it spans collection, S3, EventBridge, normalization,
     * every retry, and resolution. `FetchLatencyMs` and the per-function durations
     * measure their own hops; only this one measures what a consumer waits.
     *
     * DIMENSIONED BY eventClass, because the promise is class-specific: a
     * congestion event and a work zone travel the same pipe, but only incidents
     * carry a 90-second promise.
     */
    new logs.MetricFilter(this, 'IngestLatencyFilter', {
      logGroup: resolverLogs,
      // Both conditions matter. `resolved` alone would match lines where the field
      // is null (an unparseable retrieved_at), and a metric filter over a null value
      // publishes nothing while looking configured.
      filterPattern: logs.FilterPattern.all(
        logs.FilterPattern.stringValue('$.msg', '=', 'resolved'),
        logs.FilterPattern.numberValue('$.ingestLatencyMs', '>=', 0),
      ),
      metricNamespace: NAMESPACE,
      metricName: 'IngestLatencyMs',
      metricValue: '$.ingestLatencyMs',
      dimensions: { eventClass: '$.eventClass' },
      unit: cw.Unit.MILLISECONDS,
    });

    /** Events resolved, by what the resolver decided. The merge count is the one the demo cares about. */
    new logs.MetricFilter(this, 'ResolvedFilter', {
      logGroup: resolverLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'resolved'),
      metricNamespace: NAMESPACE,
      metricName: 'EventsResolved',
      metricValue: '1',
      dimensions: { action: '$.action' },
      unit: cw.Unit.COUNT,
    });

    /**
     * Pairs the matcher declined to decide. NOT an error metric - a visible
     * review queue is the honest answer to an ambiguous score. It is here because a
     * queue climbing steadily means the thresholds need the whiteboard, and a queue
     * at zero forever means the review band is never being reached.
     */
    new logs.MetricFilter(this, 'ReviewQueuedFilter', {
      logGroup: resolverLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'resolved'),
      metricNamespace: NAMESPACE,
      metricName: 'MatchReviewsQueued',
      metricValue: '$.reviews',
      defaultValue: 0,
      unit: cw.Unit.COUNT,
    });

    /**
     * An illegal transition, rejected rather than coerced. Should be flat
     * zero forever; a single one is a bug in the transition table or in the resolver.
     */
    new logs.MetricFilter(this, 'IllegalTransitionFilter', {
      logGroup: resolverLogs,
      filterPattern: logs.FilterPattern.stringValue('$.msg', '=', 'illegal_transition'),
      metricNamespace: NAMESPACE,
      metricName: 'IllegalTransitions',
      metricValue: '1',
      defaultValue: 0,
      unit: cw.Unit.COUNT,
    });

    // ------------------------------------------------------------------
    // Alarms
    // ------------------------------------------------------------------

    const topic = new sns.Topic(this, 'AlarmTopic', {
      displayName: 'Corridor Event Hub alarms',
      /**
       * Topic policy denies `sns:Publish` unless `aws:SecureTransport` is true.
       * Costs nothing and removes a real gap: an alarm notification carries which
       * source is silent and which corridor segment is affected, and a topic that
       * accepts plaintext publishes is a topic anything on the network path can
       * read or forge. CloudWatch publishes over TLS regardless, so nothing legitimate
       * is turned away. (cdk-nag AwsSolutions-SNS3.)
       */
      enforceSSL: true,
    });
    /**
     * WITHOUT THIS, NO ALARM CAN EVER NOTIFY. `enforceSSL` above is not additive:
     * it attaches a topic policy, and attaching ANY topic policy replaces the
     * default one SNS creates - the default being the only thing that was granting
     * publish inside this account. The synthesized document then contains exactly
     * one statement, a Deny, so every publish falls through to the implicit deny
     * and CloudWatch fails with "CloudWatch Alarms is not authorized to perform:
     * SNS:Publish". The alarms still transition to ALARM, which is what makes this
     * so quiet: the console looks correct and the email never arrives.
     *
     * `aws:SourceAccount` keeps this from being a cross-account confused deputy -
     * only alarms in THIS account can use the service principal to publish here.
     */
    topic.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudWatchAlarmsToPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [topic.topicArn],
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
    );
    if (props.alarmEmail) {
      topic.addSubscription(new subs.EmailSubscription(props.alarmEmail));
    }
    const action = new cwa.SnsAction(topic);

    /**
     * Per-source staleness. `breachOnMissingData` is the important part:
     * the failure mode here is a source going SILENT, which produces no data
     * points at all. An alarm that treats missing data as OK would never fire
     * for exactly the case it exists to catch.
     */
    /**
     * THE WINDOW IS DERIVED FROM THE CADENCE, not fixed at 30 minutes.
     *
     * A fixed window is only correct for sources faster than it. At 30 minutes flat,
     * a source polled every 45 minutes would sit in ALARM permanently while being
     * perfectly healthy - and the only guard upstream rejects cadences over a DAY
     * (ingest-stack.ts), so nothing would have caught it.
     *
     * Three missed polls, floored at 30 minutes: long enough that one cold start or
     * one slow agency response is not a page, short enough to still mean something.
     * Today every source floors to 30 minutes, so this changes no current alarm -
     * it stops the next cadence change from breaking one.
     */
    /**
     * A window guaranteed to hold at least `polls` polls of this source, never
     * shorter than `minSeconds`.
     *
     * Extracted because THREE alarms now need it and each one is wrong in a different
     * way if it hardcodes a duration. The fetch-failure alarm is the clearest case: at
     * a fixed 15-minute window, a threshold of three failures is UNREACHABLE for the
     * 600-second source, which can only poll once or twice in that time. An alarm that
     * cannot arithmetically reach its own threshold is the same defect as one watching
     * a metric nobody publishes - it just hides better.
     */
    const pollWindowSeconds = (
      sourceId: string,
      minSeconds: number,
      polls = 3,
    ): number =>
      toValidPeriodSeconds(
        Math.max(minSeconds, polls * (props.sourceCadenceSeconds[sourceId] ?? 300)),
      );

    const staleWindowSeconds = (sourceId: string): number =>
      pollWindowSeconds(sourceId, 1800);

    for (const sourceId of props.sourceIds) {
      const window = staleWindowSeconds(sourceId);
      new cw.Alarm(this, `Stale-${sourceId}`, {
        alarmName: `CorridorEventHub-stale-${sourceId}`,
        alarmDescription:
          `No successful collection from ${sourceId} in ${Math.round(window / 60)} minutes ` +
          `(polled every ${props.sourceCadenceSeconds[sourceId] ?? 300}s). ` +
          'This also degrades the confidence of records derived from it.',
        metric: collected.metric({
          statistic: 'Sum',
          period: Duration.seconds(window),
          dimensionsMap: { sourceId },
        }),
        threshold: 1,
        comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cw.TreatMissingData.BREACHING,
      }).addAlarmAction(action);
    }

    /**
     * PER-SOURCE fetch failures, one alarm each.
     *
     * WHY THIS IS A LOOP AND NOT ONE ALARM. It used to be one alarm reading
     * `fetchFailed.metric(...)` with no `dimensionsMap`, and it could never fire.
     * `MetricFilter.metric()` copies the filter's namespace and metric NAME but not its
     * DIMENSIONS, and in CloudWatch the dimensions are part of a metric's identity - so
     * the alarm watched an `CorridorEventHub/FetchFailures` with no dimensions, which nothing
     * publishes and nothing ever will. It sat `OK` from the day it was created, its
     * state timestamp frozen at its first evaluation, through a three-hour outage on
     * 2026-08-12 in which `aws-location-traffic` logged 12 failures an hour - the exact
     * condition it existed to catch. The staleness alarms above were always correct
     * because they pass `dimensionsMap` explicitly; this now does the same.
     *
     * One alarm per source rather than a metric-math aggregate across the dimension,
     * matching the staleness alarms: the notification names the failing feed, which is
     * the first thing an operator needs and the thing a summed alarm cannot say.
     *
     * `treatMissingData: NOT_BREACHING` - the opposite of the staleness alarms, and
     * deliberately. This filter publishes only when a fetch FAILS, so absence is the
     * healthy case. Silence is the staleness alarms' job, and they breach on it.
     */
    for (const sourceId of props.sourceIds) {
      const cadence = props.sourceCadenceSeconds[sourceId] ?? 300;
      const window = pollWindowSeconds(sourceId, 900, FETCH_FAILURE_POLLS);
      new cw.Alarm(this, `FetchFailure-${sourceId}`, {
        alarmName: `CorridorEventHub-fetch-failures-${sourceId}`,
        alarmDescription:
          `${FETCH_FAILURE_POLLS} or more failed fetches from ${sourceId} in ` +
          `${Math.round(window / 60)} minutes (polled every ${cadence}s). One state feed ` +
          'failing must not stop the others (isolation NFR), so this is a warning about ' +
          'data completeness rather than a system outage. The bytes for successful ' +
          'fetches are unaffected; what is missing is everything this feed would have ' +
          'reported in the meantime.',
        metric: fetchFailed.metric({
          statistic: 'Sum',
          period: Duration.seconds(window),
          dimensionsMap: { sourceId },
        }),
        threshold: FETCH_FAILURE_POLLS,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(action);
    }

    new cw.Alarm(this, 'QuarantineAlarm', {
      alarmName: 'CorridorEventHub-payload-quarantined',
      alarmDescription:
        'A payload arrived with no registered adapter. Never silently ' +
        'dropped, so this must be visible.',
      metric: quarantined.metric({ statistic: 'Sum', period: Duration.minutes(15) }),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * PER-SOURCE mapping-issue spikes - the format-drift signal, one alarm each.
     *
     * Carried the same undimensioned-metric defect as the fetch-failure alarm above and
     * could never fire either. Fixing the wiring alone was not enough, for two reasons
     * worth recording because both are easy to reintroduce.
     *
     * ONE: `Sum` over a fixed window is not comparable across sources, because it
     * measures issues-per-window and the window holds a different number of runs for
     * every cadence. Oklahoma at 60s fits 15 runs into 15 minutes and New Mexico at
     * 600s fits one or two, so the same feed health produces a 15x difference in the
     * number being thresholded. `Average` is issues PER RUN, which is cadence-
     * independent, is the number the operating notes already quote, and is robust to a
     * single odd payload while still catching the sustained shift that drift produces.
     *
     * TWO: no single threshold fits. Measured per-run over 2026-08-13..17, the healthy
     * ceiling is 6 for TxDOT and 97 for the tiled source - a 16x spread. The old global
     * 200 was above every source's total, so it was unreachable for four of the six and
     * would have needed a 30x drift on TxDOT to trip. The threshold is therefore derived
     * per source from a measured ceiling in the catalog, which is also where a future
     * baseline correction lands without touching this file.
     */
    const ceilings = props.sourceMappingIssueCeiling ?? {};
    for (const sourceId of props.sourceIds) {
      const cadence = props.sourceCadenceSeconds[sourceId] ?? 300;
      const window = pollWindowSeconds(sourceId, 900);
      const ceiling = ceilings[sourceId] ?? DEFAULT_MAPPING_ISSUE_CEILING;
      const threshold = Math.max(
        MIN_MAPPING_ISSUE_THRESHOLD,
        ceiling * MAPPING_ISSUE_SPIKE_MULTIPLE,
      );
      new cw.Alarm(this, `MappingIssueSpike-${sourceId}`, {
        alarmName: `CorridorEventHub-mapping-issue-spike-${sourceId}`,
        alarmDescription:
          `${sourceId} averaged more than ${threshold} mapping issues per run over ` +
          `${Math.round(window / 60)} minutes, against a measured healthy ceiling of ` +
          `${ceiling} (polled every ${cadence}s). Likely an agency feed format ` +
          'change. This is NOT a bug count - unmappable values are recorded ' +
          'rather than dropped, so a steady nonzero rate is correct behaviour and only ' +
          'the spike is the signal. Check the review queue before trusting output, and ' +
          'raise mappingIssueCeilingPerRun in config/sources.json if the feed has ' +
          'legitimately got noisier.',
        metric: issues.metric({
          statistic: 'Average',
          period: Duration.seconds(window),
          dimensionsMap: { sourceId },
        }),
        threshold,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        // Two periods: drift persists, one weird payload does not.
        evaluationPeriods: 2,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }).addAlarmAction(action);
    }

    const lambdaErrors = new cw.MathExpression({
      expression: 'collectorErrors + normalizerErrors',
      usingMetrics: {
        collectorErrors: new cw.Metric({
          namespace: 'AWS/Lambda',
          metricName: 'Errors',
          dimensionsMap: { FunctionName: props.collectorFunctionName },
          statistic: 'Sum',
        }),
        normalizerErrors: new cw.Metric({
          namespace: 'AWS/Lambda',
          metricName: 'Errors',
          dimensionsMap: { FunctionName: props.normalizerFunctionName },
          statistic: 'Sum',
        }),
      },
      period: Duration.minutes(5),
    });

    new cw.Alarm(this, 'LambdaErrorAlarm', {
      alarmName: 'CorridorEventHub-lambda-errors',
      alarmDescription: 'Unhandled errors in the ingest pipeline.',
      metric: lambdaErrors,
      threshold: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * DEAD-LETTER QUEUE DEPTH.
     *
     * Threshold is ONE. Every other alarm here tolerates a baseline because feeds
     * are legitimately imperfect; a dead-lettered payload is different. It means a
     * payload we successfully fetched and stored was never turned into events, so
     * the corridor is under-reporting and nothing else on this dashboard would show
     * it. There is no acceptable steady-state rate above zero.
     *
     * `ApproximateNumberOfMessagesVisible` with `Maximum` rather than `Sum`: the
     * metric is a gauge (current depth), not a counter, so summing it across
     * periods would multiply one stuck message into an imaginary pile.
     *
     * `treatMissingData: NOT_BREACHING` because SQS publishes nothing for a queue
     * that has never received a message - the healthy case. This is the opposite
     * choice from the per-source staleness alarms, where absence IS the signal.
     */
    const dlqDepth = (queueName: string) =>
      new cw.Metric({
        namespace: 'AWS/SQS',
        metricName: 'ApproximateNumberOfMessagesVisible',
        dimensionsMap: { QueueName: queueName },
        statistic: 'Maximum',
        period: Duration.minutes(5),
      });

    /**
     * DASHBOARD variant of the DLQ metrics, wrapped in `FILL(m, 0)`.
     *
     * WHY THIS WRAPPER EXISTS. SQS publishes NO metrics at all for an idle queue -
     * "all metrics emit non-negative values only when the queue is active" (SQS
     * Developer Guide, Available CloudWatch metrics). A healthy DLQ that nothing has
     * written to or polled for a few hours therefore does not render as a flat line
     * at zero; it renders as an EMPTY WIDGET. Measured on the live account: the last
     * datapoint any of the four DLQs published was at queue-creation time, ~15 hours
     * before the graph was read, so all four series were genuinely absent.
     *
     * That is the worst possible rendering for the one row whose entire message is
     * "zero is good", because "no data" is indistinguishable from a dashboard
     * pointed at the wrong queue name - the exact failure this row exists to rule
     * out.
     *
     * `FILL(m, 0)` synthesizes the zero. It does NOT need a seed datapoint to fill
     * from: verified against the live account, a raw metric returning 0 datapoints
     * over 3 hours becomes a full 36 buckets of 0.0 through FILL.
     *
     * The ALARMS above deliberately keep the RAW metric. `treatMissingData:
     * NOT_BREACHING` already reads absence correctly there, and this row is a
     * rendering concern, not an alerting one.
     *
     * `id` must be UNIQUE WITHIN A WIDGET and is spelled out at each call site rather
     * than generated from a counter: CloudWatch rejects two series sharing a metric-math
     * id in one graph, and a counter would renumber every series in the synthesized
     * template the moment someone inserts a widget above.
     */
    const filledToZero = (id: string, metric: cw.Metric, label: string) =>
      new cw.MathExpression({
        expression: `FILL(${id}, 0)`,
        usingMetrics: { [id]: metric },
        label,
        period: Duration.minutes(5),
      });

    /**
     * Messages ARRIVING on a DLQ. Valid here only because none of these four queues
     * is the target of an SQS `RedrivePolicy` - they are written by Lambda
     * `onFailure` destinations and EventBridge target DLQs, which are ordinary
     * `SendMessage` calls and therefore counted.
     *
     * IF SOMEONE LATER PUTS A REDRIVE POLICY IN FRONT OF ONE OF THESE, this widget
     * goes blind for that queue: messages moved to a DLQ by automatic redrive are
     * NOT captured by `NumberOfMessagesSent` (SQS Developer Guide, "Dead-letter
     * queues (DLQs) and CloudWatch metrics"), which is also why the alarms watch
     * depth rather than arrivals.
     */
    const dlqSent = (queueName: string) =>
      new cw.Metric({
        namespace: 'AWS/SQS',
        metricName: 'NumberOfMessagesSent',
        dimensionsMap: { QueueName: queueName },
        statistic: 'Sum',
        period: Duration.minutes(5),
      });

    new cw.Alarm(this, 'NormalizerDlqAlarm', {
      alarmName: 'CorridorEventHub-normalizer-dlq',
      alarmDescription:
        'A raw payload was fetched and stored but FAILED to normalize through all ' +
        'retries. The bytes are safe in S3 so it is replayable, but the ' +
        'events do not exist. Inspect with: npm run dlq',
      metric: dlqDepth(props.normalizerDlqName),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    new cw.Alarm(this, 'RuleDlqAlarm', {
      alarmName: 'CorridorEventHub-rule-dlq',
      alarmDescription:
        'EventBridge could not DELIVER a RawPayloadStored event to the normalizer - ' +
        'throttling, permissions, or a missing function. The handler never ran. ' +
        'Inspect with: npm run dlq',
      metric: dlqDepth(props.ruleDlqName),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * The resolver's pair. A candidate that fails to resolve is a WORSE silence than
     * one that fails to normalize: the record was fetched, stored, parsed, and
     * conflated, so every upstream dashboard is green and the corridor is simply
     * missing an event. Nothing in the pipeline says so.
     *
     * This is also where an illegal transition lands. The resolver raises
     * rather than coercing, so "rejected AND ALARMED" is this alarm plus the reason
     * on the dead letter.
     */
    new cw.Alarm(this, 'ResolverDlqAlarm', {
      alarmName: 'CorridorEventHub-resolver-dlq',
      alarmDescription:
        'A candidate event was parsed and conflated but FAILED to resolve through ' +
        'all retries - an illegal transition, a persistent write conflict, ' +
        'or a store error. Every upstream metric still looks healthy and the ' +
        'corridor is missing an event. Inspect with: npm run dlq',
      metric: dlqDepth(props.resolverDlqName),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * The ingest budget: p95 ingest-to-queryable at or under 90s, for
     * INCIDENTS specifically - the class with the tightest promise, and the one a
     * consumer reacts to fastest.
     *
     * p95 rather than Average, because the budget is stated as p95 and because an
     * average hides exactly the tail that matters: a handful of two-minute records
     * among hundreds of fast ones averages to "fine".
     *
     * Two evaluation periods before it fires. One slow period is a retry or a cold
     * start; two consecutive is the pipeline actually running behind.
     */
    new cw.Alarm(this, 'IngestLatencyP95Alarm', {
      alarmName: 'CorridorEventHub-ingest-latency-p95',
      alarmDescription:
        'p95 ingest-to-queryable latency for incidents exceeded 90s. Measured ' +
        'from the collector fetch to the event landing in the store, so it spans ' +
        'collection, normalization, every retry, and resolution. Check the resolver ' +
        'DLQ and the per-function durations to find which hop is slow.',
      metric: new cw.Metric({
        namespace: NAMESPACE,
        metricName: 'IngestLatencyMs',
        dimensionsMap: { eventClass: 'incident' },
        statistic: 'p95',
        period: Duration.minutes(5),
      }),
      threshold: 90_000,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 2,
      // No incidents in a period is not a latency breach. Silence is covered by the
      // per-source staleness alarms above, which is the right place for it.
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * Rejected AND ALARMED. The rejection is in the resolver; this is the
     * alarm half. Flat zero is the only acceptable value.
     */
    new cw.Alarm(this, 'IllegalTransitionAlarm', {
      alarmName: 'CorridorEventHub-illegal-transition',
      alarmDescription:
        'The resolver refused a transition that is not in the published table. ' +
        'Either the transition table and the resolver disagree, or a ' +
        'lifecycle edge that should exist is missing. The payload is on the ' +
        'resolver DLQ with the reason attached: npm run dlq-peek',
      metric: new cw.Metric({
        namespace: NAMESPACE,
        metricName: 'IllegalTransitions',
        statistic: 'Sum',
        period: Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    /**
     * The TTL machine's own failure mode. Every other alarm here fires when something visibly
     * breaks; this one fires when a CLOCK stops, and the symptom is an event that
     * stays `active` forever while every dashboard stays green. Threshold 1: there is
     * no acceptable rate of failed timer chains.
     */
    new cw.Alarm(this, 'LifecycleExecutionsFailedAlarm', {
      alarmName: 'CorridorEventHub-lifecycle-executions-failed',
      alarmDescription:
        'A lifecycle TTL execution FAILED, so one or more events will never expire on ' +
        'their own again. They will stay published as live until a source ' +
        'update moves them. Find them in the state machine execution list, then ' +
        'restart with the eventId from the failed input.',
      metric: new cw.Metric({
        namespace: 'AWS/States',
        metricName: 'ExecutionsFailed',
        /**
         * The dimension is the ARN, not the name, and it is BUILT here rather than
         * passed in. Passing the machine's `stateMachineArn` would make a
         * CloudFormation export between the two stacks - the trap the queue and
         * log-group props above avoid by passing names. Both stacks share an account
         * and region, so the name is enough to rebuild the ARN.
         *
         * A name in this dimension does not fail: it produces an alarm that watches
         * a metric nothing publishes and therefore never fires. For an alarm whose
         * whole job is noticing that a clock stopped, that is the worst outcome.
         */
        dimensionsMap: {
          StateMachineArn: Stack.of(this).formatArn({
            service: 'states',
            resource: 'stateMachine',
            resourceName: props.lifecycleStateMachineName,
            arnFormat: ArnFormat.COLON_RESOURCE_NAME,
          }),
        },
        statistic: 'Sum',
        period: Duration.minutes(5),
      }),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    new cw.Alarm(this, 'ResolverRuleDlqAlarm', {
      alarmName: 'CorridorEventHub-resolver-rule-dlq',
      alarmDescription:
        'EventBridge could not DELIVER a CandidateEventProduced event to the ' +
        'resolver. The handler never ran, so the candidate never became an event. ' +
        'Inspect with: npm run dlq',
      metric: dlqDepth(props.resolverRuleDlqName),
      threshold: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(action);

    // ------------------------------------------------------------------
    // Dashboard
    // ------------------------------------------------------------------

    /**
     * BUCKET SIZE = THE SLOWEST SOURCE'S POLL INTERVAL, floored at 5 minutes.
     *
     * This was a hardcoded 5 minutes, which is correct only while every source is
     * polled at least that often. The per-source filters carry a `sourceId`
     * dimension and therefore cannot carry a `defaultValue` (CloudWatch rejects the
     * pair), so a bucket with no matching log line publishes NOTHING rather than a
     * zero. A source polled every 10 minutes then leaves every other 5-minute bucket
     * empty and renders as a BROKEN LINE - which is precisely the signal this
     * dashboard reserves for "the source went silent". Measured before the change:
     * nm-dot-weathershare filled 72 of 72 buckets at a 300s cadence; at 600s it
     * would fill 36.
     *
     * The cost is deliberate and worth naming: the fastest source reads coarser.
     * ok-odot-wzdx at 60s shows ~10 collections per bucket instead of ~5. A number
     * that needs one division beats a healthy feed drawn as a fault.
     */
    const dashboardPeriod = Duration.seconds(
      toValidPeriodSeconds(
        Math.max(300, ...props.sourceIds.map((id) => props.sourceCadenceSeconds[id] ?? 300)),
      ),
    );

    const perSource = (metricName: string, stat = 'Sum') =>
      props.sourceIds.map(
        (sourceId) =>
          new cw.Metric({
            namespace: NAMESPACE,
            metricName,
            dimensionsMap: { sourceId },
            statistic: stat,
            label: sourceId,
            period: dashboardPeriod,
          }),
      );

    const dashboard = new cw.Dashboard(this, 'Dashboard', {
      dashboardName: 'Corridor-Event-Hub-ADS',
      defaultInterval: Duration.hours(3),
    });

    dashboard.addWidgets(
      new cw.TextWidget({
        markdown: [
          '# Corridor Event Hub ingest',
          '',
          'Corridor event pipeline. Rows follow the data path:',
          '**collect** -> **normalize** -> **resolve** -> **lifecycle**.',
          '',
          'All four stages are deployed. The rows below cover the first three.',
          '**Lifecycle has an alarm but no graph** (`CorridorEventHub-lifecycle-executions-failed`):',
          'its log group is not instrumented, so no state transition appears on this',
          'screen. Neither does confidence distribution, which lives only in the',
          'normalizer log line. A green dashboard is not a claim about either.',
          '',
          '`MappingIssues` is not a bug count. Unmappable values are',
          'recorded rather than dropped, so a steady nonzero rate is correct',
          'behavior. A *spike* means an agency changed its feed format.',
          '',
          `**Buckets are ${dashboardPeriod.toMinutes()} minutes** - the slowest source's poll` +
            ' interval, so no healthy feed draws as a broken line. Faster sources show' +
            ' proportionally more per bucket: ' +
            props.sourceIds
              .map((id) => `\`${id}\` ${props.sourceCadenceSeconds[id] ?? 300}s`)
              .join(', ') +
            '.',
          '',
          '**An EMPTY failure graph is healthy.** These metrics carry a `sourceId`',
          'dimension and therefore no `defaultValue`, so they publish nothing at all',
          'until something happens - no line rather than a line at zero. Read',
          '*Payloads collected* first to see who is alive.',
        ].join('\n'),
        width: 24,
        // Grew with the "what is NOT on this screen" note. A clipped text widget
        // scrolls rather than resizing, and the caveats are the part that gets cut.
        height: 10,
      }),
    );

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'Payloads collected per source (freshness)',
        left: perSource('PayloadsCollected'),
        width: 8,
        height: 6,
      }),
      new cw.GraphWidget({
        title: 'Feed latency (ms)',
        left: perSource('FetchLatencyMs', 'Average'),
        width: 8,
        height: 6,
      }),
      new cw.GraphWidget({
        title: 'Fetch failures',
        left: perSource('FetchFailures'),
        width: 8,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'Candidate events produced',
        left: perSource('CandidatesProduced'),
        width: 8,
        height: 6,
      }),
      new cw.GraphWidget({
        title: 'Mapping issues (review queue depth)',
        left: perSource('MappingIssues'),
        width: 8,
        height: 6,
      }),
      new cw.GraphWidget({
        title: 'Off-corridor records (expected, not an error)',
        left: perSource('OffCorridorRecords'),
        width: 8,
        height: 6,
      }),
    );

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'Lambda duration p95',
        left: [
          new cw.Metric({
            namespace: 'AWS/Lambda',
            metricName: 'Duration',
            dimensionsMap: { FunctionName: props.collectorFunctionName },
            statistic: 'p95',
            label: 'collector',
          }),
          new cw.Metric({
            namespace: 'AWS/Lambda',
            metricName: 'Duration',
            dimensionsMap: { FunctionName: props.normalizerFunctionName },
            statistic: 'p95',
            label: 'normalizer',
          }),
        ],
        width: 12,
        height: 6,
      }),
      new cw.GraphWidget({
        title: 'Lambda errors and throttles',
        left: [
          new cw.Metric({
            namespace: 'AWS/Lambda',
            metricName: 'Errors',
            dimensionsMap: { FunctionName: props.collectorFunctionName },
            statistic: 'Sum',
            label: 'collector errors',
          }),
          new cw.Metric({
            namespace: 'AWS/Lambda',
            metricName: 'Errors',
            dimensionsMap: { FunctionName: props.normalizerFunctionName },
            statistic: 'Sum',
            label: 'normalizer errors',
          }),
          new cw.Metric({
            namespace: 'AWS/Lambda',
            metricName: 'Throttles',
            dimensionsMap: { FunctionName: props.collectorFunctionName },
            statistic: 'Sum',
            label: 'collector throttles',
          }),
        ],
        width: 12,
        height: 6,
      }),
    );

    /**
     * Dead letters get their own row rather than sharing the errors widget. A
     * Lambda error is often transient and self-heals on retry; a dead letter is a
     * payload that is permanently NOT in the corridor until someone acts. Those
     * deserve different visual weight, and a flat line at zero here is the single
     * most reassuring thing on this dashboard.
     */
    dashboard.addWidgets(
      new cw.TextWidget({
        markdown:
          '## Resolve\n' +
          '**The latency graph is the number to hold the pipeline to**, not a ' +
          'nice-to-have: the budget is p95 ingest-to-queryable at or under 90s for ' +
          'incidents, measured ' +
          'from the collector fetch to the event landing in the store. `merged` on the ' +
          'decision graph is the cross-agency dedup the demo turns on. A review queue ' +
          'climbing steadily means the match thresholds need the whiteboard; one at ' +
          'zero forever means the ambiguous band is never being reached.',
        width: 24,
        height: 3,
      }),
    );

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'Ingest-to-queryable latency, p95 by class (Incidents <= 90s)',
        left: [
          new cw.Metric({
            namespace: NAMESPACE,
            metricName: 'IngestLatencyMs',
            dimensionsMap: { eventClass: 'incident' },
            statistic: 'p95',
            label: 'incident p95',
            period: Duration.minutes(5),
          }),
          new cw.Metric({
            namespace: NAMESPACE,
            metricName: 'IngestLatencyMs',
            dimensionsMap: { eventClass: 'work_zone' },
            statistic: 'p95',
            label: 'work_zone p95',
            period: Duration.minutes(5),
          }),
        ],
        leftAnnotations: [
          { value: 90_000, label: 'ingest budget (90s)', color: cw.Color.RED },
        ],
        width: 12,
        height: 6,
        leftYAxis: { min: 0 },
      }),
      new cw.GraphWidget({
        title: 'Resolver decisions',
        left: ['created', 'updated', 'merged', 'unchanged', 'late_ignored'].map(
          (action) =>
            new cw.Metric({
              namespace: NAMESPACE,
              metricName: 'EventsResolved',
              dimensionsMap: { action },
              statistic: 'Sum',
              label: action,
              period: Duration.minutes(5),
            }),
        ),
        right: [
          new cw.Metric({
            namespace: NAMESPACE,
            metricName: 'MatchReviewsQueued',
            statistic: 'Sum',
            label: 'queued for review',
            period: Duration.minutes(5),
          }),
        ],
        width: 12,
        height: 6,
        leftYAxis: { min: 0 },
      }),
    );

    dashboard.addWidgets(
      new cw.TextWidget({
        markdown:
          '## Dead letters\n' +
          'All four queues should be **flat at zero**. Any nonzero value means a ' +
          'payload we fetched and stored never became events — the raw bytes are safe ' +
          'in S3 and replayable, but the corridor is under-reporting until it is ' +
          'handled. Inspect with `npm run dlq`, replay with `npm run dlq-replay`.\n\n' +
          '**The zero line here is synthesized, and that is deliberate.** SQS ' +
          'publishes no metrics at all for an idle queue, so these four series are ' +
          'genuinely absent whenever the DLQs are empty and unpolled — which is the ' +
          'healthy state. Unlike the ingest rows above, this row wraps each metric in ' +
          '`FILL(m, 0)` so healthy reads as a flat zero rather than as an empty ' +
          'widget, because a blank graph here is indistinguishable from one pointed ' +
          'at the wrong queue name.',
        width: 24,
        height: 5,
      }),
    );

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'Dead-letter queue depth (should be zero)',
        left: [
          filledToZero(
            'depthNorm',
            dlqDepth(props.normalizerDlqName),
            'normalizer (handler raised)',
          ),
          filledToZero('depthRule', dlqDepth(props.ruleDlqName), 'rule (undelivered)'),
          filledToZero(
            'depthRes',
            dlqDepth(props.resolverDlqName),
            'resolver (handler raised)',
          ),
          filledToZero(
            'depthResRule',
            dlqDepth(props.resolverRuleDlqName),
            'resolver rule (undelivered)',
          ),
        ],
        width: 12,
        height: 6,
        leftYAxis: { min: 0 },
      }),
      new cw.GraphWidget({
        title: 'Messages dead-lettered over time',
        /**
         * All FOUR queues, matching the depth widget beside it. This graphed only the
         * normalizer and rule queues, so the two RESOLVER queues were invisible here -
         * and the resolver DLQ is the one the alarm above calls a WORSE silence than a
         * normalize failure, because every upstream metric stays green while the
         * corridor quietly misses an event. A row titled "dead letters" that omits
         * half the dead letters is the same failure as a blank graph.
         */
        left: [
          filledToZero('sentNorm', dlqSent(props.normalizerDlqName), 'normalizer'),
          filledToZero('sentRule', dlqSent(props.ruleDlqName), 'rule'),
          filledToZero('sentRes', dlqSent(props.resolverDlqName), 'resolver'),
          filledToZero('sentResRule', dlqSent(props.resolverRuleDlqName), 'resolver rule'),
        ],
        width: 12,
        height: 6,
        leftYAxis: { min: 0 },
      }),
    );

    new CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=Corridor-Event-Hub-ADS`,
    });
    new CfnOutput(this, 'AlarmTopicArn', { value: topic.topicArn });
    if (!props.alarmEmail) {
      new CfnOutput(this, 'AlarmSubscriptionNote', {
        value:
          'No email subscribed. Deploy with -c alarmEmail=you@example.com, or ' +
          'subscribe manually. Alarms with no subscriber are theatre.',
      });
    }
  }
}
