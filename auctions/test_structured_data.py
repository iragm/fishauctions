"""The auction page's schema.org Event (``auctions/structured_data.py``): the fields Google asks for."""

import datetime

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from auctions import structured_data
from auctions.models import Auction, Club, PickupLocation


class AuctionEventTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.creator = User.objects.create_user("sd_creator", "sd_creator@example.com", "pw")
        cls.club = Club.objects.create(name="Harbor Aquarium Society", location="Albany, NY")

    def auction(self, **fields):
        start = timezone.now() + datetime.timedelta(days=7)
        defaults = {
            "title": "Fall auction",
            "created_by": self.creator,
            "club": self.club,
            "is_online": False,
            "date_start": start,
            "promote_this_auction": True,
        }
        defaults.update(fields)
        return Auction.objects.create(**defaults)

    def event(self, auction):
        return structured_data.auction_event(RequestFactory().get("/"), auction)

    def test_an_in_person_auction_has_every_field(self):
        auction = self.auction(summernote_description="<p>Bring <b>bags</b> &amp; a cooler.</p>")
        PickupLocation.objects.create(
            name="Grange hall", auction=auction, latitude=42.6, longitude=-73.8, address="12 Private Road"
        )
        Auction.objects.filter(pk=auction.pk).update(date_end=None, location="New York")
        auction = Auction.objects.get(pk=auction.pk)
        event = self.event(auction)
        self.assertEqual(event["performer"]["@type"], "Organization")
        self.assertEqual(event["performer"]["name"], "Harbor Aquarium Society")
        self.assertEqual(event["description"], "Bring bags & a cooler.")
        self.assertEqual(event["location"]["address"], "New York")
        self.assertEqual(event["location"]["geo"]["latitude"], 42.6)
        self.assertNotIn("12 Private Road", str(event))
        self.assertTrue(event["image"].startswith("http"))
        self.assertTrue(event["endDate"])

    def test_the_description_is_cut_and_falls_back_to_the_summary(self):
        long = self.auction(summernote_description="word " * 200)
        self.assertLessEqual(len(self.event(long)["description"]), structured_data.DESCRIPTION_CHARS)
        placeholder = self.auction(summernote_description=structured_data.RULES_PLACEHOLDER)
        self.assertIn("Harbor Aquarium Society", self.event(placeholder)["description"])

    def test_an_online_auction_ends_when_it_ends(self):
        end = timezone.now() + datetime.timedelta(days=9)
        auction = self.auction(is_online=True, date_end=end)
        event = self.event(auction)
        self.assertEqual(event["location"]["@type"], "VirtualLocation")
        self.assertEqual(event["endDate"][:10], end.astimezone(auction.timezone).date().isoformat())
