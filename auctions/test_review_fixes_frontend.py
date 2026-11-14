"""Frontend fixes from the review: one management form on bulk add, a join anchor on the lot list,
an Ended badge on unsold ended tiles, and the watch star as a real button."""

import datetime

from django.urls import reverse
from django.utils import timezone

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
    def test_lots_list_join_link_has_anchor(self):
        """Signed-out visitors' join links point at the join section, and the sign-in link returns there."""
        now = timezone.now()
        self.online_auction.lot_submission_start_date = now - datetime.timedelta(days=1)
        self.online_auction.lot_submission_end_date = now + datetime.timedelta(days=3)
        self.online_auction.save()
        url = reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug})
        content = self.client.get(url).content.decode()
        self.assertIn(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}) + "#join",
            content,
        )
        self.assertIn("%23join", content)

    def test_ended_unsold_tile_has_badge(self):
        """An ended lot that did not sell carries an Ended badge on its tile."""
        self.endAuction()
        url = reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug})
        content = self.client.get(url).content.decode()
        self.assertIn('<span class="badge bg-secondary">Ended</span>', content)

    def test_watch_star_is_a_button(self):
        """The watch star is a button with a pressed state, not a clickable span."""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug})
        content = self.client.get(url).content.decode()
        self.assertIn('<button type="button" class="watch-icon', content)
        self.assertIn('aria-pressed="false"', content)
        self.assertNotIn('<span class="watch-icon', content)
