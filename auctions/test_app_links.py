"""Part LINKS — the two files that make a site link open in the app.

Both are fetched by Google's and Apple's infrastructure rather than by the app, both are checked once
and cached, and both fail *silently* when they are wrong: links simply keep opening in the browser
with nothing logged anywhere the site can see. So the things worth pinning down here are the ones
nobody would notice breaking — the exact paths, the content type, that no authentication is needed,
and that neither one ever answers with a redirect.
"""

from fnmatch import fnmatchcase

from django.test import TestCase, override_settings
from django.urls import reverse

ANDROID = ["com.fishauctions.app=AA:BB:CC"]
IOS = ["TEAMID.com.fishauctions.app"]

ASSETLINKS = "/.well-known/assetlinks.json"
# No .json extension. That is the filename in Apple's spec, not an oversight, and a request for
# apple-app-site-association.json is a different (missing) URL.
AASA = "/.well-known/apple-app-site-association"


@override_settings(ANDROID_APP_LINKS=ANDROID, IOS_APP_LINKS=IOS)
class AppLinkFilesTests(TestCase):
    def test_assetlinks_served_as_json(self):
        response = self.client.get(ASSETLINKS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_apple_file_served_as_json(self):
        response = self.client.get(AASA)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_neither_file_redirects(self):
        # Neither platform follows a redirect to reach these, so a 301 here is a silently broken
        # feature. The paths are matched exactly, which keeps APPEND_SLASH out of it.
        for path in (ASSETLINKS, AASA):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_no_authentication_required(self):
        # Nobody is signed in in these tests; that is the point of asserting it.
        self.assertEqual(self.client.get(ASSETLINKS).status_code, 200)
        self.assertEqual(self.client.get(AASA).status_code, 200)

    def test_assetlinks_shape(self):
        statements = self.client.get(ASSETLINKS).json()
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0]["relation"], ["delegate_permission/common.handle_all_urls"])
        target = statements[0]["target"]
        self.assertEqual(target["namespace"], "android_app")
        self.assertEqual(target["package_name"], "com.fishauctions.app")
        self.assertEqual(target["sha256_cert_fingerprints"], ["AA:BB:CC"])

    @override_settings(
        ANDROID_APP_LINKS=[
            "com.fishauctions.app.staging=11:22",
            "com.fishauctions.app.staging=33:44",
            "com.fishauctions.app.dev=55:66",
        ]
    )
    def test_repeated_package_collects_its_fingerprints(self):
        # Staging signs its builds with whatever keystore is on the machine that built them, so one
        # package legitimately has several certificates. One statement each, fingerprints grouped.
        statements = self.client.get(ASSETLINKS).json()
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0]["target"]["sha256_cert_fingerprints"], ["11:22", "33:44"])
        self.assertEqual(statements[1]["target"]["package_name"], "com.fishauctions.app.dev")

    def test_apple_file_shape(self):
        details = self.client.get(AASA).json()["applinks"]["details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["appIDs"], IOS)
        components = details[0]["components"]
        # An allow-list read in order: the exclusions first, then everything else. The app is the
        # site, so "everything else" is the whole site.
        self.assertEqual(components[0], {"/": "/.well-known/*", "exclude": True})
        self.assertEqual(components[-1], {"/": "/*"})

    def test_apple_file_never_claims_its_own_directory(self):
        # An app launched to fetch its own association file is a loop with no purpose.
        self.assertIn("/.well-known/*", self._excluded())

    def _excluded(self):
        components = self.client.get(AASA).json()["applinks"]["details"][0]["components"]
        return [component["/"] for component in components if component.get("exclude")]

    def test_oauth_return_paths_are_never_handed_to_the_app(self):
        """The four paths a browser has to keep for a connect flow to finish.

        The app opens Square/PayPal/Mailchimp/Google Calendar OAuth in an auth session, and the code
        is exchanged here against the session that browsing context holds. Letting the OS hand the
        redirect to the app instead abandons the auth session mid-flow and lands the user on "your
        connection session expired" — which reads as having been signed out.

        Matched against the URLconf rather than against the literal list, so renaming or moving one
        of these routes fails the build instead of quietly un-excluding it.
        """
        excluded = self._excluded()
        for url_name in (
            "square_callback",
            "paypal_callback",
            "mailchimp_callback",
            "google_calendar_callback",
        ):
            path = reverse(url_name)
            self.assertTrue(
                any(fnmatchcase(path, pattern) for pattern in excluded),
                f"{url_name} ({path}) is claimed as a Universal Link. Add a pattern covering it to "
                f"app_links.IOS_EXCLUDED_PATHS; the OAuth callbacks must stay in the browser.",
            )

    def test_every_exclusion_precedes_the_catch_all(self):
        """iOS takes the first component that matches, so an exclusion after ``/*`` is inert."""
        components = self.client.get(AASA).json()["applinks"]["details"][0]["components"]
        catch_all = components.index({"/": "/*"})
        for position, component in enumerate(components):
            if component.get("exclude"):
                self.assertLess(position, catch_all, component)

    def test_ordinary_pages_are_still_claimed(self):
        """The exclusions are narrow. An over-broad pattern here would silently stop every emailed
        link opening in the app, with nothing logged anywhere the site can see."""
        excluded = self._excluded()
        for url_name in ("allLots", "auctions", "home", "account"):
            path = reverse(url_name)
            self.assertFalse(
                any(fnmatchcase(path, pattern) for pattern in excluded),
                f"{url_name} ({path}) is excluded from Universal Links by an over-broad pattern.",
            )


class AppLinksUnconfiguredTests(TestCase):
    """A deployment that claims no apps serves no file, rather than an empty one.

    An empty-but-valid ``assetlinks.json`` verifies successfully as "this site claims no apps" and is
    indistinguishable from a working setup; a 404 is something an operator can act on.
    """

    @override_settings(ANDROID_APP_LINKS=[], IOS_APP_LINKS=[])
    def test_both_404_when_unconfigured(self):
        self.assertEqual(self.client.get(ASSETLINKS).status_code, 404)
        self.assertEqual(self.client.get(AASA).status_code, 404)

    @override_settings(ANDROID_APP_LINKS=["=AA:BB", "com.fishauctions.app="], IOS_APP_LINKS=["  "])
    def test_half_written_entries_are_not_served(self):
        self.assertEqual(self.client.get(ASSETLINKS).status_code, 404)
        self.assertEqual(self.client.get(AASA).status_code, 404)

    @override_settings(ANDROID_APP_LINKS=ANDROID, IOS_APP_LINKS=[])
    def test_one_platform_configured_does_not_serve_the_other(self):
        self.assertEqual(self.client.get(ASSETLINKS).status_code, 200)
        self.assertEqual(self.client.get(AASA).status_code, 404)
