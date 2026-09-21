#!/usr/bin/env bash
#
# Grouped listing of the npm scripts worth knowing about.
#
#   npm run help
#
# Hand-maintained, because npm has no per-script description field the way the
# Makefile had `## ...` comments to grep. Bare `npm run` lists every script
# alphabetically and is the complete answer; this one is the curated one, ordered
# so a newcomer can read it top to bottom.

cat <<'EOF'

  Corridor Event Hub - the pipeline is Python, the infrastructure is TypeScript CDK.
  These scripts are the single entry point across both.

  START HERE
    npm install               CDK deps (npm does this for you)
    npm run setup             everything, both languages
    npm run probe             run the adapters against LIVE feeds - no AWS needed

  THE TWO UIs - they answer different questions from different sources
    npm run ui                corridor strip: what is on the road NOW. Runs the adapters
                              live, one snapshot, no history. API :8787 + React :5173
    npm run trace-ui          record tracker: what HAPPENED to one record, ingestion to
                              end of life. Reads the DEPLOYED event store, read-only.
                              API :8788 + React :5174. Needs AWS_PROFILE.

    npm run serve             the strip API alone on :8787
    npm run serve-fixtures    API from captured payloads only - never touches the network
    npm run trace             the tracker API alone on :8788
    npm run ui-build          production build into ui/dist
    npm run trace-ui-build    production build into ui-trace/dist
    npm run ui-test           the strip UI geometry tests
    npm run trace-ui-test     the tracker UI tests

  TESTS AND LINTS
    npm test                  the Python test suite
    npm run test-v            the test suite with test names
    npm run lint              lint Python
    npm run fmt               format Python, then autofix
    npm run check             everything CI checks, in the order it checks it
    npm run lint:solution     the AWS Solution attribution (SO0358): stack descriptions,
                              the user-agent mapping, and every boto3 call going through
                              core.awsclients. Needs a synth; in `npm run check`

  SECURITY
    npm run lint:secrets      credentials and personal information in tracked files.
                              Seconds, no network. In `npm run check`, and it is the
                              check that guards against real PII in a fixture, which
                              no infrastructure scan can see
    npm run lint:deps         dependency advisories, npm and Python. The one check
                              that needs the network; skips loudly when offline

  DEPLOY
    npm run bundle            build the linux/aarch64 Lambda bundle
    npm run synth             cdk synth
    npm run diff              cdk diff
    npm run deploy            bundle + cdk deploy --all
    npm run destroy           tear down (the raw S3 bucket is RETAIN by design)

  OPERATE
    npm run status            health of a deployed stack, one screen
    npm run api-check         can a consumer read the query API? signed GET, any account
    npm run api-check -- --all every unparameterized route, not just /health
    npm run logs              live tail both functions
    npm run dlq               depth of both dead-letter queues (should be zero)
    npm run dlq-peek          show what is in them, non-destructively
    npm run dlq-replay        re-normalize dead letters from the original S3 bytes
    npm run dlq-purge         discard all dead letters (asks for confirmation)

  SPATIAL DATABASE
    npm run db-info           which account/cluster/secret do the scripts resolve to?
    npm run db                smoke test: postgis, row counts, LRS invariant
    npm run db-check          every read-only check: inventory, geometry, NBI, landmarks
    npm run db-migrate-plan   what a migration WOULD apply. Changes nothing.
    npm run db-migrate        apply pending sql/ migrations via the in-VPC Lambda
    npm run db-history        what has been applied, and when

  REBUILD SOURCE DATA (network, no AWS account)
    npm run corridor          rebuild the I-40 centerline from state ARNOLD/LRS
    npm run nbi               rebuild NBI clearances from FHWA's annual files
    npm run nbi-plan          what the NBI load WOULD contain. Writes no file.

  Override the interpreter with PYTHON=... npm run <script>
  Full list: npm run

EOF
