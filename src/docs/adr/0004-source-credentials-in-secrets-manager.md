# ADR 0004 — Source credentials live in Secrets Manager, and the catalog holds only a pointer

Status: Accepted · **Amended after a security review**

## Amendment — the Oklahoma exception is withdrawn

**Every credentialed source now resolves from Secrets Manager. There is no exception.**
The alternative this ADR considered and rejected — *"Everything in Secrets Manager,
including the ODOT token"* — is the one now in force, and the section below that
argues against it is left standing rather than deleted, because the argument it makes
is a good one that turned out to be answering the wrong question.

What changed is not the assessment of the token. That assessment was right, and it was
re-verified: the token is published in the federal ITS WorkZone Feed Registry, it is
byte-identical to the registry value, and it protects nothing. **What changed is the
recognition that the artifact's job is to teach.** A security review over the
reference architecture raised the committed literal as a finding, and the finding is
not about disclosure — it is that a state DOT forking this repository reads two files,
sees a 64-character token in each, and learns that tokens go in source. The mitigations
this ADR relied on to stop that were a comment at the call site, an explicit
`query_token_public` method name, and this document. All three assume the reader
arrives via the reasoning rather than via the code, and readers arrive via the code.

The concrete changes:

| Before | After |
|---|---|
| `authMethod: "query_token_public"` | `authMethod: "api_key_secret"` |
| `publicToken` literal in `config/sources.json` | `secretId: "corridor-event-hub/ok-odot-wzdx-token"` |
| `_PUBLIC_TOKEN_FALLBACK` literal in `handlers/collector.py` | deleted, with the branch that read it |
| `?&access_token=` hardcoded in the one branch that needed it | `authQueryParam: "access_token"`, catalog data |

Two consequences worth stating plainly, because they are costs:

**The feed no longer works out of the box.** `npm run probe` used to reach Oklahoma
with no AWS credentials at all, because the token was right there. It now needs
`OK_ODOT_TOKEN` in the environment or the secret in the account, exactly like TxDOT and
AZ511 — and it falls back to the fixture and says so when neither is present. That is
a real loss of a nice property, accepted because the three sources now behave the same
way and a difference that existed only because one token was committed was never a
feature.

**It costs about $0.40 a month.** Named because "the cost is small" was this ADR's own
phrase for the alternative, and a number is better than an adjective.

**`query_token_public` is gone as a method, not renamed.** An adopter with a genuinely
public token who does not want to pay for a secret has a simpler option that needs no
credential machinery: put it in the `endpoint` and use `authMethod: "none"`. That is
honest about what is happening rather than dressing a URL up as a credential mechanism,
and it does not create a second path for a future contributor to reach for.

## Context

Feeds authenticate three different ways, and the differences matter:

| Source | Auth | Is it a secret? |
|---|---|---|
| Oklahoma ODOT | `?access_token=...` | **No.** The token is published in the federal ITS WorkZone Feed Registry (`data.transportation.gov/resource/69qe-yiui`). Anyone can read it. |
| Texas DOT | `?key=...` | **Yes.** Issued to us by TxDOT. |
| NWS | none, but a `User-Agent` with contact info is required by policy | No |

The source catalog (`config/sources.json`) is the natural place to describe how a
feed authenticates — it already holds the endpoint, cadence, license, and
snapshot semantics. **But the catalog is committed to git.**

Meanwhile onboarding a source must need no pipeline code change, and an outside
agency has to be able to author an adapter against a published contract. Both push
toward putting everything in the catalog.

## Decision

The catalog holds a **pointer**, never a credential:

```json
{
  "sourceId": "tx-dot-wzdx",
  "authMethod": "api_key_secret",
  "secretId": "corridor-event-hub/tx-dot-wzdx-key"
}
```

- `authMethod: "api_key_secret"` → the collector resolves `secretId` from Secrets
  Manager at runtime. Since the amendment this is the **only** credentialed
  method, and `authQueryParam` names the query parameter the feed expects (`key` by
  default, `access_token` for Oklahoma) so a new source spelling it differently is a
  catalog edit rather than a code branch.
- ~~`authMethod: "query_token_public"` → the token may live in the catalog or code,
  because it is genuinely public. **This distinction is explicit and documented
  at the call site**, so nobody has to guess whether a given string is sensitive.~~
  **Withdrawn — see the amendment above.** The distinction was real; the
  problem was that it was drawn in a place readers do not look.
- `authMethod: "none_user_agent_required"` → no credential, but a policy-required
  header.
- `authMethod: "aws_sigv4"` → the function's own execution role. No credential at all,
  and the one credential story this deployment had already solved.

Supporting choices:

- IAM is scoped to the path prefix `corridor-event-hub/*`, not `secretsmanager:*`.
- Secrets are cached per Lambda container. At a 5-minute cadence across four
  sources, a fetch per invocation would add latency and cost for a value that
  effectively never changes.
- The S3 raw-payload metadata stores `url.split('?')[0]` — **the query string is
  deliberately stripped** so a key cannot leak into an object that is immutable
  for 7 years under Object Lock.

## Consequences

**A credential can no longer reach git through the obvious path.** The catalog is
safe to commit and safe to publish as part of the reference architecture, which
matters because it is meant to be an adoptable artifact.

**Local development needs a separate path.** `npm run probe` reads
`TX_DOT_KEY` from the environment and silently skips TxDOT when it is absent,
printing a note rather than failing. Two mechanisms for one concern is mild
duplication, justified because the probe's whole value is running with no AWS
account at all.

**Rotation is a Secrets Manager operation, not a deploy.** Update the secret and
containers pick it up as they recycle. Immediate rotation needs a forced
redeploy, which is an acceptable trade for the caching.

**Adopters get the pattern, not our keys.** A DOT cloning this repo sees
`corridor-event-hub/tx-dot-wzdx-key` as a name to create in their own account.

## The part worth arguing about

*Kept as originally written, and then answered by the amendment at the top. It is
left standing because the last sentence turned out to be the whole finding, and a
decision record that quietly deletes its own losing argument is less useful than one
that shows where the reasoning was and where it went.*

**Treating the Oklahoma token as non-secret is a judgment call.** It is published
in a federal registry, so it is not a secret in any meaningful sense, and putting
it in Secrets Manager would imply a confidentiality that does not exist while
adding a lookup for no benefit.

The risk is that the reasoning does not travel: someone later sees a token in
source and concludes that is the house style. Mitigations are the comment at the
call site, the explicit `query_token_public` name (rather than a generic
`api_key`), and this ADR. If ODOT ever issues per-consumer tokens, the fix is a
one-line catalog change to `api_key_secret` — the code path already exists.

**What the amendment settles:** the risk named in that paragraph was the correct
risk, and the three mitigations listed against it were not enough, because all three
require the reader to arrive at the reasoning before the code. A security review
found the literal by reading the code. So did the reviewer's scanner, twice, which is
why `.ash.yaml` carried two suppressions for it — and a suppression is a note to
future readers saying *we looked at this and decided it was fine*, which is exactly
the wrong thing to leave in a teaching artifact.

## Alternatives considered

**Everything in Secrets Manager, including the ODOT token.** Uniform and simpler
to explain: one rule, no judgment. Rejected because it obscures a real
distinction — a reader could not tell which credentials actually matter — and
because it adds a lookup for a value published on a government website. Worth
revisiting if the team prefers uniformity; the cost is small.

> **ADOPTED.** The rejection reasoning above is still accurate about what
> is lost: a reader genuinely cannot now tell from the catalog that the Oklahoma
> credential is published and the Texas one is not. That information moved into
> `$authComment`, which is prose rather than structure — a weaker place for it. The
> trade was taken because obscuring a distinction is recoverable and modelling a
> hardcoded credential is not.

**Environment variables on the Lambda.** Simplest. Rejected: values are visible
in the console and in `describe-function-configuration`, rotation requires a
deploy, and the value ends up in CloudFormation templates and CDK context.

**Parameter Store SecureString.** Cheaper than Secrets Manager (no per-secret
monthly charge) and sufficient here. A reasonable choice for a cost-constrained
DOT adopter. Chose Secrets Manager for native rotation support and because the
per-secret cost is negligible next to this stack's NAT bill (ADR 0001).
