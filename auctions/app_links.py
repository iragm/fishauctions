"""The two files that let a site link open in the mobile app instead of a browser.

Android reads ``/.well-known/assetlinks.json`` and iOS ``/.well-known/apple-app-site-association``.
Both are public JSON fetched by the platform, checked once at install and cached, so getting them
wrong fails silently -- links keep opening in the browser with nothing logged.

Three platform constraints, all honoured here: **no redirect** (both paths are matched exactly, so
``APPEND_SLASH`` never fires), **no authentication**, and **``application/json`` with no ``.json``
extension on the Apple one** -- that is the filename in the spec.

Both are configured from the environment (``settings.ANDROID_APP_LINKS`` / ``settings.IOS_APP_LINKS``)
since the answers differ per deployment. Unconfigured means 404, which an operator can diagnose; an
empty-but-valid file would verify as "this site claims no apps" and look identical to a working one.
"""

from django.conf import settings
from django.http import Http404, JsonResponse

# How long the platforms may cache these. They fetch repeatedly and the contents change rarely, but
# an hour is short enough that a fixed fingerprint is live the same afternoon.
CACHE_SECONDS = 3600

# Paths iOS must not hand to the app. Order matters: iOS takes the first matching component, and
# ``_apple_details`` emits these ahead of the ``/*`` catch-all.
#
# The association files themselves, because launching the app to fetch its own file is a pointless
# loop. Then the four OAuth return paths, which the *browser* has to handle: the app opens the flow
# in an auth session and the code is exchanged against that session, so an app handed the redirect
# opens the callback in its own WebView with none of the OAuth state -- which looks to the user like
# having been signed out.
#
# ``/mailchimp/callback*``, not ``/mailchimp/callback/*``: the real URL has the code in the query
# string, so anchoring the star a character earlier matches under either reading of ``*``.
# ``test_app_links`` checks these against the URLconf, so a renamed route fails the build.
#
# iOS only: ``assetlinks.json`` has no per-path granularity, so Android's half is in the app's
# intent-filter path patterns.
IOS_EXCLUDED_PATHS = [
    "/.well-known/*",
    "/square/onboard/success*",
    "/paypal/onboard/success*",
    "/mailchimp/callback*",
    "/google-calendar/callback*",
]


def _android_statements():
    """``settings.ANDROID_APP_LINKS`` as one Digital Asset Links statement per package.

    Several fingerprints for one package (staging signs with a per-developer debug keystore) are written
    by repeating the package and grouped back into one ``sha256_cert_fingerprints`` list.
    """
    by_package = {}
    order = []
    for entry in settings.ANDROID_APP_LINKS:
        package, _, fingerprint = entry.partition("=")
        package = package.strip()
        fingerprint = fingerprint.strip().upper()
        if not package or not fingerprint:
            continue
        if package not in by_package:
            by_package[package] = []
            order.append(package)
        if fingerprint not in by_package[package]:
            by_package[package].append(fingerprint)
    return [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": package,
                "sha256_cert_fingerprints": by_package[package],
            },
        }
        for package in order
    ]


def _json(payload):
    response = JsonResponse(payload, safe=False)
    response["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return response


def assetlinks(request):
    """GET /.well-known/assetlinks.json — Android App Links verification.

    The fingerprint must be the certificate Google Play re-signs with (Play Console → Release → Setup →
    App signing), not the upload key: using the upload fingerprint fails verification with no error
    anywhere. Check a deployment with::

        https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://auction.fish&relation=delegate_permission/common.handle_all_urls
        adb shell pm get-app-links com.fishauctions.app
    """
    statements = _android_statements()
    if not statements:
        raise Http404
    return _json(statements)


def apple_app_site_association(request):
    """GET /.well-known/apple-app-site-association — iOS Universal Links.

    ``components`` is an allow-list, and starting from everything is right here because the app is the
    site: every page it opens, it opens in its own WebView. ``IOS_EXCLUDED_PATHS`` is what's carved out.

    Order matters on Apple's side: enable **Associated Domains** on the App ID, then deploy this file,
    then add ``com.apple.developer.associated-domains`` to the entitlements. The other order means cloud
    signing can't build a matching profile and every export fails.
    """
    app_ids = [app_id.strip() for app_id in settings.IOS_APP_LINKS if app_id.strip()]
    if not app_ids:
        raise Http404
    components = [{"/": path, "exclude": True} for path in IOS_EXCLUDED_PATHS]
    components.append({"/": "/*"})
    return _json(
        {
            "applinks": {
                "details": [
                    {
                        "appIDs": app_ids,
                        "components": components,
                    }
                ]
            }
        }
    )
