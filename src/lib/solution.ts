/**
 * AWS Solution identity: the ID, the version, and the two places they have to appear.
 *
 * WHY THIS FILE EXISTS. Onboarding a solution to AWS requires two attributions, and
 * both of them are strings that change on every release:
 *
 *   1. every stack DESCRIPTION carries `(SO0358) - <text>. Version vX.Y.Z`;
 *   2. every AWS SDK call carries `AWSSOLUTION/SO0358/vX.Y.Z` in its User-Agent, which
 *      is how AWS attributes service API usage back to the solution.
 *
 * A release that updates one and forgets the other is the failure this file is for:
 * the ID and the version are declared ONCE here, and everything else - four stack
 * descriptions, one CloudFormation mapping per Lambda-bearing stack, and the Python
 * fallback in corridor_event_hub/core/awsclients.py - is derived from or checked against it.
 * scripts/check-solution-id.sh is what enforces the "checked against" half.
 *
 * NOT the same number as package.json / pyproject.toml. Those version the CDK app and
 * the Python package; this versions the SOLUTION as published. They are free to
 * diverge, and pinning them together would mean a patch bump to a dev dependency
 * silently republishing the solution under a new version.
 */

import { CfnMapping, Fn, Stack } from 'aws-cdk-lib';
import { Construct } from 'constructs';

/** The AWS Solutions catalog ID for Corridor Event Hub for ADS. */
export const SOLUTION_ID = 'SO0358';

/** The published solution version. `v`-prefixed, as the attribution format requires. */
export const SOLUTION_VERSION = 'v1.0.0';

/**
 * The exact string that must reach the User-Agent header of every AWS SDK call.
 * Format is fixed by the onboarding requirement: `AWSSOLUTION/$id/$version`.
 */
export const CUSTOM_USER_AGENT = `AWSSOLUTION/${SOLUTION_ID}/${SOLUTION_VERSION}`;

/**
 * The environment variable each Lambda reads the user-agent string from.
 *
 * DELIBERATELY NOT `CEH_USER_AGENT`, which already exists and means something
 * else: the outbound HTTP User-Agent this pipeline sends to STATE DOT FEEDS, where NWS
 * policy requires a contact address (corridor_event_hub/adapters/feeds.py). One is about
 * being a polite client of someone else's API; this one is about attributing our own
 * AWS usage. Naming them alike is how a future edit sends a contact email to
 * CloudTrail, or an AWSSOLUTION token to the National Weather Service.
 */
export const USER_AGENT_ENV_VAR = 'SOLUTION_USER_AGENT';

/**
 * A stack description in the attributed form: `(SO0358) - <text>. Version v1.0.0`.
 *
 * The caller passes only its own prose, so the wording of a description and the
 * attribution wrapped around it stay independent.
 */
export function solutionDescription(text: string): string {
  // A trailing period in the caller's text would produce `..` before `Version`.
  const body = text.replace(/\.\s*$/, '');
  return `(${SOLUTION_ID}) - ${body}. Version ${SOLUTION_VERSION}`;
}

/**
 * `{ SOLUTION_USER_AGENT: <Fn::FindInMap ...> }`, for spreading into a Lambda's
 * `environment`.
 *
 * WHY A MAPPING RATHER THAN THE LITERAL. The onboarding guidance asks for the string
 * to be defined once per template as `Mappings.Solution.Metadata.CustomUserAgent`, so
 * that a reader of the template - or a reviewer who never sees this TypeScript - can
 * find the solution's version in one obvious place, and a release bump is one line of
 * a rendered template rather than a value repeated per function.
 *
 * `lazy: false` plus the free `Fn.findInMap` rather than `mapping.findInMap` is what
 * keeps that promise: the mapping is rendered even before anything reads it, and the
 * reference stays an `Fn::FindInMap` intrinsic instead of being folded to a literal at
 * synth. scripts/check-solution-id.sh asserts both, because a CDK upgrade that starts
 * folding it would leave a template that still deploys and no longer has a mapping to
 * bump.
 *
 * ONE MAPPING PER STACK. Called once per function, so it finds the stack's existing
 * `Solution` mapping rather than colliding with it.
 */
export function solutionUserAgentEnv(scope: Construct): Record<string, string> {
  const stack = Stack.of(scope);
  const existing = stack.node.tryFindChild('Solution') as CfnMapping | undefined;
  if (!existing) {
    new CfnMapping(stack, 'Solution', {
      mapping: { Metadata: { CustomUserAgent: CUSTOM_USER_AGENT } },
      lazy: false,
    });
  }
  return { [USER_AGENT_ENV_VAR]: Fn.findInMap('Solution', 'Metadata', 'CustomUserAgent') };
}
