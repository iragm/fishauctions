"""Frontend fixes from the review: one management form on bulk add, a join anchor on the lot list,
an Ended badge on unsold ended tiles, and the watch star as a real button."""

import datetime

from django.urls import reverse
from django.utils import timezone

from auctions.models import Lot
from auctions.tests import StandardTestCase


class BulkAddUsersFrontendTests(StandardTestCase):
    def test_bulk_add_users_has_one_management_form(self):
        """The formset's management form is rendered once, inside the form, not a second time above it."""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('id="id_form-TOTAL_FORMS"'), 1)


class LotListFrontendTests(StandardTestCase):
    def _lot_list_content(self):
        """The buyer-facing lot list for the auction (``Auction.view_lot_link``), not the admin lots table.

        LotFilter hides lots posted in the last 20 minutes from anyone but their poster, and the fixture's
        lots were just created, so they are backdated here.
        """
        Lot.objects.filter(auction=self.online_auction).update(date_posted=timezone.now() - datetime.timedelta(days=1))
        url = reverse("allLots") + f"?auction={self.online_auction.slug}&status=all"
        return self.client.get(url).content.decode()

    def test_lots_list_join_link_has_anchor(self):
        """Signed-out visitors' sign-in link returns them to the join section."""
        # Lots can be submitted until date_end at the latest (signals.py clamps it), so the auction must still be open.
        now = timezone.now()
        self.online_auction.date_end = now + datetime.timedelta(days=5)
        self.online_auction.lot_submission_start_date = now - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = now + datetime.timedelta(days=3)
        self.online_auction.save()
        content = self._lot_list_content()
        self.assertIn(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}) + "%23join",
            content,
        )

    def test_ended_unsold_tile_has_badge(self):
        """An ended lot that did not sell carries an Ended badge on its tile."""
        self.endAuction()
        content = self._lot_list_content()
        self.assertIn('<span class="badge bg-secondary">Ended</span>', content)

    def test_watch_star_is_a_button(self):
        """The watch star is a button with a pressed state, not a clickable span."""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        # The fixture auction and its lots ended two days ago, which makes every lot "ended" and hides the watch star.
        later = timezone.now() + datetime.timedelta(days=3)
        self.online_auction.date_end = later
        self.online_auction.save()
        Lot.objects.filter(auction=self.online_auction).update(date_end=later)
        content = self._lot_list_content()
        self.assertIn('<button type="button" class="watch-icon', content)
        self.assertIn('aria-pressed="false"', content)
        self.assertNotIn('<span class="watch-icon', content)
