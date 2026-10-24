"""A recent bounce holds back joining until the address is verified again."""

import datetime
import re

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.models import Auction, AuctionTOS, Club, ClubMember, PickupLocation, UserData
from auctions.services import join_blocked_by_bounce, needs_email_reverification
from auctions.signals import bounce_handler
from auctions.tests import StandardTestCase


def _bounce(email, bounce_type="Transient", subtype="MailboxFull"):
    bounce_handler(
        sender=None,
        mail_obj={"destination": [email]},
        bounce_obj={
            "bounceType": bounce_type,
            "bounceSubType": subtype,
            "bouncedRecipients": [{"emailAddress": email}],
        },
        raw_message=None,
    )


class ReverifyEmailTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.open_auction = Auction.objects.create(
            created_by=self.user,
            title="Open online auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
        )
        self.open_location = PickupLocation.objects.create(
            name="open location", auction=self.open_auction, pickup_time=now + datetime.timedelta(days=2)
        )
        self.joiner = User.objects.create_user(username="full_inbox", password="testpassword", email="full@example.com")
        self.address = EmailAddress.objects.create(
            user=self.joiner, email=self.joiner.email, verified=True, primary=True
        )
        self.old_tos = AuctionTOS.objects.create(
            user=self.joiner, auction=self.online_auction, pickup_location=self.location, email=self.joiner.email
        )

    def _join(self):
        self.client.force_login(self.joiner)
        return self.client.post(
            reverse("auction_main", kwargs={"slug": self.open_auction.slug}),
            {"i_agree": "on", "pickup_location": str(self.open_location.pk), "time_spent_reading_rules": "5"},
        )

    def _refresh(self):
        self.joiner.userdata.refresh_from_db()
        self.address.refresh_from_db()
        self.old_tos.refresh_from_db()

    def test_a_full_inbox_sends_join_to_the_reverify_page(self):
        _bounce("FULL@example.com")
        self.old_tos.refresh_from_db()
        self.assertEqual(self.old_tos.email_address_status, "BAD")
        response = self._join()
        self.assertRedirects(
            response,
            f"{reverse('reverify_email')}?next=%2Fauctions%2F{self.open_auction.slug}%2F",
            fetch_redirect_response=False,
        )
        self.assertFalse(AuctionTOS.objects.filter(auction=self.open_auction, user=self.joiner).exists())
        page = self.client.get(response.url)
        self.assertContains(page, "Re-verify your email address")
        self.assertContains(page, "Send verification email")

    def test_a_bounce_that_is_not_the_inboxes_fault_does_not_block(self):
        _bounce(self.joiner.email, subtype="MessageTooLarge")
        self.assertFalse(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))
        self._join()
        self.assertTrue(AuctionTOS.objects.filter(auction=self.open_auction, user=self.joiner).exists())

    def test_an_old_bounce_does_not_block(self):
        self.joiner.userdata.email_bounced_on = timezone.now() - datetime.timedelta(days=91)
        self.joiner.userdata.save()
        self.assertFalse(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))

    def test_someone_an_organizer_already_added_is_not_blocked(self):
        _bounce(self.joiner.email, bounce_type="Permanent", subtype="General")
        AuctionTOS.objects.create(
            auction=self.open_auction, pickup_location=self.open_location, email="Full@example.com"
        )
        self.assertFalse(join_blocked_by_bounce(self.joiner, self.open_auction))
        self._join()
        self.assertTrue(AuctionTOS.objects.filter(auction=self.open_auction, user=self.joiner).exists())

    def _link(self, message):
        return re.search(r"https?://[^/\s]+(/reverify_email/[^\s\"<]+/)", message.body).group(1)

    def test_the_button_sends_the_email_and_the_link_lifts_the_gate(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        url = f"{reverse('reverify_email')}?next=/auctions/{self.open_auction.slug}/"
        response = self.client.post(url)
        self.assertRedirects(
            response,
            f"{reverse('reverify_email')}?next=%2Fauctions%2F{self.open_auction.slug}%2F",
            fetch_redirect_response=False,
        )
        self._refresh()
        # allauth's flag is never touched: unverified, they couldn't sign in.
        self.assertTrue(self.address.verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.joiner.email])
        # A second click inside allauth's cooldown sends nothing more.
        self.client.post(url)
        self.assertEqual(len(mail.outbox), 1)

        self.client.logout()
        response = self.client.get(self._link(mail.outbox[0]))
        self.assertRedirects(response, f"/auctions/{self.open_auction.slug}/", fetch_redirect_response=False)
        self._refresh()
        self.assertIsNone(self.joiner.userdata.email_bounced_on)
        self.assertEqual(self.old_tos.email_address_status, "VALID")
        self._join()
        self.assertTrue(AuctionTOS.objects.filter(auction=self.open_auction, user=self.joiner).exists())

    def test_the_button_drops_a_next_that_points_off_site(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        for next_url in ("https://evil.example.com/", "//evil.example.com/"):
            response = self.client.post(f"{reverse('reverify_email')}?next={next_url}&x=1")
            self.assertRedirects(response, reverse("reverify_email"), fetch_redirect_response=False)
        # One of ours keeps its own query string.
        response = self.client.post(f"{reverse('reverify_email')}?next=%2Fauctions%2F%3Ftab%3Drules%26x%3D1")
        self.assertRedirects(
            response,
            f"{reverse('reverify_email')}?next=%2Fauctions%2F%3Ftab%3Drules%26x%3D1",
            fetch_redirect_response=False,
        )

    def test_a_link_for_an_old_address_or_a_forged_one_does_nothing(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        self.client.post(reverse("reverify_email"))
        link = self._link(mail.outbox[0])
        self.joiner.email = "changed@example.com"
        self.joiner.save()
        self.client.get(link)
        self.client.get(link[:-3] + "xx/")
        self._refresh()
        self.assertIsNotNone(self.joiner.userdata.email_bounced_on)

    def test_a_link_sent_before_a_newer_bounce_no_longer_works(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        self.client.post(reverse("reverify_email"))
        UserData.objects.filter(user=self.joiner).update(
            email_bounced_on=timezone.now() + datetime.timedelta(seconds=5)
        )
        self.client.get(self._link(mail.outbox[0]))
        self.assertTrue(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))

    def test_confirming_a_new_address_through_allauth_lifts_it_too(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        new_address = EmailAddress.objects.create(user=self.joiner, email="working@example.com", verified=False)
        from allauth.account.models import EmailConfirmationHMAC

        self.client.get(reverse("account_confirm_email", args=[EmailConfirmationHMAC(new_address).key]))
        joiner = User.objects.get(pk=self.joiner.pk)
        self.assertEqual(joiner.email, "working@example.com")
        self.assertFalse(needs_email_reverification(joiner))

    def test_hide_my_email_and_general_soft_bounces_never_gate(self):
        relay = User.objects.create_user(username="relay", password="x", email="abc@privaterelay.appleid.com")
        _bounce(relay.email, bounce_type="Permanent", subtype="General")
        _bounce(self.joiner.email, subtype="General")
        self.assertFalse(needs_email_reverification(User.objects.get(pk=relay.pk)))
        self.assertFalse(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))

    def test_a_second_bounce_after_the_button_keeps_them_gated_but_signed_in(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        self.client.post(reverse("reverify_email"))
        _bounce(self.joiner.email)
        self.assertTrue(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))
        self.assertTrue(EmailAddress.objects.get(pk=self.address.pk).verified)
        self.assertEqual(self.client.get(reverse("reverify_email")).status_code, 200)

    def test_the_club_join_waits_too_when_it_would_add_them_to_auctions(self):
        club = Club.objects.create(name="Bounce club", allow_joining=True)
        self.open_auction.club = club
        self.open_auction.manage_users_through_club = "all"
        self.open_auction.save()
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        response = self.client.post(reverse("club_detail", kwargs={"slug": club.slug}), {"action": "join"})
        self.assertTrue(response.url.startswith(reverse("reverify_email")))
        self.assertFalse(ClubMember.objects.filter(club=club, user=self.joiner).exists())

    def test_the_page_sends_anyone_without_a_bounce_back(self):
        self.client.force_login(self.joiner)
        response = self.client.get(f"{reverse('reverify_email')}?next=/auctions/{self.open_auction.slug}/")
        self.assertRedirects(response, f"/auctions/{self.open_auction.slug}/", fetch_redirect_response=False)
        response = self.client.post(f"{reverse('reverify_email')}?next=https://evil.example.com/")
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.address.refresh_from_db()
        self.assertTrue(self.address.verified)


class MobileJoinBounceTests(ReverifyEmailTests.__base__):
    def test_the_app_welcome_join_is_refused_with_the_page_to_go_to(self):
        now = timezone.now()
        venue = Auction.objects.create(
            created_by=self.user,
            title="At the venue",
            is_online=False,
            date_start=now,
            date_end=now + datetime.timedelta(hours=6),
        )
        venue.save()
        joiner = User.objects.create_user(username="app_joiner", password="x", email="app@example.com")
        joiner.userdata.email_bounced_on = now
        joiner.userdata.save()
        response = self.client.post(
            reverse("mobile-checkin-join"),
            data={"auction": venue.slug},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(joiner).access_token}",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("/reverify_email/", response.json()["reverify_url"])
        self.assertFalse(AuctionTOS.objects.filter(auction=venue, user=joiner).exists())
