"""The help guides: that they cover every page and rule, that they are public, and that they talk about your auction."""

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils.html import escape

from auctions import help_guides, palette_actions, palette_routes
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

    def test_every_guide_has_a_template(self):
        for slug in help_guides.GUIDES:
            with self.subTest(slug=slug):
                self.assertTrue((help_guides.TEMPLATE_DIR / f"{slug}.html").exists())

    def test_every_running_and_taking_part_guide_exists(self):
        for slug in help_guides.GUIDE_FOR.values():
            self.assertIn(slug, help_guides.GUIDES)


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

    def test_the_footer_links_the_help(self):
        self.assertContains(self.client.get(reverse("tos")), f'href="{reverse("help")}"')

    def test_the_menu_is_the_account_menu(self):
        response = self.client.get(help_guides.GUIDES["labels"].url)
        self.assertContains(response, 'id="helpSidebar"')
        self.assertContains(response, f'class="nav-link active" href="{help_guides.GUIDES["labels"].url}"')

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
        self.assertContains(response, "uses the label “Club Member” for this")

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
