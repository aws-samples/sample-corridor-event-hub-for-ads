/**
 * The Python deployment bundle, shared by every stack that runs a Python Lambda.
 *
 * Built by scripts/build-lambda.sh, not by CDK's own bundling. `PythonFunction` from
 * @aws-cdk/aws-lambda-python-alpha would do it inline, but it requires Docker, which
 * not every adopter can assume on every machine. The shell script needs only pip.
 *
 * The script is nevertheless invoked FROM HERE, at synth, so the bundle is part of
 * building the asset rather than a step someone has to remember. `npm run deploy` runs
 * `npm run bundle` first, but a bare `npx cdk deploy` used to ship whatever happened to
 * be in build/lambda - stale code, silently, which is worse than no bundle at all
 * because it deploys and runs. The script's --if-stale mode makes this cheap: it
 * returns immediately unless an input is newer than the last successful build.
 *
 * The bundle is PURE PYTHON as of the shapely/numpy removal, so there is no longer a
 * platform to get wrong - but the script still pins linux/aarch64 and verifies that
 * nothing compiled crept in, because that property decays with one `pip install`. A
 * wheel for the wrong platform deploys perfectly and fails at INVOKE time with
 * "invalid ELF header", so a failed build fails the synth here rather than later.
 *
 * WHY THIS IS ITS OWN FILE: two stacks now consume the bundle - ingest (collector,
 * normalizer) and spatial (the migration runner). The build and the checks below are
 * the difference between a working deploy and a CDK asset error thrown a long way
 * from its cause, so they should not exist in one stack and be forgotten in the
 * other.
 */

import { aws_lambda as lambda } from 'aws-cdk-lib';
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

const PROJECT_ROOT = path.join(__dirname, '..');
export const LAMBDA_BUNDLE = path.join(PROJECT_ROOT, 'build/lambda');

/**
 * Set to skip the synth-time build entirely - for an offline synth, or CI that has
 * already built the bundle and does not want it re-examined.
 */
const SKIP_ENV = 'CEH_SKIP_BUNDLE';

/** Once per process, however many stacks ask for the bundle. */
let buildAttempted = false;

function buildBundleIfStale(): void {
  if (buildAttempted) {
    return;
  }
  buildAttempted = true;

  if (process.env[SKIP_ENV]) {
    console.log(`${SKIP_ENV} is set - using build/lambda as it stands, unverified`);
    return;
  }

  try {
    // stdio: 'inherit' so the script's own PASS/FAIL lines and its platform
    // diagnosis reach the terminal. Swallowing them would leave a synth failure
    // with no explanation of WHICH wheel was for the wrong platform.
    execFileSync('bash', ['scripts/build-lambda.sh', '--if-stale'], {
      cwd: PROJECT_ROOT,
      stdio: 'inherit',
    });
  } catch {
    throw new Error(
      'scripts/build-lambda.sh failed - see its output above. The bundle is NOT usable.\n' +
        `To synth against the existing build/lambda anyway, set ${SKIP_ENV}=1.`,
    );
  }
}

/**
 * The bundle as a CDK asset: builds it if stale, then asserts the parts the caller
 * needs are in it.
 *
 * `requiredEntries` is per-stack because the stacks need different things: every
 * handler needs `corridor_event_hub`, the ingest handlers read `config/` at import time, and
 * only the migration runner reads `sql/`. The build above makes a stale bundle
 * unlikely; this keeps the assertion anyway, because it also covers the two cases the
 * build cannot - a bundle produced by an OLDER version of the script, and one carried
 * in by CI under CEH_SKIP_BUNDLE. Naming the entries means either fails at synth
 * with the fix, rather than deploying and reporting "no migrations found" at invoke.
 */
export function lambdaBundleCode(requiredEntries: string[] = ['corridor_event_hub']): lambda.Code {
  buildBundleIfStale();

  const missing = requiredEntries.filter((entry) => !fs.existsSync(path.join(LAMBDA_BUNDLE, entry)));

  if (missing.length > 0) {
    throw new Error(
      `Python Lambda bundle at ${LAMBDA_BUNDLE} is missing: ${missing.join(', ')}\n` +
        'Run: npm run bundle    (or bash scripts/build-lambda.sh)\n' +
        'The bundle must be built for linux/aarch64 - the script verifies that.',
    );
  }

  /**
   * ONE BUILD, TWO ASSETS. Anything the caller did not ask for is excluded, which
   * matters for exactly one directory today and will matter more later:
   *
   *   sql/  is migration DATA and only the migration runner opens it. It is 6.1 MB
   *         and GROWS with every annual NBI vintage - unbounded, in a bundle that
   *         has a 250 MB ceiling. Shipping it to the collector and normalizer put
   *         70% of their upload into files they never read.
   *
   * `exclude` changes the asset hash, so CDK uploads two distinct assets from one
   * directory rather than one shared blob. That is the point: the pipeline functions
   * stop redeploying every time a migration is regenerated.
   *
   * `certs/` IS DELIBERATELY ABSENT FROM THIS LIST, so it is never excluded and ships
   * in every asset. Every function that opens the database needs it to verify the
   * Aurora certificate - which is all of them, since the normalizer
   * conflates in PostGIS, the collector's tiled source reads corridor geometry at
   * fetch time, and the query function loads geometry lazily. At 165 KB against a
   * 250 MB ceiling, working out which functions could skip it would cost more in
   * review than it saves in upload, and getting it wrong fails at invoke.
   */
  const everything = ['corridor_event_hub', 'config', 'sql'];
  const exclude = everything.filter((entry) => !requiredEntries.includes(entry));

  return lambda.Code.fromAsset(LAMBDA_BUNDLE, {
    // Both forms: the directory itself and everything under it.
    exclude: exclude.flatMap((entry) => [entry, `${entry}/**`]),
  });
}
