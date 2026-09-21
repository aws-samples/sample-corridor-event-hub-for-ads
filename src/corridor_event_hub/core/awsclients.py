"""Every boto3 client this project builds, with the AWS Solutions user agent attached.

WHY THIS EXISTS: onboarding a solution to AWS requires that each AWS SDK call carry
``AWSSOLUTION/<id>/<version>`` in its User-Agent header, which is how AWS attributes
service API usage back to the solution. botocore adds anything in
``Config(user_agent_extra=...)`` to the header it already sends, so the requirement is
satisfied by building every client through here rather than through ``boto3`` directly:

    BOTO3/1.35.0 PYTHON/3.13.1 LINUX/... BOTOCORE/1.35.0 AWSSOLUTION/SO0358/v1.0.0

ONE FUNNEL, NOT A CONVENTION. Eight call sites across handlers, the event store, the
database connection and the local dev tools used to call ``boto3.client`` themselves,
and a ninth added later would have been untagged with nothing to catch it.
``scripts/check-solution-id.sh`` fails the build on any direct ``boto3.client`` /
``boto3.resource`` / ``boto3.session.Session`` under ``corridor_event_hub``, so the funnel is
enforced rather than remembered.

THE VALUE COMES FROM THE ENVIRONMENT, with a compiled-in fallback. Every deployed
Lambda receives ``SOLUTION_USER_AGENT`` from its template, where the string lives in a
CloudFormation mapping (``lib/solution.ts``) so a release bumps it in one place. The
fallback exists for the local tools - ``npm run trace``, ``npm run probe`` - which run
the same modules with no CloudFormation environment at all, and would otherwise make
untagged calls. That means the version string exists twice, in TypeScript and here,
which is exactly the drift ``scripts/check-solution-id.sh`` asserts against.

WHAT IS DELIBERATELY NOT ROUTED THROUGH HERE. ``scripts/lib/sigv4_get.py`` signs
requests by hand with ``urllib`` and ``adapters/feeds.py`` shells out to the ``aws``
CLI for one probe path; neither is an SDK call from the solution's runtime, and both are
developer tooling rather than deployed code. ``boto3.dynamodb.types`` is imported
directly where needed: those are type converters, not clients.

boto3 is imported INSIDE the functions, not at module scope. It is a dev-only
dependency here because the Lambda runtime provides it, and ``core.eventstore``,
``core.cloud`` and ``adapters.feeds`` defer their own imports for that reason - a
module-scope import here would undo that for every local tool that imports them.
"""

from __future__ import annotations

import os
from typing import Any

#: The AWS Solutions catalog ID for Corridor Event Hub for ADS. Must match SOLUTION_ID in lib/solution.ts.
SOLUTION_ID = "SO0358"

#: The published solution version. Must match SOLUTION_VERSION in lib/solution.ts.
#: NOT the version in pyproject.toml - that versions the Python package, this versions
#: the solution as published, and tying them together would republish the solution on a
#: dependency bump.
SOLUTION_VERSION = "v1.0.0"

#: The fallback user agent, used when the environment does not carry one. Format is
#: fixed by the onboarding requirement: ``AWSSOLUTION/$solutionId/$solutionVersion``.
DEFAULT_USER_AGENT = f"AWSSOLUTION/{SOLUTION_ID}/{SOLUTION_VERSION}"

#: Where a deployed function reads the string from. Set by CDK from the stack's
#: ``Solution`` mapping. Deliberately NOT ``CEH_USER_AGENT``, which is the outbound
#: HTTP User-Agent this pipeline sends to state DOT feeds (adapters/feeds.py) and has
#: nothing to do with AWS attribution.
USER_AGENT_ENV_VAR = "SOLUTION_USER_AGENT"


def solution_user_agent() -> str:
    """The string to append to the SDK's User-Agent.

    The environment wins so that the deployed template is authoritative: bumping the
    solution version is a mapping change, and a Lambda that was deployed from an older
    template keeps reporting the version it was actually deployed from.
    """
    return os.environ.get(USER_AGENT_ENV_VAR) or DEFAULT_USER_AGENT


def solution_config(config: Any = None) -> Any:
    """A ``botocore.config.Config`` carrying the solution user agent.

    MERGES rather than replaces a caller-supplied config. A future timeout or retry
    setting is the likeliest reason someone passes one, and the merge is what stops
    that change from silently dropping the attribution - which fails nothing, breaks
    nothing, and is invisible until AWS reports no usage.

    A caller who set ``user_agent_extra`` themselves keeps it: botocore's ``merge``
    would otherwise let one of the two win, and both are things somebody deliberately
    asked to appear in the header.
    """
    from botocore.config import Config

    if config is None:
        return Config(user_agent_extra=solution_user_agent())

    theirs = getattr(config, "user_agent_extra", None)
    extra = f"{theirs} {solution_user_agent()}" if theirs else solution_user_agent()
    return config.merge(Config(user_agent_extra=extra))


def client(service: str, **kwargs: Any) -> Any:
    """``boto3.client``, with the solution user agent. Use this, never boto3 directly."""
    import boto3

    return boto3.client(service, config=solution_config(kwargs.pop("config", None)), **kwargs)


def resource(service: str, **kwargs: Any) -> Any:
    """``boto3.resource``, with the solution user agent.

    Only the collector uses the resource interface (for the DynamoDB ``Table`` helper);
    everything else uses ``client``. Tagged the same way regardless, because the header
    is a property of the call rather than of the interface that made it.
    """
    import boto3

    return boto3.resource(service, config=solution_config(kwargs.pop("config", None)), **kwargs)


def session() -> Any:
    """A plain ``boto3.session.Session``, for reading resolved configuration.

    No config attached, and none needed: this is used to ask what region the
    environment resolves to (core/cloud.py), which is a local lookup that makes no
    service call. Routed through here anyway so the funnel has no exceptions to
    remember - a client built off a session must still be built by ``client`` above.
    """
    import boto3

    return boto3.session.Session()
