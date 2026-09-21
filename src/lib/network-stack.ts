/**
 * Network stack — VPC for all Lambda functions.
 *
 * WHY THIS EXISTS: every Lambda in this system runs in the VPC. That is a deliberate
 * choice with real consequences worth understanding before adopting it, because VPC
 * plumbing is the most common way a short project loses its first week
 * (a deliberate scope decision).
 *
 * THE CONSEQUENCE. Collectors fetch from the PUBLIC INTERNET —
 * oktraffic.org and api.weather.gov. A Lambda in a VPC has no route to the
 * internet unless you give it one. So this stack needs:
 *
 *   1. NAT egress for outbound public-internet calls (the 511 and NWS feeds)
 *   2. VPC interface/gateway endpoints for AWS services, so S3, DynamoDB,
 *      EventBridge, Step Functions, and Secrets Manager traffic does NOT
 *      hairpin through NAT — that would be both slower and more expensive
 *
 * COST. NAT Gateway is the dominant line item in a low-traffic stack like this:
 * roughly $32/mo per AZ plus data processing, versus near-zero for the Lambdas
 * themselves. Interface endpoints add ~$7/mo each per AZ. For a corridor service
 * ingesting a few hundred KB per minute, the NETWORK costs more than the COMPUTE.
 *
 * That matters — the reference architecture has to be fundable by a
 * single state DOT — so the cost model must state this plainly rather than bury
 * it. `natGateways` is tunable below and defaults to 1 for exactly this reason.
 *
 * THE ALTERNATIVE, for the record. Collector Lambdas do not strictly need to be
 * in the VPC: they call public endpoints and write to S3, both reachable without
 * one. VPC placement is required for the functions that talk to Aurora
 * (ADR 0002 § The split, and why), and is
 * defensible for the rest as a uniform security posture — a single network boundary
 * is easier to reason about and audit than a mixed one. This stack takes the uniform approach as instructed; the trade is cost
 * and cold-start latency for consistency. Worth an ADR.
 */

import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface NetworkStackProps extends StackProps {
  /**
   * NAT gateways. 1 is cheapest and adequate for a prototype; 2+ removes the
   * single-AZ egress dependency for production. Each is ~$32/mo plus data.
   */
  readonly natGateways?: number;
  /** On by default (`cdk.json` context `flowLogs`); `-c flowLogs=false` opts out. */
  readonly enableFlowLogs?: boolean;
}

export class NetworkStack extends Stack {
  public readonly vpc: ec2.Vpc;
  /** Every Lambda in the system shares this SG. Egress-only. */
  public readonly lambdaSecurityGroup: ec2.SecurityGroup;
  /** For the Aurora PostGIS option, if adopted. See ADR 0002. */
  public readonly databaseSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps = {}) {
    super(scope, id, props);

    const natGateways = props.natGateways ?? 1;
    const maxAzs = 2;

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs,
      natGateways,
      ipAddresses: ec2.IpAddresses.cidr('10.20.0.0/16'),
      subnetConfiguration: [
        {
          // NAT lives here. No application code runs in public subnets.
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          // All Lambdas. Outbound via NAT, inbound from nothing.
          name: 'private-egress',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 22,
        },
        {
          // Aurora, if the PostGIS path is chosen. No internet route at all.
          name: 'isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    // -----------------------------------------------------------------------
    // Security groups
    // -----------------------------------------------------------------------

    this.lambdaSecurityGroup = new ec2.SecurityGroup(this, 'LambdaSg', {
      vpc: this.vpc,
      description: 'Corridor Event Hub Lambda functions - egress only',
      allowAllOutbound: true, // collectors need arbitrary public HTTPS
    });

    this.databaseSecurityGroup = new ec2.SecurityGroup(this, 'DatabaseSg', {
      vpc: this.vpc,
      description: 'Aurora PostGIS - accepts only from Lambda SG',
      allowAllOutbound: false,
    });

    this.databaseSecurityGroup.addIngressRule(
      this.lambdaSecurityGroup,
      ec2.Port.tcp(5432),
      'PostgreSQL from Corridor Event Hub Lambdas only',
    );

    // -----------------------------------------------------------------------
    // VPC endpoints — keep AWS-service traffic off the NAT path
    // -----------------------------------------------------------------------

    // Gateway endpoints are FREE. Always worth having.
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
    this.vpc.addGatewayEndpoint('DynamoDbEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    // Interface endpoints cost ~$7/mo each per AZ. These are the ones the
    // pipeline actually uses; adding more "just in case" is real money.
    const interfaceEndpoints: Array<[string, ec2.InterfaceVpcEndpointAwsService]> = [
      ['EventBridgeEndpoint', ec2.InterfaceVpcEndpointAwsService.EVENTBRIDGE],
      ['StepFunctionsEndpoint', ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS],
      ['SecretsManagerEndpoint', ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER],
      ['CloudWatchLogsEndpoint', ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
    ];

    for (const [id_, service] of interfaceEndpoints) {
      this.vpc.addInterfaceEndpoint(id_, {
        service,
        subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [this.lambdaSecurityGroup],
        privateDnsEnabled: true,
      });
    }

    if (props.enableFlowLogs) {
      this.vpc.addFlowLog('FlowLog', {
        destination: ec2.FlowLogDestination.toCloudWatchLogs(
          new logs.LogGroup(this, 'FlowLogGroup', {
            retention: logs.RetentionDays.ONE_WEEK,
          }),
        ),
        trafficType: ec2.FlowLogTrafficType.REJECT,
      });
    }

    // -----------------------------------------------------------------------
    // cdk-nag — what this stack does not comply with, and why
    // -----------------------------------------------------------------------

    /**
     * EC23 looks for ingress from 0.0.0.0/0 and cannot evaluate this group,
     * because the only ingress rule on it has a CIDR of `Fn::GetAtt Vpc.CidrBlock`
     * - a token, not a literal, so the rule reports a validation failure rather
     * than a pass or a fail.
     *
     * What that rule actually is: 443 from the VPC's own CIDR, added by
     * `addInterfaceEndpoint` above so that endpoint traffic stays off NAT. Nothing
     * on this group is reachable from the internet - the Lambdas sit in
     * PRIVATE_WITH_EGRESS subnets and the group's own purpose is egress. The rule
     * cannot see that at synth; a reader can.
     */
    NagSuppressions.addResourceSuppressions(this.lambdaSecurityGroup, [
      {
        id: 'CdkNagValidationFailure',
        appliesTo: ['AwsSolutions-EC23'],
        reason:
          'The only ingress is 443 from the VPC CIDR, added by addInterfaceEndpoint so AWS ' +
          'service traffic avoids NAT. The CIDR is an Fn::GetAtt token, which EC23 cannot ' +
          'resolve - hence a validation failure rather than a finding. Not open to the internet.',
      },
    ]);

    /**
     * VPC7 wants flow logs, and they are ON by default now - so in the default
     * deployment this branch does not run and there is nothing to suppress. The cost
     * argument for keeping them off (in a low-traffic corridor service the NETWORK
     * already outspends the COMPUTE) lost to the detective-control argument: REJECT
     * traffic only, on a one-week log group, is a small bill for the network-level
     * visibility an incident investigation needs.
     *
     * The suppression survives for `-c flowLogs=false`, so opting out stays honest
     * rather than failing synth - but it is scoped to that choice rather than
     * blanketing a setting someone may have since turned on.
     */
    if (!props.enableFlowLogs) {
      NagSuppressions.addResourceSuppressions(this.vpc, [
        {
          id: 'AwsSolutions-VPC7',
          reason:
            'Flow logs were explicitly disabled with `-c flowLogs=false`, against the ' +
            'default (ADR 0001). Accepted only to hold the network bill down on a ' +
            'throwaway deployment; any production DOT deployment must leave them on.',
        },
      ]);
    }

    // -----------------------------------------------------------------------
    // Outputs — so the cost conversation is unavoidable
    // -----------------------------------------------------------------------

    new CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
    new CfnOutput(this, 'NatGatewayCount', {
      value: String(natGateways),
      description: `~$${natGateways * 32}/mo for NAT before data processing charges`,
    });
    /**
     * The TOTAL, not the per-AZ figure. Interface endpoints bill per AZ, so quoting
     * ~$28 next to a `maxAzs: 2` VPC understates the network by half - which is
     * exactly the mistake this output exists to prevent.
     */
    const endpointCost = 28 * maxAzs;
    new CfnOutput(this, 'NetworkCostNote', {
      value:
        `NAT ~$${natGateways * 32}/mo + 4 interface endpoints ~$${endpointCost}/mo ` +
        `(~$28/AZ x ${maxAzs} AZs) = ~$${natGateways * 32 + endpointCost}/mo. ` +
        'In a low-traffic corridor service the NETWORK costs more than the COMPUTE.',
    });
  }

  /** Standard placement for every Lambda in this system. */
  get lambdaVpcConfig(): {
    vpc: ec2.IVpc;
    vpcSubnets: ec2.SubnetSelection;
    securityGroups: ec2.ISecurityGroup[];
  } {
    return {
      vpc: this.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [this.lambdaSecurityGroup],
    };
  }
}
