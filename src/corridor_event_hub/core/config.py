"""Configuration loading.

The corridor and the source catalog are DATA, not code.
Adopting a different route or onboarding a source must not require a core code
change, so both live in ``config/`` and are read at import time.

WHY A RESOLVER RATHER THAN A FIXED PATH: the same package runs from a git
checkout (``src/corridor_event_hub``, config at ``src/config``) and from a Lambda
bundle (``/var/task/corridor_event_hub``, config at ``/var/task/config``). Both
layouts put ``config/`` BESIDE the package, so one relative path serves both -
but only as long as they agree, and nothing in a checkout notices when the
bundle stops agreeing. The resolver is what turns that into a loud failure with
both candidates named, plus an env override for the layouts neither guess fits
(``scripts/build-lambda.sh`` staging into a temp dir, a wheel install).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # .../corridor_event_hub


def _resolve_dir(name: str, env_var: str) -> Path:
    """Explicit override wins, then the beside-the-package layout both targets share."""
    override = os.environ.get(env_var)
    if override:
        return Path(override)

    candidates = (
        # Checkout is src/{corridor_event_hub,config}, the bundle is
        # /var/task/{corridor_event_hub,config} - one relative path serves both.
        _PACKAGE_DIR.parent / name,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    raise FileNotFoundError(
        f"{name}/ not found. Set {env_var}, or run from the src/ checkout. "
        f"Looked in: {', '.join(str(c) for c in candidates)}"
    )


def config_dir() -> Path:
    """Locate ``config/``. Explicit override wins, then beside the package."""
    return _resolve_dir("config", "CEH_CONFIG_DIR")


def sql_dir() -> Path:
    """Locate ``sql/`` - the schema migrations.

    Same problem as ``config/``, same answer. The migration Lambda reads these files
    from ``/var/task/sql`` and the test suite reads them from ``src/sql``, which are
    the same path relative to the package in both.
    """
    return _resolve_dir("sql", "CEH_SQL_DIR")


#: Fetched into the bundle by scripts/build-lambda.sh, never committed. Kept as a
#: constant because the build script writes this exact name and dbconn reads it.
RDS_CA_BUNDLE_NAME = "rds-global-bundle.pem"


def rds_ca_bundle() -> Path | None:
    """The Amazon RDS root CA chain, or ``None`` if it is not on disk.

    WHY THIS IS NEEDED AT ALL, since it looks like something TLS should just handle:
    the RDS certificate authorities are **self-signed private roots**. Checked, not
    assumed - `Amazon RDS us-west-2 Root CA RSA2048 G1` is its own issuer, and the
    Lambda image's trust store carries `Amazon Root CA 1` but not that. So verifying
    an Aurora certificate against the platform trust store CANNOT succeed, and a
    default that tried would fail every connection rather than fail safe.

    WHY IT IS FETCHED AT BUILD TIME AND NOT COMMITTED. It is 165 KB of third-party
    certificate data that AWS rotates on its own schedule; a copy in git is a copy
    that goes stale silently and gets reviewed in every diff it appears in.
    ``scripts/build-lambda.sh`` downloads it into the bundle beside ``config/`` and
    ``sql/``, and asserts it is there - so a bundle that could not verify TLS to the
    database fails the BUILD rather than every invocation.

    NOT fetched at invoke time, which was the other option: that would put a network
    dependency on an external host in the connection path, and this function's whole
    point is to make the database reachable rather than to add a new way to fail.

    RETURNS None RATHER THAN RAISING. What a missing bundle means is the caller's
    decision, and ``dbconn.ssl_context()`` makes it - deciding here would put TLS
    policy in the config module.
    """
    override = os.environ.get("CEH_CERTS_DIR")
    candidates = (
        [Path(override)]
        if override
        else [_PACKAGE_DIR.parent / "certs"]
    )
    for candidate in candidates:
        pem = candidate / RDS_CA_BUNDLE_NAME
        if pem.is_file():
            return pem
    return None


def load_json(name: str) -> dict[str, Any]:
    """Read one config artifact. Fails loudly - a missing corridor definition is
    not something to paper over with a default.
    """
    with (config_dir() / name).open(encoding="utf-8") as fh:
        return json.load(fh)
