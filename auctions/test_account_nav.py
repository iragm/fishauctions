"""The Account setup menu: `auctions/account_nav.py`, its sidebar, and /account/setup/.

The menu replaced `preferences_ribbon.html`, a strip of four tabs plus a `More` dropdown holding
the other ten pages. Two things about the replacement are easy to break without noticing, so they
are what most of this file is about:

* **A page's navigation is now the sidebar.** There is no ribbon left to fall back on, so a page
  that drops out of `GROUPS` doesn't merely lose its highlight -- in the app, which draws no navbar
  over these pages, it has no navigation at all. `SidebarReachTests` opens every page in the menu
  and fails if the sidebar isn't on it.
* **The navbar's one "Account" row is a redirect**, not a page, and it lands on where you were.
"""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auctions import account_nav
from auctions.models import Club, ClubMember
from auctions.test_support import isolated_cache

#: What the sidebar button says. Present on a page means the menu rendered.
SIDEBAR_MARKER = 'data-bs-target="#accountSidebar"'


def _user(username, **userdata):
    user = get_user_model().objects.create_user(username=username, email=f"{username}@example.com", password="testpass")
    if userdata:
        for field, value in userdata.items():
            setattr(user.userdata, field, value)
        user.userdata.save()
    return user


@isolated_cache("account-nav")
class SidebarReachTests(TestCase):
    """Every page in the menu draws the menu, and nothing else does."""

    @classmethod
    def setUpTestData(cls):
        cls.user = _user("navuser")
        cls.other = _user("navother")

    def setUp(self):
        self.client.force_login(self.user)

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def test_every_page_in_the_menu_draws_the_menu(self):
        """The guarantee that replaced the ribbon. A page reachable only from the sidebar, that
        doesn't itself draw the sidebar, is a page you can enter and not leave."""
        for name in sorted(account_nav.PAGE_NAMES):
            if name == "userpage":
                url = reverse(name, kwargs={"slug": self.user.username})
            elif name == "account":
                continue  # a redirect to userpage, covered by the row above
            elif name in ("paypal_seller", "square_seller"):
                continue  # gated; PaymentRowTests covers both halves
            else:
                url = reverse(name)
            with self.subTest(page=name):
                self.assertIn(SIDEBAR_MARKER, self._html(url), f"{name} has no Account setup menu")

    def test_the_menu_stays_off_the_rest_of_the_site(self):
        for name in ("home", "allLots", "my_invoices", "feedback"):
            with self.subTest(page=name):
                self.assertNotIn(SIDEBAR_MARKER, self._html(reverse(name)))

    def test_somebody_elses_profile_is_not_your_account_page(self):
        """`userpage` is one URL name for two very different pages, and the argument is the only
        thing that tells them apart."""
        self.assertIn(SIDEBAR_MARKER, self._html(reverse("userpage", kwargs={"slug": self.user.username})))
        self.assertNotIn(SIDEBAR_MARKER, self._html(reverse("userpage", kwargs={"slug": self.other.username})))

    def test_the_current_page_is_marked(self):
        html = self._html(reverse("preferences"))
        marked = re.findall(r'<a class="nav-link ([^"]*)" href="([^"]+)"', html)
        active = [url for classes, url in marked if "active" in classes]
        self.assertEqual(active, [reverse("preferences")])

    def test_the_button_is_named_rather_than_a_bare_hamburger(self):
        """style_reference.md: there is one hamburger on the site and this is not it."""
        self.assertIn("Account setup", self._html(reverse("preferences")))


@isolated_cache("account-nav-landing")
class LandingTests(TestCase):
    """/account/setup/ -- the navbar's single Account row."""

    @classmethod
    def setUpTestData(cls):
        cls.user = _user("landinguser")

    def setUp(self):
        self.client.force_login(self.user)

    def test_it_lands_on_contact_info_the_first_time(self):
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("contact_info"))

    def test_it_lands_where_you_were(self):
        self.client.get(reverse("printing"))
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("printing"))

    def test_delete_account_is_never_where_you_were(self):
        """Sending a returning visitor to "Delete account" because that is where they were last
        reads as an accusation, and it is one keystroke from a real deletion."""
        self.client.get(reverse("preferences"))
        self.client.get(reverse("account_delete"))
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("preferences"))

    def test_a_page_outside_the_menu_is_not_remembered(self):
        self.client.get(reverse("preferences"))
        self.client.get(reverse("allLots"))
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("preferences"))

    def test_a_session_naming_a_page_that_no_longer_exists_falls_back(self):
        """A name in the session outlives a deploy that removes the page it names."""
        session = self.client.session
        session[account_nav.SESSION_KEY] = "a_page_that_was_deleted"
        session.save()
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("contact_info"))

    def test_a_post_does_not_move_where_you_were(self):
        """A form that re-renders with errors is not a visit."""
        self.client.get(reverse("printing"))
        self.client.post(reverse("preferences"), {})
        self.assertRedirects(self.client.get(reverse("account_setup")), reverse("printing"))

    def test_signed_out_it_asks_you_to_sign_in(self):
        self.client.logout()
        response = self.client.get(reverse("account_setup"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)


@isolated_cache("account-nav-payments")
class PaymentRowTests(TestCase):
    """The only two rows with a gate on them.

    Both gates are the ones `preferences_ribbon.html` had, moved rather than rewritten -- the
    Square one in particular took a fix (see `test_tap_to_pay`) that must not be lost in the move.
    """

    def test_a_bidder_is_offered_neither(self):
        user = _user("bidderonly", paypal_enabled=False, square_enabled=False)
        self.client.force_login(user)
        html = self.client.get(reverse("preferences")).content.decode()
        self.assertNotIn(reverse("paypal_seller"), html)
        self.assertNotIn(reverse("square_seller"), html)

    def test_somebody_who_runs_an_auction_is_offered_square(self):
        """`can_take_card_payments`, not `square_enabled`: the flag is off by default, and gating
        the entry on it left an organizer with no route to the page that explains how to ask."""
        user = _user("clubrunner", square_enabled=False)
        club = Club.objects.create(name="Payments Club")
        ClubMember.objects.create(club=club, user=user, name="Organizer", permission_money=True)
        self.client.force_login(user)
        html = self.client.get(reverse("preferences")).content.decode()
        self.assertIn(reverse("square_seller"), html)

    def test_the_paypal_row_follows_its_own_flag(self):
        user = _user("paypaluser", paypal_enabled=True)
        self.client.force_login(user)
        self.assertIn(reverse("paypal_seller"), self.client.get(reverse("preferences")).content.decode())

    def test_an_empty_group_is_dropped_rather_than_left_as_a_heading(self):
        user = _user("nogroups", paypal_enabled=False, square_enabled=False)
        groups = account_nav.groups_for(user, active="preferences")
        self.assertNotIn("Getting paid", [group["title"] for group in groups])
        self.assertTrue(all(group["rows"] for group in groups))


@isolated_cache("account-nav-navbar")
class NavbarTests(TestCase):
    """What is left in the navbar's user menu: three rows, and none of the settings pages."""

    @classmethod
    def setUpTestData(cls):
        cls.user = _user("navbaruser")

    def setUp(self):
        self.client.force_login(self.user)
        self.html = self.client.get(reverse("selling")).content.decode()

    def test_the_three_rows_are_there(self):
        for url in (reverse("my_invoices"), reverse("feedback"), reverse("account_setup")):
            self.assertIn(f'href="{url}"', self.html)

    def test_the_settings_pages_left(self):
        """They are behind Account now. A link left here would be a second route that doesn't mark
        itself in the sidebar, and the reason the menu was eleven rows long to begin with."""
        menu = self.html[self.html.index(f'aria-expanded="false">{self.user.username}</a>') :]
        menu = menu[: menu.index(reverse("account_logout"))]
        for url in (reverse("preferences"), reverse("contact_info"), reverse("printing"), reverse("messages")):
            self.assertNotIn(f'href="{url}"', menu)


@isolated_cache("account-nav-split")
class SettingsSplitTests(TestCase):
    """/preferences/ and /notifications/ were one page and one form.

    The split is not cosmetic: it is what removed the last JavaScript from both pages. The unit
    (`distance_unit`) stayed on /preferences/ and the three radii went to /notifications/, so
    nothing on either page can change a value another field on the same page has to be converted
    against.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = _user("splituser")

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_two_forms_partition_the_settings(self):
        """No field on both: `palette_actions._preference_form_for` picks one form per field, and a
        field on both would be saved through whichever it happened to find first."""
        from auctions.forms import ChangeUserNotificationsForm, ChangeUserPreferencesForm

        preferences = set(ChangeUserPreferencesForm.Meta.fields)
        notifications = set(ChangeUserNotificationsForm.Meta.fields)
        self.assertEqual(preferences & notifications, set())

    def test_the_unit_and_the_radii_are_on_different_pages(self):
        """The whole reason the JavaScript is gone."""
        from auctions.forms import ChangeUserNotificationsForm, ChangeUserPreferencesForm

        self.assertIn("distance_unit", ChangeUserPreferencesForm.Meta.fields)
        for field in ChangeUserNotificationsForm.DISTANCE_FIELDS:
            self.assertIn(field, ChangeUserNotificationsForm.Meta.fields)
            self.assertNotIn(field, ChangeUserPreferencesForm.Meta.fields)

    def test_neither_page_converts_distances_in_the_browser(self):
        """The converter ran on `change` of a select that is no longer on the page with the radii.
        Left behind, it would silently multiply a saved radius by 1.60934 on the wrong page."""
        for name in ("preferences", "notification_preferences"):
            html = self.client.get(reverse(name)).content.decode()
            with self.subTest(page=name):
                self.assertNotIn("MILES_TO_KM", html)
                self.assertNotIn("id_distance_unit'", html)

    def test_changing_the_unit_leaves_the_radii_alone(self):
        """Miles are what is stored. Switching to km used to re-save the three radii through a form
        that read them as kilometres -- which shrank every one of them by a factor of 1.6 whenever
        the switch was made anywhere but the page's own JavaScript."""
        self.user.userdata.email_me_about_new_auctions_distance = 100
        self.user.userdata.local_distance = 60
        self.user.userdata.save()
        response = self.client.post(
            reverse("preferences"),
            {"distance_unit": "km", "preferred_currency": "USD", "username_visible": True},
        )
        self.assertEqual(response.status_code, 302)
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.distance_unit, "km")
        self.assertEqual(self.user.userdata.email_me_about_new_auctions_distance, 100)
        self.assertEqual(self.user.userdata.local_distance, 60)

    def test_saving_stays_on_the_page_and_says_so(self):
        """It used to redirect to the reader's public profile with a success message that never
        rendered -- `SuccessMessageMixin` was listed after `UpdateView`, so its `form_valid` never
        ran. The message is the only confirmation a page of checkboxes gives."""
        response = self.client.post(
            reverse("preferences"), {"distance_unit": "mi", "preferred_currency": "USD"}, follow=True
        )
        self.assertEqual(response.redirect_chain[-1][0], reverse("preferences"))
        self.assertIn("Preferences saved", response.content.decode())

    def test_a_query_string_that_is_not_next_no_longer_500s_the_save(self):
        """`get_success_url` indexed `next` after testing only whether the query string was empty,
        so any other parameter -- a utm tag off an email link was enough -- raised on save."""
        response = self.client.post(
            reverse("preferences") + "?utm_source=newsletter",
            {"distance_unit": "mi", "preferred_currency": "USD"},
        )
        self.assertEqual(response.status_code, 302)

    def test_next_is_still_honoured(self):
        """The pages that link here asking for one setting to be changed send the reader back."""
        response = self.client.post(
            reverse("notification_preferences") + "?next=/auctions/",
            {"email_me_about_new_auctions": True},
        )
        self.assertRedirects(response, "/auctions/", fetch_redirect_response=False)

    def test_the_palette_still_reaches_a_setting_on_either_page(self):
        """`update_preferences` was written against one form. Splitting it in two must not have
        halved what the assistant can change -- "stop emailing me about new auctions" is the
        request it exists for, and that field is on the second form now."""
        from auctions import palette_actions

        fields, _ = palette_actions._preference_fields()
        self.assertIn("email_me_about_new_auctions", fields)
        self.assertIn("email_visible", fields)
        for field in ("email_me_about_new_auctions", "email_visible"):
            self.assertIsNotNone(palette_actions._preference_form_for(field), field)
