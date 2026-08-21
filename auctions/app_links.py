"""The two files that let a site link open in the mobile app instead of a browser.

Android reads ``/.well-known/assetlinks.json`` and iOS reads
``/.well-known/apple-app-site-association``; both are plain public JSON, both are fetched by the
platform (not by the app), and both are checked once at install time and then cached, which is why
getting them wrong fails *silently* — links simply keep opening in the browser with nothing logged
anywhere the site can see.

Three constraints come from the platforms rather than from us, and all three are honoured here:

* **No redirect.** Android does not follow one, and Apple does not either. Both paths are matched
  exactly by the URLconf, so ``APPEND_SLASH`` never fires (it only redirects a URL that fails to
  resolve).
* **No authentication.** Nothing on these views requires a session; the fetch comes from Google's
  and Apple's infrastructure, not from a signed-in user.
* **``application/json``, and no ``.json`` extension on the Apple one.** Apple's file has no
  extension at all — that is the filename in the spec, not an oversight.

Both are configured from the environment (see ``settings.ANDROID_APP_LINKS`` /
``settings.IOS_APP_LINKS``) because the answers differ per deployment: production claims
``com.fishauctions.app``, staging claims the ``.staging`` and ``.dev`` flavors, and a local checkout
claims nothing at all. Unconfigured ⇒ 404, which is the honest answer and is what an operator can
actually diagnose; an empty-but-valid file would verify as "this site claims no apps" and look
identical to a working one.
"""

from django.conf import settings
from django.http import Http404, JsonResponse

# How long the platforms may cache what we serve. Both fetch these repeatedly (Apple through its own
# CDN, Google on every verification attempt) and the contents change roughly never — but an hour is
# short enough that a fixed fingerprint is live the same afternoon rather than the next week.
CACHE_SECONDS = 3600

# Paths iOS must NOT hand to the app. Only these two files: an app launched to fetch its own
# association file is a loop with no purpose, and Apple's own examples exclude them.
#
# The other candidates, listed here because they are the ones that would bite rather than because
# they bite today: any path a *browser* has to handle for the flow to work (there is nothing today —
# social login is hidden for the app's user agent), and the Square/PayPal OAuth return paths, which
# the app shell already routes into an in-app browser view and which a cold app launch would handle
# worse than the browser does. Add them here if that changes; the order matters, because iOS takes
# the first component that matches.
IOS_EXCLUDED_PATHS = ["/.well-known/*"]


def _android_statements():
    """``settings.ANDROID_APP_LINKS`` as one Digital Asset Links statement per package.

    Several fingerprints for one package (staging signs its own builds with a debug keystore that
    differs per developer machine) are expressed by repeating the package; they are grouped back
    into the single ``sha256_cert_fingerprints`` list the format expects.
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

    The fingerprint has to be the certificate Google Play **re-signs** with (Play Console → Release →
    Setup → App signing → "App signing key certificate"), not the upload key. Using the upload
    fingerprint is the classic failure here: verification fails with no error anywhere and links keep
    opening in the browser. Check a deployment with::

        https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://auction.fish&relation=delegate_permission/common.handle_all_urls
        adb shell pm get-app-links com.fishauctions.app
    """
    statements = _android_statements()
    if not statements:
        raise Http404
    return _json(statements)


def apple_app_site_association(request):
    """GET /.well-known/apple-app-site-association — iOS Universal Links.

    ``components`` is an allow-list, and starting from everything is right for this site because the
    app *is* the site: every page it opens, it opens in its own WebView shell. See
    ``IOS_EXCLUDED_PATHS`` for what is carved back out.

    The order of operations on the Apple side matters and is not reversible from here: enable
    **Associated Domains** on the App ID in Certificates, Identifiers & Profiles, *then* deploy this
    file, *then* add ``com.apple.developer.associated-domains`` to the app's entitlements. Adding the
    entitlement before the capability exists on the App ID means cloud signing cannot build a
    matching profile and every Release/TestFlight export fails — the same trap as the Tap to Pay
    entitlement.
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
