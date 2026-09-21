"""Local dev API for the corridor strip UI.

    npm run ui        # this server + the Vite dev server
    npm run serve     # just this server, on :8787

WHAT THIS IS: a stand-in for the query API, which does not exist yet. It
runs the real adapters on request and returns the same document
``strip_export.build()`` produces, so the React app can be written against a
*fetch* rather than a baked-in snapshot. When the real API lands, the app changes
its base URL and this file is deleted.

WHAT THIS IS NOT: a deployment target. It is single-process, unauthenticated, and
binds to localhost only. The real query API belongs behind API Gateway, which is
where the deployed one sits; nothing here should be mistaken for that design.

Stdlib only, deliberately. Adding FastAPI/uvicorn to `pyproject.toml` would put a
web framework in the dependency closure of a package whose deployment artifact is
a Lambda bundle, and `scripts/build-lambda.sh` would start shipping it. The cost
is a hand-rolled handler; the benefit is that the Lambda bundle stays honest.

RATE LIMITS ARE THE REASON FOR THE CACHE. AZ511 allows ten requests per sixty
seconds and a browser polling every few seconds would blow through that in under a
minute, getting the key throttled - a self-inflicted outage during a demo. So a
response is cached and re-used until it is older than MIN_REFRESH_SECONDS, and a
forced refresh still cannot go faster than that. The UI shows the cached age
rather than pretending each poll is a fresh fetch.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import strip_export
from .core.serde import to_jsonable

DEFAULT_PORT = 8787

# Floor on how often the adapters may actually hit the network. The tightest
# published limit among the feeds is AZ511's 10 requests / 60s, and one build
# makes one request per source, so 20s leaves ample headroom while still feeling
# responsive in a browser.
MIN_REFRESH_SECONDS = 20.0


class StripCache:
    """One in-flight build at a time, with a minimum age before re-fetching.

    The lock matters even for a dev server: two browser tabs, or a poll landing on
    top of a manual refresh, would otherwise run the adapters concurrently and
    double every feed request - exactly what the rate limit forbids.
    """

    def __init__(self, fixtures_only: bool) -> None:
        self._fixtures_only = fixtures_only
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._built_at_monotonic: float | None = None
        self._build_count = 0

    def get(self, force: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (data, meta). Meta describes the CACHE, not the corridor."""
        with self._lock:
            age = (
                None
                if self._built_at_monotonic is None
                else time.monotonic() - self._built_at_monotonic
            )
            # `force` cannot bypass the floor - that is the point of the floor. It
            # only matters for a cache that is old enough to refresh anyway, where
            # it is indistinguishable from a normal poll. Kept as a parameter so
            # the UI's refresh button has an honest signal to report when its
            # request was throttled rather than served fresh.
            stale = age is None or age >= MIN_REFRESH_SECONDS

            if stale:
                self._data = strip_export.build(fixtures_only=self._fixtures_only)
                self._built_at_monotonic = time.monotonic()
                self._build_count += 1
                age = 0.0
                served_from_cache = False
            else:
                served_from_cache = True

            meta = {
                # Whether THIS response re-ran the adapters or replayed a cached
                # build. The UI says which, because "refreshed" and "showed you the
                # same bytes again" are different claims.
                "servedFromCache": served_from_cache,
                "cacheAgeSeconds": round(age or 0.0, 1),
                "minRefreshSeconds": MIN_REFRESH_SECONDS,
                "buildCount": self._build_count,
                "fixturesOnly": self._fixtures_only,
                # Present so a rejected force-refresh is visible rather than
                # looking like it silently worked.
                "refreshThrottled": bool(force and served_from_cache),
            }
            return self._data, meta


def _handler_factory(cache: StripCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CorridorEventHubStripDev/0.1"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(to_jsonable(payload), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The Vite dev server proxies /api, so same-origin holds in the normal
            # path. This header is here for the case where someone points a
            # standalone build at this port directly; it is localhost-only and
            # read-only, so it grants nothing that visiting the page would not.
            self.send_header("Access-Control-Allow-Origin", "*")
            # Never let a browser cache a freshness endpoint - the whole point is
            # that the client can tell how old the data is.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            path, _, query = self.path.partition("?")
            if path == "/api/health":
                self._send_json(200, {"ok": True})
                return
            if path != "/api/strip":
                self._send_json(
                    404,
                    {"error": "not found", "available": ["/api/strip", "/api/health"]},
                )
                return

            force = "refresh=1" in query or "force=1" in query
            try:
                data, meta = cache.get(force=force)
            except Exception as exc:  # noqa: BLE001 - report, do not kill the server
                traceback.print_exc()
                self._send_json(
                    500,
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "hint": "the adapters raised; see the server log for the traceback",
                    },
                )
                return

            self._send_json(200, {**data, "cache": meta})

        def log_message(self, fmt: str, *args: Any) -> None:
            # One compact line per request. The default logs every asset request
            # from the dev server too, which buries the ones that matter.
            sys.stderr.write(f"[strip-api] {fmt % args}\n")

    return Handler


def serve(port: int, fixtures_only: bool) -> int:
    cache = StripCache(fixtures_only=fixtures_only)
    # Bind to loopback explicitly. "" would listen on every interface, which on a
    # conference or campus network exposes an unauthenticated endpoint.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(cache))

    mode = "FIXTURES ONLY (no network)" if fixtures_only else "live where a key exists"
    print(f"strip API on http://127.0.0.1:{port}/api/strip  [{mode}]")
    print(f"  adapters re-run at most every {MIN_REFRESH_SECONDS:.0f}s (feed rate limits)")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local dev API for the strip UI.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="serve from captured payloads only, never the network",
    )
    args = parser.parse_args(argv)
    return serve(port=args.port, fixtures_only=args.fixtures)


if __name__ == "__main__":
    sys.exit(main())
