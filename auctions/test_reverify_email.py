"""A recent bounce holds back joining until the address is verified again."""

import datetime

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.models import Auction, AuctionTOS, Club, ClubMember, PickupLocation
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

    def test_the_button_unverifies_sends_the_email_and_the_link_lifts_the_gate(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        url = f"{reverse('reverify_email')}?next=/auctions/{self.open_auction.slug}/"
        response = self.client.post(url)
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self._refresh()
        self.assertFalse(self.address.verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.joiner.email])
        # A second click inside allauth's cooldown sends nothing more.
        self.client.post(url)
        self.assertEqual(len(mail.outbox), 1)

        from allauth.account.models import EmailConfirmationHMAC

        key = EmailConfirmationHMAC(self.address).key
        self.client.get(reverse("account_confirm_email", args=[key]))
        self._refresh()
        self.assertTrue(self.address.verified)
        self.assertIsNone(self.joiner.userdata.email_bounced_on)
        self.assertEqual(self.old_tos.email_address_status, "VALID")
        self.client.force_login(self.joiner)
        self._join()
        self.assertTrue(AuctionTOS.objects.filter(auction=self.open_auction, user=self.joiner).exists())

    def test_a_second_bounce_after_the_button_keeps_them_logged_in_and_gated(self):
        _bounce(self.joiner.email)
        self.client.force_login(self.joiner)
        self.client.post(reverse("reverify_email"))
        _bounce(self.joiner.email)
        self.assertTrue(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))
        # Still signed in, and still able to resend.
        self.assertEqual(self.client.get(reverse("reverify_email")).status_code, 200)

    def test_saving_an_already_verified_address_does_not_lift_the_gate(self):
        _bounce(self.joiner.email)
        self.address.primary = True
        self.address.save()
        self.assertTrue(needs_email_reverification(User.objects.get(pk=self.joiner.pk)))

    def test_the_page_sends_anyone_without_a_bounce_back(self):
        self.client.force_login(self.joiner)
        response = self.client.get(f"{reverse('reverify_email')}?next=/auctions/{self.open_auction.slug}/")
        self.assertRedirects(response, f"/auctions/{self.open_auction.slug}/", fetch_redirect_response=False)
        response = self.client.post(f"{reverse('reverify_email')}?next=https://evil.example.com/")
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.address.refresh_from_db()
        self.assertTrue(self.address.verified)

    def test_the_club_member_red_envelope_clears_too(self):
        club = Club.objects.create(name="Bounce club")
        member = ClubMember.objects.create(club=club, name="Member", email=self.joiner.email)
        _bounce(self.joiner.email)
        member.refresh_from_db()
        self.assertEqual(member.email_address_status, "BAD")
        self.address.verified = False
        self.address.save()
        self.address.verified = True
        self.address.save()
        member.refresh_from_db()
        self.assertEqual(member.email_address_status, "VALID")


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
