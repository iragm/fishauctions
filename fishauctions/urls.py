from django.contrib import admin
from django.urls import include, path
from oauth2_provider.urls import metadata_urlpatterns

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
    path("o/", include("oauth2_provider.urls")),
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
