"""The mobile app's own crash reports: ``POST /api/mobile/crashes/``, grouping, and ``list_app_crashes``."""

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from auctions import app_crashes
from auctions.models import AppCrash
from auctions.test_mcp_admin import AdminEndpointCase
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

DART_STACK = """#0      LabelService.fetch (package:fishauctions_application/services/label_service.dart:{line}:7)
<asynchronous suspension>
#1      PrintScreen._print (package:fishauctions_application/screens/print_screen.dart:88:5)
"""


def crash(**extra):
    return {
        "kind": "dart",
        "platform": "android",
        "message": "StateError: Bad state: No element",
        "stack": DART_STACK.format(line=41),
        "app_version": "1.0.0+12",
        **extra,
    }


class FingerprintTests(SimpleTestCase):
    def test_moved_line_numbers_are_the_same_bug(self):
        self.assertEqual(
            app_crashes.fingerprint("dart", "android", "StateError: Bad state: No element", DART_STACK.format(line=41)),
            app_crashes.fingerprint("dart", "android", "StateError: Bad state: other", DART_STACK.format(line=97)),
        )

    def test_a_dart_error_is_one_bug_on_both_platforms(self):
        stack = DART_STACK.format(line=41)
        self.assertEqual(
            app_crashes.fingerprint("dart", "android", "StateError: x", stack),
            app_crashes.fingerprint("dart", "ios", "StateError: x", stack),
        )

    def test_another_error_type_is_another_bug(self):
        stack = DART_STACK.format(line=41)
        self.assertNotEqual(
            app_crashes.fingerprint("dart", "android", "StateError: x", stack),
            app_crashes.fingerprint("dart", "android", "RangeError: x", stack),
        )

    def test_native_addresses_do_not_split_a_bug(self):
        one = "#00 pc 000000000004f1a8  /apex/libc.so (abort+164)\n#01 pc 0000000000123abc  /lib/libflutter.so"
        two = "#00 pc 000000000004f2b0  /apex/libc.so (abort+164)\n#01 pc 0000000000777def  /lib/libflutter.so"
        self.assertEqual(
            app_crashes.fingerprint("native", "android", "SIGABRT", one),
            app_crashes.fingerprint("native", "android", "SIGABRT", two),
        )


@isolated_cache("app-crashes")
class CrashEndpointTests(StandardTestCase):
    url = reverse("mobile-crashes")

    def post(self, body, **headers):
        return self.client.post(self.url, body, content_type="application/json", **headers)

    def test_a_signed_out_phone_can_report(self):
        response = self.post({"crashes": [crash()]})
        self.assertEqual(response.status_code, 201, response.content)
        row = AppCrash.objects.get()
        self.assertIsNone(row.user)
        self.assertEqual(row.app_version, "1.0.0+12")
        self.assertEqual(len(row.fingerprint), 40)

    def test_a_signed_in_report_is_linked_to_its_account(self):
        token = RefreshToken.for_user(self.user).access_token
        self.post({"crashes": [crash()]}, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(AppCrash.objects.get().user, self.user)

    def test_a_stale_token_is_anonymous_not_refused(self):
        response = self.post({"crashes": [crash()]}, HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertEqual(response.status_code, 201, response.content)

    def test_long_text_is_cut_not_refused(self):
        self.post({"crashes": [crash(stack="x" * (app_crashes.STACK_CHARS + 50), os_version="y" * 300)]})
        row = AppCrash.objects.get()
        self.assertEqual(len(row.stack), app_crashes.STACK_CHARS)
        self.assertEqual(len(row.os_version), 100)

    def test_an_unknown_kind_is_refused(self):
        response = self.post({"crashes": [crash(kind="oops")]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AppCrash.objects.exists())

    def test_a_batch_is_capped(self):
        response = self.post({"crashes": [crash()] * 21})
        self.assertEqual(response.status_code, 400)


class ListAppCrashesTests(AdminEndpointCase):
    def setUp(self):
        super().setUp()
        other = User.objects.create_user("other_phone", "other@example.com", "testpassword")
        app_crashes.record(self.user, crash())
        app_crashes.record(other, crash(platform="ios", app_version="1.0.0+13", stack=DART_STACK.format(line=50)))
        app_crashes.record(None, crash(kind="native", message="SIGSEGV", stack="#00 pc 0000beef /lib/libapp.so"))

    def test_crashes_are_grouped_into_bugs(self):
        result = self.call("list_app_crashes")
        self.assertFalse(result["isError"], result)
        self.assertIn("3 app crashes in 7 days, 2 distinct.", result["content"][0]["text"])
        bugs = {bug["kind"]: bug for bug in result["structuredContent"]["bugs"]}
        dart = bugs["dart"]
        self.assertEqual((dart["times"], dart["people"]), (2, 2))
        self.assertEqual(dart["platforms"], ["android", "ios"])
        self.assertEqual(dart["app_versions"], ["1.0.0+12", "1.0.0+13"])

    def test_what_a_phone_wrote_is_fenced(self):
        bug = self.call("list_app_crashes", {"platform": "android"})["structuredContent"]["bugs"][0]
        self.assertTrue(bug["message"].startswith("«"), bug["message"])
        self.assertTrue(bug["stack"].endswith("»"), bug["stack"])

    def test_one_bug_by_fingerprint_prefix(self):
        fingerprint = AppCrash.objects.filter(kind="native").get().fingerprint
        bugs = self.call("list_app_crashes", {"fingerprint": fingerprint[:8]})["structuredContent"]["bugs"]
        self.assertEqual([bug["fingerprint"] for bug in bugs], [fingerprint[:12]])

    def test_site_health_counts_them(self):
        result = self.call("site_health")
        self.assertEqual(result["structuredContent"]["app_crashes_last_24h"], {"crashes": 3, "bugs": 2})
        self.assertIn("the app reported 3 crashes", result["content"][0]["text"])
