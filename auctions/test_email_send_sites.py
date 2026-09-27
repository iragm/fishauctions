"""Every templated email, sent through post_office with real model objects, the way the site sends it.

The templates live in the database and are edited by data migrations, so nothing but a render catches
a broken one, and a render with stand-in objects misses an attribute a real model doesn't have. Where
calling the real send site is cheap it is called; otherwise ``mail.send`` gets the same context keys
the send site passes, named in a comment beside it.
"""

from unittest.mock import PropertyMock, patch

from django.contrib.sites.models import Site
from django.test import override_settings
from django.urls import reverse
from post_office import mail
from post_office.models import Email

from auctions.models import Club, ClubHistory, ClubMember, Lot
from auctions.tests import StandardTestCase

ADDRESS = "PO Box 1, Burlington VT 05401"
ICON = "/media/club_icons/tfcb.png"


@override_settings(MAILING_ADDRESS=ADDRESS, NAVBAR_BRAND="Fish Auctions")
class SendSiteTests(StandardTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.club = Club.objects.create(mailing_address="PO Box 1, Springfield IL 62701", name="Tropical Fish Club")
        for auction in (cls.online_auction, cls.in_person_auction):
            auction.club = cls.club
            auction.save()
        for user, first in ((cls.user, "Jamie"), (cls.admin_user, "Ira"), (cls.user_with_no_lots, "Wes")):
            user.first_name = first
            user.save()
        for tos in (cls.online_tos, cls.in_person_tos):
            tos.name = "Jamie Smith"
            tos.save()

    def setUp(self):
        super().setUp()
        patcher = patch.object(Club, "icon_thumbnail_url", new_callable=PropertyMock, return_value=ICON)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.domain = Site.objects.get_current().domain
        self.organizer = self.online_auction.created_by.first_name

    def sent(self, to):
        return Email.objects.filter(to=to).latest("id")

    def check(self, email, greeting, club_header):
        """What every one of them has to get right."""
        html, text = email.html_message, email.message
        for part in (html, text):
            self.assertNotIn("{%", part)
            self.assertNotIn("{{", part)
            self.assertNotIn("Best wishes", part)
            self.assertIn(ADDRESS, part)
        self.assertTrue(html.startswith("<!DOCTYPE html>"), html[:60])
        self.assertIn(f"{greeting}<br>", html.replace("</p>", "<br>"))
        self.assertTrue(text.strip().startswith(greeting), text[:60])
        self.assertIn("icon-footer.png", html)
        if club_header:
            self.assertIn(f'src="https://{self.domain}{ICON}"', html)
            self.assertIn("Tropical Fish Club", html)
        else:
            self.assertNotIn(ICON, html)

    def test_tos_notifications(self):
        from auctions.management.commands.auctiontos_notifications import send_tos_notification

        for template, tos in (
            ("online_auction_welcome", self.online_tos),
            ("in_person_auction_welcome", self.in_person_tos),
            ("auction_print_reminder", self.in_person_tos),
            ("reprint_reminder", self.in_person_tos),
        ):
            with self.subTest(template):
                Email.objects.all().delete()
                send_tos_notification(template, tos)
                self.check(self.sent(tos.user.email), "Hey Jamie,", club_header=True)

    def test_organizer_notes(self):
        # auctiontos_notifications: {"domain", "tos"}
        for template in ("wrong_location_selected", "user_joined_auction_despite_ban"):
            with self.subTest(template):
                mail.send(
                    "admin@example.com", template=template, context={"domain": self.domain, "tos": self.online_tos}
                )
                self.check(self.sent("admin@example.com"), f"Hey {self.organizer},", club_header=False)

    def test_invoice_ready(self):
        from auctions.tasks import send_invoice_notification

        userdata = self.online_auction.created_by.userdata
        userdata.is_trusted = True
        userdata.save()
        self.online_auction.email_users_when_invoices_ready = True
        self.online_auction.save()
        self.invoice.status = "UNPAID"
        self.invoice.save()
        self.online_tos.email = "jamie@example.com"
        self.online_tos.save()
        send_invoice_notification(self.invoice.pk)
        email = self.sent("jamie@example.com")
        self.check(email, "Hey Jamie,", club_header=True)
        self.assertIn(f"/invoices/{self.invoice.no_login_link}/", email.html_message)

    def test_outbid(self):
        # bidding.py: {"name", "domain", "lot"}
        mail.send(
            "jamie@example.com",
            template="outbid_notification",
            context={"name": "Jamie", "domain": self.domain, "lot": self.lot},
        )
        email = self.sent("jamie@example.com")
        self.check(email, "Hey Jamie,", club_header=True)
        self.assertIn("Increase your bid", email.html_message)

    def test_join_reminder(self):
        # auctiontos_notifications: {"domain", "auction", "uuid", "lots", "user", "unsubscribe", ...}
        mail.send(
            "jamie@example.com",
            template="join_auction_reminder",
            context={
                "domain": self.domain,
                "auction": self.online_auction,
                "uuid": "u",
                "lots": [self.lot],
                "user": self.user,
                "unsubscribe": self.user.userdata.unsubscribe_link,
            },
        )
        email = self.sent("jamie@example.com")
        self.check(email, "Hey Jamie,", club_header=True)
        self.assertIn(f"/unsubscribe/{self.user.userdata.unsubscribe_link}/", email.html_message)

    def test_organizer_auction_emails(self):
        # auction_emails: {"auction", "domain", "unsubscribe"} (+ "subject", "enable_help" for the welcome)
        for template in ("auction_welcome", "auction_invoices", "auction_thanks"):
            with self.subTest(template):
                mail.send(
                    "admin@example.com",
                    template=template,
                    context={
                        "auction": self.online_auction,
                        "domain": self.domain,
                        "unsubscribe": "tok",
                        "subject": "Your auction is ready",
                        "enable_help": True,
                    },
                )
                email = self.sent("admin@example.com")
                self.check(email, f"Hey {self.organizer},", club_header=False)
                # One opt-out, the footer's.
                self.assertEqual(email.html_message.count("/unsubscribe/tok/"), 1)
                self.assertEqual(email.message.count("/unsubscribe/tok/"), 1)

    def test_help_is_a_bullet_in_the_welcome(self):
        mail.send(
            "admin@example.com",
            template="auction_welcome",
            context={"auction": self.online_auction, "domain": self.domain, "subject": "s", "enable_help": True},
        )
        self.assertIn("<li><a href=", self.sent("admin@example.com").html_message)

    def test_non_auction_lot_emails(self):
        lot = Lot.objects.create(
            lot_name="Guppies", user=self.user, winner=self.user_with_no_lots, winning_price=5, quantity=1
        )
        lot.send_non_auction_lot_emails()
        self.check(self.sent(self.user_with_no_lots.email), "Hey Wes,", club_header=False)
        self.check(self.sent(self.user.email), "Hey Jamie,", club_header=False)

    def test_lot_ended_relist(self):
        # endauctions: {"domain", "lot", "unsubscribe"}
        self.lot.user = self.user
        mail.send("jamie@example.com", template="lot_ended_relist", context={"domain": self.domain, "lot": self.lot})
        email = self.sent("jamie@example.com")
        self.check(email, "Hey Jamie,", club_header=False)
        self.assertNotIn("//lots/new", email.message + email.html_message)

    def test_site_notices(self):
        # sendnotifications: {"domain", "name"}; email_unseen_chats: {"name", "domain", "data", "unsubscribe"}
        for template, context in (
            ("watched_items_ending", {"domain": self.domain, "name": "Jamie"}),
            ("unread_chat_messages", {"domain": self.domain, "name": "Jamie", "data": self.user.userdata}),
        ):
            with self.subTest(template):
                mail.send("jamie@example.com", template=template, context=context)
                self.check(self.sent("jamie@example.com"), "Hey Jamie,", club_header=False)

    def test_missing_name_greets_there(self):
        self.online_tos.name = ""
        self.online_tos.save()
        from auctions.management.commands.auctiontos_notifications import send_tos_notification

        send_tos_notification("online_auction_welcome", self.online_tos)
        self.check(self.sent(self.online_tos.user.email), "Hey there,", club_header=True)

    def send_membership_email(self):
        from auctions.tasks import send_club_member_email

        self.member = ClubMember.objects.create(club=self.club, name="Jamie Smith", email="member@example.com")
        with patch("auctions.tasks.mail.send") as send:
            sent = send_club_member_email(self.member, "Welcome", "Your membership is active.", force_email=True)
        return sent, send

    def test_membership_email(self):
        _sent, send = self.send_membership_email()
        kwargs = send.call_args.kwargs
        self.assertTrue(kwargs["html_message"].startswith("<!DOCTYPE html>"))
        self.assertIn("Hey Jamie,", kwargs["html_message"])
        self.assertTrue(kwargs["message"].startswith("Hey Jamie,"))
        self.assertIn(f'src="https://{self.domain}{ICON}"', kwargs["html_message"])
        self.assertIn("View your membership", kwargs["html_message"])

    def test_membership_email_footer_names_the_club_and_how_to_leave(self):
        """CASL: the sender, its postal address, a way to reach it, and an unsubscribe link, in both parts."""
        _sent, send = self.send_membership_email()
        kwargs = send.call_args.kwargs
        unsubscribe = reverse(
            "club_member_contact_pref", kwargs={"slug": self.club.url_key, "uuid": self.member.uuid, "level": "none"}
        )
        for part in (kwargs["html_message"], kwargs["message"]):
            self.assertIn("Sent by Tropical Fish Club via Fish Auctions", part)
            self.assertIn("PO Box 1, Springfield IL 62701", part)
            self.assertIn(kwargs["headers"]["Reply-to"], part)
            self.assertIn(ADDRESS, part)
            self.assertIn(f"https://{self.domain}{unsubscribe}", part)
        # The club's own footer, not the site's promotional opt-out.
        self.assertNotIn("Stop promotional emails", kwargs["html_message"])

    def test_the_unsubscribe_link_works(self):
        self.send_membership_email()
        url = reverse(
            "club_member_contact_pref", kwargs={"slug": self.club.url_key, "uuid": self.member.uuid, "level": "none"}
        )
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url)
        self.member.refresh_from_db()
        self.assertEqual(self.member.contact_status, "do_not_contact")

    def test_a_club_named_atop_its_own_address_is_not_named_twice(self):
        Club.objects.filter(pk=self.club.pk).update(mailing_address="Tropical Fish Club\nPO Box 1\nSpringfield IL")
        self.club.refresh_from_db()
        _sent, send = self.send_membership_email()
        self.assertIn(
            "Sent by Tropical Fish Club via Fish Auctions\nPO Box 1, Springfield IL", send.call_args.kwargs["message"]
        )

    def test_no_membership_email_without_a_mailing_address(self):
        Club.objects.filter(pk=self.club.pk).update(mailing_address="")
        self.club.refresh_from_db()
        sent, send = self.send_membership_email()
        self.assertFalse(sent)
        self.assertFalse(send.called)
        self.assertTrue(ClubHistory.objects.filter(club=self.club, action__contains="has no mailing address").exists())

    def test_one_line_address_that_starts_with_the_club_name_is_kept_whole(self):
        Club.objects.filter(pk=self.club.pk).update(mailing_address="Tropical Fish Club, PO Box 5, Burlington VT")
        self.club.refresh_from_db()
        _sent, send = self.send_membership_email()
        self.assertIn("Tropical Fish Club, PO Box 5, Burlington VT", send.call_args.kwargs["message"])

    def test_an_address_cannot_be_cleared_from_club_settings(self):
        from auctions.forms import ClubEditForm

        form = ClubEditForm(data={"name": self.club.name, "mailing_address": ""}, instance=self.club)
        self.assertFalse(form.is_valid())
        self.assertIn("mailing_address", form.errors)

    def test_blank_club_member_name_greets_there(self):
        """AuctionTOS stores "Unknown" for a blank name, and club-managed members share it."""
        from auctions.tasks import send_club_member_email

        member = ClubMember.objects.create(club=self.club, name="Unknown", email="u@example.com")
        with patch("auctions.tasks.mail.send") as send:
            send_club_member_email(member, "Welcome", "Hi", force_email=True)
        self.assertTrue(send.call_args.kwargs["message"].startswith("Hey there,"))


class SuiteMailBackendTests(StandardTestCase):
    def test_post_office_never_reaches_a_real_server(self):
        """The runner, not the environment, picks post_office's backend: the dev env's is a real SMTP login."""
        from post_office.settings import get_backend

        self.assertEqual(get_backend(), "django.core.mail.backends.locmem.EmailBackend")
