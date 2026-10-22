"""The far-from-pickup location check, its "yes, my location is accurate" box, and the setup box for experienced creators."""

import datetime

from django.contrib.sites.models import Site
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from post_office import mail
from post_office.models import Email

from auctions.forms import UserLocation
from auctions.models import Auction, AuctionTOS, PickupLocation
from auctions.tests import StandardTestCase

# Burlington, VT and Boston, MA: about 180 miles apart.
NEAR = (44.48, -73.21)
FAR = (42.36, -71.06)


class LocationCheckTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=5)
        self.online_auction.save()
        self.location.latitude, self.location.longitude = NEAR
        self.location.save()
        self.place_user(FAR)
        # Only this row is due; the fixture's others are someone else's business.
        AuctionTOS.objects.update(location_check_email_sent=True)
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(
            location_check_email_sent=False, createdon=timezone.now() - datetime.timedelta(hours=2)
        )

    def place_user(self, point, confirmed=False):
        userdata = self.user.userdata
        userdata.latitude, userdata.longitude = point
        userdata.location_confirmed = confirmed
        userdata.save()

    def sent(self):
        return Email.objects.filter(template__name="location_check", to=self.user.email)

    def test_far_away_is_asked_once(self):
        call_command("auctiontos_notifications")
        self.assertEqual(self.sent().count(), 1)
        self.assertIn(reverse("contact_info"), self.sent().get().html_message)
        call_command("auctiontos_notifications")
        self.assertEqual(self.sent().count(), 1)

    def test_in_person_too(self):
        self.in_person_location.latitude, self.in_person_location.longitude = NEAR
        self.in_person_location.save()
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(location_check_email_sent=True)
        AuctionTOS.objects.filter(pk=self.in_person_tos.pk).update(
            location_check_email_sent=False, createdon=timezone.now() - datetime.timedelta(hours=2)
        )
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(days=5)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=6)
        self.in_person_auction.save()
        call_command("auctiontos_notifications")
        self.assertEqual(self.sent().count(), 1)

    def test_nearby_confirmed_new_or_added_are_not_asked(self):
        cases = {
            "nearby": lambda: self.place_user(NEAR),
            "confirmed": lambda: self.place_user(FAR, confirmed=True),
            "within the hour": lambda: AuctionTOS.objects.filter(pk=self.online_tos.pk).update(
                createdon=timezone.now()
            ),
            "added by the organizer": lambda: AuctionTOS.objects.filter(pk=self.online_tos.pk).update(
                manually_added=True
            ),
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.place_user(FAR)
                AuctionTOS.objects.filter(pk=self.online_tos.pk).update(
                    location_check_email_sent=False,
                    manually_added=False,
                    createdon=timezone.now() - datetime.timedelta(hours=2),
                )
                arrange()
                call_command("auctiontos_notifications")
                self.assertEqual(self.sent().count(), 0)

    def test_the_email_renders(self):
        mail.send(
            self.user.email,
            template="location_check",
            context={"domain": Site.objects.get_current().domain, "tos": self.online_tos},
        )
        email = self.sent().get()
        for part in (email.html_message, email.message):
            self.assertNotIn("{{", part)
            self.assertNotIn("{%", part)
            self.assertIn("miles away", part)

    def test_wrong_location_email_says_how_far(self):
        mail.send(
            "organizer@example.com",
            template="wrong_location_selected",
            context={"domain": Site.objects.get_current().domain, "tos": self.online_tos},
        )
        email = Email.objects.get(to="organizer@example.com")
        self.assertRegex(email.message, r"from about \d+ miles away")


class ConfirmBoxTests(StandardTestCase):
    def test_only_once_a_location_is_saved(self):
        userdata = self.user.userdata
        userdata.latitude = userdata.longitude = 0
        userdata.save()
        self.assertNotIn("location_confirmed", UserLocation(instance=userdata).fields)
        userdata.latitude, userdata.longitude = NEAR
        userdata.save()
        self.assertIn("location_confirmed", UserLocation(instance=userdata).fields)

    def test_ticking_it_saves(self):
        userdata = self.user.userdata
        userdata.latitude, userdata.longitude = NEAR
        userdata.address = "1 Main St"
        userdata.save()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("contact_info")), "Yes, my location is accurate")
        self.client.post(
            reverse("contact_info"),
            {
                "first_name": "Jamie",
                "last_name": "Smith",
                "address": "1 Main St",
                "location_coordinates": f"{NEAR[0]},{NEAR[1]}",
                "location_confirmed": "on",
            },
        )
        userdata.refresh_from_db()
        self.assertTrue(userdata.location_confirmed)


class SetupBoxTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.creator = self.user_who_does_not_join
        self.auction = Auction.objects.create(
            created_by=self.creator,
            title="Set up but nobody joined",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=8),
            summernote_description="Our rules",
        )
        PickupLocation.objects.create(name="Hall", auction=self.auction, latitude=NEAR[0], longitude=NEAR[1])
        self.client.force_login(self.creator)

    def make_experienced(self):
        for title in ("Another", "And another"):
            Auction.objects.create(created_by=self.creator, title=title, date_start=timezone.now())

    def page(self):
        return self.client.get(reverse("auction_main", kwargs={"slug": self.auction.slug}))

    def test_experienced_creator_done_with_the_basics_sees_no_box(self):
        self.make_experienced()
        self.assertNotContains(self.page(), "Finish setting up")

    def test_new_creator_still_sees_it(self):
        self.assertContains(self.page(), "Finish setting up")

    def test_experienced_creator_without_rules_still_sees_it(self):
        self.make_experienced()
        Auction.objects.filter(pk=self.auction.pk).update(
            summernote_description="You should remove this line and edit this section to suit your auction."
        )
        self.assertContains(self.page(), "Finish setting up")
