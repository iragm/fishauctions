"""A picture added by link is fetched once, here, to see that it really comes back as a picture.

Some hosts block linking from other sites (by ``Referer``), so a link that works in the seller's own
browser shows nothing, or a placeholder, on the lot page. The fetch sends this site as the referrer, as
a visitor's browser would, and refuses anything but a ``200`` image. Only public addresses are fetched:
every hop's host must resolve to global addresses, so a link can't make the server read its own network.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

#: Seconds for each hop to connect and for each read.
TIMEOUT = 5
#: Bytes read before the picture counts as too big to link to.
MAX_BYTES = 15 * 1024 * 1024
#: Redirects followed. One that changes the path is treated as a hotlink blocker's placeholder.
MAX_REDIRECTS = 3

NOT_A_PICTURE = "That link doesn't give back a picture here (the site may block linking), so upload the image instead."
TOO_BIG = "That picture is too big to link to, so upload a smaller copy instead."


def public_host(host: str) -> bool:
    """Whether every address ``host`` resolves to is a public one."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return bool(infos)


def _referer() -> str:
    from django.contrib.sites.models import Site

    try:
        return f"https://{Site.objects.get_current().domain}/"
    except Exception:
        return ""


def problem(url: str) -> str:
    """Why ``url`` can't be used as a picture, in one sentence, or ``""`` when it can."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; image-link-check)", "Accept": "image/*"}
    referer = _referer()
    if referer:
        headers["Referer"] = referer
    first_path = urlparse(url).path
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not public_host(parsed.hostname or ""):
            return NOT_A_PICTURE
        try:
            response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False)
        except requests.RequestException:
            return NOT_A_PICTURE
        with response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                # http -> https, or another host serving the same file, is fine; a different file isn't.
                if urlparse(url).path != first_path:
                    return NOT_A_PICTURE
                continue
            if response.status_code != 200:
                return NOT_A_PICTURE
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                return NOT_A_PICTURE
            size = 0
            try:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return TOO_BIG
            except requests.RequestException:
                return NOT_A_PICTURE
            return "" if size else NOT_A_PICTURE
    return NOT_A_PICTURE
