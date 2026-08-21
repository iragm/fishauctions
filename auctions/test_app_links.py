"""Part LINKS — the two files that make a site link open in the app.

Both are fetched by Google's and Apple's infrastructure rather than by the app, both are checked once
and cached, and both fail *silently* when they are wrong: links simply keep opening in the browser
with nothing logged anywhere the site can see. So the things worth pinning down here are the ones
nobody would notice breaking — the exact paths, the content type, that no authentication is needed,
and that neither one ever answers with a redirect.
"""

from django.test import TestCase, override_settings

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
        components = self.client.get(AASA).json()["applinks"]["details"][0]["components"]
        excluded = [c["/"] for c in components if c.get("exclude")]
        self.assertIn("/.well-known/*", excluded)


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
