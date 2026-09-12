"""The gate before creating an auction, and the repair queue for the auctions created before it.

Both halves of one problem. ``Auction.club`` is set at creation from ``UserData.club``, nothing on
the way to creating an auction ever asked for that, and so about four auctions in five belong to no
club and are invisible to every number in ``club_health``. The gate stops the backlog growing; the
queue on ``/admin-unlinked-auctions/`` works off what is already there.

See ``services.missing_contact_info``, ``auctions/club_matching.py`` and
``views.usability.UnlinkedAuctions``.
"""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.club_matching import (
    Suggestion,
    best_match,
    initials,
    normalize,
    similarity,
    suggest_clubs,
)
from auctions.models import Auction, Club, ClubMember
from auctions.services import CONTACT_GATE_NEEDS_PHONE, missing_contact_info, readable_list
from auctions.tests import StandardTestCase, give_contact_info


class MissingContactInfoTests(StandardTestCase):
    """The one function both gates ask, and the only thing they disagree about."""

    def test_a_blank_account_is_missing_everything(self):
        self.assertEqual(
            missing_contact_info(self.user_who_does_not_join, require_phone=True),
            ["first name", "last name", "address", "phone number"],
        )

    def test_the_lot_gate_does_not_ask_for_a_phone_number(self):
        give_contact_info(self.user, phone="")
        self.assertEqual(missing_contact_info(self.user), [])
        self.assertEqual(missing_contact_info(self.user, require_phone=True), ["phone number"])

    def test_whitespace_is_not_an_answer(self):
        give_contact_info(self.user, phone="   ")
        self.assertEqual(missing_contact_info(self.user, require_phone=True), ["phone number"])

    def test_a_filled_in_account_is_missing_nothing(self):
        give_contact_info(self.user)
        self.assertEqual(missing_contact_info(self.user, require_phone=True), [])

    def test_the_list_reads_as_a_sentence(self):
        self.assertEqual(readable_list(["a"]), "a")
        self.assertEqual(readable_list(["a", "b"]), "a and b")
        self.assertEqual(readable_list(["a", "b", "c"]), "a, b and c")


class AuctionCreationGateTests(StandardTestCase):
    """Creating an auction sends you to the contact info page first, which is where the club is."""

    def setUp(self):
        super().setUp()
        userdata = self.user.userdata
        userdata.can_create_club_auctions = True
        userdata.save()
        self.client.login(username="my_lot", password="testpassword")

    def _post(self):
        return self.client.post(
            reverse("create_auction"),
            {
                "title": "Gated auction",
                "date_start": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cloned_from": "",
            },
        )

    def test_somebody_with_no_contact_info_is_sent_to_fill_it_in(self):
        response = self.client.get(reverse("create_auction"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("contact_info"), response["Location"])
        self.assertIn("next", response["Location"])

    def test_a_missing_phone_number_alone_stops_you(self):
        give_contact_info(self.user, phone="")
        response = self.client.get(reverse("create_auction"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("contact_info"), response["Location"])

    def test_the_gate_runs_before_the_auction_is_created(self):
        """A gate that runs after the thing it gates is not a gate."""
        self._post()
        self.assertFalse(Auction.objects.filter(title="Gated auction").exists())

    def test_the_permission_check_also_runs_before_the_auction_is_created(self):
        """This used to call super().dispatch() first, so the auction was made and then discarded."""
        give_contact_info(self.user)
        userdata = self.user.userdata
        userdata.can_create_club_auctions = False
        userdata.save()
        self._post()
        self.assertFalse(Auction.objects.filter(title="Gated auction").exists())

    def test_a_filled_in_organizer_gets_through(self):
        give_contact_info(self.user)
        self.assertEqual(self.client.get(reverse("create_auction")).status_code, 200)
        self._post()
        self.assertTrue(Auction.objects.filter(title="Gated auction").exists())

    def test_the_contact_page_then_requires_the_phone_number_the_gate_refused(self):
        """Otherwise saving the page sends them straight back to the gate that sent them."""
        self.client.get(reverse("create_auction"))
        self.assertTrue(self.client.session.get(CONTACT_GATE_NEEDS_PHONE))
        response = self.client.get(reverse("contact_info"))
        self.assertTrue(response.context["form"].fields["phone_number"].required)

    def test_the_lot_gate_leaves_the_phone_number_optional(self):
        response = self.client.get(reverse("contact_info"))
        self.assertFalse(response.context["form"].fields["phone_number"].required)

    def test_answering_it_once_puts_the_phone_number_back_to_optional(self):
        self.client.get(reverse("create_auction"))
        self.client.post(
            reverse("contact_info") + f"?next={reverse('create_auction')}",
            {
                "first_name": "Given",
                "last_name": "Surname",
                "address": "1 Test Street",
                "phone_number": "555-0100",
                "club_affiliation": "",
            },
        )
        self.assertIsNone(self.client.session.get(CONTACT_GATE_NEEDS_PHONE))
        self.assertEqual(missing_contact_info(User.objects.get(pk=self.user.pk), require_phone=True), [])

    def test_there_is_no_loop_between_the_gate_and_the_page(self):
        """Save the contact page as the gate asks and the next hop is the auction form itself."""
        self.client.get(reverse("create_auction"))
        self.client.post(
            reverse("contact_info") + f"?next={reverse('create_auction')}",
            {
                "first_name": "Given",
                "last_name": "Surname",
                "address": "1 Test Street",
                "phone_number": "555-0100",
                "club_affiliation": "",
            },
        )
        self.assertEqual(self.client.get(reverse("create_auction")).status_code, 200)


class ClubNameMatchingTests(TestCase):
    """Clubs write themselves down three ways and all three name the same club."""

    def test_clubs_that_share_initials_are_not_the_same_club(self):
        """``Club.save`` derives an abbreviation from the name, so almost every club has one.

        Treating a derived abbreviation as evidence made every club sharing initials one club --
        and aquarium societies collide constantly: Milwaukee, Minnesota and Missouri are all MAS.
        A 300-row import would have quietly merged them, which is the worst outcome this matcher
        has, because a merge attaches one society's history to another.
        """
        clubs = [
            Club.objects.create(name="Milwaukee Aquarium Society"),
            Club.objects.create(name="Minnesota Aquarium Society"),
            Club.objects.create(name="Missouri Aquarium Society"),
        ]
        self.assertEqual([club.abbreviation for club in clubs], ["MAS", "MAS", "MAS"])
        for club in clubs:
            match, _score = best_match(club.name, clubs)
            self.assertEqual(match, club)
        self.assertIsNone(best_match("Motor City Aquarium Society", clubs)[0])

    def test_a_derived_abbreviation_is_not_evidence_when_the_name_has_punctuation(self):
        """The same trap one layer down: two ways to derive an abbreviation, and only one is used.

        ``Club.save`` splits on whitespace, so "Mid-Atlantic Aquarium Society" derives ``MAS``.
        :func:`initials` splits on punctuation too and reads the same name as ``maas``, so
        comparing against that alone called ``MAS`` hand-written -- and every club with a hyphen or
        an ampersand in its name went back to swallowing its neighbours.
        """
        mid_atlantic = Club.objects.create(name="Mid-Atlantic Aquarium Society")
        michigan = Club.objects.create(name="Michigan Aquarium Society")
        self.assertEqual([mid_atlantic.abbreviation, michigan.abbreviation], ["MAS", "MAS"])
        clubs = [mid_atlantic, michigan]
        self.assertEqual(best_match("Michigan Aquarium Society", clubs)[0], michigan)
        self.assertEqual(best_match("Mid-Atlantic Aquarium Society", clubs)[0], mid_atlantic)

    def test_an_acronym_still_finds_the_club_it_stands_for(self):
        """The half that has to keep working: nothing is lost by ignoring derived abbreviations."""
        club = Club.objects.create(name="Greater Seattle Aquarium Society")
        self.assertEqual(best_match("GSAS", [club])[0], club)

    def test_an_abbreviation_a_person_chose_is_still_evidence(self):
        """One somebody typed says something the name does not, so it is still compared."""
        club = Club.objects.create(name="Tropical Fish Club of Erie County", abbreviation="Erie Fish")
        self.assertEqual(best_match("Erie Fish", [club])[0], club)

    def test_generic_words_are_dropped_from_the_comparison(self):
        self.assertEqual(normalize("Greater Seattle Aquarium Society"), "greater seattle")
        self.assertEqual(normalize("The Fish Club of Boston"), "boston")

    def test_initials_are_built_from_the_whole_name(self):
        self.assertEqual(initials("Greater Seattle Aquarium Society"), "gsas")

    def test_an_abbreviation_matches_the_name_it_stands_for(self):
        self.assertEqual(similarity("GSAS", "Greater Seattle Aquarium Society"), 1.0)
        self.assertEqual(similarity("Greater Seattle Aquarium Society", "gsas"), 1.0)

    def test_a_shortened_spelling_still_matches(self):
        self.assertGreater(similarity("Greater Seattle Aquarium Society", "greater-seattle aquarium soc."), 0.82)

    def test_two_different_clubs_do_not_match(self):
        self.assertLess(similarity("Boston Aquarium Society", "Seattle Aquarium Society"), 0.82)

    def test_names_that_are_only_generic_words_match_nothing(self):
        self.assertEqual(similarity("Aquarium Society", "Fish Club"), 0.0)

    def test_best_match_returns_nothing_when_nothing_is_close(self):
        club = Club.objects.create(name="Boston Aquarium Society")
        self.assertEqual(best_match("Seattle Aquarium Society", [club]), (None, 0.0))

    def test_ties_always_go_the_same_way(self):
        first = Club.objects.create(name="Same Name Society")
        Club.objects.create(name="Same Name Society")
        for _ in range(3):
            self.assertEqual(best_match("Same Name Society", list(Club.objects.all()))[0], first)


class SuggestClubsTests(StandardTestCase):
    """Four signals, ranked by how much anybody should believe them."""

    def _auction(self, title="Untitled", created_by=None):
        return Auction.objects.create(
            created_by=created_by or self.user,
            title=title,
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=1),
        )

    def test_no_signal_is_no_suggestion(self):
        Club.objects.create(name="Unrelated Society")
        self.assertEqual(suggest_clubs([self._auction()]), {})

    def test_the_organizers_other_linked_auctions_win(self):
        club = Club.objects.create(name="Already Filed Society")
        linked = self._auction(title="Last year")
        linked.club = club
        linked.save()
        suggestion = suggest_clubs([self._auction(title="This year")])
        self.assertEqual(next(iter(suggestion.values())).club, club)
        self.assertEqual(next(iter(suggestion.values())).confidence, "high")

    def test_the_affiliation_on_the_account_is_next(self):
        club = Club.objects.create(name="Declared Society")
        userdata = self.user.userdata
        userdata.club = club
        userdata.save()
        suggestion = next(iter(suggest_clubs([self._auction()]).values()))
        self.assertEqual(suggestion.club, club)
        self.assertEqual(suggestion.confidence, "medium")

    def test_belonging_to_exactly_one_club_counts(self):
        club = Club.objects.create(name="Only Society")
        ClubMember.objects.create(club=club, user=self.user, name="Member")
        suggestion = next(iter(suggest_clubs([self._auction()]).values()))
        self.assertEqual(suggestion.club, club)

    def test_belonging_to_two_clubs_says_nothing(self):
        for name in ("First Society", "Second Society"):
            ClubMember.objects.create(club=Club.objects.create(name=name), user=self.user, name="Member")
        self.assertEqual(suggest_clubs([self._auction()]), {})

    def test_the_auction_name_is_the_last_resort_and_is_marked_as_a_guess(self):
        club = Club.objects.create(name="Greater Seattle Aquarium Society")
        suggestion = next(iter(suggest_clubs([self._auction(title="Greater Seattle Aquarium Society")]).values()))
        self.assertEqual(suggestion.club, club)
        self.assertEqual(suggestion.confidence, "low")

    def test_an_abbreviation_has_to_be_a_whole_word(self):
        """ "NEC" is inside "connect", and an auction matched that way is filed under a stranger."""
        Club.objects.create(name="Northeast Council", abbreviation="NEC")
        self.assertEqual(suggest_clubs([self._auction(title="Connecticut spring sale")]), {})

    def test_it_does_not_go_back_to_the_database_per_auction(self):
        club = Club.objects.create(name="Bulk Society")
        userdata = self.user.userdata
        userdata.club = club
        userdata.save()
        auctions = [self._auction(title=f"Auction {index}") for index in range(10)]
        clubs = list(Club.objects.all())
        with self.assertNumQueries(3):
            self.assertEqual(len(suggest_clubs(auctions, clubs)), 10)


class UnlinkedAuctionsPageTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.club = Club.objects.create(name="Receiving Society")
        self.unlinked = Auction.objects.create(
            created_by=self.user_with_no_lots,
            title="Belongs to nobody",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=5),
            date_end=timezone.now() - datetime.timedelta(days=1),
        )

    def test_an_ordinary_user_cannot_open_it(self):
        self.client.login(username="my_lot", password="testpassword")
        self.assertNotEqual(self.client.get(reverse("admin_unlinked_auctions")).status_code, 200)

    def test_the_page_lists_an_auction_with_no_club(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("admin_unlinked_auctions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Belongs to nobody")

    def test_linking_sets_the_club_and_makes_the_creator_an_admin(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            reverse("link_auctions_to_club"),
            {"club": self.club.pk, "auction": [self.unlinked.pk], "grant_admin": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.unlinked.refresh_from_db()
        self.assertEqual(self.unlinked.club, self.club)
        member = ClubMember.objects.get(club=self.club, user=self.user_with_no_lots)
        self.assertTrue(member.permission_admin)

    def test_the_admin_grant_can_be_declined(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(reverse("link_auctions_to_club"), {"club": self.club.pk, "auction": [self.unlinked.pk]})
        self.unlinked.refresh_from_db()
        self.assertEqual(self.unlinked.club, self.club)
        self.assertFalse(ClubMember.objects.filter(club=self.club, user=self.user_with_no_lots).exists())

    def test_an_auction_that_already_has_a_club_is_left_alone(self):
        """Two admins on the same page, or one double submit, must not re-file somebody's answer."""
        other = Club.objects.create(name="Answered Already Society")
        self.unlinked.club = other
        self.unlinked.save()
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(
            reverse("link_auctions_to_club"),
            {"club": self.club.pk, "auction": [self.unlinked.pk], "grant_admin": "on"},
        )
        self.unlinked.refresh_from_db()
        self.assertEqual(self.unlinked.club, other)

    def test_an_ordinary_user_cannot_link_anything(self):
        self.client.login(username="my_lot", password="testpassword")
        self.client.post(
            reverse("link_auctions_to_club"),
            {"club": self.club.pk, "auction": [self.unlinked.pk], "grant_admin": "on"},
        )
        self.unlinked.refresh_from_db()
        self.assertIsNone(self.unlinked.club)

    def test_the_club_health_page_says_how_much_it_cannot_see(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("admin_club_health"))
        self.assertContains(response, "belong to no club")

    def test_linking_recomputes_that_clubs_rollup(self):
        self.client.login(username="admin_user", password="testpassword")
        self.client.post(
            reverse("link_auctions_to_club"),
            {"club": self.club.pk, "auction": [self.unlinked.pk], "grant_admin": "on"},
        )
        self.assertEqual(self.club.health.real_auctions, 1)


class MakeClubAdminButtonTests(StandardTestCase):
    """The "Make X admin of Y" button beside "Trust this user" on the auction page.

    It is offered in two situations (``can_make_club_admin`` is an OR): the creator is not an admin
    of their club, *or* the auction has no club.  The second one is the trap -- filing the clubless
    auctions is normally a side effect of saving the ``ClubMember``, and there is no save to make
    when the person is already an admin.
    """

    def setUp(self):
        super().setUp()
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.club = Club.objects.create(name="Creator's Society")
        userdata = self.user.userdata
        userdata.club = self.club
        userdata.save()
        self.client.login(username="admin_user", password="testpassword")

    def _press(self, auction):
        return self.client.get(reverse("auction_main", kwargs={"slug": auction.slug}) + "?make_club_admin=true")

    def test_it_makes_the_creator_an_admin_and_files_their_auction(self):
        self._press(self.online_auction)
        self.assertTrue(ClubMember.objects.get(club=self.club, user=self.user).permission_admin)
        self.online_auction.refresh_from_db()
        self.assertEqual(self.online_auction.club, self.club)

    def test_it_still_files_the_auction_when_they_are_already_an_admin(self):
        """No ClubMember save happens in this case, so nothing else can do the filing."""
        ClubMember.objects.create(club=self.club, user=self.user, name="Already", permission_admin=True)
        self.assertIsNone(self.online_auction.club)
        self._press(self.online_auction)
        self.online_auction.refresh_from_db()
        self.assertEqual(self.online_auction.club, self.club)

    def test_an_ordinary_user_cannot_press_it(self):
        self.client.login(username="no_lots", password="testpassword")
        self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}) + "?make_club_admin=true")
        self.online_auction.refresh_from_db()
        self.assertIsNone(self.online_auction.club)


class SuggestionShapeTests(TestCase):
    def test_a_suggestion_is_immutable(self):
        suggestion = Suggestion(club=None, reason="because", confidence="low")
        with self.assertRaises(Exception):
            suggestion.club = "something else"
