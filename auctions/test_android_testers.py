import uuid

from django.test import override_settings
from django.urls import reverse

from auctions import help_guides, palette_routes
from auctions.models import MobileDevice
from auctions.tests import StandardTestCase

GROUP = "https://groups.google.com/g/example-testers"
ANDROID_BROWSER = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/129.0 Mobile Safari/537.36"
ANDROID_APP = ANDROID_BROWSER + " FishAuctionsApp/2026.9.26 (Flutter; Android)"
BANNER = "Help test the Android app</a>"


@override_settings(PLAY_TESTERS_GROUP_URL=GROUP, PLAY_STORE_URL="")
class AndroidTestersTests(StandardTestCase):
    def test_the_page_links_the_group_and_the_opt_in(self):
        response = self.client.get(reverse("android_testers"))
        self.assertContains(response, f'href="{GROUP}"')
        self.assertContains(response, 'href="https://play.google.com/apps/testing/com.fishauctions.app"')

    @override_settings(PLAY_TESTERS_GROUP_URL="")
    def test_no_group_no_page(self):
        self.assertEqual(self.client.get(reverse("android_testers")).status_code, 404)
        self.assertNotIn("android_testers", {r.key for r in palette_routes._permitted_routes(self.user)})

    @override_settings(PLAY_STORE_URL="https://play.google.com/store/apps/details?id=com.fishauctions.app")
    def test_once_public_it_sends_people_to_the_store(self):
        response = self.client.get(reverse("android_testers"))
        self.assertRedirects(
            response,
            "https://play.google.com/store/apps/details?id=com.fishauctions.app",
            fetch_redirect_response=False,
        )
        self.assertNotContains(self._home(ANDROID_BROWSER), BANNER)

    def _home(self, user_agent):
        self.client.force_login(self.user)
        return self.client.get(reverse("faq"), HTTP_USER_AGENT=user_agent)

    def test_banner_on_an_android_browser(self):
        self.assertContains(self._home(ANDROID_BROWSER), BANNER)

    def test_no_banner_elsewhere(self):
        self.assertNotContains(self._home("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), BANNER)
        self.assertNotContains(self._home(ANDROID_APP), BANNER)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("faq"), HTTP_USER_AGENT=ANDROID_BROWSER), BANNER)

    def test_no_banner_once_dismissed_or_already_in_the_app(self):
        self.client.cookies["hide_android_testers"] = "true"
        self.assertNotContains(self._home(ANDROID_BROWSER), BANNER)
        del self.client.cookies["hide_android_testers"]
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), platform=MobileDevice.PLATFORM_ANDROID)
        self.assertNotContains(self._home(ANDROID_BROWSER), BANNER)

    def test_the_app_guide_links_it(self):
        response = self.client.get(help_guides.GUIDES["mobile-app"].url)
        self.assertContains(response, f'href="{reverse("android_testers")}"')
