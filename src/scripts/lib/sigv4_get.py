#!/usr/bin/env python3
"""Signed GET against an IAM-authorized endpoint, using the standard library only.

    python3 scripts/lib/sigv4_get.py https://<id>.execute-api.<region>.amazonaws.com/health

Prints one line per URL. Called by scripts/api-check.sh, and usable on its own when
all you have is a URL and a set of credentials in the environment.

The exit code answers "is this endpoint healthy", NOT "was every request successful".
2xx and 400 both exit 0, because a 400 proves the request was authorized, reached the
handler, and was parsed - a route that validates its parameters and says so is
working. 401/403/404, 5xx and transport failures exit 1. Without that distinction a
health check over every route reports FAILED for an API that is entirely fine, which
is the kind of false alarm that gets a check ignored.

WHY NOT `curl --aws-sigv4`, which is the obvious one-liner:

  - It needs curl 7.75 (2021). Amazon Linux 2 ships 7.61 and CentOS 7 ships 7.29,
    so the command that works on a laptop fails on a bastion with
    `option --aws-sigv4: is unknown` - which reads like a typo, not a version floor.
  - It takes the secret key as `--user KEY:SECRET`, putting a live credential in the
    process list, where `ps` hands it to every other user on the host.
  - The signing region is a literal in the provider string (`aws:amz:<region>:...`).
    Get it wrong and the API returns 403 with no hint that the region was the
    problem. Here the region is derived from the API's own hostname, so it cannot
    disagree with the endpoint being called.

WHY NOT boto3: it is a dev-only dependency in this project because the Lambda
runtime provides it (see pyproject.toml), so a fresh checkout does not have it - and
working in an environment nobody has set up yet is the whole point of this script.
SigV4 for a GET with no payload is thirty lines of hmac. botocore is still used if
it happens to be importable, so a profile-only shell works too.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "execute-api"
ALGORITHM = "AWS4-HMAC-SHA256"
TIMEOUT_SECONDS = 15
# Enough to see `{"status": "ok", "route": ...}` or an error message, not enough for a
# WZDx feed to fill the terminal.
BODY_PREVIEW_CHARS = 160


def credentials() -> tuple[str, str, str | None, str]:
    """Returns ``(access_key, secret_key, session_token, where_it_came_from)``.

    Environment first, because that is how a CI runner and a container hand out
    credentials - and because an exported key must win over a
    profile here for the same reason it wins inside the AWS SDKs. Getting that order
    backwards would make this tool disagree with `aws sts get-caller-identity`,
    which is exactly the confusion it exists to resolve.
    """
    key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key and secret:
        return key, secret, os.environ.get("AWS_SESSION_TOKEN"), "environment"

    try:
        import botocore.session
    except ImportError:
        raise SystemExit(
            "no credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set and\n"
            "botocore is not importable, so a profile cannot be read either.\n"
            "Export the two variables (plus AWS_SESSION_TOKEN if they are temporary),\n"
            "or run this through scripts/api-check.sh, which resolves a profile for you."
        ) from None

    resolved = botocore.session.get_session().get_credentials()
    if resolved is None:
        raise SystemExit(
            "no credentials: nothing in the environment, and botocore found no usable\n"
            "profile or instance role. Export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
    frozen = resolved.get_frozen_credentials()
    return frozen.access_key, frozen.secret_key, frozen.token, "botocore (profile or role)"


def region_from_host(host: str) -> str | None:
    """`<id>.execute-api.<region>.amazonaws.com` -> `<region>`.

    The signature is scoped to a region, and a mismatch is indistinguishable from an
    authorization failure at the wire. Reading it off the host makes the mismatch
    unrepresentable rather than merely unlikely.
    """
    parts = host.split(".")
    if len(parts) >= 5 and parts[1] == SERVICE:
        return parts[2]
    return None


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_request(url: str, region: str, key: str, secret: str, token: str | None) -> dict[str, str]:
    """SigV4 headers for an unsigned-payload GET (AWS SigV4 signing process, task 1-4)."""
    split = urllib.parse.urlsplit(url)
    canonical_uri = urllib.parse.quote(split.path or "/", safe="/-_.~")
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    headers = {"host": split.netloc, "x-amz-date": amz_date}
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in sorted(headers))

    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            hashlib.sha256(b"").hexdigest(),
        ]
    )
    scope = f"{datestamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _sign(
        _sign(_sign(_sign(f"AWS4{secret}".encode(), datestamp), region), SERVICE),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"{ALGORITHM} Credential={key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def get(url: str, region: str | None) -> tuple[int | None, int, str]:
    """Returns ``(http_status, latency_ms, body_or_error)``. Never raises for a bad status."""
    key, secret, token, _ = credentials()
    split = urllib.parse.urlsplit(url)
    # Same rule as adapters/feeds.py require_https, for the same reason: urlopen will
    # just as happily accept `http://` - putting a signed Authorization header on the
    # wire in cleartext - or `file://`, which would turn a mistyped API_URL into a
    # local file read that still prints like an HTTP result.
    if split.scheme != "https":
        raise SystemExit(f"refusing to sign a non-https address: {url!r}")
    host = split.netloc
    signing_region = region or region_from_host(host)
    if not signing_region:
        raise SystemExit(
            f"cannot tell which region to sign for from host {host!r} - pass --region <region>."
        )

    request = urllib.request.Request(
        url, headers=sign_request(url, signing_region, key, secret, token), method="GET"
    )
    started = time.monotonic()
    try:
        # The scheme is checked above, so only https reaches this line.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            body = response.read().decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
            return response.status, int((time.monotonic() - started) * 1000), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, int((time.monotonic() - started) * 1000), body
    except ssl.SSLCertVerificationError as exc:
        # The same failure the local feed tools hit when a machine has no usable CA
        # bundle. Named here because "certificate verify failed" against an AWS
        # endpoint reads like an AWS problem and is not.
        return (
            None,
            int((time.monotonic() - started) * 1000),
            f"TLS verification failed ({exc}). This machine has no usable CA bundle: try\n"
            "      pip install certifi && export SSL_CERT_FILE=$(python3 -m certifi)",
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised: the report is the product
        return None, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {exc}"


def _preview(body: str) -> str:
    """One line of the body. Pretty-printed JSON becomes unreadable across columns."""
    try:
        collapsed = json.dumps(json.loads(body), separators=(",", ":"))
    except ValueError:
        collapsed = " ".join(body.split())
    return collapsed[:BODY_PREVIEW_CHARS]


def main(argv: list[str]) -> int:
    region: str | None = None
    urls: list[str] = []
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--region":
            region = rest.pop(0) if rest else None
        else:
            urls.append(arg)

    if not urls:
        print(__doc__.strip().splitlines()[2].strip())
        return 2

    worst = 0
    for url in urls:
        status, latency_ms, body = get(url, region)
        path = urllib.parse.urlsplit(url).path or "/"
        shown = "---" if status is None else str(status)
        print(f"  {shown:>4}  {latency_ms:>5}ms  {path:<32} {_preview(body)}")
        # 400 counts as healthy: the handler rejected the request, which means
        # everything in front of the handler worked. See the docstring.
        if status is None or not (200 <= status < 300 or status == 400):
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
