"""Text somebody typed reaches the page as text, and ``?next=`` never leaves the site.

Each test here is a hole that was open: a name, a lot title or a filter query that went into markup
unescaped, or into a crispy ``HTML()`` layout object, which renders its string as a Django template.
"""

from django.contrib.auth.models import User
from django.template import Context
from django.test import RequestFactory
from django.urls import reverse

from auctions.consumers import LotConsumer, post_chat_message
from auctions.forms import BapAwardForm, BulkSellLotsToOnlineHighBidder, LiteralHTML
from auctions.models import Club, Invoice, Lot, LotHistory
from auctions.tests import StandardTestCase
from auctions.views.base import safe_next_url

HOSTILE = "<img src=x onerror=alert(1)>{{ csrf_token }}{% now 'Y' %}"


class LiteralHTMLTests(StandardTestCase):
    def test_is_not_rendered_as_a_template(self):
        rendered = LiteralHTML("{{ secret }}{% now 'Y' %}").render(None, Context({"secret": "leaked"}))
        self.assertEqual(rendered, "{{ secret }}{% now 'Y' %}")

    def test_bap_award_form_shows_a_hostile_lot_name_as_text(self):
        club = Club.objects.create(name="Untrusted club")
        self.lot.lot_name = HOSTILE
        form = BapAwardForm(club=club, lot=self.lot)
        html = form.helper.layout.fields[0].render(form, Context({"csrf_token": "tok"}))
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("{{ csrf_token }}", html)


class BulkSellQueryTests(StandardTestCase):
    def test_the_filter_query_is_escaped_in_the_button(self):
        form = BulkSellLotsToOnlineHighBidder(auction=self.online_auction, query=HOSTILE, queryset=Lot.objects.none())
        button = next(f for f in form.helper.layout.fields[1].fields if isinstance(f, LiteralHTML))
        html = button.render(form, Context({}))
        self.assertNotIn("<img", html)
        self.assertIn("{{ csrf_token }}", html)

    def test_the_filter_query_is_escaped_in_the_dialog(self):
        self.client.force_login(self.user)
        url = reverse("bulk_set_lots_won", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "<script>alert(1)</script>"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


class InvoiceRenewalToggleTests(StandardTestCase):
    def test_a_hostile_name_is_escaped_in_the_modal_title(self):
        self.online_tos.name = "<script>alert(1)</script>"
        self.online_tos.save()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("invoice_renewal_toggle", kwargs={"pk": invoice.pk}), {"renewal_needed": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


class SafeNextUrlTests(StandardTestCase):
    def test_only_this_site(self):
        factory = RequestFactory()
        self.assertEqual(safe_next_url(factory.get("/", {"next": "https://evil.example/"}), "/home/"), "/home/")
        self.assertEqual(safe_next_url(factory.get("/", {"next": "//evil.example/"}), "/home/"), "/home/")
        self.assertEqual(safe_next_url(factory.get("/", {"next": "/lots/"}), "/home/"), "/lots/")
        self.assertEqual(safe_next_url(factory.get("/", {"other": "1"}), "/home/"), "/home/")

    def test_label_preferences_do_not_redirect_off_site(self):
        from auctions.views import UserLabelPrefsView

        view = UserLabelPrefsView()
        view.request = RequestFactory().get("/printing/", {"next": "https://evil.example/"})
        view.request.user = self.user
        self.assertEqual(view.get_success_url(), reverse("userpage", kwargs={"slug": self.user.username}))


class ChatTests(StandardTestCase):
    def test_a_long_message_is_cut_to_the_column(self):
        limit = LotHistory._meta.get_field("message").max_length
        history = post_chat_message(self.lot, self.user, "x" * (limit + 500))
        self.assertEqual(len(LotHistory.objects.get(pk=history.pk).message), limit)

    def test_the_auction_seller_is_recognised_on_their_own_lot(self):
        """``auctiontos_seller.user`` was compared as a User object with a pk, so never matched."""
        consumer = LotConsumer()
        consumer.lot = Lot.objects.get(pk=self.lot.pk)
        consumer.lot.user = None
        consumer.user = self.user  # online_tos, the lot's seller
        self.assertTrue(consumer._is_seller())
        consumer.user = User.objects.get(pk=self.userB.pk)
        self.assertFalse(consumer._is_seller())


class MissingObjectTests(StandardTestCase):
    """A made-up pk is a 404, not a DoesNotExist mailed to the admins as a 500."""

    def test_banning_a_missing_user(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/api/users/ban/999999/").status_code, 404)

    def test_deactivating_a_missing_lot(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post("/api/lots/deactivate/999999/").status_code, 404)

    def test_stats_for_a_missing_auction(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/auctionstats/no-such-auction/activity").status_code, 404)
