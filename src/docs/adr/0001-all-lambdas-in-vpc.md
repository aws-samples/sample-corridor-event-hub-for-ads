# ADR 0001 — All Lambda functions run in the VPC

Status: Accepted

## Context

Corridor Event Hub ingests from public state 511 endpoints and federal APIs, and may hold
a spatial database (see [ADR 0002](0002-conflation-behind-a-swappable-interface.md)).
Lambda functions can run either outside any VPC — the default, with direct
internet access — or inside one.

Forces at play:

- The collector Lambdas must reach the **public internet** (`oktraffic.org`,
  `api.weather.gov`). A Lambda in a VPC has no internet route unless one is
  provided.
- If the Aurora PostGIS option is adopted, functions touching it **must** be in
  the VPC.
- The deliverable is a reference architecture a state DOT can adopt, so
  the network posture will be reviewed by people with an institutional security
  baseline.
- The prototype must be cheap enough to be credible at single-state-DOT scale
 .

## Decision

**Every** Lambda function in the system runs in the VPC, in
`PRIVATE_WITH_EGRESS` subnets, sharing one egress-only security group.

To make that true rather than aspirational:

- NAT provides outbound internet for the feed fetches.
- Gateway endpoints (S3, DynamoDB — free) and interface endpoints (EventBridge,
  Step Functions, Secrets Manager, CloudWatch Logs) keep AWS-service traffic off
  the NAT path.
- The `logRetention` CDK prop is **not used**, because it injects a singleton
  custom-resource Lambda that runs outside the VPC. Explicit `logs.LogGroup`
  constructs replace it.
- `scripts/check-vpc.sh` asserts the property against the **synthesized
  template** in CI.

## Consequences

**Cost, and it is the notable one.** In a low-traffic corridor service the
network costs more than the compute:

| Item | Approximate monthly |
|---|---|
| NAT Gateway (1 AZ) | ~$32 + data processing |
| 4 interface endpoints × 2 AZs | **~$56** — ~$28 per AZ, and `maxAzs: 2` |
| **Network total as deployed** | **~$88** |
| Lambda + DynamoDB + S3 at this volume | single-digit dollars |

**Quote the $88, not the $28.** The per-AZ figure is the useful one for reasoning
about the [1-AZ lever](../../README.md#cost), but the stack deploys across
`maxAzs: 2`, so the endpoints are doubled and the network total is ~$88 rather than
~$60. The full bill, including the observability line this table omits, is in
[README.md](../../README.md#cost).

Verify current pricing for the target region before quoting these; they are
order-of-magnitude figures for the cost conversation, not a quote. `natGateways`
is a context variable so the number is a visible choice.

**Cold starts** gain roughly 100–200ms for ENI attachment. Irrelevant against a
90-second ingest-to-queryable budget.

**A uniform boundary is easier to audit** than a mixed one. "Which of these
twelve functions is internet-facing?" is a question nobody has to ask.

**The check is necessary because the violation is invisible in source.** The
`logRetention` case proves it: nothing in the stack file mentioned a helper
Lambda, and it only appeared on template inspection. Any future CDK prop that
injects a helper — `autoDeleteObjects`, custom-resource providers — will be
caught by CI rather than by a security review months later.

## Alternatives considered

**Collectors outside the VPC, database-touching functions inside.** Cheaper: no
NAT needed if collectors have direct internet access, saving ~$32/mo. Rejected
because it creates two security postures to reason about, and because the
instruction was uniform VPC placement. Worth revisiting if a DOT adopter is cost
constrained — the collectors write only to S3 and call only public endpoints, so
they are the defensible exception.

**VPC with no NAT, using only VPC endpoints.** Would work if every dependency
were an AWS service. It is not: the feeds are on the public internet. Rejected as
infeasible, not as undesirable.

**Two NAT gateways.** Removes the single-AZ egress dependency. Deferred for the
prototype at ~$32/mo extra; `natGateways: 2` in context is the switch. A
production DOT deployment should do this.
