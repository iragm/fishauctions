"""The promo page: where it shows, that every feature on it links to a real help section, and its photo strip."""

import datetime
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from auctions import help_stats
from auctions.help_stats import PROMO_MIN_PHOTOS
from auctions.models import AuctionTOS, Lot, LotImage
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase
from auctions.views.site_pages import promo_lot_photos

HELP_LINK = re.compile(r'href="(/help/([\w-]+)/)(?:#([\w-]+))?"')


@isolated_cache("promo-page")
@override_settings(ENABLE_PROMO_PAGE=True)
class PromoPageTests(StandardTestCase):
    def test_a_signed_out_visitor_lands_on_it_at_its_own_address(self):
        """One address for everyone, so a link copied from it works for whoever it's sent to."""
        self.assertRedirects(self.client.get(reverse("home")), reverse("promo"))
        self.assertRedirects(
            self.client.get(reverse("home") + "?utm_source=flyer"), reverse("promo") + "?utm_source=flyer"
        )

    @override_settings(ENABLE_CLUB_FINDER=True)
    def test_bring_your_club_goes_to_the_map(self):
        self.assertContains(self.client.get(reverse("promo")), f'href="{reverse("clubs")}">Bring your club', count=1)

    def test_about_is_the_promo_page_signed_in_too(self):
        self.client.force_login(self.user)
        self.assertTemplateUsed(self.client.get(reverse("promo")), "promo.html")

    @override_settings(ENABLE_PROMO_PAGE=False)
    def test_without_the_promo_page_about_is_the_help(self):
        self.assertRedirects(self.client.get(reverse("promo")), reverse("help"))

    def test_every_help_link_lands_on_a_section(self):
        links = HELP_LINK.findall(self.client.get(reverse("promo")).content.decode())
        self.assertGreater(len(links), 10)
        for path, slug, anchor in set(links):
            with self.subTest(slug=slug, anchor=anchor):
                guide = self.client.get(path)
                self.assertEqual(guide.status_code, 200)
                if anchor:
                    self.assertContains(guide, f'id="{anchor}"')

    def test_the_club_tools_are_featured(self):
        response = self.client.get(reverse("promo"))
        for slug, anchor in (
            ("club-membership", ""),
            ("club-membership", "#auto-renew"),
            ("club-membership", "#cards"),
            ("club-events", "#google-calendar"),
            ("club-email", "#mailing-lists"),
            ("club-email", "#discord"),
            ("breeder-award-programs", ""),
            ("club-donations", ""),
            ("ai-agents", ""),
        ):
            with self.subTest(slug=slug, anchor=anchor):
                self.assertContains(response, f'href="{reverse("help_guide", kwargs={"slug": slug})}{anchor}"')


@isolated_cache("promo-photos")
@override_settings(
    ENABLE_PROMO_PAGE=True,
    CLOUDFLARE_IMAGES_ENABLED=True,
    CLOUDFLARE_IMAGES_ACCOUNT_HASH="hash",
    CLOUDFLARE_IMAGES_DOMAIN="",
)
class PromoPhotoTests(StandardTestCase):
    def _sold_lot(self, seller, name, source="ACTUAL", **lot_fields):
        fields = {"winning_price": 10, **lot_fields}
        lot = Lot.objects.create(lot_name=name, auction=self.online_auction, user=seller, quantity=1, **fields)
        Lot.objects.filter(pk=lot.pk).update(date_end=timezone.now() - datetime.timedelta(days=3))
        LotImage.objects.create(lot_number=lot, image_source=source, cloudflare_image_id=f"cf-{lot.pk}")
        return lot

    def _walk_in(self, n):
        return AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, name=f"walk-in {n}", bidder_number=f"9{n:02}"
        )

    def _sellers(self, count):
        return [User.objects.create_user(username=f"promo_seller_{i}") for i in range(count)]

    def _counted(self):
        help_stats.refresh()
        return promo_lot_photos()

    def test_sellers_own_photos_of_sold_lots_two_per_seller(self):
        sellers = self._sellers(PROMO_MIN_PHOTOS)
        for seller in sellers:
            for n in range(3):
                self._sold_lot(seller, f"{seller.username} {n}")
        self._sold_lot(sellers[0], "from the internet", source="RANDOM")
        self._sold_lot(sellers[1], "unsold", winning_price=None)
        self._sold_lot(sellers[2], "removed", banned=True)

        photos = self._counted()
        names = [photo["name"] for photo in photos]
        self.assertEqual(len(names), 2 * PROMO_MIN_PHOTOS)
        for left_out in ("from the internet", "unsold", "removed"):
            self.assertNotIn(left_out, names)
        self.assertTrue(all(photo["url"].startswith("https://imagedelivery.net/hash/") for photo in photos))

    def test_sellers_without_accounts_are_told_apart(self):
        for n in range(PROMO_MIN_PHOTOS):
            self._sold_lot(None, f"walk-in {n}", auctiontos_seller=self._walk_in(n))
        self.assertEqual(len(self._counted()), PROMO_MIN_PHOTOS)

    def test_a_lot_taken_down_after_the_count_leaves_the_strip(self):
        lots = [self._sold_lot(seller, seller.username) for seller in self._sellers(PROMO_MIN_PHOTOS + 1)]
        help_stats.refresh()
        Lot.objects.filter(pk=lots[0].pk).update(banned=True)
        names = [photo["name"] for photo in promo_lot_photos()]
        self.assertEqual(len(names), PROMO_MIN_PHOTOS)
        self.assertNotIn(lots[0].lot_name, names)

    def test_too_few_photos_and_there_is_no_strip(self):
        for seller in self._sellers(PROMO_MIN_PHOTOS - 1):
            self._sold_lot(seller, seller.username)
        self.assertEqual(self._counted(), [])

    def test_the_page_never_counts_them(self):
        with patch("auctions.help_stats._promo_photos") as count, patch("auctions.help_stats.request_refresh"):
            self.client.get(reverse("home"))
        count.assert_not_called()
