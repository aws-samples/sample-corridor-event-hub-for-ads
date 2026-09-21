"""Where the feed list lives, so local tools cannot drift.

WHY THIS FILE EXISTS: the target list (URL, headers, adapter, key resolution) used
to be inline in ``probe.py``. The moment a second tool needs to run the same
adapters over the same feeds, an inline list becomes two lists that disagree, and
then one tool shows something the other does not. Same reason the probe already
cross-checks itself against the adapter registry.

PORTABILITY NOTE: this file names agencies, endpoints and states, so it lives in
``adapters/`` for exactly the reason ``registry.py`` does - that directory is where
state-specific knowledge is SUPPOSED to live, and
``check-portability.sh`` exempts it by design. In ``core/`` this file would
(correctly) fail the build.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from ..core.config import load_json
from .adapter import Adapter
from .registry import ADAPTERS

# A User-Agent is sent to EVERY feed, not only where a source demands one.
#
# 1. NWS policy requires an identifying User-Agent with contact info; requests
#    without one may be blocked outright.
#
# 2. ``urllib`` defaults to ``Python-urllib/3.x``, and Cloudflare-fronted agency
#    endpoints reject that outright - oktraffic.org returns 403 with body
#    ``error code: 1010`` for it while serving the identical URL to curl. Nothing
#    about the URL or the token is wrong; the default UA alone is enough to make an
#    entire state's feed vanish behind a 403 that reads like an auth failure. The
#    deployed collector sends one for the same reason.
USER_AGENT = os.environ.get(
    "CEH_USER_AGENT", "corridor-event-hub-prototype (contact: set CEH_USER_AGENT)"
)

# Sent to every feed. `Accept` is per-target because only the GeoJSON feeds want it.
BASE_HEADERS = {"User-Agent": USER_AGENT}

SECRET_LOOKUP_TIMEOUT_SECONDS = 15


@dataclass
class FeedTarget:
    source_id: str
    label: str
    url: str
    adapter: Adapter
    # Local captured payload, used when a key is absent (replay path).
    fixture: str
    # Where the credential came from, for honest reporting.
    key_source: str
    # False when a required key is missing, so live fetch must be skipped.
    live: bool
    # Why the key lookup failed, when it did. Surfaced verbatim by the tools so a
    # missing credential explains itself instead of pointing at a generic fix.
    key_error: str | None = None
    # Env var that supplies the key, so tools can print an actionable note.
    env_var: str | None = None
    # Secrets Manager id, the other way to supply the key (ADR 0004).
    secret_id: str | None = None
    headers: dict | None = None
    # Custom fetcher for sources that are not a single URL GET.
    #
    # WHY THIS SEAM EXISTS: the tiled source is not one request. It is N requests
    # whose addresses are derived from the corridor geometry, authenticated with
    # SigV4 rather than a key in the query string. Expressing that as a URL string
    # would be a fiction, and special-casing it inside the probe would put
    # source-specific fetch logic into a tool whose whole point is treating every
    # source alike. A callable returning the same ``(status, body)`` a URL GET
    # returns keeps all callers uniform.
    fetcher: Callable[[], tuple[int, str]] | None = None

    def __post_init__(self) -> None:
        require_https(self.url, self.source_id)


def require_https(url: str, context: str) -> str:
    """Reject any feed address that is not HTTPS. Returns the url, so it can wrap.

    WHY THIS IS ENFORCED RATHER THAN ASSUMED: every address in this file and in
    ``sources.json`` is https today, and a credential is appended to three of them as
    a query parameter. ``urllib.request.urlopen`` will just as happily open ``http://``
    - putting a key on the wire in cleartext - or ``file://`` and ``ftp://``, which
    would turn one edited catalog line into a local-file read that still looks like a
    feed fetch. That is what bandit B310 warns about, and the warning is only a false
    positive while something actually checks. This is that something.

    Not a TLS verification setting: urllib already verifies certificates by default.
    This only settles which scheme is allowed to be attempted at all.
    """
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(
            f"{context}: feed addresses must be https, got {url!r}. A credential is "
            f"appended to some of these, so a non-TLS scheme would publish it."
        )
    return url


def resolve_key(env_var: str, secret_id: str) -> tuple[str, str | None]:
    """Resolve a feed credential. Returns ``(key, reason_it_failed)``.

    Order:

    1. an env var - works with no AWS credentials at all
    2. Secrets Manager via the AWS CLI - so anyone with deploy credentials gets the
       source without having to know the key exists

    Never committed either way (ADR 0004).

    Shelling out to the CLI rather than importing boto3 keeps the "no AWS account
    needed" promise intact: if credentials are absent this fails and the source falls
    back to its fixture.

    WHY THIS RETURNS A REASON. It used to be a bare ``except Exception: return ""``,
    which collapsed four completely different problems into one message that named
    none of them:

      - ``aws`` not on PATH (the common one - a Makefile shell does not inherit a
        VS Code terminal's PATH, so the CLI is missing even though it works in the
        terminal you tested from)
      - no AWS_PROFILE / expired SSO session
      - the secret does not exist in this account or region
      - the caller lacks secretsmanager:GetSecretValue

    All four rendered as "no key available (set TX_DOT_KEY or grant read on ...)",
    which points at the two fixes LEAST likely to be the actual cause. Distinguishing
    them costs a few lines and turns a puzzle into an instruction.
    """
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env, None

    try:
        completed = subprocess.run(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                secret_id,
                "--query",
                "SecretString",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
            timeout=SECRET_LOOKUP_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "", (
            "the `aws` CLI is not on PATH for this process. A Makefile or IDE-launched"
            " shell often has a narrower PATH than your terminal - try"
            f" `export {env_var}=<key>` instead, or start the server from a shell where"
            " `aws` resolves."
        )
    except subprocess.TimeoutExpired:
        return "", (
            f"the `aws` CLI did not respond within {SECRET_LOOKUP_TIMEOUT_SECONDS}s"
            " (an expired SSO session can hang waiting for a login)."
        )
    except OSError as exc:
        return "", f"could not run the `aws` CLI: {exc}"

    if completed.returncode == 0:
        out = completed.stdout.strip()
        if out and out != "None":
            return out, None
        return "", f"secret {secret_id} resolved but is empty."

    stderr = " ".join(completed.stderr.split())
    lowered = stderr.lower()

    # A profile using `credential_process = <helper> ...` makes the AWS CLI shell out to
    # that helper. When the HELPER is missing, `aws` itself runs fine and reports
    # `[Errno 2] No such file or directory: '<helper>'` - which looks like a missing AWS
    # CLI and is not. Checked before the generic credential cases because its message
    # would otherwise be swallowed by them.
    if "no such file or directory" in lowered and "credential" not in lowered:
        missing = stderr.rsplit(":", 1)[-1].strip().strip("'\"")
        return "", (
            f"the AWS profile's credential_process helper is not on PATH: {missing}."
            f" Add its directory to PATH, or set {env_var} directly."
        )
    if "resourcenotfound" in lowered:
        profile = os.environ.get("AWS_PROFILE", "<default>")
        region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "<default>"
        return "", (
            f"secret {secret_id} does not exist in this account/region"
            f" (profile {profile}, region {region})."
        )
    if "accessdenied" in lowered or "not authorized" in lowered:
        return "", f"not authorized to read {secret_id} with the current credentials."
    if (
        "unable to locate credentials" in lowered
        or "expiredtoken" in lowered
        or "sso session" in lowered
        or "invalidclienttokenid" in lowered
    ):
        profile = os.environ.get("AWS_PROFILE")
        hint = (
            f"AWS_PROFILE={profile} is set but its credentials are missing or expired"
            if profile
            else "AWS_PROFILE is not set for this process"
        )
        return "", f"{hint} - {stderr[:160]}"
    return "", f"`aws secretsmanager get-secret-value` failed: {stderr[:200]}"


def _key_source_label(env_var: str, key: str) -> str:
    if os.environ.get(env_var):
        return f"{env_var} env"
    return "Secrets Manager" if key else "not available"


def catalog_entry(source_id: str) -> dict:
    """One source catalog entry, or {} if absent.

    Public because more than one local tool needs it: the strip export carries
    licence terms into its published artifact, and re-reading sources.json in each
    tool is how two tools end up disagreeing about a source.
    """
    for source in load_json("sources.json")["sources"]:
        if source["sourceId"] == source_id:
            return source
    return {}


def aws_credentials_available() -> bool:
    """Whether boto3 can find credentials, without making a network call.

    Used to decide ``live`` for the IAM-authenticated source. boto3 is a dev
    dependency (it ships with the Lambda runtime), so its absence is a normal
    state for a local checkout and means the same thing as absent credentials:
    fall back to the fixture and say so.
    """
    try:
        import botocore.session
    except ImportError:
        return False
    try:
        return botocore.session.get_session().get_credentials() is not None
    except Exception:  # noqa: BLE001 - a broken profile is the same as no profile
        return False


def fetch_traffic_tiles(region: str, zoom: int) -> tuple[int, str]:
    """Fetch the tiles covering the corridor and wrap them in one JSON envelope.

    Returns the same ``(status, body)`` a URL GET returns, so the probe and the
    collector treat this source like any other.

    WHY AN ENVELOPE: tiles are binary and every other stage of this pipeline - the
    raw S3 object, the fixture, the replay path - is text. One JSON object
    carrying base64 tiles WITH their z/x/y addresses keeps the raw-bytes contract
    intact. The addresses are not optional metadata: MVT coordinates are local to
    a tile, so bytes without an address cannot be georeferenced at all.
    """
    import base64

    # core.awsclients pulls in boto3, a dev dependency (the Lambda runtime provides
    # it), and core.lrs reads corridor.json at import time. Both are deferred so
    # importing this module - which every local tool does - costs neither.
    from ..core.awsclients import client as aws_client
    from ..core.lrs import corridor
    from ..core.tiles import tiles_covering_line

    client = aws_client("geo-maps", region_name=region)
    addresses = tiles_covering_line(corridor.centerline, zoom)

    tiles = []
    for address in addresses:
        try:
            body = client.get_tile(
                Tileset="vector.traffic",
                Z=str(address.z),
                X=str(address.x),
                Y=str(address.y),
            )["Blob"].read()
        except Exception as exc:  # noqa: BLE001 - one bad tile must not lose the rest
            tiles.append(
                {"z": address.z, "x": address.x, "y": address.y, "error": str(exc)[:200]}
            )
            continue

        tiles.append(
            {
                "z": address.z,
                "x": address.x,
                "y": address.y,
                "bytes": len(body),
                "mvtBase64": base64.b64encode(body).decode("ascii"),
            }
        )

    fetched = [t for t in tiles if "mvtBase64" in t]
    # No tile at all is a failed fetch, not an empty result. Reporting 200 with an
    # empty envelope would look like "the corridor is clear".
    status = 200 if fetched else 0
    return status, json.dumps(
        {
            "tileset": "vector.traffic",
            "zoom": zoom,
            "region": region,
            "tiles": tiles,
        }
    )


def feed_targets() -> list[FeedTarget]:
    """Every feed the local tools exercise.

    Sources needing a key are still listed when the key is absent, with
    ``live=False`` - so a tool can fall back to fixtures and SAY it did, rather than
    silently omitting a state.
    """
    # Oklahoma resolves like every other credentialed feed now. It used
    # to read a literal out of the catalog, which meant this one source was live for
    # anyone who cloned the repo and the other two were not - a difference that looked
    # like Oklahoma being easier rather than like a token being committed.
    ok_token, ok_token_error = resolve_key("OK_ODOT_TOKEN", "corridor-event-hub/ok-odot-wzdx-token")
    tx_key, tx_key_error = resolve_key("TX_DOT_KEY", "corridor-event-hub/tx-dot-wzdx-key")
    az_key, az_key_error = resolve_key("AZ511_KEY", "corridor-event-hub/az511-key")

    # Zoom is catalog configuration, not a constant here: it is a property of what
    # the source publishes where, and the catalog explains the 4^zoom cost tradeoff.
    traffic_catalog = catalog_entry("aws-location-traffic")
    traffic_zoom = int(traffic_catalog.get("tileZoom") or 8)
    traffic_region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    has_aws = aws_credentials_available()

    return [
        FeedTarget(
            source_id="ok-odot-wzdx",
            label="Oklahoma ODOT WZDx (work zones)",
            url=f"https://oktraffic.org/api/Geojsons/workzones?access_token={ok_token}",
            adapter=ADAPTERS["ok-odot-wzdx"],
            fixture="ok-odot-wzdx.json",
            key_source=_key_source_label("OK_ODOT_TOKEN", ok_token),
            key_error=ok_token_error,
            live=bool(ok_token),
            env_var="OK_ODOT_TOKEN",
            # A Secrets Manager secret NAME, not a credential - the value is
            # resolved at runtime by resolve_key() above. Same for the two below.
            secret_id="corridor-event-hub/ok-odot-wzdx-token",  # nosec B106
            headers=dict(BASE_HEADERS),
        ),
        FeedTarget(
            source_id="tx-dot-wzdx",
            label="TxDOT DriveTexas WZDx (work zones, WZDx 4.2)",
            url=f"https://api.drivetexas.org/api/conditions.wzdx.geojson?key={tx_key}",
            adapter=ADAPTERS["tx-dot-wzdx"],
            fixture="tx-dot-wzdx.json",
            key_source=_key_source_label("TX_DOT_KEY", tx_key),
            key_error=tx_key_error,
            live=bool(tx_key),
            env_var="TX_DOT_KEY",
            secret_id="corridor-event-hub/tx-dot-wzdx-key",  # nosec B106
            headers=dict(BASE_HEADERS),
        ),
        FeedTarget(
            source_id="az511-events",
            label="AZ511 events (incident + closure + work zone)",
            url=f"https://az511.gov/api/v2/get/event?key={az_key}&format=json",
            adapter=ADAPTERS["az511-events"],
            fixture="az511-events.json",
            key_source=_key_source_label("AZ511_KEY", az_key),
            key_error=az_key_error,
            live=bool(az_key),
            env_var="AZ511_KEY",
            secret_id="corridor-event-hub/az511-key",  # nosec B106
            headers=dict(BASE_HEADERS),
        ),
        FeedTarget(
            source_id="nws-alerts",
            label="NWS active alerts (weather + road surface)",
            url="https://api.weather.gov/alerts/active?area=AZ,NM,TX,OK",
            headers={**BASE_HEADERS, "Accept": "application/geo+json"},
            adapter=ADAPTERS["nws-alerts"],
            fixture="nws-alerts.json",
            key_source="none required",
            live=True,
        ),
        FeedTarget(
            source_id="nm-dot-weathershare",
            label="NMDOT road conditions via WeatherShare OSS (closure + work zone)",
            url="https://oss.weathershare.org/data/ROADINFO/OSS_roadinfo.json",
            adapter=ADAPTERS["nm-dot-weathershare"],
            fixture="nm-dot-weathershare.json",
            key_source="none required",
            live=True,
            headers=dict(BASE_HEADERS),
        ),
        FeedTarget(
            source_id="aws-location-traffic",
            label=(
                f"Amazon Location traffic tiles (congestion + incidents, z{traffic_zoom})"
            ),
            url=(
                f"https://maps.geo.{traffic_region}.amazonaws.com"
                f"/v2/tiles/vector.traffic/{{z}}/{{x}}/{{y}}"
            ),
            adapter=ADAPTERS["aws-location-traffic"],
            fixture="aws-location-traffic.json",
            # No key to resolve: this is the one source authenticated by the same
            # IAM identity that deploys the stack (ADR 0004).
            key_source="AWS IAM (SigV4)" if has_aws else "no AWS credentials",
            live=has_aws,
            fetcher=lambda: fetch_traffic_tiles(traffic_region, traffic_zoom),
        ),
    ]
