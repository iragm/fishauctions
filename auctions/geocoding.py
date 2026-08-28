"""Turning a typed address into a point on the map.

Two callers wanted the same three lines of Google Geocoding API and had their own copy:
``tasks.geocode_club_member`` and ``tasks.geocode_speaker``. A third wanted it and could not have
it -- the assistant, which is handed addresses out loud and has no map to click on.

That third caller is why this returns the *formatted address* as well as the coordinates. The web
form geocodes in JavaScript and shows the result as a marker the person can see and drag; an
assistant has no marker, so the only honest equivalent is to say which place was found and let
somebody agree with it before anything is saved. A pickup location saved at the wrong point, or at
no point at all, is the worst outcome here: it is what every "how far away is this auction" answer
is measured from, and nothing on the page it lands on will ever look wrong.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"
TIMEOUT = 10


def configured() -> bool:
    """Whether this deployment can geocode at all. Blank on a fork with no Google key."""
    return bool(getattr(settings, "GOOGLE_MAPS_SERVER_API_KEY", ""))


def geocode(address: str) -> dict | None:
    """``{"latitude", "longitude", "coordinates", "address"}`` for a typed address, or ``None``.

    ``None`` covers every way this can fail to produce an answer -- no key, nothing typed, no
    result, a refusal from Google, a timeout -- because every one of them means the same thing to
    a caller: you do not have a point, so do not save one.
    """
    address = (address or "").strip()
    if not address or not configured():
        return None
    try:
        response = requests.get(
            ENDPOINT,
            params={"address": address, "key": settings.GOOGLE_MAPS_SERVER_API_KEY},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        logger.warning("Could not geocode an address", exc_info=True)
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    best = data["results"][0]
    point = best["geometry"]["location"]
    return {
        "latitude": point["lat"],
        "longitude": point["lng"],
        # The string the ``PlainLocationField`` stores, and what the pre_save signal splits again.
        "coordinates": f"{point['lat']},{point['lng']}",
        # Google's own spelling of the place it found. This is the half a person confirms: "12 Mill
        # Lane" typed into a box could be any of a dozen Mill Lanes, and the formatted address is
        # the only thing that says which one was picked.
        "address": best.get("formatted_address") or address,
    }
