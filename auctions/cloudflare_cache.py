"""Purging a file out of Cloudflare's edge cache.

Separate from :mod:`auctions.cloudflare_images`, which talks to a different API with a different
token: Images is account-scoped and holds its own copies of uploads, while this is the CDN cache
sitting in front of the site's own ``/media/`` and needs ``Zone.Cache Purge`` on the zone.

It exists for one reason. ``nginx_fishauctions.conf`` serves ``/media/`` with
``Cache-Control: public, max-age=2592000``, which is right for a file written once under a unique
name and never edited -- and wrong the moment a file has to *stop* existing. Deleting the row and
the file leaves the edge handing out the image for another thirty days, and "I deleted it, the CDN
did not" is not the expeditious removal 17 U.S.C. 512(c)(1)(C) asks for. So a takedown purges.

Unconfigured is a supported state, not an error: a deployment with no Cloudflare in front of it has
nothing to purge, and one that has Cloudflare but no purge token still gets the file removed from
the origin. Both log, and neither raises -- a failed purge must not roll back a deletion that has
already happened.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PURGE_URL = "https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache"

#: Cloudflare's own cap on one purge-by-URL call.
MAX_URLS_PER_CALL = 30


def enabled():
    return bool(
        getattr(settings, "CLOUDFLARE_ZONE_ID", "") and getattr(settings, "CLOUDFLARE_CACHE_PURGE_API_TOKEN", "")
    )


def purge_urls(urls):
    """Purge these absolute URLs from the edge. Returns True if Cloudflare accepted the purge.

    Never raises. The caller is always something that has already deleted a file, and there is
    nothing useful for it to do with an exception except log it, which happens here.
    """
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return False
    if not enabled():
        # Worth a warning rather than a debug line: on a site that *is* behind Cloudflare, this is
        # the difference between a file being gone and a file still being served for a month.
        logger.warning(
            "Not purging %s URL(s) from the edge cache: CLOUDFLARE_ZONE_ID and "
            "CLOUDFLARE_CACHE_PURGE_API_TOKEN are not both set",
            len(urls),
        )
        return False
    ok = True
    headers = {"Authorization": f"Bearer {settings.CLOUDFLARE_CACHE_PURGE_API_TOKEN}"}
    url = PURGE_URL.format(zone_id=settings.CLOUDFLARE_ZONE_ID)
    for start in range(0, len(urls), MAX_URLS_PER_CALL):
        batch = urls[start : start + MAX_URLS_PER_CALL]
        try:
            response = requests.post(url, headers=headers, json={"files": batch}, timeout=30)
            payload = response.json()
        except (requests.RequestException, ValueError):
            logger.exception("Cloudflare cache purge failed for %s", batch)
            ok = False
            continue
        if not payload.get("success"):
            logger.error("Cloudflare cache purge rejected %s: %s", batch, payload.get("errors"))
            ok = False
    return ok
