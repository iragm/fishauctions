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
from auctions.models import Lot, LotImage, MobileDevice
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
        self.assertContains(self.client.get(reverse("tos")), f'href="{reverse("help")}"')

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

    def test_nobody_else_sees_an_auctions_settings(self):
        self.online_auction.alternative_split_label = "Club Member"
        self.online_auction.save()
        response = self.client.get(help_guides.guide_url("auction-rules", self.online_auction))
        self.assertNotContains(response, "Club Member")

    def test_signed_out_there_are_no_tips(self):
        response = self.client.get(help_guides.GUIDES["run-an-in-person-auction"].url)
        self.assertNotContains(response, "help-tip")

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
        self.assertContains(self.client.get(reverse("help")), "ain't reading all that")

    def test_nobody_with_an_agent_is_offered_one(self):
        from auctions.models import UserAPIKey

        _raw, prefix, key_hash = UserAPIKey.generate()
        UserAPIKey.objects.create(user=self.user, name="mine", prefix=prefix, key_hash=key_hash)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("help")), "ain't reading all that")


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

    def test_the_labels_guide_lists_what_your_labels_print(self):
        self.in_person_auction.label_print_fields = "lot_name,custom_checkbox_label"
        self.in_person_auction.use_custom_checkbox_field = False
        self.in_person_auction.save()
        self.client.force_login(self.user)
        response = self.client.get(help_guides.guide_url("labels", self.in_person_auction))
        self.assertContains(response, "print: Lot number, Lot name.")

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

    def test_a_browser_is_asked_whether_it_is_subscribed(self):
        response = self.guide("in-person-auctions")
        self.assertContains(response, "You have lot notifications turned off.")
        self.assertContains(response, "pushManager.getSubscription")
        self.assertNotContains(response, "pushGetState")

    def test_with_the_app_they_go_to_the_app_not_the_browser(self):
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        with patch("auctions.notifications.push_configured", return_value=True):
            response = self.guide("in-person-auctions")
        self.assertContains(response, "They go to the app on your phone, not this browser.")
        self.assertNotContains(response, "pushManager.getSubscription")

    def test_in_the_app_the_app_is_asked_about_this_phone(self):
        response = self.guide("in-person-auctions", HTTP_USER_AGENT=self.APP_UA)
        self.assertContains(response, "pushGetState")
        self.assertNotContains(response, "pushManager.getSubscription")

    def test_signed_out_nobody_is_asked(self):
        response = self.client.get(help_guides.GUIDES["in-person-auctions"].url)
        self.assertNotContains(response, "help-push-device")

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
