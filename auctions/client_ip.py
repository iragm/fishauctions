"""Where a request came from, for the things that count per address.

Every rate limit on the site keys on this, so it has to be a value the caller can't choose. It used
to be the **left-most** ``X-Forwarded-For`` entry, which is exactly the part the caller writes:
nginx forwards ``$proxy_add_x_forwarded_for``, which *appends* the real address to whatever arrived,
so a header sent by hand stays in front. One extra header per request and the contact-form limit,
the DMCA limit, the OAuth registration limit and the Square token audit line all came apart.

So the order here runs from the hop we trust most outwards, and never reaches the client's own text:

1. ``CF-Connecting-IP`` when :data:`settings.BEHIND_CLOUDFLARE` -- Cloudflare sets it itself and
   strips any copy the caller sent. Prod is orange-clouded, where ``X-Real-IP`` is only the edge.
2. ``X-Real-IP``, which our nginx sets from ``$remote_addr`` (``nginx_fishauctions.conf``). A client
   can send one, but nginx overwrites it.
3. ``REMOTE_ADDR``, when there is no proxy at all.

Its own module so the mobile services, the MCP endpoint and the moderation views share one answer
rather than four copies of the wrong one.
"""

from django.conf import settings


def client_ip(request) -> str:
    """The caller's address, or ``""`` when nothing trustworthy says. Never client-supplied."""
    if request is None:
        return ""
    meta = getattr(request, "META", None) or {}
    if getattr(settings, "BEHIND_CLOUDFLARE", False):
        cloudflare = (meta.get("HTTP_CF_CONNECTING_IP") or "").strip()
        if cloudflare:
            return cloudflare
    real = (meta.get("HTTP_X_REAL_IP") or "").strip()
    if real:
        return real
    return (meta.get("REMOTE_ADDR") or "").strip()
