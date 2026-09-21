"""Adapter registry.

WHY THIS FILE EXISTS SEPARATELY: mapping a ``source_id`` to an adapter is the one
place something has to name specific states and agencies. Core services must
contain no state names, and ``scripts/check-portability.sh`` enforces
it - correctly flagging the registry when it lived in ``handlers/``.

So the registry lives in ``adapters/``, alongside the adapters it registers. That
directory is exempt from the portability check by design, because it is where
state-specific knowledge is SUPPOSED to live. The normalizer imports this
and stays agnostic.

Onboarding a source is an adapter module plus a catalog entry plus one line
here - no change to the pipeline itself.
"""

from __future__ import annotations

from .adapter import Adapter
from .aws_location_traffic import AwsLocationTrafficAdapter
from .az511_events import Az511EventsAdapter
from .nm_dot_weathershare import NmDotWeathershareAdapter
from .nws_alerts import NwsAlertsAdapter
from .ok_odot_wzdx import OkOdotWzdxAdapter
from .tx_dot_wzdx import TxDotWzdxAdapter

# Keys MUST match `sourceId` in config/sources.json. A payload arriving for an
# unregistered source is quarantined rather than dropped, so a mismatch
# here surfaces as an alarm instead of silent data loss.
ADAPTERS: dict[str, Adapter] = {
    "ok-odot-wzdx": OkOdotWzdxAdapter(),
    "tx-dot-wzdx": TxDotWzdxAdapter(),
    "az511-events": Az511EventsAdapter(),
    "nws-alerts": NwsAlertsAdapter(),
    "aws-location-traffic": AwsLocationTrafficAdapter(),
    "nm-dot-weathershare": NmDotWeathershareAdapter(),
}


def adapter_for(source_id: str) -> Adapter | None:
    return ADAPTERS.get(source_id)


def registered_source_ids() -> list[str]:
    return list(ADAPTERS)
