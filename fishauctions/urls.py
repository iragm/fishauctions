from django.contrib import admin
from django.contrib.auth.decorators import user_passes_test
from django.urls import URLPattern, include, path
from oauth2_provider import urls as oauth2_urls
from oauth2_provider.urls import metadata_urlpatterns

from auctions.mcp.auth import throttle_registration

#: Application-management views the toolkit ships. ``LoginRequiredMixin`` is the only gate they
#: carry, so out of the box any signed-in member of the site can register OAuth clients and list
#: their own -- pages that belong to whoever runs the server, not to whoever has an account on it.
#: Named rather than matched on a prefix so a toolkit release that adds a view fails loudly here
#: instead of quietly shipping it ungated.
_APPLICATION_VIEW_NAMES = {"list", "register", "detail", "delete", "update"}


def _superusers_only(patterns):
    """Re-wrap a list of URL patterns behind ``is_superuser``, keeping their names.

    Rebuilt rather than decorated in place because the toolkit's module-level lists are shared:
    mutating a ``URLPattern`` there would change it for anybody else who imported it, including
    the metadata mount below.
    """
    gate = user_passes_test(lambda user: user.is_superuser)
    return [URLPattern(entry.pattern, gate(entry.callback), entry.default_args, entry.name) for entry in patterns]


def _wrap_named(patterns, name, wrapper):
    """The same, for one named view -- the DCR endpoint, which gets a rate limit rather than a gate."""
    return [
        URLPattern(
            entry.pattern,
            wrapper(entry.callback) if entry.name == name else entry.callback,
            entry.default_args,
            entry.name,
        )
        for entry in patterns
    ]


# The authorization server, assembled by hand instead of `include("oauth2_provider.urls")`, because
# two of its parts need something the toolkit doesn't do for us:
#
#   * the application-management pages are for whoever runs this server, not for every signed-in
#     member of the site, and the toolkit gates them on login alone;
#   * dynamic client registration has to be open to anonymous callers (it is the first call a
#     client makes), which means the Application table is writable by strangers -- so that one
#     endpoint gets a per-address rate limit. See auctions/mcp/auth.py.
#
# Everything else is passed through untouched, names included, so reverse("oauth2_provider:...")
# keeps working exactly as it did.
_oauth2_urlpatterns = (
    oauth2_urls.metadata_urlpatterns
    + oauth2_urls.base_urlpatterns
    + [entry for entry in oauth2_urls.management_urlpatterns if entry.name not in _APPLICATION_VIEW_NAMES]
    + _superusers_only([entry for entry in oauth2_urls.management_urlpatterns if entry.name in _APPLICATION_VIEW_NAMES])
    + oauth2_urls.oidc_urlpatterns
    + _wrap_named(oauth2_urls.dcr_urlpatterns, "dcr-register", throttle_registration)
)

urlpatterns = [
    # The OAuth 2.1 authorization server that guards /mcp/ (see auctions/mcp/auth.py and the
    # OAUTH2_PROVIDER block in settings.py).
    #
    # Two mounts, on purpose. The toolkit's own URLs live under /o/ — authorize, token, register.
    # The discovery documents are mounted a *second* time at the domain root, because RFC 8414 and
    # RFC 9728 both put them at the origin (`/.well-known/oauth-authorization-server`,
    # `/.well-known/oauth-protected-resource`) and that is the first place Claude looks. Without the
    # root mount they are only reachable at /o/.well-known/..., which is a fallback some clients
    # never try; the symptom is "couldn't reach the MCP server" with no request ever arriving at
    # the authorization server.
    #
    # The separate instance namespace keeps reverse("oauth2_provider:...") pointing unambiguously
    # at the /o/ mount below.
    path("", include((metadata_urlpatterns, "oauth2_provider"), namespace="oauth2_metadata")),
    path("o/", include((_oauth2_urlpatterns, "oauth2_provider"))),
    path("", include("auctions.urls")),
    path("api/mobile/", include("auctions.mobile.urls")),
    path("summernote/", include("django_summernote.urls")),
    path("admin/", admin.site.urls),
    path("", include("allauth.urls")),
    # path("__debug__/", include(debug_toolbar.urls)),
]

# Same pages Django would serve by default, but a failed render logs the real
# traceback instead of Django silently falling back (see auctions/error_views.py).
handler404 = "auctions.error_views.error_404"
handler500 = "auctions.error_views.error_500"
