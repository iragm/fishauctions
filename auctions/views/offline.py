"""The emergency page at /offline/: set lot winners, add users and lots with the site down.

A root-scoped service worker (:class:`OfflineWorkerView`) caches the page and its files, and shows it
in place of any page the site can't serve because it is unreachable (a network error, a 502-504, or
one of Cloudflare's 52x). A plain 500 is a bug on one page, so it gets the 500 page, which links here.

Entries are kept in the browser's IndexedDB and pushed through :class:`OfflineSyncView`, which is the
app's offline sync (:mod:`auctions.mobile.services.offline`) behind the site's own sign-in instead of
the app's tokens. Ids, idempotency and conflicts are all decided there.
"""

import base64
import hashlib
import re
from functools import lru_cache
from pathlib import Path

from django.contrib.staticfiles import finders
from django.http import HttpResponse
from django.template.loader import get_template, render_to_string
from django.utils.decorators import method_decorator
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView, View
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from auctions.mobile.serializers import OfflineSyncSerializer
from auctions.mobile.services import offline
from auctions.models import Auction

__all__ = [
    "OfflineCloudflarePageView",
    "OfflinePageView",
    "OfflineSnapshotView",
    "OfflineSyncView",
    "OfflineWorkerView",
]

#: What the worker caches, besides the page itself. Everything the page loads must be here.
OFFLINE_STATIC_FILES = (
    "css/vendor/bootstrap.min.css",
    "css/auction_site.css",
    "js/offline_store.js",
    "js/offline_page.js",
    "favicon-32x32.png",
)
OFFLINE_TEMPLATES = ("offline/page.html", "offline/worker.js")


@lru_cache(maxsize=1)
def worker_version():
    """A hash of everything the worker caches, so a deploy that changes any of it installs a new worker.

    Hashed names change the worker's bytes too, but only with DEBUG off; this works either way.
    """
    digest = hashlib.sha256()
    for name in OFFLINE_STATIC_FILES:
        path = finders.find(name)
        if path:
            digest.update(Path(path).read_bytes())
    for name in OFFLINE_TEMPLATES:
        digest.update(Path(get_template(name).origin.name).read_bytes())
    return digest.hexdigest()[:12]


class OfflinePageView(TemplateView):
    """The emergency page. The same bytes for everyone, since the worker caches one copy per browser."""

    template_name = "offline/page.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["offline_static_files"] = OFFLINE_STATIC_FILES
        return context


class OfflineCloudflarePageView(OfflinePageView):
    """The emergency page as one file, for Cloudflare's 5xx custom page (zone custom_pages ``500_errors``).

    Cloudflare stores a copy and shows it whenever it can't reach the site. That is the only time a
    browser without the worker sees it, so the page can't load anything from here: its CSS and JS are
    written in. It runs on the address that failed, which is this site, so it shares IndexedDB with
    /offline/ and its entries go in the same way. Cloudflare keeps the copy it took, so a deploy that
    changes the page or its files needs the custom page re-fetched: the page names the worker version it
    was built from, which /offline-worker.js also names, and routines/cloudflare.md compares the two.
    """

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["inline"] = _inline_files()
        return context


def _inline_files():
    """OFFLINE_STATIC_FILES, written into one page. Not cached: this is fetched once per deploy, by Cloudflare."""

    def read(name, pattern):
        text = Path(finders.find(name)).read_text(encoding="utf-8")
        # Anything that would reach out to the network (fonts, source maps) goes: the network is what's down.
        text = re.sub(pattern, "", text)
        # A literal "</" would end the <script> or <style> early; "<\\/" means the same to both.
        return text.replace("</", "<\\/")

    css = [
        read(name, r"@import url\([^)]*\);|/\*# sourceMappingURL=[^*]*\*/")
        for name in OFFLINE_STATIC_FILES
        if name.endswith(".css")
    ]
    js = [read(name, r"//# sourceMappingURL=\S*") for name in OFFLINE_STATIC_FILES if name.endswith(".js")]
    favicon = Path(finders.find("favicon-32x32.png")).read_bytes()
    return {
        "css": mark_safe("\n".join(css)),  # noqa: S308 -- our own static files
        "js": mark_safe("\n".join(js)),  # noqa: S308 -- our own static files
        "favicon": "data:image/png;base64," + base64.b64encode(favicon).decode(),
        "version": worker_version(),
    }


class OfflineWorkerView(View):
    """The service worker, served from the site's root so it can answer for every page."""

    def get(self, request):
        body = render_to_string(
            "offline/worker.js",
            {"version": worker_version(), "offline_static_files": OFFLINE_STATIC_FILES},
        )
        response = HttpResponse(body, content_type="application/javascript")
        # Browsers re-check a worker at most daily anyway; no-cache makes them ask every time.
        response["Cache-Control"] = "no-cache"
        response["Service-Worker-Allowed"] = "/"
        return response


class _OfflineAPIView(APIView):
    """The app's offline endpoints, signed in the way the website is."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_scope = "offline_page"
    throttle_classes = [ScopedRateThrottle]


@method_decorator([never_cache, ensure_csrf_cookie], name="dispatch")
class OfflineSnapshotView(_OfflineAPIView):
    """GET /offline/snapshot/: the auction the page works on, as the app gets it, plus who it is for.

    ``?auction=<slug>`` asks for one auction, else the caller's last admin auction. ``?ping=1`` returns
    only ``user_id``: the page asks that every few seconds to tell whether the site is back, and to get
    a fresh CSRF cookie before it syncs.
    """

    def get(self, request):
        if request.query_params.get("ping"):
            return Response({"user_id": request.user.pk})
        slug = request.query_params.get("auction")
        if slug:
            auction = Auction.objects.filter(slug=slug, is_deleted=False).first()
            if auction is None or not auction.permission_check(request.user):
                auction = None
        else:
            auction = offline.get_last_admin_auction(request.user)
        return Response({**offline.build_snapshot(auction), "user_id": request.user.pk})


@method_decorator(never_cache, name="dispatch")
class OfflineSyncView(_OfflineAPIView):
    """POST /offline/sync/: apply the page's queued entries, then return a fresh snapshot."""

    def post(self, request):
        serializer = OfflineSyncSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        auction = Auction.objects.filter(slug=serializer.validated_data["auction"], is_deleted=False).first()
        if auction is None:
            return Response({"detail": "Auction not found."}, status=status.HTTP_404_NOT_FOUND)
        if not auction.permission_check(request.user):
            return Response(
                {"detail": "You do not have permission to sync this auction."}, status=status.HTTP_403_FORBIDDEN
            )
        results = offline.apply_ops(auction, request.user, serializer.validated_data["ops"])
        return Response(
            {"results": results, "snapshot": {**offline.build_snapshot(auction), "user_id": request.user.pk}}
        )
