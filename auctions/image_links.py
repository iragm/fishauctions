"""A picture added by link is fetched once, here, to see that it really comes back as a picture.

Some hosts block linking from other sites (by ``Referer``), so a link that works in the seller's own
browser shows nothing, or a placeholder, on the lot page. The fetch sends this site as the referrer, as
a visitor's browser would, and refuses anything but a ``200`` image. Only public addresses are fetched:
every hop's host is resolved once, every address it gives must be global, and the connection goes to
that vetted address (with the original ``Host`` header and, for https, SNI and certificate check against
the host name), so a second lookup can't be rebound onto the server's own network.

The whole check has one deadline across every hop, and it reads only the first few KB: an ``image/*``
answer whose first bytes arrive is enough, so a slow or huge file never holds a worker.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import certifi
import urllib3

#: Seconds for each connect and each read, never more than what is left of :data:`DEADLINE`.
TIMEOUT = 5
#: Seconds for the whole check, across every hop.
DEADLINE = 8
#: Bytes read to confirm a picture; nothing after them is downloaded.
ENOUGH_BYTES = 4 * 1024
#: A declared ``Content-Length`` above this counts as too big to link to.
MAX_BYTES = 15 * 1024 * 1024
#: Redirects followed. One that changes the path is treated as a hotlink blocker's placeholder.
MAX_REDIRECTS = 3

NOT_A_PICTURE = "That link doesn't give back a picture here (the site may block linking), so upload the image instead."
TOO_BIG = "That picture is too big to link to, so upload a smaller copy instead."

FETCH_ERRORS = (urllib3.exceptions.HTTPError, OSError, ValueError)


def public_address(host: str, port: int) -> str:
    """One address for ``host``, resolved once, or ``""`` unless every address it resolves to is public."""
    if not host:
        return ""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return ""
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return ""
        if not address.is_global:
            return ""
        addresses.append(str(address))
    return addresses[0] if addresses else ""


def public_host(host: str) -> bool:
    """Whether every address ``host`` resolves to is a public one."""
    return bool(public_address(host, 443))


def _referer() -> str:
    from django.contrib.sites.models import Site

    try:
        return f"https://{Site.objects.get_current().domain}/"
    except Exception:
        return ""


def _open(url: str, address: str, headers: dict, timeout: float):
    """GET ``url`` from exactly ``address``: no second lookup, no redirects, nothing read yet."""
    parsed = urlparse(url)
    https = parsed.scheme == "https"
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if https else 80)
    host_header = parsed.netloc.rsplit("@", 1)[-1]
    common = {"port": port, "timeout": urllib3.Timeout(connect=timeout, read=timeout), "retries": False, "maxsize": 1}
    if https:
        pool = urllib3.HTTPSConnectionPool(
            address,
            server_hostname=hostname,
            assert_hostname=hostname,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            **common,
        )
    else:
        pool = urllib3.HTTPConnectionPool(address, **common)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    return pool.urlopen(
        "GET",
        path,
        headers={**headers, "Host": host_header},
        redirect=False,
        retries=False,
        preload_content=False,
        assert_same_host=False,
    )


def _looks_like_markup(start: bytes, content_type: str) -> bool:
    return content_type != "image/svg+xml" and start.lstrip()[:1] == b"<"


def problem(url: str) -> str:
    """Why ``url`` can't be used as a picture, in one sentence, or ``""`` when it can."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; image-link-check)", "Accept": "image/*"}
    referer = _referer()
    if referer:
        headers["Referer"] = referer
    deadline = time.monotonic() + DEADLINE
    first_path = urlparse(url).path
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return NOT_A_PICTURE
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return NOT_A_PICTURE
        address = public_address(parsed.hostname or "", port)
        left = deadline - time.monotonic()
        if not address or left <= 0:
            return NOT_A_PICTURE
        try:
            response = _open(url, address, headers, min(TIMEOUT, left))
        except FETCH_ERRORS:
            return NOT_A_PICTURE
        try:
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                # http -> https, or another host serving the same file, is fine; a different file isn't.
                if urlparse(url).path != first_path:
                    return NOT_A_PICTURE
                continue
            if response.status != 200:
                return NOT_A_PICTURE
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                return NOT_A_PICTURE
            try:
                if int(response.headers.get("Content-Length") or 0) > MAX_BYTES:
                    return TOO_BIG
            except ValueError:
                return NOT_A_PICTURE
            start = b""
            try:
                while len(start) < ENOUGH_BYTES:
                    if time.monotonic() > deadline:
                        return NOT_A_PICTURE
                    chunk = response.read1(ENOUGH_BYTES - len(start))
                    if not chunk:
                        break
                    start += chunk
            except FETCH_ERRORS:
                return NOT_A_PICTURE
            if not start or _looks_like_markup(start, content_type):
                return NOT_A_PICTURE
            return ""
        finally:
            response.close()
    return NOT_A_PICTURE
