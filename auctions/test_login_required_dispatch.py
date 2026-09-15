"""Signed-out visitors are turned away, not handed a 500, by views that override ``dispatch``.

``LoginRequiredMixin`` does its check inside its own ``dispatch``, so a subclass that overrides
``dispatch`` and works with ``request.user`` before calling ``super()`` runs that code for anonymous
visitors too. ``AddToCalendarView`` did exactly that and queried ``AuctionTOS`` with an
``AnonymousUser``, which raised. The other views listed here have the same shape but happen to
refuse safely on their own -- a redirect with a message, or a 403 -- so they are held to that rather
than to a login redirect. Views wrapped in ``login_required()`` in ``urls.py`` are checked before
``dispatch`` ever runs and aren't listed.
"""

from django.conf import settings
from django.shortcuts import resolve_url
from django.urls import reverse

from auctions.models import Bid, LotImage
from auctions.tests import StandardTestCase

STATS_ENDPOINTS = (
    "auction_stats_activity",
    "auction_stats_pictures",
    "auction_stats_distance_traveled",
    "auction_stats_previous_auctions",
    "auction_stats_lots_submitted",
    "auction_stats_location_volume",
    "auction_stats_feature_use",
    "auction_stats_referrers",
    "auction_sell_prices",
)


class AnonymousDispatchTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        # A crash should come back as a 500 response rather than stop a loop at the first one.
        self.client.raise_request_exception = False

    def assertRefused(self, url):
        """Turned away -- redirected or forbidden -- rather than crashing or rendering the page."""
        response = self.client.get(url)
        self.assertIn(response.status_code, (302, 403), f"{url} answered {response.status_code}")
        return response

    def test_add_to_calendar_sends_anonymous_visitors_to_login(self):
        url = f"{reverse('add_to_calendar')}?type=ics&location={self.location.pk}&second=False"
        response = self.assertRefused(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(resolve_url(settings.LOGIN_URL), response["Location"])

    def test_delete_views(self):
        bid = Bid.objects.create(user=self.userB, lot_number=self.lot, amount=5)
        image = LotImage.objects.create(lot_number=self.lot)
        for name, pk in (("delete_lot", self.lot.pk), ("delete_bid", bid.pk), ("delete_image", image.pk)):
            with self.subTest(name):
                self.assertRefused(reverse(name, kwargs={"pk": pk}))

    def test_auction_stats_endpoints(self):
        for name in STATS_ENDPOINTS:
            with self.subTest(name):
                self.assertRefused(reverse(name, kwargs={"slug": self.online_auction.slug}))
