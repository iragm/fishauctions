"""Printed bidder paddles: who a batch is for, what makes a printed one out of date, and the PDF."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pypdfium2
from django.contrib.auth.models import User
from django.urls import reverse

from auctions.models import AuctionTOS, Club, ClubMember, UserData, UserLabelPrefs
from auctions.tests import StandardTestCase
from auctions.views import paddles


def page_texts(pdf):
    document = pypdfium2.PdfDocument(pdf)
    try:
        return [page.get_textpage().get_text_bounded() for page in document]
    finally:
        document.close()


def page_count(pdf):
    return len(page_texts(pdf))


class PaddleTestCase(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.auction = self.in_person_auction
        self.page = reverse("auction_paddles", kwargs={"slug": self.auction.slug})
        self.pdf = reverse("auction_paddles_pdf", kwargs={"slug": self.auction.slug})
        self.client.force_login(self.user)

    def print_paddles(self, **params):
        """The form's submit, followed through the redirect that names the people."""
        params.setdefault("plain_paper", "1")
        response = self.client.get(self.pdf, params)
        if response.status_code == 302 and "people=" in response["Location"]:
            response = self.client.get(response["Location"])
        return response

    def people_named(self, response):
        return parse_qs(urlparse(response["Location"]).query)["people"][0].split(",")

    def message(self, response):
        return " ".join(str(m) for m in response.context["messages"])


class WhatIsPrintedTests(PaddleTestCase):
    def test_a_sheet_per_person_who_can_bid_in_name_order(self):
        for tos, name in ((self.in_person_buyer, "Carol"), (self.admin_in_person_tos, "Alice")):
            tos.name = name
            tos.save()
        self.in_person_tos.bidding_allowed = False
        self.in_person_tos.save()
        response = self.print_paddles(who="everyone")
        self.assertEqual(response["Content-Type"], "application/pdf")
        alice, carol = page_texts(response.content)
        self.assertIn("502", alice)
        self.assertIn("Alice", alice)
        self.assertIn("555", carol)

    def test_printing_records_what_each_paddle_says(self):
        self.print_paddles()
        tos = AuctionTOS.objects.get(pk=self.in_person_buyer.pk)
        self.assertEqual((tos.paddle_printed_number, tos.paddle_printed_name), (tos.bidder_number, tos.name))
        self.assertTrue(tos.paddle_is_current)
        self.assertEqual(len(self.auction.unprinted_paddles()), 0)

    def test_one_person(self):
        response = self.print_paddles(who="one", tos=self.in_person_buyer.pk)
        self.assertEqual(page_count(response.content), 1)
        self.assertIn("paddle-555.pdf", response["Content-Disposition"])
        self.assertEqual(self.auction.unprinted_paddles(), [self.admin_in_person_tos, self.in_person_tos])

    def test_the_form_becomes_a_list_of_people_so_a_second_fetch_prints_the_same_paddles(self):
        response = self.client.get(self.pdf, {"who": "unprinted", "plain_paper": "1"})
        self.assertEqual(len(self.people_named(response)), 3)
        for _ in range(2):
            again = self.client.get(response["Location"])
            self.assertEqual(again["Content-Type"], "application/pdf")
            self.assertEqual(page_count(again.content), 3)

    def test_one_pdf_holds_a_hundred(self):
        with patch.object(paddles, "MAX_PADDLES", 2):
            response = self.client.get(self.pdf, {"who": "everyone", "plain_paper": "1"})
            self.assertEqual(len(self.people_named(response)), 2)
            self.client.get(response["Location"])
        # "Not printed yet" carries on with whoever was left.
        self.assertEqual(len(self.auction.unprinted_paddles()), 1)

    def test_nothing_left_to_print_comes_back_saying_so(self):
        self.print_paddles()
        response = self.client.get(self.pdf, {"who": "unprinted", "plain_paper": "1"}, follow=True)
        self.assertRedirects(response, self.page)
        self.assertIn("no paddles to print", self.message(response))


class OutOfDateTests(PaddleTestCase):
    def setUp(self):
        super().setUp()
        self.print_paddles()

    def test_a_new_number_puts_the_paddle_back_in_the_batch(self):
        self.in_person_tos.bidder_number = "77"
        self.in_person_tos.save()
        self.assertEqual(self.auction.unprinted_paddles(), [self.in_person_tos])

    def test_so_does_losing_a_number_to_somebody_else(self):
        """Taking 555 moves its holder with update(), which no save() hook sees."""
        self.in_person_tos.force_set_bidder_number("555")
        self.assertEqual(
            set(self.auction.unprinted_paddles()),
            {self.in_person_tos, self.in_person_buyer},
        )

    def test_so_does_a_new_name(self):
        self.in_person_buyer.name = "Somebody Else"
        self.in_person_buyer.save()
        self.assertEqual(self.auction.unprinted_paddles(), [self.in_person_buyer])

    def test_so_does_fixing_an_accent(self):
        """The database's collation calls these the same name. The paddle doesn't."""
        self.in_person_buyer.name = "Jose"
        self.in_person_buyer.save()
        self.print_paddles()
        self.in_person_buyer.name = "José"
        self.in_person_buyer.save()
        self.assertEqual(self.auction.unprinted_paddles(), [self.in_person_buyer])

    def test_the_actions_menu_offers_a_reprint(self):
        self.assertIn("Reprint paddle", AuctionTOS.objects.get(pk=self.in_person_buyer.pk).actions_dropdown_html)
        self.in_person_buyer.name = "Somebody Else"
        self.in_person_buyer.save()
        menu = AuctionTOS.objects.get(pk=self.in_person_buyer.pk).actions_dropdown_html
        self.assertIn("Print paddle", menu)
        self.assertNotIn("Reprint paddle", menu)
        self.assertIn(f"{self.page}?tos={self.in_person_buyer.pk}", menu)


class PlainPaperTests(PaddleTestCase):
    def test_a_sheet_label_printer_has_to_be_emptied_first(self):
        response = self.client.get(self.pdf, {"who": "unprinted"}, follow=True)
        self.assertRedirects(response, self.page)
        self.assertIn("plain paper", self.message(response))
        self.assertEqual(len(self.auction.unprinted_paddles()), 3)
        self.assertContains(self.client.get(self.page), 'name="plain_paper"')

    def test_a_thermal_label_printer_is_a_different_printer(self):
        UserLabelPrefs.objects.create(user=self.user, preset="thermal_sm")
        response = self.print_paddles(who="unprinted", plain_paper="")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertNotContains(self.client.get(self.page), 'name="plain_paper"')


class PageTests(PaddleTestCase):
    def test_the_page_counts_sheets(self):
        response = self.client.get(self.page)
        self.assertEqual((response.context["unprinted_count"], response.context["everyone_count"]), (3, 3))
        self.print_paddles(who="one", tos=self.in_person_tos.pk)
        response = self.client.get(self.page)
        self.assertEqual((response.context["unprinted_count"], response.context["everyone_count"]), (2, 3))

    def test_a_link_from_a_person_picks_them(self):
        response = self.client.get(self.page, {"tos": self.in_person_buyer.pk})
        self.assertEqual(response.context["chosen"], self.in_person_buyer)
        self.assertContains(response, 'value="one" id="who-one" checked')

    def test_online_auctions_have_no_paddles(self):
        self.client.force_login(self.user)
        url = reverse("auction_paddles", kwargs={"slug": self.online_auction.slug})
        self.assertRedirects(
            self.client.get(url), self.online_auction.get_absolute_url(), fetch_redirect_response=False
        )

    def test_only_whoever_runs_the_people(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(self.page).status_code, 403)
        self.assertEqual(self.client.get(self.pdf, {"who": "everyone", "plain_paper": "1"}).status_code, 403)

    def test_the_more_menu_links_it(self):
        self.assertContains(self.client.get(self.auction.get_absolute_url()), self.page)


class LayoutTests(PaddleTestCase):
    def test_the_paper_is_the_auctions_countrys(self):
        self.assertEqual(paddles.paper_for(self.auction), "letter")
        UserData.objects.filter(user=self.user).update(preferred_currency="EUR")
        self.user.refresh_from_db()
        self.auction.created_by = User.objects.get(pk=self.user.pk)
        self.assertEqual(paddles.paper_for(self.auction), "A4")

    def test_a_short_number_is_as_tall_as_allowed_and_a_long_one_fits_the_half(self):
        self.in_person_tos.bidder_number = "7"
        self.assertEqual(paddles.lay_out(self.in_person_tos, "letter")["number_pt"], paddles.NUMBER_MAX_PT)
        self.in_person_tos.bidder_number = "12345"
        sheet = paddles.lay_out(self.in_person_tos, "letter")
        half_pt = (8.5 / 2 - 2 * paddles.SIDE_MARGIN_INCHES) * 72
        self.assertLessEqual(sheet["number_pt"] * paddles.width_in_ems("12345"), half_pt + 0.5)

    def test_a_placeholder_name_prints_as_no_name(self):
        self.in_person_tos.name = "Unknown"
        self.assertEqual(paddles.lay_out(self.in_person_tos, "letter")["name"], "")

    def test_the_barcode_is_bars_only(self):
        svg = paddles.barcode_svg("11111<b>", 90)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<b>", svg)


class CrossLinkTests(PaddleTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Paddle Club", current_auction=self.auction)
        ClubMember.objects.create(club=self.club, user=self.user, permission_admin=True)
        self.auction.club = self.club
        self.auction.save()

    def test_paddles_point_at_paddle_stickers(self):
        barcodes = reverse("club_barcode_labels", kwargs={"slug": self.club.slug})
        self.assertContains(self.client.get(self.page), f'href="{barcodes}"')

    def test_paddle_stickers_point_at_paddles(self):
        response = self.client.get(reverse("club_barcode_labels", kwargs={"slug": self.club.slug}))
        self.assertContains(response, "Paddle sticker")
        self.assertContains(response, f'href="{self.page}"')
