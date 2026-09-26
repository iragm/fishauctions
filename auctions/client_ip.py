"""Where a request came from, for the things that count per address.

Every rate limit on the site keys on this, so it has to be a value the caller can't choose. It used
to be the **left-most** ``X-Forwarded-For`` entry, which is exactly the part the caller writes:
nginx forwards ``$proxy_add_x_forwarded_for``, which *appends* the real address to whatever arrived,
so a header sent by hand stays in front. One extra header per request and the contact-form limit,
the DMCA limit, the OAuth registration limit and the Square token audit line all came apart.

So the order here runs from the hop we trust most outwards, and never reaches the client's own text:

1. ``CF-Connecting-IP``, when :data:`settings.BEHIND_CLOUDFLARE` *and the request reached us from
   Cloudflare* -- see :func:`_reached_us_from_cloudflare`. Cloudflare sets the header itself and
   strips any copy the caller sent. Prod is orange-clouded, where ``X-Real-IP`` is only the edge.
2. ``X-Real-IP``, which our nginx sets from ``$remote_addr`` (``nginx_fishauctions.conf``). A client
   can send one, but nginx overwrites it.
3. ``REMOTE_ADDR``, when there is no proxy at all.

Its own module so the mobile services, the MCP endpoint and the moderation views share one answer
rather than four copies of the wrong one.
"""

import ipaddress
from functools import lru_cache

from django.conf import settings

#: Cloudflare's published edge ranges -- https://www.cloudflare.com/ips-v4 and .../ips-v6, fetched
#: 2026-09-19. Stale entries fail safe: an address on a range added since is simply not believed,
#: and those visitors get recorded as the edge rather than as themselves.
_CLOUDFLARE_RANGES = """
173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18
190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13
104.24.0.0/14 172.64.0.0/13 131.0.72.0/22
2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29
2c0f:f248::/32
"""

_CLOUDFLARE_NETWORKS = tuple(ipaddress.ip_network(prefix) for prefix in _CLOUDFLARE_RANGES.split())


@lru_cache(maxsize=2048)
def _is_cloudflare_edge(address: str) -> bool:
    """Is ``address`` one of Cloudflare's own machines? Cached: this runs on every page view."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in _CLOUDFLARE_NETWORKS)


def _reached_us_from_cloudflare(meta) -> bool:
    """Whether the connection into nginx came from Cloudflare, which is what makes CF's header worth
    anything.

    ``CF-Connecting-IP`` is only a claim. nginx sets no value for it and forwards whatever arrived,
    so anyone who finds the origin address and talks to it directly can write whatever they like --
    and that address feeds ban evasion, shill-bid detection, geolocation and every rate limit. What
    can't be written by hand is who opened the socket: nginx puts that in ``X-Real-IP`` from
    ``$remote_addr``, overwriting any copy the caller sent. Behind Cloudflare that is an edge
    machine, so checking it against Cloudflare's published ranges is what separates a request that
    really came through Cloudflare from one that only says it did.

    This is the same test nginx's own ``real_ip_header`` module performs with ``set_real_ip_from``;
    doing it here keeps it in one place with the rest of the answer, and out of the proxy config.
    """
    connecting = (meta.get("HTTP_X_REAL_IP") or meta.get("REMOTE_ADDR") or "").strip()
    return bool(connecting) and _is_cloudflare_edge(connecting)


def client_ip(request) -> str:
    """The caller's address, or ``""`` when nothing trustworthy says. Never client-supplied."""
    if request is None:
        return ""
    meta = getattr(request, "META", None) or {}
    if getattr(settings, "BEHIND_CLOUDFLARE", False):
        cloudflare = (meta.get("HTTP_CF_CONNECTING_IP") or "").strip()
        if cloudflare and _reached_us_from_cloudflare(meta):
            return cloudflare
    real = (meta.get("HTTP_X_REAL_IP") or "").strip()
    if real:
        return real
    return (meta.get("REMOTE_ADDR") or "").strip()
