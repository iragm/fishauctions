import datetime
import uuid
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from post_office.models import Email

from auctions import help_guides, palette_routes
from auctions.models import MobileDevice, PageView, User, UserData
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

    def test_the_page_links_the_app_guide(self):
        response = self.client.get(reverse("android_testers"))
        self.assertContains(response, f'href="{help_guides.GUIDES["mobile-app"].url}"')

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


@override_settings(PLAY_TESTERS_GROUP_URL=GROUP, PLAY_STORE_URL="")
class AndroidTestersInviteTests(StandardTestCase):
    def _android_user(self, name, joined_days_ago=400, viewed_days_ago=10, platform="Android", email=None):
        user = User.objects.create_user(name, email or f"{name}@example.com", "x")
        User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - datetime.timedelta(days=joined_days_ago))
        view = PageView.objects.create(user=user, platform=platform, url="/")
        PageView.objects.filter(pk=view.pk).update(date_start=timezone.now() - datetime.timedelta(days=viewed_days_ago))
        return user

    def _sent_to(self):
        return {
            address
            for to in Email.objects.filter(template__name="android_testers_invite").values_list("to", flat=True)
            for address in to
        }

    def test_only_old_accounts_recently_on_android(self):
        self._android_user("fits")
        self._android_user("too-new", joined_days_ago=30)
        self._android_user("too-long-ago", viewed_days_ago=120)
        self._android_user("iphone", platform="iOS")
        UserData.objects.filter(user=self._android_user("unsubscribed")).update(has_unsubscribed=True)
        UserData.objects.filter(user=self._android_user("leaving")).update(account_deletion_requested=timezone.now())
        call_command("android_testers_invite", stdout=StringIO())
        self.assertEqual(self._sent_to(), {"fits@example.com"})

    def test_the_email_links_the_group_play_and_the_guide(self):
        self._android_user("fits")
        call_command("android_testers_invite", stdout=StringIO())
        html = Email.objects.get(template__name="android_testers_invite").html_message
        self.assertIn(f'href="{GROUP}"', html)
        self.assertIn('href="https://play.google.com/apps/testing/com.fishauctions.app"', html)
        self.assertIn(help_guides.GUIDES["mobile-app"].url, html)

    def test_a_rerun_sends_nothing_new_even_after_the_mail_log_is_purged(self):
        self._android_user("fits")
        call_command("android_testers_invite", stdout=StringIO())
        Email.objects.all().delete()
        call_command("android_testers_invite", stdout=StringIO())
        self.assertFalse(self._sent_to())

    def test_one_email_per_inbox(self):
        self._android_user("first", email="Shared@example.com")
        self._android_user("second", email="shared@example.com")
        call_command("android_testers_invite", stdout=StringIO())
        self.assertEqual(len(self._sent_to()), 1)

    def test_a_bad_address_does_not_stop_the_run(self):
        self._android_user("bad", email="not-an-address")
        self._android_user("good")
        call_command("android_testers_invite", stdout=StringIO())
        self.assertEqual(self._sent_to(), {"good@example.com"})

    def test_dry_run_sends_nothing(self):
        self._android_user("fits")
        out = StringIO()
        call_command("android_testers_invite", "--dry-run", stdout=out)
        self.assertIn("would email 1", out.getvalue())
        self.assertFalse(self._sent_to())

    @override_settings(PLAY_TESTERS_GROUP_URL="")
    def test_refuses_without_the_closed_test(self):
        with self.assertRaises(CommandError):
            call_command("android_testers_invite", stdout=StringIO())
