"""One Postgres connection, shared by everything that needs one.

Extracted from ``handlers/db_migrate.py``, which was the only DB client until the
conflator arrived. Two clients meant two chances to get the TLS posture, the
credential path, or the endpoint resolution subtly different - and the one that is
wrong would be the one nobody exercised.

CACHED PER CONTAINER, not per call. A Lambda container serves many invocations, and
a fresh connection per record would spend more time in TLS and authentication than
in the query. The cache is deliberately not a POOL: one connection per container,
which is what a single-threaded handler can use, and what keeps the count
proportional to concurrency rather than to concurrency times pool size.

    THE CONNECTION LIMIT IS THE THING TO WATCH. Aurora Serverless v2 at the 0.5 ACU
    floor allows a low hundreds of connections, and Lambda concurrency is not
    bounded by anything here. At this project's cadence - five sources on 60-300s
    timers - concurrency stays in single digits and this is fine. It stops being
    fine the moment something fans out, and the fix then is RDS Proxy rather than a
    bigger number here. Recorded because the failure mode is "too many connections"
    under exactly the load you were hoping to handle.
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
from typing import Any

from .awsclients import client as aws_client
from .config import rds_ca_bundle

_secrets = aws_client("secretsmanager")

#: One connection per container. Reset with ``reset()`` in tests.
_connection: Any | None = None


def connect() -> Any:
    """The connection, opening it on first use.

    Credentials come from Secrets Manager (ADR 0004) and never from an environment
    variable. Host and port DO come from env, wired by CDK from the cluster's own
    endpoint: they are not secret, and taking them from there rather than from the
    secret body means a replaced cluster cannot be silently reached at a stale
    endpoint recorded inside the secret.
    """
    global _connection
    if _connection is not None and _alive(_connection):
        return _connection
    _connection = _open()
    return _connection


def reset() -> None:
    """Drop the cached connection. For tests, and for a handler that saw an error
    it does not want the next invocation to inherit."""
    global _connection
    if _connection is not None:
        # A close that fails on a connection we are discarding anyway changes nothing.
        with contextlib.suppress(Exception):
            _connection.close()
    _connection = None


def _alive(connection: Any) -> bool:
    """A cached connection can be dead: Aurora restarts, idle timeouts, failover.

    Checked with a round trip rather than assumed, because the alternative is one
    failed invocation per container death - and those look like random handler
    errors rather than a connection problem.
    """
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()
        connection.commit()
        return True
    except Exception:  # noqa: BLE001 - any failure means reopen
        return False


def _open() -> Any:
    try:
        import pg8000.dbapi
    except ImportError as exc:  # pragma: no cover - a bundle problem, not a logic one
        raise RuntimeError(
            "pg8000 is not in the bundle. It is a runtime dependency - run: npm run bundle"
        ) from exc

    secret_arn = os.environ.get("SPATIAL_DB_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError(
            "SPATIAL_DB_SECRET_ARN is not set. A function that needs the spatial "
            "database must be granted the secret and given the endpoint - see "
            "lib/spatial-stack.ts."
        )

    secret = json.loads(_secrets.get_secret_value(SecretId=secret_arn)["SecretString"])
    host = os.environ.get("SPATIAL_DB_HOST") or secret.get("host")
    port = int(os.environ.get("SPATIAL_DB_PORT") or secret.get("port") or 5432)
    database = os.environ.get("SPATIAL_DB_NAME") or secret.get("dbname")
    if not host or not database:
        raise RuntimeError(
            "no host or database name - set SPATIAL_DB_HOST / SPATIAL_DB_NAME, or "
            "attach the secret to the cluster so it carries them"
        )

    return pg8000.dbapi.connect(
        user=secret["username"],
        password=secret["password"],
        host=host,
        port=port,
        database=database,
        ssl_context=ssl_context(),
        timeout=int(os.environ.get("SPATIAL_DB_CONNECT_TIMEOUT", "10")),
        application_name=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "corridor-event-hub"),
    )


#: The escape hatch, and it is deliberately awkward to reach. See ``ssl_context()``.
_INSECURE_ENV = "SPATIAL_DB_TLS_INSECURE"

_RDS_TRUSTSTORE = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"


def ssl_context() -> ssl.SSLContext:
    """TLS to Aurora. VERIFIED BY DEFAULT - which it was not, and that was a security finding.

    AN EXPLICIT CONTEXT IS PASSED, and that part was always right: pg8000's default
    (``ssl_context=None``) tries TLS and SILENTLY FALLS BACK TO PLAINTEXT if the
    server does not offer it. Passing a context makes the same refusal an error, so
    the one outcome that cannot happen is an unnoticed unencrypted connection.

    WHAT CHANGED, AND WHY IT IS THE WHOLE POINT. This function used to disable
    hostname checking and certificate verification whenever ``SPATIAL_DB_CA_BUNDLE``
    was unset - which was the common case, because it was optional. The traffic was
    encrypted and UNAUTHENTICATED: anything that could answer on 5432 could serve the
    corridor. Nothing failed, nothing warned, and the fallback was reached by doing
    nothing at all. An insecure-by-default fallback is precisely the pattern that
    survives silently into whatever is built on top of a reference architecture, so
    the default is now inverted: verification is ON, and turning it off takes a
    deliberate act that leaves a trace.

    THE RDS CAs ARE SELF-SIGNED PRIVATE ROOTS, which is the fact the whole ordering
    turns on and the one worth checking rather than assuming:
    ``Amazon RDS us-west-2 Root CA RSA2048 G1`` is its own issuer, and the Lambda
    image's trust store carries ``Amazon Root CA 1`` but not that. So "just verify
    against the platform trust store" is not a working default for Aurora - it would
    fail every connection. The CA has to come from somewhere, and
    ``scripts/build-lambda.sh`` fetches it into the bundle.

    Four outcomes, in order:

    1. ``SPATIAL_DB_CA_BUNDLE`` names a readable PEM -> verify against it. The explicit
       override, and the escape hatch for a rotated CA or a non-RDS Postgres.

       A CA bundle that is SET BUT UNREADABLE raises rather than falling back. It used
       to fall back, which made a typo in the path indistinguishable from a considered
       decision not to verify - the worst possible reading of an operator's intent.

    2. The bundled RDS chain is present -> verify against it. **This is the deployed
       path**, and it needs no configuration, which is the point: a control that has to
       be switched on is a control that is off. Absent from a local checkout that has
       never run ``npm run bundle``, hence case 3.

    3. Neither -> ``create_default_context()`` with verification and hostname checking
       left ON, against the platform trust store. Correct for a local Postgres with a
       publicly-trusted certificate, and it FAILS LOUDLY against Aurora with a
       verification error rather than downgrading. Failing there is the behaviour being
       bought; the old code answered the same situation by connecting anyway.

    4. ``SPATIAL_DB_TLS_INSECURE`` explicitly truthy -> the old unverified context.
       For a local Postgres with a self-signed certificate, which is a real need and
       the reason this is not simply deleted. Checked LAST so it cannot pre-empt a
       working verified path, and REFUSED INSIDE LAMBDA - if
       ``AWS_LAMBDA_FUNCTION_NAME`` is set, the variable raises instead of applying,
       so the escape hatch cannot be the thing a deployed function is quietly using.
       Setting it in the stack is then a change that fails at invoke rather than one
       nobody notices.
    """
    ca_bundle = os.environ.get("SPATIAL_DB_CA_BUNDLE")
    if ca_bundle:
        if not os.path.exists(ca_bundle):
            raise RuntimeError(
                f"SPATIAL_DB_CA_BUNDLE points at {ca_bundle!r}, which does not exist. "
                "Refusing to connect unverified: a bad path and a decision not to "
                "verify are different things, and this used to treat them the same. "
                f"Fetch the bundle with: curl -o {ca_bundle} {_RDS_TRUSTSTORE}"
            )
        return ssl.create_default_context(cafile=ca_bundle)

    bundled = rds_ca_bundle()
    if bundled is not None:
        return ssl.create_default_context(cafile=str(bundled))

    if _insecure_requested():
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            raise RuntimeError(
                f"{_INSECURE_ENV} is set on a deployed function. It exists for a local "
                "Postgres with a self-signed certificate and is refused here, because "
                "an unauthenticated connection to the corridor database is not a "
                "posture a deployment should be able to reach by setting a variable. "
                f"Set SPATIAL_DB_CA_BUNDLE instead - see {_RDS_TRUSTSTORE}"
            )
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    # Verification on, against whatever the platform trusts. Against Aurora this fails
    # rather than downgrading, and the error names the CA. That is the point of the change.
    return ssl.create_default_context()


def _insecure_requested() -> bool:
    """Truthy means truthy, not merely present.

    ``SPATIAL_DB_TLS_INSECURE=false`` must not disable verification, and an env var
    read as ``bool(os.environ.get(...))`` does exactly that - the string "false" is
    true. A footgun on a variable whose entire job is to turn a security control off.
    """
    return os.environ.get(_INSECURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def tls_posture() -> str:
    """One line for the migration report and ``npm run db-check``.

    Reported on every run rather than left to be discovered, and it is the string
    that made the problem findable: the code said "certificate NOT verified" out loud and had
    been saying it for weeks.
    """
    ca_bundle = os.environ.get("SPATIAL_DB_CA_BUNDLE")
    if ca_bundle:
        return f"encrypted, certificate verified against {ca_bundle}"
    bundled = rds_ca_bundle()
    if bundled is not None:
        return f"encrypted, certificate verified against the bundled RDS chain ({bundled.name})"
    if _insecure_requested():
        return (
            f"encrypted, certificate NOT verified - {_INSECURE_ENV} is set "
            "(local development only; refused inside Lambda)"
        )
    return "encrypted, certificate verified against the platform trust store"
