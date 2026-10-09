"""The help guides: that they cover every page and rule, that they are public, and that they talk about your auction."""

import datetime
import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from auctions import help_guides, palette_actions, palette_routes
from auctions.field_adoption import FieldAdoption
from auctions.models import Auction, AuctionTOS, Club, ClubMember, Lot, LotImage, MobileDevice, User
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


class HelpCoverageTests(SimpleTestCase):
    """The build fails when a page or an auction rule is added and no guide mentions it.

    A page is covered by ``{% page "url_name" %}`` in a guide, a rule by ``{% rule "field" %}``.
    """

    def test_every_page_is_in_a_guide_or_has_a_reason_not_to_be(self):
        documented = help_guides.documented_pages()
        missing = sorted(
            name
            for name in palette_routes.ROUTES
            if name not in documented
            and name not in help_guides.NOT_IN_HELP
            and name not in help_guides.NOT_YET_DOCUMENTED
        )
        self.assertEqual(
            missing,
            [],
            "These pages are in no help guide. Write them up in auctions/templates/help/guides/ with "
            '{% page "name" %}, or excuse them in help_guides.NOT_IN_HELP.',
        )

    def test_the_backlog_only_shrinks(self):
        documented = set(help_guides.documented_pages())
        self.assertEqual(
            sorted(documented & help_guides.NOT_YET_DOCUMENTED),
            [],
            "These are documented now: take them off help_guides.NOT_YET_DOCUMENTED.",
        )
        self.assertEqual(sorted(help_guides.NOT_YET_DOCUMENTED & set(help_guides.NOT_IN_HELP)), [])

    def test_no_guide_or_list_names_a_page_that_does_not_exist(self):
        named = set(help_guides.documented_pages()) | help_guides.NOT_YET_DOCUMENTED | set(help_guides.NOT_IN_HELP)
        self.assertEqual(sorted(named - set(palette_routes.ROUTES)), [])

    def test_every_auction_rule_is_explained_somewhere(self):
        documented = help_guides.documented_rules()
        missing = [
            name
            for name in help_guides.rule_fields()
            if name not in documented and name not in help_guides.RULES_NOT_YET_DOCUMENTED
        ]
        self.assertEqual(
            missing,
            [],
            "These auction rules are explained in no help guide. Add a section to "
            'auctions/templates/help/guides/auction-rules.html using {% rule "name" %}.',
        )

    def test_the_rules_backlog_only_shrinks(self):
        documented = set(help_guides.documented_rules())
        self.assertEqual(sorted(documented & help_guides.RULES_NOT_YET_DOCUMENTED), [])
        self.assertEqual(sorted(help_guides.RULES_NOT_YET_DOCUMENTED - set(help_guides.rule_fields())), [])

    def test_every_button_a_guide_names_is_on_the_site(self):
        self.assertEqual(
            help_guides.missing_ui_labels(),
            {},
            'These {% ui "..." %} labels are in a guide but nowhere on the site: the button was renamed or '
            "removed. Say what the site says now.",
        )

    def test_every_guide_has_a_template(self):
        for slug in help_guides.GUIDES:
            with self.subTest(slug=slug):
                self.assertTrue((help_guides.TEMPLATE_DIR / f"{slug}.html").exists())

    def test_every_running_and_taking_part_guide_exists(self):
        for slug in help_guides.GUIDE_FOR.values():
            self.assertIn(slug, help_guides.GUIDES)


class ClubSettingsCoverageTests(StandardTestCase):
    """The build fails when a club setting is added and no guide names it with ``{% ui "Label" %}``."""

    def test_every_club_setting_is_named_in_a_guide(self):
        club = Club.objects.create(name="Coverage club")
        named = help_guides.documented_ui()
        missing = sorted(
            f"{name} ({label!r})"
            for name, label in help_guides.club_setting_labels(club).items()
            if label not in named and name not in help_guides.CLUB_SETTINGS_NOT_IN_HELP
        )
        self.assertEqual(
            missing,
            [],
            'These club settings are in no help guide. Name each one with {% ui "Label" %} in a club guide, '
            "or excuse it in help_guides.CLUB_SETTINGS_NOT_IN_HELP.",
        )

    def test_every_excuse_is_a_real_setting(self):
        club = Club.objects.create(name="Coverage club")
        labels = help_guides.club_setting_labels(club)
        self.assertEqual(sorted(set(help_guides.CLUB_SETTINGS_NOT_IN_HELP) - set(labels)), [])


class RuleUsageWordingTests(SimpleTestCase):
    """The badge beside each rule says what "using" it means for that setting's default."""

    def usage(self, default, off_default=55, known=True):
        return FieldAdoption("x", "X", off_default, 0, 100, default=default, default_known=known).usage

    def test_a_setting_that_starts_off_is_used_by(self):
        self.assertEqual(self.usage(False), "Used by 55% of auctions")
        self.assertEqual(self.usage(0), "Used by 55% of auctions")

    def test_a_setting_that_starts_on_is_turned_off(self):
        self.assertEqual(self.usage(True), "55% of auctions turn this off")

    def test_any_other_default_is_changed(self):
        self.assertEqual(self.usage("allow"), "55% of auctions change this")

    def test_a_handful_is_not_rounded_to_nothing(self):
        self.assertEqual(FieldAdoption("x", "X", 1, 0, 1000, default=False).usage, "Used by under 1% of auctions")
        self.assertEqual(self.usage(False, off_default=0), "Used by 0% of auctions")

    def test_no_default_says_nothing(self):
        self.assertEqual(self.usage(None, known=False), "")


@override_settings(WEBSITE_FOCUS="fish", ALLOW_SEARCH_INDEXING=True)
class HelpPagesArePublicTests(StandardTestCase):
    def test_the_index_and_every_guide_open_signed_out(self):
        self.assertEqual(self.client.get(reverse("help")).status_code, 200)
        for guide in help_guides.GUIDES.values():
            with self.subTest(slug=guide.slug):
                response = self.client.get(guide.url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'<meta name="description" content="{escape(guide.summary)}"')

    def test_an_unknown_guide_is_a_404(self):
        self.assertEqual(self.client.get(reverse("help_guide", kwargs={"slug": "nope"})).status_code, 404)

    def test_the_sitemap_lists_every_guide(self):
        body = self.client.get("/sitemap.xml").content.decode()
        for guide in help_guides.GUIDES.values():
            self.assertIn(guide.url, body)

    def test_robots_points_at_the_sitemap(self):
        self.assertContains(self.client.get("/robots.txt"), "/sitemap.xml")
        self.assertNotContains(self.client.get(reverse("help")), "noindex")

    @override_settings(ALLOW_SEARCH_INDEXING=False)
    def test_a_copy_that_is_not_indexed_says_so_everywhere(self):
        robots = self.client.get("/robots.txt")
        self.assertNotContains(robots, "sitemap")
        # Not Disallow: a crawler has to fetch a page to read its noindex and drop it.
        self.assertNotContains(robots, "Disallow")
        self.assertNotContains(self.client.get("/sitemap.xml"), "/help/")
        self.assertContains(self.client.get(reverse("help")), '<meta name="robots" content="noindex, nofollow" />')

    @override_settings(WEBSITE_FOCUS="birds")
    def test_the_fish_guide_is_only_on_a_fish_site(self):
        guide = help_guides.GUIDES["bagging-fish"]
        self.assertEqual(self.client.get(guide.url).status_code, 404)
        self.assertNotContains(self.client.get("/sitemap.xml"), guide.url)
        self.assertNotContains(self.client.get(reverse("help")), guide.url)
        self.assertNotContains(self.client.get(help_guides.GUIDES["online-auctions"].url), guide.url)
        self.assertNotIn(guide.title, [result["guide"] for result in help_guides.search("airstone")])

    def test_the_footer_links_the_help(self):
        # Not the TOS page: it reads tos.html from the project root, which is gitignored and absent in CI.
        footer_link = f'<a class="text-muted" href="{reverse("help")}">Help</a>'
        self.assertContains(self.client.get(reverse("support")), footer_link)

    def test_the_menu_is_the_account_menu(self):
        response = self.client.get(help_guides.GUIDES["labels"].url)
        self.assertContains(response, 'id="helpSidebar"')
        self.assertContains(response, f'class="nav-link active" href="{help_guides.GUIDES["labels"].url}"')

    def test_the_rules_guide_says_how_many_auctions_use_each_rule(self):
        with isolated_cache("help-rule-usage"):
            from auctions import help_stats

            help_stats.refresh()
            response = self.client.get(help_guides.GUIDES["auction-rules"].url)
        self.assertContains(response, 'id="rule-winning_bid_percent_to_club">Club cut</strong> <span class="badge')
        self.assertContains(response, "of auctions")

    def test_a_page_never_counts_it_asks_for_a_count(self):
        with (
            isolated_cache("help-uncounted"),
            patch("auctions.field_adoption.field_adoption") as count,
            patch("auctions.help_stats._site_stats") as site,
            patch("auctions.help_stats.request_refresh") as ask,
        ):
            response = self.client.get(help_guides.GUIDES["auction-rules"].url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "of auctions</span>")
        count.assert_not_called()
        site.assert_not_called()
        ask.assert_called()

    def test_other_guides_name_rules_without_the_badge(self):
        response = self.client.get(help_guides.GUIDES["run-an-online-auction"].url)
        self.assertContains(response, 'id="rule-copy_users_when_copying_this_auction">')
        self.assertNotContains(response, "of auctions</span>")

    def test_search_finds_a_section(self):
        response = self.client.get(reverse("help"), {"q": "alternate split"})
        self.assertContains(response, "/help/auction-rules/#alternate-split")


class HelpIsAboutYourAuctionTests(StandardTestCase):
    def test_the_wrong_kind_of_guide_points_at_the_right_one(self):
        self.client.force_login(self.user)
        url = help_guides.guide_url("run-an-in-person-auction", self.online_auction)
        response = self.client.get(url)
        self.assertContains(response, "is an online auction")
        self.assertContains(response, help_guides.GUIDES["run-an-online-auction"].url)

    def test_an_admin_guide_points_a_bidder_at_the_bidder_guide(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(help_guides.guide_url("run-an-online-auction", self.online_auction))
        self.assertContains(response, "mostly for the people running the auction")
        self.assertContains(response, help_guides.GUIDES["online-auctions"].url)

    def test_the_last_auction_used_is_the_default(self):
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse("help"))
        self.assertContains(response, f"You were last in {self.in_person_auction.title}")
        self.assertContains(response, help_guides.GUIDES["run-an-in-person-auction"].url)

    def test_an_admin_sees_their_own_setting(self):
        self.online_auction.alternative_split_label = "Club Member"
        self.online_auction.save()
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("auction-rules", self.online_auction))
        self.assertContains(response, "Yours: Club Member")

    def test_a_blank_donation_threshold_is_off_not_blank(self):
        self.online_auction.force_donation_threshold = None
        self.online_auction.save()
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("auction-rules", self.online_auction))
        self.assertContains(response, "Yours: Off")

    def test_the_printable_description_steps_say_which_are_done(self):
        done = 'bg-success fw-normal">Done'
        self.online_auction.custom_field_1 = "allow"
        self.online_auction.custom_field_1_name = "Description"
        self.online_auction.use_description = True
        self.online_auction.label_print_fields = "lot_name"
        self.online_auction.save()
        self.client.force_login(self.user)
        url = help_guides.guide_url("auction-rules", self.online_auction)
        self.assertContains(self.client.get(url), done, count=1)
        self.online_auction.use_description = False
        self.online_auction.label_print_fields = "lot_name,custom_field_1"
        self.online_auction.save()
        self.assertContains(self.client.get(url), done, count=3)
        self.client.logout()
        self.assertNotContains(self.client.get(url), done)

    def test_a_rule_for_the_other_kind_of_auction_has_no_yours_badge(self):
        from auctions.templatetags.help_tags import _rule_value

        self.assertEqual(_rule_value(self.online_auction, "online_bidding"), "")
        self.assertEqual(_rule_value(self.in_person_auction, "date_end"), "")
        self.assertNotEqual(_rule_value(self.in_person_auction, "online_bidding"), "")

    def test_the_paypal_pay_button_is_only_mentioned_to_people_who_have_it(self):
        url = help_guides.GUIDES["payments"].url
        self.assertNotContains(self.client.get(url), "Enter your club's PayPal credentials")
        self.assertContains(self.client.get(url), "Sending PayPal invoices")  # the CSV is for everyone
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertContains(response, "Connected a PayPal account of your own")
        # Their own account is not their club: the credentials line is for a club that enters its own.
        self.assertNotContains(response, "Enter your club's PayPal credentials")

    def test_paypal_invoice_counts_are_about_your_auction(self):
        from auctions.help_stats import paypal_invoices
        from auctions.models import Invoice

        auction = self.online_auction
        self.assertEqual(paypal_invoices(None), {})
        Invoice.objects.filter(auction=auction).delete()
        Invoice.objects.create(auction=auction, auctiontos_user=self.tosB, status="DRAFT", calculated_total=-5)
        facts = paypal_invoices(self.user, auction, is_admin=True)
        self.assertEqual((facts["ready"], facts["open"]), (0, 1))

    def test_nobody_else_sees_an_auctions_settings(self):
        self.online_auction.alternative_split_label = "Club Member"
        self.online_auction.save()
        response = self.client.get(help_guides.guide_url("auction-rules", self.online_auction))
        self.assertNotContains(response, "Club Member")

    def test_signed_out_there_are_no_tips(self):
        for slug in ("run-an-in-person-auction", "online-auctions", "in-person-auctions"):
            html = self.client.get(help_guides.GUIDES[slug].url).content.decode()
            # The connect-an-agent line and the AI tips are for everyone; every help-tip is personal.
            self.assertNotIn("help-tip", html, slug)
            self.assertIn('class="help-note help-ai"', html)

    def test_the_auction_help_link_lands_on_the_guide_for_that_auction(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("auction_help", kwargs={"slug": self.in_person_auction.slug}))
        self.assertRedirects(
            response,
            help_guides.guide_url("run-an-in-person-auction", self.in_person_auction),
            fetch_redirect_response=False,
        )
        self.client.logout()
        response = self.client.get(reverse("auction_help", kwargs={"slug": self.in_person_auction.slug}))
        self.assertRedirects(
            response, help_guides.guide_url("in-person-auctions", self.in_person_auction), fetch_redirect_response=False
        )


class HelpOverMcpTests(StandardTestCase):
    def test_search_help_answers_from_the_guides_first(self):
        result = palette_actions.search_help(None, {"query": "alternate split"})
        self.assertTrue(result["found"])
        first = result["help"][0]
        self.assertEqual(first["source"], "Guide")
        self.assertIn("/help/auction-rules/#alternate-split", first["url"])

    def test_guides_alone(self):
        result = palette_actions.search_help(None, {"query": "alternate split", "source": "guides"})
        self.assertTrue(all(row["source"] == "Guide" for row in result["help"]))

    def test_paging_runs_across_guides_into_the_faq(self):
        from auctions.models import FAQ

        FAQ.objects.create(category_text="Fees", question="What is an alternate split?", answer="x")
        everything = palette_actions.search_help(None, {"query": "alternate split", "limit": 50})["help"]
        second_page = palette_actions.search_help(None, {"query": "alternate split", "limit": 1, "offset": 1})["help"]
        self.assertEqual(second_page, everything[1:2])
        self.assertEqual(everything[-1]["source"], "FAQ")


class HelpFormattingTests(StandardTestCase):
    def test_tips_and_mike_have_their_own_look(self):
        html = self.client.get(help_guides.GUIDES["auction-rules"].url).content.decode()
        self.assertIn('class="help-note help-hint"', html)
        self.assertIn('class="help-mike-icon"', html)
        self.assertNotIn("bi-emoji-smile", html)

    def test_no_section_heading_is_bigger_than_h5(self):
        import re

        for slug in help_guides.GUIDES:
            source = (help_guides.TEMPLATE_DIR / f"{slug}.html").read_text()
            with self.subTest(slug=slug):
                self.assertEqual(re.findall(r'<h[1-6] class="h[1-4]\b', source), [])

    def test_signed_out_everybody_is_offered_an_ai_agent(self):
        self.assertContains(self.client.get(reverse("help")), "have it search and summarize the help")

    def test_nobody_with_an_agent_is_offered_one(self):
        from auctions.models import UserAPIKey

        _raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(user=self.user, name="mine", prefix=prefix, key_hash=key_hash)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("help")), "have it search and summarize the help")


class HelpKnowsTheAppStoresTests(StandardTestCase):
    def test_before_release_it_says_the_app_is_not_out(self):
        response = self.client.get(help_guides.GUIDES["mobile-app"].url)
        self.assertContains(response, "isn't in the App Store or on Google Play yet")

    @override_settings(APP_STORE_URL="https://apps.apple.com/app/x", PLAY_STORE_URL="")
    def test_a_store_link_replaces_the_note(self):
        response = self.client.get(help_guides.GUIDES["mobile-app"].url)
        self.assertNotContains(response, "isn't in the App Store")
        self.assertContains(response, 'href="https://apps.apple.com/app/x"')
        self.assertNotContains(response, "Google Play</a>")


class HelpUsesYourAuctionTests(StandardTestCase):
    def test_the_fee_example_uses_your_own_fees(self):
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("auction-rules", self.online_auction))
        # 25% and a $2 entry fee on a $10 lot leaves the seller $5.50.
        self.assertContains(response, "fees work out on a $10 lot")
        self.assertContains(response, "$5.50")

    def test_everybody_else_gets_the_usual_example(self):
        response = self.client.get(help_guides.GUIDES["auction-rules"].url)
        self.assertContains(response, '30%" works out on a $10 lot')
        self.assertContains(response, "$6.00")
        self.assertNotContains(response, "Yours:")

    def test_an_in_person_bidder_is_told_whether_online_bidding_is_on(self):
        self.in_person_auction.online_bidding = "disable"
        self.in_person_auction.save()
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(help_guides.guide_url("in-person-auctions", self.in_person_auction))
        self.assertContains(response, f"Online bidding is off in {self.in_person_auction.title}")

    def test_an_online_bidder_gets_their_lot_form_fees_invoice_and_pickup(self):
        self.online_auction.use_quantity_field = True
        self.online_auction.reserve_price = "required"
        self.online_auction.save()
        self.client.force_login(self.userB)
        response = self.client.get(help_guides.guide_url("online-auctions", self.online_auction))
        self.assertContains(response, "asks for: Quantity, Description, Minimum bid (required)")
        self.assertContains(response, "charges you $10 for every lot of yours that gets no bids")
        self.assertContains(response, "rounds totals to the dollar")
        self.assertContains(response, f"{self.online_auction.title} takes bids in whole dollars")
        self.assertContains(response, "invoices don't have a")
        self.assertContains(response, self.invoiceB.get_absolute_url())
        self.assertContains(response, "You pick up from")
        self.assertContains(response, f"type=google&location={self.location.pk}")

    def test_an_in_person_seller_gets_their_auction_set_up(self):
        # Lot submission can't open after the auction starts.
        self.in_person_auction.date_start = timezone.now() + datetime.timedelta(days=2)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=3)
        self.in_person_auction.lot_submission_start_date = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.save()
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(help_guides.guide_url("in-person-auctions", self.in_person_auction))
        self.assertContains(response, "takes the same cut whoever adds the lot")
        self.assertContains(response, "has no Quantity box")
        self.assertContains(response, f"Adding lots to {self.in_person_auction.title} opens")
        self.assertContains(response, "charges $10 for a lot that goes up and doesn't sell")
        self.assertNotContains(response, "member list")

    def test_the_labels_guide_lists_what_your_labels_print(self):
        self.in_person_auction.label_print_fields = "lot_name,custom_checkbox_label"
        self.in_person_auction.use_custom_checkbox_field = False
        self.in_person_auction.save()
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("labels", self.in_person_auction))
        self.assertContains(response, "print: Lot number, Lot name.")

    def test_scanning_without_a_club_is_the_lot_queue(self):
        self.client.force_login(self.user)
        url = help_guides.guide_url("scanning", self.in_person_auction)
        self.assertContains(self.client.get(url), f"Because {self.in_person_auction.title} doesn't have a club set")
        self.in_person_auction.club = Club.objects.create(name="Scanning club")
        self.in_person_auction.save()
        self.assertNotContains(self.client.get(url), "doesn't have a club set")

    def test_tap_to_pay_says_when_the_auction_has_none(self):
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("in-person-auctions", self.in_person_auction))
        self.assertContains(response, "Some clubs allow tap to pay with credit card, but it doesn't look like")

    def test_your_account_says_what_is_missing(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(help_guides.GUIDES["your-account"].url)
        self.assertContains(response, "Yay, you already signed up!")
        self.assertContains(response, "You still haven't joined any auctions")
        self.assertContains(response, "address isn't filled out yet")


class HelpKnowsYourPhoneTests(StandardTestCase):
    APP_UA = "FishAuctionsApp/1.0 (Flutter; Android)"

    def guide(self, slug, **extra):
        self.client.force_login(self.user)
        return self.client.get(help_guides.GUIDES[slug].url, **extra)

    def test_a_browser_subscribes_and_tests_right_here(self):
        response = self.guide("in-person-auctions")
        self.assertContains(response, "Turn on notifications")
        self.assertContains(response, "pushManager.getSubscription")
        self.assertContains(response, reverse("push_test"))
        self.assertNotContains(response, "pushGetState")

    def test_with_the_app_they_go_to_the_app_not_the_browser(self):
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        with patch("auctions.notifications.push_configured", return_value=True):
            response = self.guide("in-person-auctions")
        self.assertContains(response, "Notify me when bidding starts")
        self.assertContains(response, "They go to the app on your phone, not this browser.")
        self.assertNotContains(response, "pushManager.getSubscription")

    def test_once_on_with_the_app_only_the_test_is_left(self):
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        self.user.userdata.push_notifications_when_lots_sell = True
        self.user.userdata.save()
        with patch("auctions.notifications.push_configured", return_value=True):
            response = self.guide("in-person-auctions")
        self.assertContains(response, '<ol id="help-push-steps" class="mb-0" hidden>', html=False)
        self.assertContains(response, '<div id="help-push-done">', html=False)

    def test_in_the_app_the_app_is_asked_about_this_phone(self):
        response = self.guide("in-person-auctions", HTTP_USER_AGENT=self.APP_UA)
        self.assertContains(response, "pushGetState")
        self.assertContains(response, "pushEnable")
        self.assertNotContains(response, "pushManager.getSubscription")

    def test_signed_out_gets_the_steps_not_the_buttons(self):
        response = self.client.get(help_guides.GUIDES["in-person-auctions"].url)
        self.assertNotContains(response, "help-push")
        self.assertContains(response, "turn notifications on right here")

    def test_the_app_guide_says_whether_you_have_the_app(self):
        self.assertContains(self.guide("mobile-app"), "You haven't signed in to the app on a phone yet.")
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="", push_enabled=False)
        self.assertContains(
            self.guide("mobile-app"), "You've signed in to the app on a phone, but it can't send you notifications yet."
        )
        self.assertContains(self.guide("mobile-app", HTTP_USER_AGENT=self.APP_UA), "You're reading this in the app.")


class HelpStatsTests(StandardTestCase):
    STATS = {
        "misc": {"club_stats": {"gross": 1234.0, "total_lots": 120, "checked_in": 40}},
        "lot_sell_prices": {"labels": [], "data": [[12] + [9] * 12]},
        "previous_auctions": {"labels": [], "data": [[10, 5, 25]]},
    }

    def test_facts_come_from_the_cached_stats(self):
        from auctions.help_stats import auction_facts

        self.online_auction.cached_stats = self.STATS
        facts = auction_facts(self.online_auction)
        self.assertEqual(facts["gross"], 1234)
        self.assertEqual(facts["lots"], 120)
        self.assertEqual(facts["unsold_pct"], 10)
        self.assertEqual(facts["first_timers"], 10)

    def test_no_stats_no_facts(self):
        from auctions.help_stats import auction_facts

        self.online_auction.cached_stats = None
        self.assertEqual(auction_facts(self.online_auction), {})

    def test_the_guide_quotes_your_last_auction_once_it_is_over(self):
        self.online_auction.cached_stats = self.STATS
        self.online_auction.date_end = timezone.now() - datetime.timedelta(days=5)
        self.online_auction.save()
        self.location.pickup_time = timezone.now() - datetime.timedelta(days=4)
        self.location.save()
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("run-an-online-auction", self.online_auction))
        self.assertContains(response, f"Last time, {self.online_auction.title} had 120 lots and sold $1234")

    def test_site_stats_count_real_lots(self):
        from auctions.help_stats import _site_stats

        self.online_auction.date_start = timezone.now() - datetime.timedelta(days=30)
        self.online_auction.save()
        facts = _site_stats(min_lots=1)
        self.assertIn("online_unsold", facts)
        self.assertEqual(_site_stats(min_lots=10**6), {})

    def test_watchers_are_people_in_promoted_online_auctions(self):
        from auctions.help_stats import _watchers
        from auctions.models import Watch

        self.online_auction.date_start = timezone.now() - datetime.timedelta(days=30)
        self.online_auction.save()
        Watch.objects.create(user=self.userB, lot_number=self.lot)
        # Four people joined; one watched a lot.
        self.assertEqual(_watchers(min_people=4), {"online_watchers": 25})
        self.assertEqual(_watchers(min_people=5), {})

    def test_label_scans_count_printed_in_person_labels(self):
        from auctions.help_stats import _label_scans
        from auctions.models import PageView

        seller = self.admin_in_person_tos
        lots = Lot.objects.filter(lot_name__in=["scanned", "unscanned", "unprinted"])
        scanned = Lot.objects.create(
            lot_name="scanned", auction=self.in_person_auction, auctiontos_seller=seller, label_printed=True
        )
        Lot.objects.create(
            lot_name="unscanned", auction=self.in_person_auction, auctiontos_seller=seller, label_printed=True
        )
        Lot.objects.create(lot_name="unprinted", auction=self.in_person_auction, auctiontos_seller=seller)
        PageView.objects.create(lot_number=scanned, source="qr")
        PageView.objects.create(lot_number=scanned, source="qr")
        self.assertEqual(_label_scans(lots, min_lots=2), {"labels_scanned": 50})
        self.assertEqual(_label_scans(lots, min_lots=3), {})

        with patch("auctions.help_stats.site_stats", return_value={"labels_scanned": 50}):
            response = self.client.get(help_guides.GUIDES["labels"].url)
        self.assertContains(response, "50% of lot labels from all in-person auctions are scanned")

    def test_photo_prices_come_from_big_promoted_in_person_auctions(self):
        from auctions.help_stats import _in_person_photos

        self.in_person_auction.promote_this_auction = True
        self.in_person_auction.save()
        seller = self.admin_in_person_tos
        for price in (10, 20, 30):
            Lot.objects.create(
                lot_name="bare", auction=self.in_person_auction, auctiontos_seller=seller, winning_price=price
            )
        for price in (40, 50, 60):
            lot = Lot.objects.create(
                lot_name="pictured", auction=self.in_person_auction, auctiontos_seller=seller, winning_price=price
            )
            LotImage.objects.create(lot_number=lot)
        facts = _in_person_photos(min_gross=100, min_lots=3)
        self.assertEqual(facts["auctions"], 1)
        rows = [(row["label"], row["lots"], row["median"]) for row in facts["rows"]]
        self.assertEqual(rows, [("No photo", 3, 20), ("One photo", 3, 50)])
        self.assertEqual(facts["premium"], 150)
        self.assertEqual(_in_person_photos(min_gross=10**6, min_lots=3), {})

        self.client.force_login(self.user)
        with patch("auctions.help_stats.in_person_photos", return_value=facts):
            response = self.client.get(help_guides.GUIDES["run-an-in-person-auction"].url)
        self.assertContains(response, "about 150% more")


class RulesChartTests(StandardTestCase):
    def auction_read_for(self, words, seconds, readers=2):
        auction = Auction.objects.create(
            title="Secret club auction",
            created_by=self.user,
            date_start=timezone.now() - datetime.timedelta(days=30),
            summernote_description="<p>word</p>" * words,
        )
        AuctionTOS.objects.bulk_create(
            AuctionTOS(
                auction=auction, pickup_location=self.location, name=f"Reader {i}", time_spent_reading_rules=seconds
            )
            for i in range(readers)
        )
        return auction

    def test_the_chart_is_anonymous_and_drops_outliers(self):
        from auctions.help_stats import _rules_chart, visible_words

        self.assertEqual(visible_words("<p>Bring&nbsp;cash</p><p>no <b>bettas</b></p>"), 4)
        for words in range(40, 140, 10):
            self.auction_read_for(words, 30 + words % 7)
        self.auction_read_for(3000, 31)
        self.auction_read_for(100, 400)
        # A tab left open, people added by an admin, and too few readers: none of them count.
        self.auction_read_for(100, 3600)
        AuctionTOS.objects.filter(auction=self.auction_read_for(100, 500)).update(manually_added=True)
        self.auction_read_for(100, 500, readers=1)

        chart = _rules_chart(min_reads=2, min_auctions=5)
        self.assertEqual(chart["auctions"], 10)
        self.assertNotIn([3000, 31], chart["points"])
        self.assertTrue(all(seconds < 100 for _words, seconds in chart["points"]))
        self.assertNotIn("Secret", str(chart))
        self.assertEqual(_rules_chart(min_reads=3, min_auctions=5), {})

        with patch("auctions.help_stats.rules_chart", return_value=chart):
            response = self.client.get(help_guides.GUIDES["auction-rules"].url)
        self.assertContains(response, "no one will read them anyway")
        self.assertContains(response, 'id="rules-reading-points"')
        with patch("auctions.help_stats.rules_chart", return_value={}):
            response = self.client.get(help_guides.GUIDES["auction-rules"].url)
        self.assertNotContains(response, "no one will read them anyway")

    def test_search_does_not_read_the_chart(self):
        with patch("auctions.help_stats.rules_chart", return_value={"points": [[10, 20]], "auctions": 1}):
            texts = [text for _anchor, _heading, text in help_guides._sections("auction-rules")]
        self.assertFalse(any("getContext" in text for text in texts))


class PaymentPagesAreHelpTests(StandardTestCase):
    def test_square_and_paypal_redirect_into_the_guide(self):
        self.assertRedirects(self.client.get("/square/"), "/help/payments/#square", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/paypal/"), "/help/payments/#paypal", fetch_redirect_response=False)

    def test_the_guide_has_your_connect_button(self):
        self.user.userdata.square_enabled = True
        self.user.userdata.save()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(help_guides.GUIDES["payments"].url), reverse("square_connect"))

    def test_ai_redirects_into_the_guide(self):
        self.assertRedirects(self.client.get("/ai/"), "/help/ai-agents/#connect", fetch_redirect_response=False)


class HelpKnowsYourClubTests(StandardTestCase):
    """The club guides quote the reader's club back to its admins, and say nothing to anyone else."""

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name="Chart Test Club")
        self.member = ClubMember.objects.create(club=self.club, user=self.user, name="Admin", permission_admin=True)
        self.user.userdata.last_club_used = self.club
        self.user.userdata.save()
        self.client.force_login(self.user)

    def guide(self, slug):
        return self.client.get(help_guides.GUIDES[slug].url)

    def test_the_member_count_is_paid_up_members_when_the_club_charges_dues(self):
        ClubMember.objects.create(club=self.club, name="Lapsed", email="lapsed@example.com")
        self.assertContains(self.guide("club-membership"), "Chart Test Club has 2 active members.")
        self.club.membership_system = "rolling"
        self.club.membership_annual_fee = 20
        self.club.save()
        self.member.membership_expiration_date = timezone.localdate() + datetime.timedelta(days=30)
        self.member.save()
        response = self.guide("club-membership")
        self.assertContains(response, "Chart Test Club has 1 active member.")
        self.assertContains(response, "Rolling annual membership, $20 a year.")

    def test_it_says_whether_an_api_key_can_renew_memberships(self):
        from auctions.models import ClubAPIKey

        self.assertContains(self.guide("club-membership"), "doesn't have an API key that can renew memberships")
        key = ClubAPIKey(club=self.club, name="Renewal form", can_renew_memberships=True)
        key.prefix, key.key_hash = "ck_test", "x"
        key.save()
        self.assertContains(self.guide("club-membership"), "has an API key that can renew memberships: Renewal form")

    def test_the_auction_rule_says_whether_it_is_on_in_your_auction(self):
        self.online_auction.club = self.club
        self.online_auction.save()
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self.assertContains(self.guide("club-membership"), f"That rule is off in {self.online_auction.title}")
        Auction.objects.filter(pk=self.online_auction.pk).update(
            add_membership_fee_to_invoices_for_expired_members=True
        )
        self.assertContains(self.guide("club-membership"), f"That rule is on in {self.online_auction.title}")

    def test_the_mailing_address_tip_is_only_for_a_club_without_one(self):
        self.assertContains(self.guide("club-email"), "Membership and donation emails wait until Chart Test Club")
        self.club.mailing_address = "PO Box 1, Springfield"
        self.club.save()
        self.assertNotContains(self.guide("club-email"), "Membership and donation emails wait")

    def test_it_lists_the_membership_emails_still_off(self):
        self.assertContains(
            self.guide("club-email"), "Still off for Chart Test Club: welcome letter, renewal confirmation."
        )
        self.club.send_welcome_email_to_new_members = True
        self.club.send_membership_renewal_confirmation = True
        self.club.save()
        self.assertNotContains(self.guide("club-email"), "Still off for")

    def test_it_says_whether_a_calendar_is_connected_and_public(self):
        self.assertContains(self.guide("club-events"), "Chart Test Club hasn't connected a calendar yet")
        self.club.google_calendar_refresh_token = "token"
        self.club.google_calendar_id = "abc@group.calendar.google.com"
        self.club.save()
        self.assertContains(self.guide("club-events"), "has connected a calendar, but it's still private")
        self.club.google_calendar_is_public = True
        self.club.save()
        self.assertContains(self.guide("club-events"), "has connected a calendar, and it's public")

    def test_own_paypal_credentials_are_only_mentioned_to_a_club_that_enters_them(self):
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        self.assertNotContains(self.guide("payments"), "Enter your club's PayPal credentials")
        self.assertNotContains(self.guide("club-money"), "PayPal credentials")
        self.club.allow_non_oauth_paypal = True
        self.club.save()
        self.assertContains(self.guide("payments"), "Enter your club's PayPal credentials")
        self.assertContains(self.guide("club-money"), "PayPal credentials")

    def test_a_signed_out_reader_and_a_plain_member_get_none_of_it(self):
        member = User.objects.create_user("plain", "plain@example.com", "pw")
        ClubMember.objects.create(club=self.club, user=member, name="Plain")
        member.userdata.last_club_used = self.club
        member.userdata.save()
        for logged_in in (None, member):
            self.client.logout()
            if logged_in:
                self.client.force_login(logged_in)
            for slug in ("club-membership", "club-email", "club-events"):
                html = self.guide(slug).content.decode()
                for phrase in ("active member", "connected a calendar", "Still off for", "API key that can renew"):
                    self.assertNotIn(phrase, html, slug)
                self.assertNotIn("help-tip", html, slug)


class HelpKnowsYourAiAgentsTests(StandardTestCase):
    def connect(self, name, count=1):
        import secrets

        from oauth2_provider.models import get_access_token_model, get_application_model, get_refresh_token_model

        application = get_application_model().objects.create(
            name=name,
            client_type="public",
            authorization_grant_type="authorization-code",
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
        )
        for _ in range(count):
            access = get_access_token_model().objects.create(
                user=self.user,
                application=application,
                token=secrets.token_hex(20),
                expires=timezone.now() + datetime.timedelta(hours=1),
                scope="read write",
            )
            get_refresh_token_model().objects.create(
                user=self.user, application=application, access_token=access, token=secrets.token_hex(20)
            )

    def test_two_connections_from_one_client_are_called_out(self):
        self.connect("Claude", count=2)
        self.connect("ChatGPT")
        self.client.force_login(self.user)
        response = self.client.get(help_guides.GUIDES["ai-agents"].url)
        self.assertContains(response, "Claude is connected 2 times")
        self.assertNotContains(response, "ChatGPT is connected")

    def test_a_single_connection_says_nothing(self):
        self.connect("Claude")
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(help_guides.GUIDES["ai-agents"].url), "is connected 1 times")
        self.assertEqual(help_guides.HelpContext(user=self.user).ai_duplicates, [])

    def test_signed_out_there_is_nothing_to_call_out(self):
        self.assertEqual(help_guides.HelpContext().ai_duplicates, [])

    def test_the_curl_example_is_inside_the_keys_panel(self):
        self.client.force_login(self.user)
        html = self.client.get(help_guides.GUIDES["ai-agents"].url).content.decode()
        panel = html[html.index('id="api-key-panel"') :]
        self.assertIn("Authorization: Bearer ak_your_key_here", panel)
        self.assertNotIn("Authorization: Bearer ak_your_key_here", html[: html.index('id="api-key-panel"')])
